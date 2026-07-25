"""Guardrail tests for the portal module's Phase 2 attachment isolation.

Every handler here is exercised through the *real* production stack --
``portal_router`` (``chat_routes.py`` + ``file_routes.py``) -> ``core_facade``
-> ``attachment_service`` -> the ``PortalAttachmentOwner`` shadow-ownership
table -- with only ``ChatService`` itself replaced by a small duck-typed
fake (``FakeChatService``). This keeps the tests fast and independent of a
real LLM provider/conversation manager, while still proving the actual
security-critical code behaves correctly against the verified ground truth
for this phase:

* Core's ``Attachment`` row (``astrbot/core/db/po.py:340-368``) has no owner
  column, and ``BaseDatabase.get_attachment_by_id`` has no owner filter --
  so nothing in core stops a client from referencing another user's
  ``attachment_id``. ``PortalAttachmentOwner`` plus
  ``attachment_service.assert_owns``/``assert_owns_all`` are the only
  remedy, and every ownership miss (row absent, or owned by someone else)
  must be an indistinguishable HTTP 404 -- never 403, which would be an
  existence oracle.
* The send-path vector is the #1 security requirement of this phase: a
  media message-part's file is resolved server-side purely from
  ``attachment_id`` (the client's ``filename``/``path`` are never used to
  locate a file), so if the portal didn't verify ownership before calling
  ``ChatService.build_chat_stream``, any authenticated user could make the
  agent read and reason over another user's uploaded file.
* Core's ``sanitize_upload_filename`` does not uniquify, and all uploads
  land in one shared flat directory -- so two users uploading a
  same-named file would collide on disk (the second write silently
  overwrites the first) unless the portal's own uniquification
  (``attachment_service._make_stored_filename``) is actually wired in
  before core ever writes a byte.

``FakeChatService`` below implements exactly the surface
``core_facade.py``/``attachment_service.py`` actually touch on a real
``ChatService`` instance: ``save_uploaded_file`` (writes real bytes to a
real temp directory, so the filename-collision test is a genuine
regression test rather than a tautology) plus a ``db`` shim exposing
``get_platform_session_by_id``/``get_attachment_by_id``/``delete_attachment``.

Auth for these tests is set up the same way
``tests/test_portal_chat_isolation.py`` does it: minting access tokens
directly via ``security.mint_access_token`` and inserting ``PortalUser``
rows straight into a temp portal database, bypassing
``session_service.issue_token_pair`` (an independently tracked, orthogonal
Phase 1 concern -- see that file's module docstring).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.portal.api import chat_routes
from astrbot.portal.api.router import portal_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalUser
from astrbot.portal.services import attachment_service, core_facade

PORTAL_TEST_SECRET = "portal-attachment-isolation-test-secret-32b!"


# ---------------------------------------------------------------------------
# FakeChatService: implements exactly the surface core_facade.py /
# attachment_service.py actually call on ``request.app.state.services.chat``.
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    session_id: str
    creator: str
    platform_id: str = "webchat"


@dataclass
class FakeCoreAttachment:
    """Duck-typed stand-in for ``astrbot.core.db.po.Attachment`` -- only the
    three fields ``core_facade.py`` actually reads (``attachment_id``,
    ``path``, ``mime_type``; ``type`` kept for parity/documentation)."""

    attachment_id: str
    path: str
    type: str
    mime_type: str


async def _fake_stream(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _attach_type_for_content_type(content_type: str) -> str:
    """Local, test-only duplicate of the image/audio/video/else-file
    bucketing ``attachment_service._attach_type_for_mime`` performs --
    kept separate (not imported) so this fake models "what a real
    ``ChatService.save_uploaded_file`` would derive", not "whatever the
    module under test itself does"."""
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "record"
    if content_type.startswith("video/"):
        return "video"
    return "file"


