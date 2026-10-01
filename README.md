# Reachout API (backend)

Reachout helps people run their outreach and job search from one place:

- **Outreach**: import contacts, write one message with `{name}`/`{company}` placeholders, and send everyone a
  personal copy by email (and optionally WhatsApp), with attachments, from their **own** accounts.
- **Replies**: reads the user's mailbox read-only, matches replies to what they sent, works out what the person
  wants (interested, asks for résumé, wants a call…) and suggests an answer they can send from the app.
- **Career**: turns application emails (applied → review → assessment → interview → offer) into one timeline per
  job, and scores job-alert emails against the user's profile.
- **Website**: a one-page website builder with themes, SEO and a contact form; messages land in **Leads**.
- **Integrations**: GitHub (repos, files, PRs), LinkedIn (official sign-in and sharing), Naukri.

This repository is the **Python API** behind it. The app UI and the public landing pages are separate repositories:

| Repository | What | Hosted on |
|---|---|---|
| **reachout-backend** (this one) | Flask API, background jobs, public user websites (`/p/<name>`) | Render (Docker) |
| reachout-frontend | React app: `/app`, `/login`, `/signup` | Netlify |
| reachout-web | Landing, contact, privacy and terms pages | Netlify |

```
Browser ──► Netlify (app site) ──signed proxy──► Render (this API) ──► MongoDB Atlas
        └─► Netlify (landing)  ──signed proxy──┘        │
                                                         ├─► user's mailbox (IMAP read-only / SMTP)
                                                         └─► GitHub, LinkedIn, web push, Sentry
```

The browser only ever talks to Netlify. Netlify forwards `/api/*` (and `/p/*`) here and signs each request
with `NETLIFY_PROXY_SECRET`; this server refuses anything unsigned. That keeps the login cookie first-party and
makes the API unreachable directly.

---

## Tech

Python 3.12 · Flask 3 · MongoDB (pymongo) · Fernet encryption (cryptography) · gunicorn · Playwright (Chromium,
for WhatsApp Web only) · openpyxl / pypdf (imports) · pywebpush (browser notifications) · Sentry (optional).

## Project layout

```
app.py              Core: config, security, encryption, accounts & sign-in, contacts, templates, files,
                    campaigns (send jobs), WhatsApp linking, leads, bounces, page serving, startup
bridge.py           Shared plumbing feature modules use (access to app.py, HTTP helper, timers, deadlines)
whatsapp.py         WhatsApp Web automation (Playwright); also a standalone CLI
features/
  apps.py           Applications tracker + the one mail sync (applications → replies → inbox → jobs → bounces)
  replies.py        Reply detection, intent reading, suggested answers, sending replies in-thread
  inbox.py          Inbox insights: emails grouped by company and category
  jobs.py           Job alerts → scored job matches; scheduled email queue; hiring posts
  github.py         GitHub workspace (repos, files, commits, PRs)
  linkedin.py       LinkedIn sign-in and posting (official API only)
  notify.py         Notifications: live stream (SSE), browser push, Mac alerts
  portfolio.py      Projects for the website (from GitHub) + Git publishing
  site.py           Website builder: themes, sections, renderer, SEO, public pages, contact form
tests/              pytest suite (security regressions + core flows)
Dockerfile          The image Render runs
render.yaml         Render blueprint
```

Each feature module exposes a Flask blueprint `bp`, registered at the bottom of `app.py`. Modules reach shared
helpers through `bridge.C` (the core `app` module) rather than importing `app.py` directly.

## How data is stored

- **MongoDB.** Most per-user data lives in the `kv` collection as one document per user and key
  (`<uid>:recipients`, `<uid>:applications`, `<uid>:website`…), read and written with `Workspace.load()` /
  `Workspace.save()`. Event-like data has its own collections: `send_log`, `mail_index`, `replies`, `leads`,
  `notifications`, `queue`, `sessions`, `otps`, `ratelimits`, `site_*`. Files go to GridFS.
- **Everything is encrypted** with Fernet before it's stored (`seal()` / `unseal()`). Emails are looked up by a
  keyed HMAC (`lookup_hash()`), never stored in plain text. The key comes from `SECRET_KEY`, or `data/secret.key`
  if that's not set. **Losing the key makes all data unreadable; leaking it with a database dump exposes it.**
  Keep it in a password manager, never next to a backup.
- **Concurrency.** `Workspace.save()` replaces the whole document, so read-modify-write must hold `ws.lock`.
  Locks, in-memory caches and background jobs live in one process: run **one worker** (threads are fine).

## Security model

- Sign-in by one-time email code (no passwords). Codes are HMAC-hashed, single-use, attempt-limited, with a
  daily lock after repeated wrong codes. Sessions: random token in an HttpOnly cookie, hash stored server-side,
  7 days idle / 30 days max, "log out everywhere" supported.
- CSRF: every state-changing `/api/` call needs `X-Requested-With: fetch` and a JSON body.
- Rate limits are stored in MongoDB (shared, survive restarts, expire automatically).
- Strict security headers and a CSP on every page; published user sites get their own nonce-based CSP.
- Outbound requests are restricted: custom mail servers must be public hosts on mail ports, push endpoints must
  be real push services, links from emails/data are checked before use.
