# Portal (multi-user end-user chat frontend)

Portal is a chat interface for **your users**: they register and sign in individually, each gets conversation history isolated from everyone else's, and their messages are handled by the AI Agents you configured in AstrBot.

It is not the same thing as the [WebUI](./webui.md), and confusing the two is dangerous:

| | WebUI | Portal |
|---|---|---|
| Who it is for | **you** (the operator) | **your users** |
| Identity model | one admin account | many users, separate accounts |
| What it can do | configure models, install plugins, read logs, change config | chat, and nothing else |
| Where it lives | served by AstrBot at `/` | a separate project you deploy yourself |
| Cookie | `astrbot_dashboard_jwt` | `astrbot_portal_jwt` |

The two auth systems use independent signing keys, cookie names and claim shapes, and do not recognise each other: a Portal token presented to a WebUI endpoint gets a 401, and vice versa.

> [!WARNING]
> Portal exposes your AstrBot instance to **untrusted external users**. Read the "What you must do" section below before going live — none of those items are optional hardening; each one is a prerequisite whose absence causes a real incident.

## Getting started

**AstrBot ships the Portal API, not the Portal frontend.** The chat interface is a separate project — [AstrBotDevs/astrbot-portal](https://github.com/AstrBotDevs/astrbot-portal) — so the two can be updated independently. AstrBot's Docker images and release archives contain the WebUI only.

Deploy the frontend from that repository:

```bash
git clone https://github.com/AstrBotDevs/astrbot-portal
cd astrbot-portal
npm install
npm run build      # output in dist/
```

Then serve `dist/` with any web server. There are two ways to wire it to AstrBot, and **the first is strongly preferred**.

### Option 1: one origin, via a reverse proxy (recommended)

Serve the SPA and proxy `/api/` to AstrBot from the same hostname. The browser sees a single origin, so session cookies stay first-party and no CORS is involved. Nothing needs configuring on the AstrBot side.

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;

    root /srv/astrbot-portal/dist;
    location / { try_files $uri $uri/ /index.html; }

    location /api/ {
        proxy_pass http://127.0.0.1:6185;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # AstrBot reads this for the cookie Secure flag
        proxy_buffering off;                          # replies are SSE
        proxy_read_timeout 3600s;
    }
}
```

### Option 2: separate origins, via CORS

> [!WARNING]
> This makes Portal's session cookies **third-party cookies**. Safari blocks those by default and Chrome has been phasing them out, so a share of your users will silently fail to stay signed in. Nothing in AstrBot or the frontend can work around it. Use option 1 unless a single origin is genuinely impossible.

Build with the API origin compiled in, then allowlist the SPA's origin in `cmd_config.json`:

```bash
VITE_API_BASE_URL=https://bot.example.com npm run build
```

```jsonc
{
  "portal": {
    "cors_allowed_origins": ["https://chat.example.com"],
    "cross_origin_cookies": true
  }
}
```

Both sides must be HTTPS — `SameSite=None` is only honoured together with `Secure`. AstrBot refuses to start if `cross_origin_cookies` is on while any allowed origin is plain `http://` (localhost is exempt for local development). CORS is scoped to the end-user API and deliberately excludes `/api/v1/portal/admin/*`.

## Where users come from

Portal supports three identity methods:

### 1. Self-registration (works)

Open by default. Users register an email and password on the Portal sign-in page. The password policy is AstrBot's existing one (at least 8 characters, mixed case and a digit), hashed with PBKDF2-SHA256.

### 2. Operator-provisioned invites (works)

In the WebUI sidebar, open **Portal User Management** and issue an invite for an email address. You get a one-time link back.

> [!IMPORTANT]
> **AstrBot cannot send email**, so this link is not delivered for you — you have to hand it over yourself, via chat, an internal channel, or however you prefer. The link is shown **once**; it cannot be retrieved after you close the dialog, and its confidentiality depends entirely on how you deliver it.

Invites are single-use and expire after 7 days by default. Reopening a consumed link returns 410.

### 3. SSO (works)

Generic OIDC, WeCom (企业微信) and Feishu (飞书). Configure under `portal` in `cmd_config.json`:

```jsonc
{
  "portal": {
    "sso_enabled": true,
    "sso_enabled_providers": ["oidc"],
    // The frontend's own public URL. Each provider's redirect URI is derived
    // as {base}/oauth/callback/{provider} and must match the IdP app
    // registration byte-for-byte.
    "sso_redirect_base_url": "https://chat.example.com",
    "oidc_issuer": "https://accounts.example.com",
    "oidc_client_id": "…",
    "oidc_client_secret": "…"
  }
}
```

A provider only renders a button once it is both enabled *and* fully credentialed — half-configured providers stay invisible rather than failing on click. `sso_redirect_base_url` is required: without it SSO refuses to start regardless of the other settings, and it is also what AstrBot derives its post-login redirect allowlist from.

Note one deliberate product behaviour: **SSO is a sign-in method for accounts that already exist, not a signup path.** An unrecognised identity is rejected; a user must first be provisioned or invited by you, then link their SSO identity while signed in (with an explicit second confirmation). This closes the whole class of "make an identity provider assert a victim's email address to obtain their account" attacks by construction. If you expected "scan a QR code and you're in, no provisioning needed", that is a product decision to change.

## What you must do

All three of these are required.

### 1. Keep operator paths off the public internet

Portal's end-user API lives under `/api/v1/portal/*` and **must** be publicly reachable. But the same prefix also contains `/api/v1/portal/admin/*`, which carries user provisioning, invite issuance, knowledge-base authorisation and Agent Bundle management. It differs from the end-user paths by one path segment, which makes it the easiest rule to get wrong.

The public internet should reach **only** `/api/v1/portal/*` **minus** `/api/v1/portal/admin/*` (plus, if you are reverse-proxying option 1 above, the SPA's own static files).

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

Settings live under `portal` in `data/cmd_config.json` and are applied at startup. Keys map one-to-one onto the fields of `PortalSettings` in `astrbot/portal/config.py`, so anything documented there can be set even if it is not in the shipped template.

Two deliberate behaviours:

- **An unknown key aborts startup** rather than being ignored, so a typo'd setting name cannot silently leave the value at its default.
- **The cookie and CSRF header names cannot be overridden.** They are a wire protocol shared with the frontend, not a preference; renaming one would break every client while looking like a valid setting.

There is no admin UI for these yet — edit the file and restart.

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
| `cors_allowed_origins` | `[]` | Origins allowed to call the API cross-origin. Empty means no CORS layer is installed at all. `"*"` is rejected. |
| `cross_origin_cookies` | `False` | Issue cookies as `SameSite=None; Secure`. Requires the above, and HTTPS. |

Uploads are restricted by an **allowlist** of MIME types — AstrBot's core imposes no size limit and no type restriction at all, so every bound comes from here: images png/jpeg/gif/webp, audio wav/mpeg/aac/ogg/webm/mp4, video mp4/webm/quicktime, documents pdf/plain/csv/json.

## Known limitations

Besides the single-process constraint above:

1. **Cross-origin deployments depend on third-party cookies.** See option 2 in [Getting started](#getting-started). If that is your only viable topology, expect Safari users to be unable to stay signed in; the durable fix is a token-based auth mode, which does not exist yet.
2. **There is no password reset flow.** It depends on sending email, which AstrBot cannot do. A user who forgets their password must be re-invited by you.
3. **The AGPL §13 source endpoint is not implemented.** AstrBot is licensed AGPL-3.0, which requires offering users of a network service the source of **the version actually running**. The login page's source link currently points at the upstream repository, not at your running commit. **If you open Portal to external users, closing this is a compliance prerequisite.**
4. **Non-image MIME types are only validated as declared.** The core sniffs real types for images only; Portal additionally verifies PDF (magic bytes) and JSON (parseability), but audio, video and text still rest on the client's declared Content-Type.
5. **Quotas are per user, with no global disk watermark.** All users together can still fill the disk.
6. **No self-service SSO linking UI.** The link and unlink endpoints exist, but the end-user interface has no account-settings page to start the flow.
7. **No subagent-level selection.** Users pick a bundle (persona + model + knowledge bases), not individual subagents; real subagent isolation would require changing AstrBot's core.

## See also

- [WebUI](./webui.md) — the operator's control panel
- [Knowledge Base](./knowledge-base.md) — create knowledge bases here before authorising them in Portal
- [SubAgent orchestration](./subagent.md) — background on why the isolated profile is mandatory
- Architecture decisions and implementation record: `design/portal-multiuser-plan.md` in the repository, including the full security checklist and the deviations recorded during implementation