class _FakeDb:
    def __init__(self) -> None:
        self.sessions_by_id: dict[str, FakeSession] = {}
        self.attachments_by_id: dict[str, FakeCoreAttachment] = {}
        self.deleted_attachment_ids: list[str] = []

    async def get_platform_session_by_id(self, session_id: str) -> FakeSession | None:
        return self.sessions_by_id.get(session_id)

    async def get_attachment_by_id(self, attachment_id: str) -> FakeCoreAttachment | None:
        return self.attachments_by_id.get(attachment_id)

    async def delete_attachment(self, attachment_id: str) -> None:
        self.deleted_attachment_ids.append(attachment_id)
        self.attachments_by_id.pop(attachment_id, None)


class FakeChatService:
    """Duck-typed stand-in for ``ChatService``, real-disk-backed for the
    one method (``save_uploaded_file``) whose actual byte-write behavior
    the filename-collision test needs to exercise for real."""

    def __init__(self, attachments_dir: Path) -> None:
        self.attachments_dir = attachments_dir
        self.db = _FakeDb()
        self.build_chat_stream_calls: list[dict[str, Any]] = []
        self.update_message_calls: list[dict[str, Any]] = []
        self.save_uploaded_file_calls: list[str] = []

    def add_session(self, session: FakeSession) -> None:
        self.db.sessions_by_id[session.session_id] = session

    async def build_chat_stream(self, username: str, post_data: dict) -> AsyncIterator[str]:
        self.build_chat_stream_calls.append(post_data)
        return _fake_stream(
            [
                'data: {"type": "plain", "text": "hi"}\n\n',
                'data: {"type": "end"}\n\n',
            ]
        )

    async def update_message(self, username: str, data: dict) -> dict:
        self.update_message_calls.append(data)
        return {"message": None, "needs_regenerate": True, "truncated_after_message": True}

    async def prepare_regenerate_message_payload(self, username: str, data: dict) -> dict:
        # Deliberately ignores any client-supplied message-parts input --
        # matches core_facade.regenerate's own documented contract that
        # regenerate reconstructs the payload server-side from the
        # session's own prior history, never from the client's request body.
        return {"message": [], "session_id": data["session_id"]}

    async def save_uploaded_file(self, upload: Any) -> dict[str, Any]:
        """Mirrors the one behavior of the real
        ``ChatService.save_uploaded_file`` this test file needs to be
        real, not mocked away: it writes into ONE SHARED FLAT DIRECTORY
        keyed by ``upload.filename`` -- exactly the core behavior that
        makes filename uniquification a portal-layer must-fix, not a
        cosmetic nicety."""
        self.save_uploaded_file_calls.append(upload.filename)
        dest = self.attachments_dir / upload.filename
        await upload.save(str(dest))
        attachment_id = str(uuid.uuid4())
        content_type = getattr(upload, "content_type", None) or "application/octet-stream"
        attachment = FakeCoreAttachment(
            attachment_id=attachment_id,
            path=str(dest),
            type=_attach_type_for_content_type(content_type),
            mime_type=content_type,
        )
        self.db.attachments_by_id[attachment_id] = attachment
        return {"attachment_id": attachment_id}


# ---------------------------------------------------------------------------
# App / client plumbing -- same shape as test_portal_chat_isolation.py, plus
# monkeypatching the module-level ``portal_async_sessionmaker`` name in every
# module that imports it directly (chat_routes, attachment_service,
# core_facade all call it outside of any FastAPI ``Depends()``, so the
# dependency-override below does not reach them on its own).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-attachment-isolation-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def attachments_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "core-attachments"
    directory.mkdir()
    return directory


@pytest.fixture
def fake_chat_service(attachments_dir: Path) -> FakeChatService:
    return FakeChatService(attachments_dir)


