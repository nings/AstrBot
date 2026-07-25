# Portal (multi-user end-user chat frontend)

Portal is a chat interface for **your users**: they register and sign in individually, each gets conversation history isolated from everyone else's, and their messages are handled by the AI Agents you configured in AstrBot.

It is not the same thing as the [WebUI](./webui.md), and confusing the two is dangerous:

| | WebUI | Portal |
|---|---|---|
| Who it is for | **you** (the operator) | **your users** |
| Identity model | one admin account | many users, separate accounts |
| What it can do | configure models, install plugins, read logs, change config | chat, and nothing else |
| Path | `/` | `/portal` |
| Cookie | `astrbot_dashboard_jwt` | `astrbot_portal_jwt` |

The two auth systems use independent signing keys, cookie names and claim shapes, and do not recognise each other: a Portal token presented to a WebUI endpoint gets a 401, and vice versa.

> [!WARNING]
> Portal exposes your AstrBot instance to **untrusted external users**. Read the "What you must do" section below before going live — none of those items are optional hardening; each one is a prerequisite whose absence causes a real incident.

## Getting started

Portal is a second frontend bundle alongside the WebUI.

```bash
cd dashboard
npm run build:portal      # output in dashboard/dist-portal/
```

Start AstrBot and open `http://localhost:6185/portal`. You will land on a login page, and can register directly (self-registration is open by default).

For development with hot reload:

```bash
npm run dev:portal
```

Docker images and release archives already include this bundle. If you build a wheel yourself, use `ASTRBOT_BUILD_DASHBOARD=1 uv build`.

## Where users come from

Portal supports three identity methods, but **only the first two work today**:

### 1. Self-registration (works)

Open by default. Users register an email and password at `/portal`. The password policy is AstrBot's existing one (at least 8 characters, mixed case and a digit), hashed with PBKDF2-SHA256.

### 2. Operator-provisioned invites (works)

In the WebUI sidebar, open **Portal User Management** and issue an invite for an email address. You get a one-time link back.

> [!IMPORTANT]
> **AstrBot cannot send email**, so this link is not delivered for you — you have to hand it over yourself, via chat, an internal channel, or however you prefer. The link is shown **once**; it cannot be retrieved after you close the dialog, and its confidentiality depends entirely on how you deliver it.

Invites are single-use and expire after 7 days by default. Reopening a consumed link returns 410.

### 3. SSO (implemented, but cannot be enabled yet)