- Every user's data is scoped by `uid`; account deletion removes everything and stops running jobs.

## Run locally

Needs Python 3.12 and MongoDB on `127.0.0.1:27017` (`brew install mongodb-community`, or Docker:
`docker run -d -p 127.0.0.1:27017:27017 mongo:8`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements.lock.txt
.venv/bin/playwright install chromium      # only if you want WhatsApp
.venv/bin/python app.py                     # → http://127.0.0.1:5050
```

Without `SMTP_HOST`, sign-in codes are printed in the terminal instead of emailed. The API runs on its own;
for the UI, run the frontend repo's `npm run dev` (it proxies `/api` to `:5050`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

Uses a throwaway database with a random name (dropped afterwards); never touches `reachout`. Page tests skip
when the `web/` pages aren't next to this folder. GitHub Actions runs lint, tests (against MongoDB) and the
Docker build on every push.

## Configuration

Copy `.env.example` to `.env` for local use; on Render set these in the dashboard.

| Variable | Default | What it does |
|---|---|---|
| `SECRET_KEY` | `data/secret.key` | Encryption + session key. **Must stay the same forever** for existing data. |
| `MONGODB_URI` / `MONGODB_DB` | local / `reachout` | Database connection and name |
| `SITE_URL` | request host | Public address of the **app** site (links, sitemap, share cards) |
| `LANDING_URL` | — | Public address of the landing site (split hosting) |
| `NETLIFY_PROXY_SECRET` | — | When set, only Netlify-signed requests are accepted (except `/healthz`) |
| `APP_ENV` | — | `production` turns on secure cookies, HSTS and startup checks |
| `COOKIE_SECURE` | `1` in production | HTTPS-only cookies |
| `TRUST_PROXY` | — | Trust `X-Forwarded-*` from the load balancer (Render: `1`) |
| `ADMIN_EMAILS` | — | Comma-separated; can see enquiries from the main Reachout site |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `MAIL_FROM` | — | Server email for sign-in codes |
| `ALLOW_SIGNUP` | `1` | `0` closes new sign-ups |
| `DAILY_LIMIT` | `100` | Messages per user per day (campaigns + replies + queue) |
| `MAX_CONTACTS` / `MAX_FILES_MB` | `5000` / `25` | Per-user limits |
| `WHATSAPP_ENABLED` | `1` | `0` removes WhatsApp sending everywhere |
| `MIN_WA_DELAY` / `MAX_BROWSERS` | `20` / `3` | WhatsApp pacing and concurrent browsers |
| `SENTRY_DSN` | — | Error reports (no cookies, bodies or form data are sent) |
| `STREAM_SECONDS` / `STREAM_SLOTS` | `50` / `4` | Live notification connection length and cap (Netlify: `20`) |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | — | LinkedIn app (or set by an admin in the app) |
| `VAPID_EMAIL` | — | Contact address for browser push |
| `OPERATOR_NAME` / `JURISDICTION` | — | Shown on the privacy and terms pages |
| `DATA_DIR` | `./data` | Where `secret.key` lives if `SECRET_KEY` isn't set |
| `PORT` / `HOST` | `5050` / `127.0.0.1` | Local server address |

## Deploy to Render

1. **Make a shared secret** (also used on both Netlify sites):
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
2. Render → **New → Blueprint** → pick this repository. It reads `render.yaml` (Docker, health check on
   `/healthz`, plan `starter`).
3. Fill in the values it asks for:
   - `SECRET_KEY`: **your existing key** if you're moving existing data (a new key can't read it)
   - `SITE_URL` = `https://<app>.netlify.app`, `LANDING_URL` = `https://<landing>.netlify.app`
   - `NETLIFY_PROXY_SECRET` (step 1), `MONGODB_URI`, `ADMIN_EMAILS`, `SMTP_*`, optional `SENTRY_DSN`
4. MongoDB Atlas → **Network Access**: allow Render's outbound IPs (or `0.0.0.0/0` with a strong password),
   and turn on **backups**.
5. Deploy, then check `https://<service>.onrender.com/healthz` returns `ok`. Any other URL opened directly
   returns "Not found": that's the signed-proxy protection working.
6. Put the Render address into both Netlify repos' `netlify.toml` and redeploy them.

Notes:
- Keep **one gunicorn worker** (see `Dockerfile`). Background jobs need an instance that doesn't sleep.
- WhatsApp is off in `render.yaml`. Turning it on needs the `standard` plan (Chrome needs memory) and carries
  the risk that WhatsApp restricts the user's number.
- Point an uptime monitor at `/healthz`; it also checks the database.

## Adding a feature

1. Create `features/<name>.py` with `bp = Blueprint("<name>", __name__)` and routes under `/api/<name>/…`.
2. Protect routes with `@login_required` (from `bridge`), which passes a `Workspace`; read/write user data with
   `ws.load()` / `ws.save()` inside `with ws.lock:`, and validate input with `C().v_text()`, `v_email()`, `v_int()`.
3. Raise `C().Invalid("message", "field")` for user errors; the frontend shows it next to the field.
4. Register the blueprint at the bottom of `app.py`, add any new collection to `USER_COLLECTIONS` so account
   deletion removes it, and add tests in `tests/`.