@pytest.fixture
def app(portal_sessionmaker, fake_chat_service: FakeChatService, monkeypatch) -> FastAPI:
    from types import SimpleNamespace

    application = FastAPI()
    application.include_router(portal_router, prefix="/api/v1")
    application.state.services = SimpleNamespace(chat=fake_chat_service)
    application.state.portal_jwt_secret = PORTAL_TEST_SECRET

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    # chat_routes.py, attachment_service.py, and core_facade.py all call
    # `portal_async_sessionmaker()` directly (module-level name, not a
    # FastAPI Depends()) for their own transaction bookkeeping -- redirect
    # all three to our temp test database.
    monkeypatch.setattr(chat_routes, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(attachment_service, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(core_facade, "portal_async_sessionmaker", portal_sessionmaker)

    return application


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _insert_user(sessionmaker, *, user_id: str) -> None:
    async with sessionmaker() as session:
        session.add(
            PortalUser(
                user_id=user_id,
                email=f"{user_id}@example.test",
                password_hash=None,
                display_name=user_id,
                auth_method="local",
                role="user",
                status="active",
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def two_users(portal_sessionmaker) -> tuple[str, str]:
    user_a = f"user-a-{uuid.uuid4().hex}"
    user_b = f"user-b-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    await _insert_user(portal_sessionmaker, user_id=user_b)
    return user_a, user_b


def _auth(user_id: str) -> tuple[dict, dict]:
    """Return (cookies, headers) for an authenticated + CSRF-ready request."""
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


async def _upload(
    client: httpx.AsyncClient,
    cookies: dict,
    headers: dict,
    *,
    filename: str = "photo.png",
    content: bytes = b"hello world",
    content_type: str = "text/plain",
) -> httpx.Response:
    return await client.post(
        "/api/v1/portal/files",
        files={"file": (filename, content, content_type)},
        cookies=cookies,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1. User B requesting user A's attachment_id gets 404, not 403.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_can_download_their_own_upload(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    cookies_a, headers_a = _auth(user_a)

    upload_resp = await _upload(client, cookies_a, headers_a, content=b"A's own bytes")
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["attachment_id"]

    download_resp = await client.get(
        f"/api/v1/portal/files/{attachment_id}", cookies=cookies_a
    )
    assert download_resp.status_code == 200, download_resp.text
    assert download_resp.content == b"A's own bytes"


@pytest.mark.asyncio
async def test_other_user_download_is_404_not_403(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)

    upload_resp = await _upload(client, cookies_a, headers_a, content=b"A's secret file")
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["attachment_id"]

    cookies_b, _headers_b = _auth(user_b)
    response = await client.get(
        f"/api/v1/portal/files/{attachment_id}", cookies=cookies_b
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# 2. The send-path vector: B putting A's attachment_id into a media part of
#    a send-message request is rejected and the agent never receives the
#    file. Covers the edit path too; documents why regenerate is exempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_with_other_users_attachment_is_rejected_and_agent_never_invoked(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(
        client, cookies_a, headers_a, content=b"A's private financial document"
    )
    assert upload_resp.status_code == 201, upload_resp.text
    a_attachment_id = upload_resp.json()["attachment_id"]

    # B has their own legitimately-owned session; the attack is embedding
    # A's attachment_id into B's own, otherwise-valid, send-message request.
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_b)
    fake_chat_service.add_session(session)
    cookies_b, headers_b = _auth(user_b)

    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
        json={
            "message": [
                {
                    "type": "file",
                    "attachment_id": a_attachment_id,
                    "filename": "steal.txt",
                },
                {"type": "plain", "text": "please summarize the attached file"},
            ]
        },
        cookies=cookies_b,
        headers=headers_b,
    )

    assert response.status_code == 404, response.text
    assert response.status_code != 403
    # The whole point: the agent (ChatService.build_chat_stream) must never
    # even be invoked, so zero bytes of A's file are ever read/forwarded.
    assert fake_chat_service.build_chat_stream_calls == []


@pytest.mark.asyncio
async def test_send_message_with_whitespace_padded_media_type_is_still_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    """Regression test for the send-path ownership-gate bypass via an
    unnormalized ``type`` comparison.

    Core's ``build_webchat_message_parts`` matches a part's type via
    ``str(part.get("type", "")).strip()`` before checking it against
    ``MEDIA_PART_TYPES`` (``message_parts_helper.py``), so a type value like
    ``" file"`` (leading whitespace) is still recognized by core as a media
    part and its ``attachment_id`` is resolved via ``get_attachment_by_id``
    -- which has no owner filter at all. If this module's own
    ``_collect_media_attachment_ids`` compared ``part.get("type")`` to
    ``_MEDIA_PART_TYPES`` WITHOUT the identical ``str(...).strip()``
    normalization, such a part would be invisible to the ownership gate
    (the collected attachment_ids list would be empty, so
    ``assert_owns_all`` is never even called) while still being resolved
    and forwarded by core -- letting an attacker read another user's file
    by padding the type string. This pins the fix: whitespace padding must
    not let an unowned attachment slip past the gate.
    """
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(
        client, cookies_a, headers_a, content=b"A's private financial document"
    )
    assert upload_resp.status_code == 201, upload_resp.text
    a_attachment_id = upload_resp.json()["attachment_id"]

    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_b)
    fake_chat_service.add_session(session)
    cookies_b, headers_b = _auth(user_b)

    for padded_type in (" file", "file ", "file\n", "\tfile"):
        response = await client.post(
            f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
            json={
                "message": [
                    {
                        "type": padded_type,
                        "attachment_id": a_attachment_id,
                        "filename": "steal.txt",
                    },
                    {"type": "plain", "text": "please summarize the attached file"},
                ]
            },
            cookies=cookies_b,
            headers=headers_b,
        )

        assert response.status_code == 404, (padded_type, response.text)
        assert response.status_code != 403

    # The whole point: the agent must never be invoked for any of these
    # whitespace-padded variants, so zero bytes of A's file are ever
    # read/forwarded.
    assert fake_chat_service.build_chat_stream_calls == []


@pytest.mark.asyncio
async def test_send_message_with_unknown_attachment_id_is_404_same_as_unowned(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(client, cookies_a, headers_a)
    a_attachment_id = upload_resp.json()["attachment_id"]

    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_b)
    fake_chat_service.add_session(session)
    cookies_b, headers_b = _auth(user_b)

    unowned_resp = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
        json={"message": [{"type": "file", "attachment_id": a_attachment_id}]},
        cookies=cookies_b,
        headers=headers_b,
    )
    garbage_resp = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
        json={"message": [{"type": "file", "attachment_id": str(uuid.uuid4())}]},
        cookies=cookies_b,
        headers=headers_b,
    )

    assert unowned_resp.status_code == 404, unowned_resp.text
    assert garbage_resp.status_code == 404, garbage_resp.text
    assert unowned_resp.json() == garbage_resp.json()
    assert fake_chat_service.build_chat_stream_calls == []