Generic OIDC, WeCom (企业微信) and Feishu (飞书) are all fully implemented and covered by tests, but **there is currently no way to configure them** — see [Configuration](#configuration) below. Until a configuration mechanism exists, no SSO buttons appear on the login page.

Note one deliberate product behaviour: **SSO is a sign-in method for accounts that already exist, not a signup path.** An unrecognised identity is rejected; a user must first be provisioned or invited by you, then link their SSO identity while signed in (with an explicit second confirmation). This closes the whole class of "make an identity provider assert a victim's email address to obtain their account" attacks by construction. If you expected "scan a QR code and you're in, no provisioning needed", that is a product decision to change.

## What you must do

All three of these are required.

### 1. Keep operator paths off the public internet

Portal's end-user API lives under `/api/v1/portal/*` and **must** be publicly reachable. But the same prefix also contains `/api/v1/portal/admin/*`, which carries user provisioning, invite issuance, knowledge-base authorisation and Agent Bundle management. It differs from the end-user paths by one path segment, which makes it the easiest rule to get wrong.

The public internet should reach **only**:

- `/portal/*` (SPA assets)
- `/api/v1/portal/*` **minus** `/api/v1/portal/admin/*`

These must be restricted to your internal network or VPN:

```
/api/v1/portal/admin/*      ← the one most often missed
/api/v1/conversations*
/api/v1/sessions*
/api/v1/session-groups*
/api/v1/config*
/api/v1/api-keys*
/api/v1/plugins*
/api/v1/persona*
/api/v1/subagents*
```

Nginx example — order matters, deny the narrower path first:

```nginx
location ^~ /api/v1/portal/admin/ {
    allow 10.0.0.0/8;
    deny  all;
    proxy_pass http://astrbot;
}
location ^~ /api/v1/portal/ {
    proxy_pass http://astrbot;
}
```

Once configured, actually probe `/api/v1/portal/admin/` from outside and confirm you get a refused connection or a 403 rather than a 200.

### 2. Route Portal traffic to an isolated config profile

End-user input reaches an Agent **that can call tools**. AstrBot's subagent orchestration (`SubAgentOrchestrator`) is a single global configuration with no per-conversation tool authorisation, so the blast radius is whatever you enabled globally — possibly including filesystem writes, shell access and unrestricted network fetches.

Use `umop_config_router` to route Portal's umo prefix (`webchat:FriendMessage:webchat!*`) to a dedicated AstrBotConfig profile with dangerous tools and subagents disabled.

AstrBot checks this at startup and logs the result every time. **If it is not configured you get a CRITICAL warning but the service still starts** — there is currently no "refuse to start unless isolated" switch, so that warning has to be seen and acted on by a human.

### 3. Run a single process

Keep `replicas: 1`. Three pieces of state live in process memory: the core's chat-run table, the rate-limiter counters, and SSO's state/nonce plus link-confirmation tickets. Multiple replicas will break stream resumption, let rate limits be bypassed, and make SSO callbacks fail at random.

## What you curate

Portal's design principle: **end users can only pick things you approved in advance**, and can never name a provider, model or knowledge base directly.

### Knowledge-base whitelist

WebUI → **Portal Knowledge Base Visibility**. Only knowledge bases you explicitly authorise here can be selected by end users in a conversation.

> [!CAUTION]
> Authorising a knowledge base means **every** Portal user can select it and have its contents surface in answers. This is the only gate between internal corpora and external users.

Unauthorised knowledge bases are entirely invisible to end users — requests return 404 rather than 403, so responses cannot be used to enumerate which corpora you have.

### Agent Bundles

WebUI → **Portal Agent Bundles**. A bundle is a **persona + chat model + knowledge bases** preset you compose; users pick it by name and never see which model it uses.

> [!CAUTION]
> Enabling a bundle lets every Portal user route their conversation to that model, which is a direct cost decision.

`selected_provider`, `selected_model`, `provider_id` and `persona_id` in a client request body are **unconditionally discarded**. Model selection is always resolved server-side from the bundle.

## What end users can do

- Register and sign in, keep multiple conversations, rename and delete them
- Streaming replies, stop mid-generation, regenerate
- **Resume** an in-flight reply after a page reload or a dropped connection
- Upload images, audio, video and documents (PDF / text / CSV / JSON) for multimodal input
- Select from the knowledge bases you authorised, per conversation
- Switch between the Agent Bundles you enabled

Users are fully isolated from each other: conversations, messages, attachments and knowledge-base bindings are all per-user, and cross-user access returns 404.

## Configuration

> [!WARNING]
> **Portal currently has no configuration mechanism.** `get_portal_settings()` returns hardcoded defaults from the source: it reads no environment variables, no `cmd_config.json`, and there is no admin UI. Changing any setting today means editing the `PortalSettings` defaults in `astrbot/portal/config.py` and restarting.
>
> The direct consequence is that **SSO cannot be enabled** (`sso_enabled = False`, `sso_enabled_providers` is empty, and all three credential sets are `None`), and every quota and limit is fixed at the values below. Adding a configuration mechanism is this module's highest-priority outstanding work.

Key defaults (`astrbot/portal/config.py`):

| Setting | Default | Meaning |
|---|---|---|
| `self_registration_open` | `True` | Whether self-registration is open |
| `access_token_ttl_seconds` | `900` | Access token, 15 minutes |
| `refresh_token_ttl_seconds` | `2592000` | Refresh token, 30 days, rotated with replay detection |
| `auth_rate_limit_capacity` | `5` | Failed sign-ins before backoff |
| `max_requests_per_window` | `200` | Messages per user per day |
| `usage_window_seconds` | `86400` | Quota window, one day |
| `max_concurrent_runs_per_user` | `2` | Concurrent generations per user |
| `max_upload_file_bytes` | `20 MiB` | Single-file limit |
| `max_daily_upload_bytes` | `200 MiB` | Upload bytes per user per day |
| `kb_selection_enabled` | `True` | Whether users may select knowledge bases |
| `max_kb_ids_per_session` | `3` | Knowledge bases bindable to one conversation |
| `agent_bundle_selection_enabled` | `True` | Whether users may select Agent Bundles |
| `invite_token_ttl_seconds` | `604800` | Invite validity, 7 days |
| `sso_enabled` | `False` | SSO master switch |

Uploads are restricted by an **allowlist** of MIME types — AstrBot's core imposes no size limit and no type restriction at all, so every bound comes from here: images png/jpeg/gif/webp, audio wav/mpeg/aac/ogg/webm/mp4, video mp4/webm/quicktime, documents pdf/plain/csv/json.

## Known limitations

Besides the missing configuration mechanism and the single-process constraint above:

1. **There is no password reset flow.** It depends on sending email, which AstrBot cannot do. A user who forgets their password must be re-invited by you.
2. **The AGPL §13 source endpoint is not implemented.** AstrBot is licensed AGPL-3.0, which requires offering users of a network service the source of **the version actually running**. The login page's source link currently points at the upstream repository, not at your running commit. **If you open Portal to external users, closing this is a compliance prerequisite.**
3. **Non-image MIME types are only validated as declared.** The core sniffs real types for images only; Portal additionally verifies PDF (magic bytes) and JSON (parseability), but audio, video and text still rest on the client's declared Content-Type.
4. **Quotas are per user, with no global disk watermark.** All users together can still fill the disk.
5. **No self-service SSO linking UI.** The link and unlink endpoints exist, but the end-user interface has no account-settings page to start the flow.
6. **No subagent-level selection.** Users pick a bundle (persona + model + knowledge bases), not individual subagents; real subagent isolation would require changing AstrBot's core.

## See also

- [WebUI](./webui.md) — the operator's control panel
- [Knowledge Base](./knowledge-base.md) — create knowledge bases here before authorising them in Portal
- [SubAgent orchestration](./subagent.md) — background on why the isolated profile is mandatory
- Architecture decisions and implementation record: `design/portal-multiuser-plan.md` in the repository, including the full security checklist and the deviations recorded during implementation
