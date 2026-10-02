import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from astrbot_gateway.app import create_app
from astrbot_gateway.config import Plan, Settings
from fastapi.testclient import TestClient

SECRET = "test-secret"


async def sse_stream():
    yield b'data: {"type": "plain", "data": "hi"}\n\n'


class FakeAstrBot:
    """Records requests and mimics the AstrBot Open API responses."""

    def __init__(self) -> None:
        self.chat_bodies: list[dict] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.session_params: list[dict] = []
        self.chat_error: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "astrbot-key"
        if request.url.path == "/api/v1/file":
            body = request.read()
            filename = body.split(b'filename="')[1].split(b'"')[0].decode()
            self.uploads.append((filename, body))
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {"attachment_id": f"att-{len(self.uploads)}"},
                },
            )
        if request.url.path == "/api/v1/chat":
            self.chat_bodies.append(json.loads(request.read()))
            if self.chat_error:
                return httpx.Response(
                    200, json={"status": "error", "message": self.chat_error}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_stream(),
            )
        if request.url.path == "/api/v1/chat/sessions":
            self.session_params.append(dict(request.url.params))
            return httpx.Response(200, json={"status": "ok", "data": {"sessions": []}})
        return httpx.Response(404)


@pytest.fixture
def fake() -> FakeAstrBot:
    return FakeAstrBot()