@pytest.mark.asyncio
async def test_edit_message_with_other_users_attachment_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(client, cookies_a, headers_a)
    assert upload_resp.status_code == 201, upload_resp.text
    a_attachment_id = upload_resp.json()["attachment_id"]

    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_b)
    fake_chat_service.add_session(session)
    cookies_b, headers_b = _auth(user_b)

    response = await client.patch(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages/1",
        json={
            "content": {
                "type": "user",
                "message": [{"type": "file", "attachment_id": a_attachment_id}],
            }
        },
        cookies=cookies_b,
        headers=headers_b,
    )

    assert response.status_code == 404, response.text
    assert response.status_code != 403
    assert fake_chat_service.update_message_calls == []


@pytest.mark.asyncio
async def test_regenerate_takes_no_client_message_parts_so_attack_surface_is_moot(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    """``regenerate`` reconstructs its payload server-side from the owned
    session's own prior history (``ChatService
    .prepare_regenerate_message_payload``) and takes no client message-parts
    input at all -- see ``core_facade.regenerate``'s own docstring. There is
    therefore no way for a client to smuggle another user's attachment_id
    through this endpoint: any JSON body sent is not even bound to a request
    model on this route. This test pins that behavior (a smuggled
    attachment_id in the body is silently ignored, never forwarded) rather
    than exercising an ownership check that structurally cannot be reached
    here.
    """
    user_a, _user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)

    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages/1/regenerate",
        json={"message": [{"type": "file", "attachment_id": "attacker-supplied-id"}]},
        cookies=cookies_a,
        headers=headers_a,
    )

    assert response.status_code == 200, response.text
    forwarded = fake_chat_service.build_chat_stream_calls[-1]
    assert forwarded["message"] == []


