# AstrBot Gateway

AstrBot Gateway is a standalone multi-user gateway that sits in front of the AstrBot Open API. It handles user authentication, file isolation, document parsing and quotas. **It requires no changes to AstrBot's code.** It talks to AstrBot only over HTTP, so either one can be upgraded independently.

```
Your frontend / app ──JWT──▶ AstrBot Gateway ──X-API-Key──▶ AstrBot (internal network only)
                        │
                        ├─ Verifies JWT and derives the username (u_<sub>)
                        ├─ Files stored per user with random names; ownership checks
                        ├─ Documents converted to text, then sent to the model
                        └─ Daily chat count and storage quota per plan
```

## What the gateway handles

| Problem | How the gateway handles it |
|---|---|
| Users could fake a `username` and pose as someone else or as an admin | `username` comes only from the JWT `sub` claim, as `u_<sub>` (or `h_<hash>` if it contains special characters). It can never equal `astrbot` |
| Files with the same name overwrite each other | Files are stored at `data/files/<username>/<random id>.<ext>`; images are sent to AstrBot under a random name too |
| A file can be read by anyone who knows its ID | Every file lookup is filtered by owner. Clients never see AstrBot's `attachment_id` |
| The model only sees a server file path | PDF/Word/Excel/PPT/CSV/TXT files are converted to text in the gateway and placed in the message; images use the model's built-in image support |
| Clients can send extra fields such as `selected_provider` | The gateway builds the AstrBot request itself and sends only `username / session_id / message / config_id` |
| No per-user quotas | Daily chat count and storage quota are set per plan |
| Files are never cleaned up | Gateway files expire automatically after `GATEWAY_FILE_TTL_DAYS` days |

## Endpoints

Every endpoint needs `Authorization: Bearer <JWT>`. The JWT is signed with HS256 by your own login system.

```json
{ "sub": "10086", "plan": "pro", "exp": 1767225600 }
```

`sub` and `exp` are required. If `plan` is omitted, `GATEWAY_DEFAULT_PLAN` is used.

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/files` | Upload a file (multipart, field name `file`). Returns `file_id` |
| `GET` | `/v1/files` | List your own files |
| `GET` | `/v1/files/{file_id}/content` | Download your own file |
| `DELETE` | `/v1/files/{file_id}` | Delete your own file |
| `POST` | `/v1/chat` | Chat. Streams the reply over SSE; the response header `X-Session-Id` returns the conversation ID |
| `GET` | `/v1/chat/sessions?page=1&page_size=20` | List your own conversations |
| `GET` | `/healthz` | Health check |

Chat request body:

```json
{
  "message": "Find the risky clauses in this contract",
  "session_id": "optional; one is generated if omitted, pass it back to continue the conversation",
  "file_ids": ["file_id returned by the upload"]
}
```

The SSE event format is the same as AstrBot's `/api/v1/chat`, so frontends that already use it can switch directly.

Supported file types:

- Images (sent to the model's built-in image support): `.png .jpg .jpeg .gif .webp`
- Documents (converted to text in the gateway): `.pdf .docx .pptx .xlsx .xls .csv .txt .md .json .html`

## Configuration

See `.env.example` for every option. The key ones:

| Variable | Description |
|---|---|
| `ASTRBOT_BASE_URL` | AstrBot address, e.g. `http://astrbot:6185` |
| `ASTRBOT_API_KEY` | API key created in AstrBot WebUI → Settings → API Keys. Only `chat` and `file` scopes are needed |
| `GATEWAY_JWT_SECRET` | Secret shared with your login system (at least 32 bytes) |
| `GATEWAY_PLANS` | Plans as JSON; see below |
| `GATEWAY_MAX_DOC_CHARS` | Maximum characters of document text sent per file; anything longer is truncated |

```json
{
  "free": {"config_id": null,       "daily_chat_limit": 50,   "storage_mb": 100},
  "pro":  {"config_id": "pro_conf", "daily_chat_limit": null, "storage_mb": 2048}
}
```

`config_id` is the ID of an AstrBot config profile; it decides which model, persona and knowledge base the plan uses. `null` follows AstrBot's default routing. A `daily_chat_limit` of `null` means unlimited.

## Deployment

```bash
cd gateway
cp .env.example .env   # fill in the API key and JWT secret
docker compose -f compose.example.yml up -d
```

Or run it directly:

```bash
cd gateway
uv sync
uv run python -m astrbot_gateway
```

### AstrBot settings to check (configuration only, no code changes)

1. **Do not expose AstrBot to the public internet.** Only the gateway may reach port 6185. The example compose file does not publish that port.
2. Give the API key only the `chat` and `file` scopes.
3. `admins_id` must not contain IDs starting with `u_` or `h_`. The default `astrbot` is fine.
4. Keep `computer_use_runtime` set to `none` or a remote sandbox, and leave `computer_use_require_admin` on. Never use `local`.

## Known limitations

- **One gateway instance per deployment.** Quotas and file records live in SQLite. To run several instances, switch to a shared database, or route each user to a fixed instance.
- **Images uploaded to AstrBot are not deleted with the gateway file.** AstrBot's Open API has no delete endpoint. Because of the random names and ownership checks this does not cause cross-user access; clear `data/attachments` in AstrBot periodically.
- **Document text is saved in the conversation history.** This keeps follow-up questions working; it is visible only in that user's own conversation.
- **Data analysis that needs code execution (e.g. complex Excel calculations)** currently converts the table to text before sending it. For sandbox execution, AstrBot needs a remote sandbox plus a change that copies files into the sandbox automatically, which is outside this gateway's scope.

## Development

```bash
cd gateway
uv sync
uv run pytest
```