@pytest.fixture
def client(tmp_path: Path, fake: FakeAstrBot):
    settings = Settings(
        astrbot_base_url="http://astrbot",
        astrbot_api_key="astrbot-key",
        jwt_secret=SECRET,
        data_dir=tmp_path,
        max_file_mb=1,
        max_doc_chars=50,
        plans={
            "free": Plan(config_id=None, daily_chat_limit=2, storage_mb=1),
            "pro": Plan(config_id="pro_conf", daily_chat_limit=None, storage_mb=10),
        },
        default_plan="free",
    )
    app = create_app(settings, astrbot_transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as test_client:
        yield test_client


def auth(sub: str, plan: str | None = None) -> dict:
    claims = {"sub": sub, "exp": int(time.time()) + 3600}
    if plan:
        claims["plan"] = plan
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


def upload(client, sub, name, content, plan=None):
    return client.post(
        "/v1/files", files={"file": (name, content)}, headers=auth(sub, plan)
    )


def test_rejects_missing_invalid_and_expired_tokens(client):
    assert client.get("/v1/files").status_code == 401
    bad = jwt.encode({"sub": "a", "exp": int(time.time()) + 60}, "other", "HS256")
    assert (
        client.get("/v1/files", headers={"Authorization": f"Bearer {bad}"}).status_code
        == 401
    )
    expired = jwt.encode({"sub": "a", "exp": int(time.time()) - 60}, SECRET, "HS256")
    assert (
        client.get(
            "/v1/files", headers={"Authorization": f"Bearer {expired}"}
        ).status_code
        == 401
    )
    assert client.get("/v1/files", headers=auth("a", "vip")).status_code == 403


def test_document_text_is_inlined_and_upstream_body_is_fixed(client, fake):
    file_id = upload(client, "alice", "data.csv", b"a,b\n1,2\n", "pro").json()[
        "file_id"
    ]
    resp = client.post(
        "/v1/chat",
        json={
            "message": "summarize",
            "file_ids": [file_id],
            "selected_provider": "expensive",
            "_skip_user_history": True,
        },
        headers=auth("alice", "pro"),
    )
    assert resp.status_code == 200
    assert "hi" in resp.text
    body = fake.chat_bodies[-1]
    assert set(body) == {"username", "session_id", "message", "config_id"}
    assert body["username"] == "u_alice"
    assert body["config_id"] == "pro_conf"
    assert body["session_id"] == resp.headers["X-Session-Id"]
    assert body["message"][0]["type"] == "plain"
    assert body["message"][0]["text"].startswith('<document name="data.csv">')
    assert "| a | b |" in body["message"][0]["text"]
    assert body["message"][1] == {"type": "plain", "text": "summarize"}
    assert fake.uploads == []


def test_long_document_is_truncated(client, fake):
    file_id = upload(client, "alice", "long.txt", b"x" * 200).json()["file_id"]
    client.post(
        "/v1/chat", json={"message": "q", "file_ids": [file_id]}, headers=auth("alice")
    )
    assert (
        "[Truncated: showing the first 50 characters]"
        in (fake.chat_bodies[-1]["message"][0]["text"])
    )


def test_same_filename_does_not_overwrite_between_users(client):
    alice = upload(client, "alice", "report.txt", b"alice data").json()["file_id"]
    bob = upload(client, "bob", "report.txt", b"bob data").json()["file_id"]
    assert (
        client.get(f"/v1/files/{alice}/content", headers=auth("alice")).content
        == b"alice data"
    )
    assert (
        client.get(f"/v1/files/{bob}/content", headers=auth("bob")).content
        == b"bob data"
    )


def test_users_cannot_touch_each_others_files(client, fake):
    file_id = upload(client, "alice", "secret.txt", b"secret").json()["file_id"]
    assert (
        client.get(f"/v1/files/{file_id}/content", headers=auth("bob")).status_code
        == 404
    )
    assert client.delete(f"/v1/files/{file_id}", headers=auth("bob")).status_code == 404
    resp = client.post(
        "/v1/chat",
        json={"message": "read it", "file_ids": [file_id]},
        headers=auth("bob"),
    )
    assert resp.status_code == 404
    assert fake.chat_bodies == []
    assert client.get("/v1/files", headers=auth("bob")).json() == {"files": []}
    assert len(client.get("/v1/files", headers=auth("alice")).json()["files"]) == 1


def test_image_is_forwarded_with_random_name(client, fake):
    file_id = upload(client, "alice", "photo.png", b"\x89PNG fake").json()["file_id"]
    assert fake.uploads[0][0] == f"{file_id}.png"
    client.post(
        "/v1/chat",
        json={"message": "what is this", "file_ids": [file_id]},
        headers=auth("alice"),
    )
    assert fake.chat_bodies[-1]["message"][0] == {
        "type": "image",
        "attachment_id": "att-1",
        "filename": "photo.png",
    }


def test_upload_limits(client):
    assert upload(client, "alice", "run.exe", b"MZ").status_code == 415
    assert (
        upload(client, "alice", "big.txt", b"x" * (1024 * 1024 + 1)).status_code == 413
    )
    assert upload(client, "alice", "empty.txt", b"").status_code == 400
    upload(client, "alice", "a.txt", b"x" * 700 * 1024)
    assert upload(client, "alice", "b.txt", b"x" * 400 * 1024).status_code == 413


def test_delete_removes_file(client, tmp_path):
    file_id = upload(client, "alice", "a.txt", b"data").json()["file_id"]
    assert (
        client.delete(f"/v1/files/{file_id}", headers=auth("alice")).status_code == 200
    )
    assert list((tmp_path / "files" / "u_alice").iterdir()) == []
    assert (
        client.get(f"/v1/files/{file_id}/content", headers=auth("alice")).status_code
        == 404
    )


def test_daily_chat_limit(client):
    for _ in range(2):
        assert (
            client.post("/v1/chat", json={"message": "hi"}, headers=auth("alice"))
        ).status_code == 200
    assert (
        client.post("/v1/chat", json={"message": "hi"}, headers=auth("alice"))
    ).status_code == 429
    assert (
        client.post("/v1/chat", json={"message": "hi"}, headers=auth("bob"))
    ).status_code == 200


def test_upstream_error_is_relayed(client, fake):
    fake.chat_error = "session_id belongs to another username"
    resp = client.post(
        "/v1/chat",
        json={"message": "hi", "session_id": "taken"},
        headers=auth("alice"),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "session_id belongs to another username"


def test_sessions_are_scoped_to_caller(client, fake):
    resp = client.get("/v1/chat/sessions?username=bob&page=2", headers=auth("alice"))
    assert resp.status_code == 200
    assert fake.session_params[-1]["username"] == "u_alice"
    assert fake.session_params[-1]["page"] == "2"


def test_username_cannot_match_astrbot_admin(client, fake):
    client.post("/v1/chat", json={"message": "hi"}, headers=auth("astrbot"))
    client.post("/v1/chat", json={"message": "hi"}, headers=auth("a!b c"))
    assert fake.chat_bodies[0]["username"] == "u_astrbot"
    assert fake.chat_bodies[1]["username"].startswith("h_")