# ---------------------------------------------------------------------------
# 3. Unknown/garbage attachment_id on download: 404, indistinguishable from
#    the unowned case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_download_attachment_id_is_404_indistinguishable_from_unowned(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(client, cookies_a, headers_a)
    real_owned_by_a = upload_resp.json()["attachment_id"]

    cookies_b, _headers_b = _auth(user_b)
    unowned_resp = await client.get(
        f"/api/v1/portal/files/{real_owned_by_a}", cookies=cookies_b
    )
    garbage_resp = await client.get(
        f"/api/v1/portal/files/{uuid.uuid4()}", cookies=cookies_b
    )

    assert unowned_resp.status_code == 404, unowned_resp.text
    assert garbage_resp.status_code == 404, garbage_resp.text
    assert unowned_resp.json() == garbage_resp.json()


# ---------------------------------------------------------------------------
# 4. A client-supplied `path`/`stored_filename` key in a media part is
#    stripped and never reaches the core payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_supplied_path_and_stored_filename_never_reach_core_payload(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    user_a, _user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    upload_resp = await _upload(client, cookies_a, headers_a, content=b"legit own bytes")
    assert upload_resp.status_code == 201, upload_resp.text
    own_attachment_id = upload_resp.json()["attachment_id"]

    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)

    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
        json={
            "message": [
                {
                    "type": "file",
                    "attachment_id": own_attachment_id,
                    "filename": "report.txt",
                    "path": "/etc/passwd",
                    "stored_filename": "../../etc/shadow",
                }
            ]
        },
        cookies=cookies_a,
        headers=headers_a,
    )

    assert response.status_code == 200, response.text
    forwarded_parts = fake_chat_service.build_chat_stream_calls[-1]["message"]
    assert len(forwarded_parts) == 1
    assert "path" not in forwarded_parts[0]
    assert "stored_filename" not in forwarded_parts[0]
    assert forwarded_parts[0]["attachment_id"] == own_attachment_id
    assert forwarded_parts[0]["filename"] == "report.txt"


# ---------------------------------------------------------------------------
# 5. The filename-collision bug: two different users uploading a file with
#    the SAME original filename must end up with distinct stored files, and
#    each must still read back their OWN bytes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_filename_from_two_users_does_not_collide_on_disk(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    cookies_b, headers_b = _auth(user_b)

    upload_a = await _upload(
        client,
        cookies_a,
        headers_a,
        filename="photo.png",
        content=b"AAAA-this-is-user-As-image-bytes",
        content_type="image/png",
    )
    upload_b = await _upload(
        client,
        cookies_b,
        headers_b,
        filename="photo.png",
        content=b"BBBB-this-is-user-Bs-image-bytes",
        content_type="image/png",
    )

    assert upload_a.status_code == 201, upload_a.text
    assert upload_b.status_code == 201, upload_b.text
    id_a = upload_a.json()["attachment_id"]
    id_b = upload_b.json()["attachment_id"]
    assert id_a != id_b

    download_a = await client.get(f"/api/v1/portal/files/{id_a}", cookies=cookies_a)
    download_b = await client.get(f"/api/v1/portal/files/{id_b}", cookies=cookies_b)

    assert download_a.status_code == 200, download_a.text
    assert download_b.status_code == 200, download_b.text
    # The real assertion: each user reads back THEIR OWN bytes, not the
    # other's -- if the portal failed to uniquify the on-disk filename,
    # B's write would have silently overwritten A's file and both ids
    # would now resolve to B's bytes.
    assert download_a.content == b"AAAA-this-is-user-As-image-bytes"
    assert download_b.content == b"BBBB-this-is-user-Bs-image-bytes"
    assert download_a.content != download_b.content

    # And ownership still holds even though the two rows share an original
    # filename: A's id must still be unreachable by B.
    cross_resp = await client.get(f"/api/v1/portal/files/{id_a}", cookies=cookies_b)
    assert cross_resp.status_code == 404
