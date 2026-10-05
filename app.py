#!/usr/bin/env python3
"""
Reachout – multi-user web app for personalised outreach over WhatsApp and email.

Storage: everything lives in MongoDB (MONGODB_URI, database MONGODB_DB). Every
user record, contact list, template, profile, setting, history entry, enquiry,
uploaded file and WhatsApp login is encrypted (Fernet / AES-128-CBC + HMAC)
before it is written; files go to Cloudinary (CLOUDINARY_URL) as private,
still-encrypted objects, with only a reference kept in MongoDB (GridFS when
Cloudinary isn't configured). Email addresses are looked up through a
keyed HMAC, never stored in the clear. An older SQLite store (DATA_DIR/app.db)
is copied into MongoDB automatically on first start.

Auth: passwordless. Every sign-up and log-in is confirmed with a 6-digit code
sent to the user's email (expires in 10 minutes, 5 attempts, 60 s resend
cooldown). Configure SMTP_* env vars for sending; without them, local runs
print the code in the terminal.

Local:       .venv/bin/python backend/app.py    -> http://127.0.0.1:5050
Production:  see README.md (Docker + gunicorn, one worker process).
"""

import base64
import csv
import functools
import email as email_lib
import hashlib
import imaplib
import html
import hmac
import io
import json
import os
import random
import re
import secrets
import shutil
import smtplib
import sqlite3
import string
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from mimetypes import guess_type
from pathlib import Path

import dns.exception
import dns.resolver
from cryptography.fernet import Fernet, InvalidToken
from gridfs import GridFSBucket
from gridfs.errors import NoFile
import requests
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from flask import Flask, g, jsonify, make_response, redirect, request, send_file, send_from_directory, session
from werkzeug.utils import secure_filename

import whatsapp as wa

BACKEND = Path(__file__).resolve().parent
HERE = BACKEND.parent  # project root: web/, data/ and legacy files live here, next to backend/
WEB = HERE / "web"
DATA = Path(os.environ.get("DATA_DIR", HERE / "data")).resolve()
SQLITE_PATH = DATA / "app.db"  # previous storage; migrated into MongoDB on first start
def env_int(name, default):
    """A whole-number setting. Unset, empty or invalid values fall back to the default (a blank line in a
    hosting dashboard must not stop the server from starting)."""
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        print(f"[Reachout] Ignoring {name}={os.environ.get(name)!r}: not a whole number; using {default}.", flush=True)
        return default


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017")
MONGODB_DB = os.environ.get("MONGODB_DB", "reachout")
# cloudinary://<api key>:<api secret>@<cloud name>, from the Cloudinary dashboard. Empty: files stay in GridFS.
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "").strip()

ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "1") != "0"
DAILY_LIMIT = env_int("DAILY_LIMIT", 100)        # messages per user per day
MIN_WA_DELAY = env_int("MIN_WA_DELAY", 20)        # seconds, enforced server-side
MAX_BROWSERS = env_int("MAX_BROWSERS", 3)         # concurrent WhatsApp browsers
MAX_CONTACTS = env_int("MAX_CONTACTS", 5000)
MAX_FILES_MB = env_int("MAX_FILES_MB", 25)        # per user
MAX_FILE_MB = 10                                                # per uploaded file
MAX_FILES = 20
ALLOWED_DOCS = {"pdf", "doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "csv", "ppt", "pptx",
                "png", "jpg", "jpeg", "webp", "gif"}
HEADLESS = os.environ.get("WA_HEADLESS", "1") != "0"
# WhatsApp sending automates WhatsApp Web, which WhatsApp's terms don't allow and which can get numbers
# banned. Hosted versions can switch it off entirely: WHATSAPP_ENABLED=0.
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED", "1") != "0"
NO_SANDBOX = os.environ.get("CHROMIUM_NO_SANDBOX", "0") == "1"
# A saved WhatsApp login is deleted this many hours after it was linked (the user is told, and links again to send).
WA_SESSION_HOURS = env_int("WA_SESSION_HOURS", 2)
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
LOGGED_IN = "#pane-side, [aria-label='Chat list']"
# Browser caches are rebuilt automatically; leaving them out keeps the stored login small.
WA_SKIP = {"Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache",
           "GrShaderCache", "GraphiteDawnCache", "CacheStorage", "component_crx_cache",
           "extensions_crx_cache", "Crashpad", "BrowserMetrics", "BrowserMetrics-spare.pma", "ScriptCache",
           "SingletonLock", "SingletonCookie", "SingletonSocket"}

OTP_TTL = 600
OTP_ATTEMPTS = 5
OTP_COOLDOWN = 60
OTP_FAILS_PER_DAY = 15          # wrong codes per email per day before a 24 h lock
SYSTEM_MAIL_PER_HOUR = env_int("SYSTEM_MAIL_PER_HOUR", 300)  # login/notice emails the server may send
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = env_int("SMTP_PORT", 465)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
# Production = served to other people. APP_ENV=production is the explicit switch; COOKIE_SECURE=1 still implies it.
# Anything bound to a non-loopback address is treated as production too, so a forgotten flag can't
# silently turn on developer conveniences (codes printed to the log, first account = admin) on a public server.
_HOST = os.environ.get("HOST", "127.0.0.1")
PRODUCTION = (os.environ.get("APP_ENV", "").lower() == "production" or os.environ.get("COOKIE_SECURE", "0") == "1"
              or _HOST not in ("127.0.0.1", "localhost", "::1"))
SECURE_COOKIES = os.environ.get("COOKIE_SECURE", "1" if PRODUCTION else "0") == "1"
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
# Split hosting (landing on one Netlify site, app on another, this API on Render): the landing page's address.
LANDING_URL = os.environ.get("LANDING_URL", "").rstrip("/")
# Shared secret of Netlify's signed proxy rewrites. When set, every request (except /healthz) must come
# through Netlify: calls straight to this server are refused, and the visitor's real address is taken
# from Netlify's signed request.
NETLIFY_PROXY_SECRET = os.environ.get("NETLIFY_PROXY_SECRET", "")
# Version of the terms/privacy policy a new account accepts (recorded with the account). Bump when they change.
TERMS_VERSION = "2026-10-01"
# Shown on the privacy policy and terms pages.
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "the Reachout team")
JURISDICTION = os.environ.get("JURISDICTION", "India")  # public address, e.g. https://reachout.example.com
DEV_OTP = not SMTP_HOST and not PRODUCTION  # print codes to the terminal instead of emailing
# Site owners who can see website enquiries (Leads). Locally, with none set, the oldest account is the owner.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

CORE_FIELDS = ["name", "company", "phone", "email"]
STATUS_FIELDS = ["wa_status", "wa_last", "email_status", "email_last", "email_opened", "replied_at", "reply_intent", "reply_count", "added_at"]
CRM_FIELDS = ["stage", "follow_up"]
STAGES = {"new": "New", "contacted": "Contacted", "replied": "Replied", "interested": "Interested",
          "not_interested": "Not interested", "won": "Won"}
LEAD_STATUSES = {"new": "New", "contacted": "Contacted", "qualified": "Qualified", "closed": "Closed", "spam": "Spam"}
LEAD_TOPICS = ["General question", "Sales & pricing", "Support", "Partnership", "Other"]
HEADER_ALIASES = {
    "phone": {"phone", "mobile", "mobile no", "mobile number", "phone number", "phone no", "contact",
              "contact number", "contact no", "whatsapp", "whatsapp number", "whatsapp no", "number", "cell"},
    "email": {"email", "e mail", "mail", "email id", "email address", "mail id"},
    "name": {"name", "hr name", "contact name", "person", "full name", "recipient", "recipient name"},
    "company": {"company", "company name", "organisation", "organization", "org", "firm", "employer"},
}
DEFAULT_SETTINGS = {
    "country_code": "91",
    "min_delay": 45,
    "max_delay": 120,
    "limit": 20,
    "mode": "whatsapp",
    "template_id": "",
    "documents": None,  # None = attach every file until the user picks
    "wa_connected": False,
}
STARTER_TEMPLATE = """Hi {name},

I hope you're doing well. I'm {sender_name}, and I'm reaching out to <say why you're contacting {company}>.

<one or two lines about you or your offer>

I've attached <what you've attached> for your reference. I'd be glad to hear from you whenever it suits.

Thank you,
{sender_name}
{sender_email}"""


# ---------------------------------------------------------------- secrets + crypto

def load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "secret.key"
    if not path.exists():
        # Created owner-only atomically (no moment where other accounts on the machine could read it).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
    return path.read_text().strip()


SECRET = load_secret()


def _derive(label):
    return hashlib.sha256(f"{label}:{SECRET}".encode()).digest()


fernet = Fernet(base64.urlsafe_b64encode(_derive("reachout-data-v1")))
HASH_KEY = _derive("reachout-lookup-v1")


def lookup_hash(value):
    return hmac.new(HASH_KEY, value.encode(), "sha256").hexdigest()


def seal(obj):
    return fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode())


class Undecryptable(Exception):
    """Stored data that can't be decrypted: wrong/rotated key or corruption. Never treated as empty."""


def unseal(blob, default=None, strict=False):
    try:
        return json.loads(fernet.decrypt(blob)) if blob else default
    except (InvalidToken, ValueError):
        if strict:
            raise Undecryptable("stored data could not be decrypted")
        return default


SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    # Error reports only: no request bodies, cookies or personal data are sent (send_default_pii=False),
    # and headers that could carry secrets are scrubbed before anything leaves the server.
    import sentry_sdk

    def _scrub(event, _hint):
        req = event.get("request") or {}
        for k in list((req.get("headers") or {})):
            if k.lower() in ("cookie", "authorization", "x-nf-sign", "x-nf-client-connection-ip", "x-forwarded-for"):
                req["headers"][k] = "[removed]"
        req.pop("data", None)
        req.pop("cookies", None)
        return event
    sentry_sdk.init(dsn=SENTRY_DSN, send_default_pii=False, traces_sample_rate=0.0, before_send=_scrub,
                    environment="production" if os.environ.get("APP_ENV") == "production" else "development")

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=_derive("reachout-session-v1").hex(),
    MAX_CONTENT_LENGTH=(MAX_FILE_MB * 3) * 1024 * 1024,
    SESSION_COOKIE_NAME="__Host-ro_session" if SECURE_COOKIES else "ro_session",  # __Host-: HTTPS only, this host only
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------------------------------------------------------------- database (MongoDB)

class Mongo:
    """Lazily connected handles to the MongoDB collections and GridFS bucket."""

    def __init__(self):
        self.client = self.db = self.files = None

    def connect(self):
        self.client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000, appname="reachout", tz_aware=True)
        self.client.admin.command("ping")
        self.db = self.client[MONGODB_DB]
        self.files = GridFSBucket(self.db, bucket_name="files")

    def __getattr__(self, name):  # M.users, M.kv, M.send_log, ...
        if self.db is None:
            raise RuntimeError("Database not connected")
        return self.db[name]


M = Mongo()


def check_config():
    """Refuse to run a public server with settings that would weaken it."""
    if not PRODUCTION:
        return
    problems = []
    if not SITE_URL.startswith("https://"):
        problems.append("SITE_URL must be your public https:// address (it's used for links, the sitemap and share cards; "
                        "without it the address comes from request headers, which visitors control).")
    if not ADMIN_EMAILS:
        print("[Reachout] Note: ADMIN_EMAILS is not set, so nobody can see Reachout's own site enquiries.", flush=True)
    if os.environ.get("SECRET_KEY") and len(os.environ["SECRET_KEY"]) < 32:
        problems.append("SECRET_KEY is too short. Use at least 32 random characters (python -c 'import secrets; print(secrets.token_urlsafe(48))').")
    if not SECURE_COOKIES:
        print("[Reachout] WARNING: COOKIE_SECURE=0 on a public server: login cookies can travel over plain http.", flush=True)
    if problems:
        sys.exit("Reachout won't start with these production settings:\n- " + "\n- ".join(problems))


def init_storage():
    check_config()
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        M.connect()
    except PyMongoError as e:
        sys.exit(f"Can't connect to MongoDB at {MONGODB_URI.split('@')[-1]}: {e}\n"
                 "Start MongoDB (brew services start mongodb-community) or set MONGODB_URI.")
    M.users.create_index("email_hash", unique=True)
    M.users.create_index("created")
    M.otps.create_index("expire_at", expireAfterSeconds=0)  # MongoDB removes expired codes itself
    M.kv.create_index("uid")
    M.send_log.create_index([("uid", ASCENDING), ("day", ASCENDING)])
    M.send_log.create_index([("uid", ASCENDING), ("rid", ASCENDING), ("ts", DESCENDING)])
    M.send_log.create_index([("uid", ASCENDING), ("ts", DESCENDING)])
    M.send_log.create_index("track", sparse=True)
    M.documents.create_index([("uid", ASCENDING), ("name_hash", ASCENDING)], unique=True)
    M.leads.create_index([("created", DESCENDING)])
    M.leads.create_index([("uid", ASCENDING), ("created", DESCENDING)], sparse=True)
    M.sessions.create_index("expire_at", expireAfterSeconds=0)
    M.site_images.create_index("uid")
    M.site_stats.create_index("uid")
    M.sessions.create_index("uid")
    M.ratelimits.create_index("expire_at", expireAfterSeconds=0)
    M.queue.create_index("uid")
    if SQLITE_PATH.exists() and M.users.estimated_document_count() == 0:
        migrate_sqlite()


class Cloudinary:
    """Minimal client for Cloudinary's REST API (signed requests; the secret never leaves the server)."""

    PART = 4 * 1024 * 1024  # the free plan takes raw files up to 10 MB; smaller parts also survive slow uplinks

    def __init__(self, url):
        m = re.fullmatch(r"cloudinary://([^:@/]+):([^@/]+)@([\w-]+)/?", url)
        if not m:
            raise ValueError("CLOUDINARY_URL should look like cloudinary://<api key>:<api secret>@<cloud name>")
        self.key, self.secret, self.cloud = m.groups()
        self.api = f"https://api.cloudinary.com/v1_1/{self.cloud}"

    def _signed(self, params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["timestamp"] = int(time.time())
        payload = "&".join(f"{k}={params[k]}" for k in sorted(params))
        params["signature"] = hashlib.sha1((payload + self.secret).encode()).hexdigest()
        params["api_key"] = self.key
        return params

    def _check(self, r):
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:200]
            raise RuntimeError(f"Cloudinary {r.status_code}: {msg}")
        return r.json() if r.content else {}

    def upload(self, data, public_id, kind="raw", delivery="private"):
        for attempt in range(3):
            try:
                r = requests.post(f"{self.api}/{kind}/upload", data=self._signed({"public_id": public_id, "type": delivery}),
                                  files={"file": (public_id.rsplit("/", 1)[-1], data)}, timeout=(15, 300))
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        return self._check(r)

    def download(self, public_id):
        r = requests.get(f"{self.api}/raw/download", params=self._signed({"public_id": public_id, "type": "private"}),
                         timeout=(15, 300))
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            self._check(r)
        return r.content

    def destroy(self, public_id, kind="raw", delivery="private"):
        r = requests.post(f"{self.api}/{kind}/destroy",
                          data=self._signed({"public_id": public_id, "type": delivery, "invalidate": "true"}), timeout=30)
        return self._check(r).get("result") == "ok"


CLOUD = Cloudinary(CLOUDINARY_URL) if CLOUDINARY_URL else None


def put_file(data, uid, kind):
    """Store encrypted bytes; returns the reference to keep in MongoDB.

    With Cloudinary: {"cld": [public ids...], "size": n}, private raw objects that only this server can fetch
    (still Fernet-encrypted, so Cloudinary never sees the contents). Otherwise a GridFS file id."""
    sealed = fernet.encrypt(data)
    if not CLOUD:
        return M.files.upload_from_stream(uuid.uuid4().hex, sealed, metadata={"uid": uid, "kind": kind})
    base = f"reachout/{uid}/{kind}-{uuid.uuid4().hex}"
    parts = [sealed[i:i + CLOUD.PART] for i in range(0, len(sealed), CLOUD.PART)] or [b""]
    ids = []
    try:
        for n, part in enumerate(parts):
            pid = base if len(parts) == 1 else f"{base}.{n}"
            CLOUD.upload(part, pid)
            ids.append(pid)
    except Exception:
        drop_file({"cld": ids})  # no half-written files left behind
        raise
    return {"cld": ids, "size": len(sealed)}


def get_file(ref):
    try:
        if isinstance(ref, dict):
            parts = [CLOUD.download(pid) for pid in ref.get("cld", [])] if CLOUD else [None]
            if any(p is None for p in parts):
                return None
            return fernet.decrypt(b"".join(parts))
        return fernet.decrypt(M.files.open_download_stream(ref).read())
    except (NoFile, InvalidToken):
        return None


def drop_file(ref):
    if isinstance(ref, dict):
        for pid in ref.get("cld", []):
            try:
                CLOUD and CLOUD.destroy(pid)
            except Exception as e:  # a leftover object costs a little space; never block the user's action
                print(f"[Reachout] Couldn't remove {pid} from Cloudinary: {e}", flush=True)
        return
    try:
        M.files.delete(ref)
    except NoFile:
        pass


def move_files_to_cloudinary(log=print):
    """One-off: copy every GridFS file to Cloudinary, point its record at the copy, then remove the GridFS one.

    Each copy is downloaded again and compared before anything is deleted. Safe to run more than once."""
    if not CLOUD:
        raise SystemExit("Set CLOUDINARY_URL first.")
    moved = 0
    for coll in (M.documents, M.wa_sessions):
        for row in coll.find({"file_id": {"$not": {"$type": "object"}}}):
            old = row["file_id"]
            data = get_file(old)
            if data is None:
                log(f"  skipped {coll.name} {row['_id']}: GridFS file missing or unreadable")
                continue
            uid = row.get("uid") or row["_id"]
            ref = put_file(data, uid, "document" if coll.name == "documents" else "whatsapp")
            if get_file(ref) != data:
                drop_file(ref)
                raise SystemExit(f"Verification failed for {coll.name} {row['_id']}; nothing was changed for it.")
            if coll.update_one({"_id": row["_id"], "file_id": old}, {"$set": {"file_id": ref}}).modified_count:
                drop_file(old)
                moved += 1
                log(f"  moved {coll.name} {row['_id']} ({len(data) / 1e6:.2f} MB)")
            else:
                drop_file(ref)  # the record changed meanwhile; keep the newer one
    for row in M.site_images.find({"data": {"$exists": True}}):
        raw = base64.b64decode(unseal(row["data"], "") or "")
        if not raw:
            continue
        up = CLOUD.upload(raw, f"reachout/{row['uid']}/site-{row['_id']}", kind="image", delivery="upload")
        M.site_images.update_one({"_id": row["_id"]}, {"$set": {"url": up["secure_url"], "public_id": up["public_id"]},
                                                         "$unset": {"data": ""}})
        moved += 1
        log(f"  moved site image {row['_id']}")
    left = M.db["files.files"].count_documents({})
    log(f"Done: {moved} file(s) moved; {left} file(s) still in GridFS.")
    return moved


def new_user(email, name, uid=None, terms=None):
    uid = uid or uuid.uuid4().hex
    M.users.insert_one({"_id": uid, "email_hash": lookup_hash(email), "data": seal({"email": email, "name": name}),
                        "created": time.time(), "last_login": None,
                        "terms_accepted": {"version": terms, "at": time.time()} if terms else None})
    return uid


def find_user(email=None, uid=None):
    row = M.users.find_one({"_id": uid} if uid else {"email_hash": lookup_hash(email)})
    if not row:
        return None
    return {"id": row["_id"], **unseal(row["data"], {})}


SESSION_DAYS = 30
SESSION_IDLE_DAYS = 7  # a login not used for a week expires


def start_session(uid):
    """Log in: a random token goes in the (signed, HttpOnly) cookie; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    M.sessions.insert_one({"_id": lookup_hash("session:" + token), "uid": uid, "created": now,
                           "expire_at": datetime.fromtimestamp(now + SESSION_DAYS * 86400, tz=timezone.utc)})
    session.clear()
    session.permanent = True
    session["sid"] = token


def current_uid():
    """The logged-in user for this request, or None. Cached per request."""
    if "uid" not in g:
        token = session.get("sid")
        row = M.sessions.find_one({"_id": lookup_hash("session:" + token)}) if isinstance(token, str) else None
        now = time.time()
        if row and now - row.get("seen", row["created"]) > SESSION_IDLE_DAYS * 86400:
            M.sessions.delete_one({"_id": row["_id"]})  # unused for too long: sign in again
            row = None
        elif row and now - row.get("seen", 0) > 3600:
            M.sessions.update_one({"_id": row["_id"]}, {"$set": {"seen": now}})
        g.uid = row["uid"] if row and find_user(uid=row["uid"]) else None
    return g.uid


def end_session():
    token = session.get("sid")
    if isinstance(token, str):
        M.sessions.delete_one({"_id": lookup_hash("session:" + token)})
    session.clear()


USER_COLLECTIONS = ("kv", "send_log", "documents", "notifications", "push_subs", "mail_index", "replies",
                    "site_images", "site_stats", "leads", "queue", "search")


def delete_user_data(uid):
    """MongoDB has no cascades: remove everything that belongs to one account."""
    DELETED[uid] = time.time()  # running jobs (campaigns, mail sync) stop writing for this account
    M.queue.update_many({"uid": uid, "status": {"$in": ["queued", "sending"]}}, {"$set": {"status": "cancelled"}})
    for f in M.files.find({"metadata.uid": uid}):
        drop_file(f._id)
    for row in [*M.documents.find({"uid": uid}), *M.wa_sessions.find({"_id": uid})]:
        if isinstance(row.get("file_id"), dict):
            drop_file(row["file_id"])
    if CLOUD:
        for row in M.site_images.find({"uid": uid, "public_id": {"$exists": True}}):
            try:
                CLOUD.destroy(row["public_id"], kind="image", delivery="upload")
            except Exception as e:
                print(f"[Reachout] Couldn't remove image {row['public_id']}: {e}", flush=True)
    for name in USER_COLLECTIONS:
        getattr(M, name).delete_many({"uid": uid})
    M.wa_sessions.delete_one({"_id": uid})
    M.site.delete_many({"_id": {"$regex": "^portfolio-slug:"}, "uid": uid})
    M.sessions.delete_many({"uid": uid})
    M.users.delete_one({"_id": uid})


# ---------------------------------------------------------------- validation

class Invalid(Exception):
    def __init__(self, message, field=None, status=400):
        super().__init__(message)
        self.message, self.field, self.status = message, field, status


@app.errorhandler(Invalid)
def on_invalid(e):
    return jsonify(error=e.message, field=e.field), e.status


@app.errorhandler(Undecryptable)
def on_undecryptable(e):
    app.logger.error("Undecryptable data for %s %s", request.method, request.path)
    return jsonify(error="Some of your saved data couldn't be read (the encryption key may have changed). "
                         "Nothing was changed. Restore the original data/secret.key and try again."), 500


@app.errorhandler(Exception)
def on_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description or e.name), e.code
        return e
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    if request.path.startswith("/api/"):
        return jsonify(error="Something went wrong on our side. Please try again."), 500
    return "Something went wrong. Please try again.", 500


@app.errorhandler(413)
def on_too_large(_):
    return jsonify(error=f"That upload is too large. Each file can be up to {MAX_FILE_MB} MB."), 413


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_CHARS_RE = re.compile(r"^\+?[\d\s\-().]+$")
COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def v_text(value, field, label, max_len, required=False, min_len=1):
    value = str(value if value is not None else "").strip()
    if not value:
        if required:
            raise Invalid(f"{label} is required.", field)
        return ""
    if len(value) < min_len:
        raise Invalid(f"{label} must be at least {min_len} characters.", field)
    if len(value) > max_len:
        raise Invalid(f"{label} must be {max_len} characters or fewer.", field)
    return value


def v_email(value, field="email", required=True, label="Email"):
    import unicodedata
    value = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not value:
        if required:
            raise Invalid(f"{label} is required.", field)
        return ""
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise Invalid("Enter a valid email address, like name@example.com.", field)
    local, _, domain = value.rpartition("@")
    try:  # internationalised domains are stored in their ASCII form, so look-alike letters can't pose as another domain
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise Invalid("Enter a valid email address, like name@example.com.", field)
    if not local.isascii() and any(c.isascii() and c.isalpha() for c in local):
        raise Invalid("That email mixes letters from different alphabets. Please type it again.", field)
    return f"{local}@{domain}"


def v_phone(value, field="phone", required=False):
    value = str(value or "").strip()
    if not value:
        if required:
            raise Invalid("Phone number is required.", field)
        return ""
    digits = re.sub(r"\D", "", value)
    if not PHONE_CHARS_RE.match(value) or not 8 <= len(digits) <= 15:
        raise Invalid("Enter a valid phone number: 8–15 digits, optionally starting with +.", field)
    return value


def safe_int(value, default=0):
    """int() for untrusted input that never raises (bad or giant values give the default)."""
    try:
        return int(str(value)[:12]) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def v_int(value, field, label, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"{label} must be a whole number.", field)
    if not lo <= n <= hi:
        raise Invalid(f"{label} must be between {lo} and {hi}.", field)
    return n


def v_template_text(value, field, label, max_len, required):
    value = v_text(value, field, label, max_len, required)
    try:
        list(string.Formatter().parse(value))
    except ValueError:
        raise Invalid(f"{label} has an unmatched {{ or }}. Fields look like {{name}}.", field)
    return value


def clean_contact(data, partial=False):
    """Validate a contact's fields; returns only the fields that were given."""
    out = {}
    for k, v in (data or {}).items():
        if k in STATUS_FIELDS or k == "id":
            continue
        if k == "stage":
            if v not in STAGES:
                raise Invalid("Choose a valid stage.", "stage")
            out[k] = v
        elif k == "follow_up":
            v = str(v or "").strip()
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise Invalid("Enter a valid follow-up date.", "follow_up")
            out[k] = v
        elif k == "phone":
            out[k] = v_phone(v)
        elif k == "email":
            out[k] = v_email(v, required=False)
        elif k in ("name", "company"):
            out[k] = v_text(v, k, k.capitalize(), 120)
        else:
            if not COLUMN_RE.match(str(k)):
                raise Invalid(f"Column names can use lowercase letters, numbers and _ only (got “{k}”).", k)
            out[k] = v_text(v, k, k.replace("_", " ").capitalize(), 500)
    extra = [k for k in out if k not in CORE_FIELDS + STATUS_FIELDS + CRM_FIELDS]
    if len(extra) > IMPORT_MAX_COLS:
        raise Invalid(f"A contact can have up to {IMPORT_MAX_COLS} extra columns.")
    if not partial and not out.get("phone") and not out.get("email"):
        raise Invalid("Add a phone number or an email address.", "phone")
    return out


# ---------------------------------------------------------------- per-user workspace

LOCKS = {}


def now_iso():
    """When a contact was added (local time, to the second); used by the "Added" filter."""
    return datetime.now().isoformat(timespec="seconds")


def new_id():
    return uuid.uuid4().hex[:10]


KV_MAX_BYTES = 12 * 1024 * 1024
DELETED = {}  # uid -> time: accounts deleted in this process (background writers check it)


class Workspace:
    def __init__(self, uid):
        self.uid = uid
        self.lock = LOCKS.setdefault(uid, threading.RLock())

    def load(self, key, default):
        row = M.kv.find_one({"_id": f"{self.uid}:{key}"})
        return unseal(row["data"], default, strict=True) if row else default

    def save(self, key, value):
        blob = seal(value)
        if len(blob) > KV_MAX_BYTES:  # MongoDB refuses documents over 16 MB; fail clearly, before that point
            raise Invalid("This is more data than one account can store here. Remove some items (old contacts, "
                          "columns or history) and try again.", status=413)
        if DELETED.get(self.uid) or not M.users.find_one({"_id": self.uid}, {"_id": 1}):
            return  # the account was deleted while a background job was still running: don't recreate its data
        M.kv.replace_one({"_id": f"{self.uid}:{key}"}, {"uid": self.uid, "key": key, "data": blob}, upsert=True)

    def settings(self):
        return {**DEFAULT_SETTINGS, **self.load("settings", {})}

    def update_settings(self, **changes):
        with self.lock:
            self.save("settings", {**self.settings(), **changes})

    def profile(self):
        return self.load("profile", {})

    # history
    def log(self, channel, to, name, status, detail="", rid=None, preview="", track=None, message_id=None,
            when=None, source=None):
        when = when or datetime.now()
        entry = {"timestamp": when.isoformat(timespec="seconds"), "channel": channel, "to": to,
                 "name": name, "status": status, "detail": detail, "preview": preview[:280]}
        if source:
            entry["source"] = source
        doc = {"uid": self.uid, "rid": rid, "day": when.date().isoformat(), "ts": when.timestamp(), "data": seal(entry)}
        if track:
            doc.update(track=track, opens=0, first_open=None, last_open=None)
        if message_id:
            doc["mid"] = lookup_hash("mid:" + message_id.strip("<> ").lower())
            doc["to_hash"] = lookup_hash("to:" + to.lower())
        M.send_log.insert_one(doc)

    @staticmethod
    def _with_opens(row):
        e = unseal(row["data"])
        if e:
            e["rid"] = row.get("rid")
            e["replied"] = bool(row.get("replied_at"))
        if e and "track" in row:
            e.update(tracked=True, opens=row.get("opens", 0), first_open=row.get("first_open"),
                     last_open=row.get("last_open"))
        return e

    def contact_log(self, contact):
        """Messages sent to one contact (older entries without an id are matched by phone/email)."""
        keys = {k for k in ("+" + phone_key(contact, self.settings()["country_code"]) if contact.get("phone") else "",
                            contact.get("email", "").lower()) if k}
        rows = M.send_log.find({"uid": self.uid, "rid": {"$in": [contact["id"], None]}}).sort("ts", DESCENDING).limit(500)
        out = []
        for r in rows:
            e = self._with_opens(r)
            if e and (r.get("rid") == contact["id"] or str(e.get("to", "")).lower() in keys):
                out.append(e)
        return out

    def events(self, rid):
        return self.load(f"contact:{rid}", [])

    def add_event(self, rid, kind, text):
        with self.lock:
            events = self.events(rid)
            event = {"id": new_id(), "type": kind, "text": text, "at": datetime.now().isoformat(timespec="seconds")}
            events.insert(0, event)
            self.save(f"contact:{rid}", events[:500])
        return event

    def drop_events(self, rids):
        M.kv.delete_many({"_id": {"$in": [f"{self.uid}:contact:{r}" for r in rids]}})

    def history(self, limit=1000):
        rows = M.send_log.find({"uid": self.uid}).sort("ts", DESCENDING).limit(limit)
        return [e for e in (self._with_opens(r) for r in rows) if e]

    def sent_today(self):
        rows = M.send_log.find({"uid": self.uid, "day": date.today().isoformat()}, {"data": 1})
        return sum(1 for r in rows if (unseal(r["data"], {}) or {}).get("status") == "sent")

    def mark_opened(self, rid, when):
        with self.lock:
            rows = self.load("recipients", [])
            for r in rows:
                if r["id"] == rid and not r.get("email_opened"):
                    r["email_opened"] = when
                    self.save("recipients", rows)
                    return True
        return False

    def set_status(self, rid, channel, status):
        prefix = "wa" if channel == "whatsapp" else "email"
        with self.lock:
            rows = self.load("recipients", [])
            for r in rows:
                if r["id"] == rid:
                    if r.get(f"{prefix}_status") != "sent" or status in ("sent", "bounced"):
                        r[f"{prefix}_status"] = status
                    r[f"{prefix}_last"] = datetime.now().isoformat(timespec="seconds")
                    if status == "sent" and r.get("stage", "new") == "new":
                        r["stage"] = "contacted"
            self.save("recipients", rows)

    # documents (bytes in GridFS, encrypted; file name encrypted in the metadata record)
    def documents(self):
        rows = M.documents.find({"uid": self.uid}).sort("created", ASCENDING)
        return [{"name": unseal(r["meta"], {}).get("name", "file"), "size": r["size"]} for r in rows]

    def _doc_key(self, name):
        return lookup_hash(f"{self.uid}:{name}")

    def doc_bytes(self, name):
        row = M.documents.find_one({"uid": self.uid, "name_hash": self._doc_key(name)})
        return get_file(row["file_id"]) if row else None

    def save_doc(self, name, data):
        file_id = put_file(data, self.uid, "document")
        old = M.documents.find_one_and_update(
            {"uid": self.uid, "name_hash": self._doc_key(name)},
            {"$set": {"size": len(data), "meta": seal({"name": name}), "file_id": file_id, "created": time.time()}},
            upsert=True)
        if old:
            drop_file(old["file_id"])

    def delete_doc(self, name):
        row = M.documents.find_one_and_delete({"uid": self.uid, "name_hash": self._doc_key(name)})
        if row:
            drop_file(row["file_id"])

    def files_size(self):
        return sum(d["size"] for d in self.documents())


def seed_workspace(ws, name, email):
    ws.save("recipients", [])
    ws.save("templates", [{"id": new_id(), "name": "Introduction", "subject": "Quick introduction – {sender_name}",
                           "body": STARTER_TEMPLATE}])
    ws.save("profile", {"name": name, "phone": "", "email": email, "smtp_host": "smtp.gmail.com",
                        "smtp_port": 465, "smtp_user": email, "smtp_password": ""})
    ws.save("settings", {})


def public_profile(p):
    out = {k: v for k, v in p.items() if k != "smtp_password"}
    out["has_password"] = bool(p.get("smtp_password"))
    return out


# ---------------------------------------------------------------- WhatsApp login storage

@contextmanager
def wa_profile(ws, keep=lambda: True):
    """Unpack the encrypted WhatsApp login into a private temp folder for one browser session.

    On exit it is packed, encrypted and written back to the database (when keep() is true),
    and the folder is always deleted."""
    folder = Path(tempfile.mkdtemp(prefix="reachout-wa-"))
    folder.chmod(0o700)
    try:
        row = M.wa_sessions.find_one({"_id": ws.uid})
        blob = get_file(row["file_id"]) if row else None
        if blob:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                tar.extractall(folder, filter="data")
        yield folder
        if keep():
            store_wa_folder(ws, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def store_wa_folder(ws, folder):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=3) as tar:
        tar.add(folder, arcname=".", filter=lambda t: None if WA_SKIP & set(Path(t.name).parts) else t)
    store_wa_blob(ws.uid, buf.getvalue())


def store_wa_blob(uid, data):
    file_id = put_file(data, uid, "whatsapp")
    old = M.wa_sessions.find_one_and_update({"_id": uid}, {"$set": {"file_id": file_id, "updated": time.time()},
                                                           "$setOnInsert": {"created": time.time()}}, upsert=True)
    if old:
        drop_file(old["file_id"])


def wa_expires_at(uid):
    row = M.wa_sessions.find_one({"_id": uid}, {"created": 1, "updated": 1})
    if not row:
        return None
    return (row.get("created") or row.get("updated") or time.time()) + WA_SESSION_HOURS * 3600


def expire_wa_sessions(now=None):
    """Delete every saved WhatsApp login older than WA_SESSION_HOURS and tell its owner. Returns how many went.

    A login that is sending or being linked right now is left until that finishes (the next pass removes it)."""
    now = now or time.time()
    cutoff = now - WA_SESSION_HOURS * 3600
    gone = 0
    for row in M.wa_sessions.find({"$or": [{"created": {"$lt": cutoff}},
                                           {"created": {"$exists": False}, "updated": {"$lt": cutoff}}]}):
        uid = row["_id"]
        link = WA_LINKS.get(uid)
        if uid in BUSY or (link and link.state in ("starting", "qr", "saving")):
            continue
        if not M.wa_sessions.find_one_and_delete({"_id": uid, "file_id": row["file_id"]}):
            continue  # changed meanwhile
        drop_file(row["file_id"])
        ws = Workspace(uid)
        ws.update_settings(wa_connected=False, wa_linked_at="")
        bridge_mod().notify(uid, "WhatsApp was unlinked automatically",
                            f"For your security, saved WhatsApp logins are deleted {WA_SESSION_HOURS} hours after linking. "
                            "Link it again when you want to send.", "/app/whatsapp", "whatsapp")
        gone += 1
    return gone


def wa_expiry_watcher():
    while True:
        time.sleep(60)
        try:
            n = expire_wa_sessions()
            if n:
                print(f"[Reachout] removed {n} expired WhatsApp login(s).", flush=True)
        except Exception as e:
            print(f"[Reachout] WhatsApp expiry error: {e}", flush=True)


# ---------------------------------------------------------------- rate limits + request guards

def rate_limited(key, limit, window, peek=False):
    """Fixed-window counter kept in MongoDB (shared by every worker, survives restarts, expires on its own).
    Keys are hashed, so no emails or IPs are stored in the clear. peek=True only checks whether the counter
    is already over OTP_FAILS_PER_DAY-style limits without counting."""
    now = time.time()
    bucket = int(now // window)
    kid = lookup_hash(f"rl:{json.dumps(key, default=str)}:{window}:{bucket}")
    if peek:
        row = M.ratelimits.find_one({"_id": kid})
        return bool(row) and row["n"] > (limit or OTP_FAILS_PER_DAY)
    try:
        row = M.ratelimits.find_one_and_update(
            {"_id": kid}, {"$inc": {"n": 1}, "$setOnInsert": {"expire_at": datetime.fromtimestamp((bucket + 2) * window, tz=timezone.utc)}},
            upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        row = M.ratelimits.find_one_and_update({"_id": kid}, {"$inc": {"n": 1}}, return_document=ReturnDocument.AFTER)
    return row["n"] > limit


def netlify_signed():
    """True when this request carries a valid Netlify proxy signature (X-Nf-Sign: an HS256 JWT)."""
    cached = g.get("nf_ok")
    if cached is not None:
        return cached
    ok = False
    token = request.headers.get("X-Nf-Sign", "")
    parts = token.split(".")
    if NETLIFY_PROXY_SECRET and len(parts) == 3:
        def b64(x):
            return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        try:
            header, claims = json.loads(b64(parts[0])), json.loads(b64(parts[1]))
            want = hmac.new(NETLIFY_PROXY_SECRET.encode(), f"{parts[0]}.{parts[1]}".encode(), "sha256").digest()
            ok = (header.get("alg") == "HS256" and hmac.compare_digest(want, b64(parts[2]))
                  and float(claims.get("exp", time.time() + 1)) > time.time() - 30)
        except (ValueError, TypeError):
            ok = False
    g.nf_ok = ok
    return ok


def client_ip():
    """The caller's address. Behind Netlify (signed) it's the visitor's address Netlify reports; otherwise,
    with TRUST_PROXY, only the proxy's X-Forwarded-For is honoured (ProxyFix)."""
    ip = request.remote_addr or ""
    if NETLIFY_PROXY_SECRET and netlify_signed():
        ip = request.headers.get("X-Nf-Client-Connection-Ip") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or ip
    return ":".join(ip.split(":")[:4]) if ":" in ip else ip  # group IPv6 by /64 so rotating addresses doesn't help


NF_REFUSED_LOGGED = []


@app.before_request
def only_via_netlify():
    if NETLIFY_PROXY_SECRET and request.path != "/healthz" and not netlify_signed():
        if not NF_REFUSED_LOGGED:  # once per process: enough to spot a misconfigured secret, no log flooding
            NF_REFUSED_LOGGED.append(1)
            app.logger.warning("Refused %s %s: not a signed Netlify request (check NETLIFY_PROXY_SECRET on both sides)",
                               request.method, request.path)
        return jsonify(error="Not found."), 404


@app.before_request
def guard():
    # State-changing API calls must come from our own pages (blocks cross-site form posts).
    if request.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify(error="Bad request."), 400


def app_csp(nonce):
    """Policy for Reachout's own pages: scripts only from this server (or by per-request nonce) plus the
    editor's CDN; no plugins; can't be framed by other sites."""
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "img-src 'self' data: blob: https:; media-src 'self' data:; "
            "connect-src 'self' https://cdn.jsdelivr.net; worker-src 'self' blob:; "
            "frame-src 'self' https: blob:; child-src 'self' https: blob:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


def add_nonce(html):
    """Stamp inline <script> tags in our own pages with this request's nonce (used by the CSP header)."""
    nonce = g.get("csp_nonce") or secrets.token_urlsafe(16)
    g.csp_nonce = nonce
    return re.sub(r"<script(?![^>]*\bsrc=)(?![^>]*\bnonce=)", f'<script nonce="{nonce}"', html)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    if SECURE_COOKIES or request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    nonce = g.get("csp_nonce")
    if nonce and resp.mimetype == "text/html":
        resp.headers.setdefault("Content-Security-Policy", app_csp(nonce))
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    if request.path.startswith(("/api/", "/app")):
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    if request.path.startswith("/assets/") and resp.status_code == 200:
        resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        uid = current_uid()
        if not uid:
            session.clear()
            return jsonify(error="Your session has ended. Please log in again."), 401
        return fn(Workspace(uid), *args, **kwargs)
    return wrapper


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise Invalid("Bad request.")
    return data


# ---------------------------------------------------------------- email (system + user)

def send_system_email(to, subject, text):
    if DEV_OTP:
        print(f"\n[Reachout dev] Email to {to}: {subject}\n{text}\n", flush=True)
        return
    try:
        if not SMTP_HOST:
            raise Invalid("Email sending isn't configured on this server. Contact the site owner.", status=503)
        return _send_via_smtp(to, subject, text)
    except Invalid:
        raise
    except Exception as e:
        app.logger.error("System email to a user failed: %s: %s", type(e).__name__, str(e)[:200])
        raise Invalid("We couldn't send the email right now. Please try again in a few minutes.", status=503)


def _send_via_smtp(to, subject, text):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = MAIL_FROM, to, subject
    m.set_content(text)
    server = (smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) if SMTP_PORT == 465
              else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20))
    try:
        if SMTP_PORT != 465:
            server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(m)
    finally:
        try:
            server.quit()
        except Exception:
            pass


MAIL_PORTS = (25, 465, 587, 2525)


def public_host(host, field="smtp_host"):
    """Refuse mail servers that resolve to private, loopback or link-local addresses. Otherwise anyone
    could make this server connect to internal services (database, cloud metadata) and probe the network."""
    import ipaddress
    import socket
    if os.environ.get("ALLOW_PRIVATE_MAIL_HOSTS") == "1" and not PRODUCTION:
        return host
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise Invalid(f"Couldn't find the mail server {host}. Check the address.", field)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise Invalid("That mail server address isn't allowed. Use your provider's public server, like smtp.gmail.com.", field)
    return host


def tls_context():
    import ssl
    return ssl.create_default_context()  # verifies the server's certificate and name


def smtp_connect(profile):
    host, port = profile.get("smtp_host", ""), int(profile.get("smtp_port") or 465)
    if not host or not profile.get("smtp_password"):
        raise RuntimeError("Email isn't set up yet. Add an app password on the Profile page.")
    if port not in MAIL_PORTS:
        raise RuntimeError("Use port 465 or 587 for your mail server.")
    public_host(host)
    server = (smtplib.SMTP_SSL(host, port, timeout=30, context=tls_context()) if port == 465
              else smtplib.SMTP(host, port, timeout=30))
    if port != 465:
        server.starttls(context=tls_context())
    server.login(profile.get("smtp_user") or profile["email"], profile["smtp_password"])
    return server


LINK_RE = re.compile(r"(https?://[^\s<]+|(?:www\.|linkedin\.com/|github\.com/)[^\s<]+|[\w.+-]+@[\w-]+\.[\w.-]+|\b[\w-]+\.vercel\.app\b)")


def text_to_html(text, pixel_url=None):
    """Plain message -> simple HTML twin: same words, clickable links, optional 1x1 open-tracking image."""
    def link(m):
        v = m.group(0)
        href = f"mailto:{v}" if "@" in v and "/" not in v else (v if v.startswith("http") else "https://" + v)
        return f'<a href="{html.escape(href)}">{html.escape(v)}</a>'
    parts, last = [], 0
    for m in LINK_RE.finditer(text):
        parts.append(html.escape(text[last:m.start()])); parts.append(link(m)); last = m.end()
    parts.append(html.escape(text[last:]))
    body = "".join(parts).replace("\n", "<br>\n")
    pixel = f'<img src="{html.escape(pixel_url)}" width="1" height="1" alt="" style="display:block;border:0">' if pixel_url else ""
    return (f'<!doctype html><html><body style="margin:0;padding:0">'
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1f2937">{body}</div>'
            f"{pixel}</body></html>")


def build_email(profile, to, subject, text, files, pixel_url=None):
    m = EmailMessage()
    m["From"] = f'{profile.get("name", "")} <{profile["email"]}>'
    m["To"] = to
    m["Subject"] = subject
    # Our own Message-ID lets bounce notices be matched back to this exact email.
    m["Message-ID"] = make_msgid(idstring="ro", domain=profile["email"].split("@")[-1])
    m.set_content(text)
    if pixel_url:
        m.add_alternative(text_to_html(text, pixel_url), subtype="html")
    for f in files:
        ctype = guess_type(f.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        m.add_attachment(f.read_bytes(), maintype=main, subtype=sub, filename=f.name)
    return m


# ---------------------------------------------------------------- OTP auth

def otp_hash(email_hash, code):
    return hmac.new(HASH_KEY, f"otp:{email_hash}:{code}".encode(), "sha256").hexdigest()


def otp_throttle(eh):
    """The same cooldown and hourly cap for every address, registered or not, so responses don't reveal who has an account."""
    now = time.time()
    row = M.otps.find_one({"_id": "cool:" + eh}, {"sent_at": 1})
    if row and now - row["sent_at"] < OTP_COOLDOWN:
        wait = int(OTP_COOLDOWN - (now - row["sent_at"])) + 1
        raise Invalid(f"Please wait {wait} seconds before requesting another code.", "email", 429)
    if rate_limited(("otp-email", eh), 6, 3600):
        raise Invalid("Too many codes requested for this email. Try again in an hour.", "email", 429)
    if rate_limited(("system-mail",), SYSTEM_MAIL_PER_HOUR, 3600):
        raise Invalid("We're getting a lot of requests right now. Please try again in a few minutes.", "email", 429)
    M.otps.replace_one({"_id": "cool:" + eh}, {"sent_at": now, "expire_at": datetime.fromtimestamp(now + OTP_COOLDOWN, tz=timezone.utc)}, upsert=True)


def issue_otp(email, purpose, extra=None):
    eh = lookup_hash(email)
    now = time.time()
    if rate_limited(("otp-fail-day", eh), 0, 86400, peek=True):
        raise Invalid("This account is temporarily locked after too many wrong codes. Try again tomorrow.", "email", 429)
    otp_throttle(eh)
    code = f"{secrets.randbelow(10**6):06d}"
    send_system_email(
        email, f"{code} is your Reachout code",
        {"login": f"Your Reachout login code is {code}.",
         "signup": f"Welcome to Reachout. Your sign-up code is {code}.",
         "delete": f"Your code to permanently delete your Reachout account is {code}. "
                   f"If you didn't ask for this, ignore this email."}[purpose]
        + f"\n\nIt expires in {OTP_TTL // 60} minutes. Never share this code with anyone.")
    M.otps.replace_one({"_id": eh}, {"purpose": purpose, "code_hash": otp_hash(eh, code), "data": seal(extra or {}),
                                     "expires": now + OTP_TTL, "attempts": 0, "sent_at": now,
                                     "expire_at": datetime.fromtimestamp(now + OTP_TTL, tz=timezone.utc)}, upsert=True)


def check_otp(email, code, purposes):
    """Verify a code; returns (purpose, extra). Consumes the code on success."""
    code = re.sub(r"\s", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise Invalid("Enter the 6-digit code from the email.", "code")
    eh = lookup_hash(email)
    row = M.otps.find_one({"_id": eh})
    if not row or row["purpose"] not in purposes:
        raise Invalid("No active code for this email. Request a new one.", "code")
    if time.time() > row["expires"]:
        M.otps.delete_one({"_id": eh})
        raise Invalid("This code has expired. Request a new one.", "code")
    if row["attempts"] >= OTP_ATTEMPTS:
        raise Invalid("Too many wrong attempts. Request a new code.", "code")
    if not hmac.compare_digest(row["code_hash"], otp_hash(eh, code)):
        # A day-long cap on wrong guesses on top of the per-code limit: slow, patient guessing gets locked out.
        if rate_limited(("otp-fail-day", eh), OTP_FAILS_PER_DAY, 86400):
            M.otps.delete_one({"_id": eh})
            raise Invalid("Too many wrong codes. This account is locked for 24 hours.", "code", 429)
        # Atomic increment, so parallel guesses can't exceed the limit.
        after = M.otps.find_one_and_update({"_id": eh, "code_hash": row["code_hash"]}, {"$inc": {"attempts": 1}},
                                           return_document=ReturnDocument.AFTER)
        left = OTP_ATTEMPTS - (after["attempts"] if after else OTP_ATTEMPTS)
        raise Invalid(f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left."
                      if left > 0 else "Too many wrong attempts. Request a new code.", "code")
    # Delete only if still unused and under the limit, so one code can't be used twice.
    if M.otps.delete_one({"_id": eh, "code_hash": row["code_hash"], "attempts": {"$lt": OTP_ATTEMPTS}}).deleted_count != 1:
        raise Invalid("No active code for this email. Request a new one.", "code")
    return row["purpose"], unseal(row["data"], {})


@app.post("/api/auth/request-code")
def request_code():
    p = body()
    email = v_email(p.get("email"))
    if rate_limited(("otp-ip", client_ip()), 20, 3600):
        raise Invalid("Too many requests from this network. Try again later.", status=429)
    user = find_user(email)
    if p.get("intent") == "signup":
        name = v_text(p.get("name"), "name", "Full name", 80, required=True, min_len=2)
        if not p.get("agree"):
            raise Invalid("Please tick the box to accept the terms.", "agree")
        if not user and not ALLOW_SIGNUP:
            otp_throttle(lookup_hash(email))  # same response as a registered email: don't reveal who has an account
            send_system_email(email, "Reachout sign-ups are closed", "Thanks for your interest in Reachout. "
                              "New sign-ups are closed right now; we'll be glad to have you when they reopen.")
            return jsonify(ok=True, resend_in=OTP_COOLDOWN, dev=DEV_OTP)
        # An existing account just gets a login code, so sign-up doesn't reveal who is registered.
        issue_otp(email, "login" if user else "signup", {} if user else {"name": name, "terms": TERMS_VERSION})
    elif user:
        issue_otp(email, "login")
    else:
        # Same response (and the same cooldown) for unknown emails, so this can't be used to check who has an account.
        otp_throttle(lookup_hash(email))
        send_system_email(email, "Reachout sign-in attempt",
                          "Someone tried to log in to Reachout with this email, but there's no account for it. "
                          "If it was you, sign up instead.")
    return jsonify(ok=True, resend_in=OTP_COOLDOWN, dev=DEV_OTP)


@app.post("/api/auth/verify-code")
def verify_code():
    p = body()
    email = v_email(p.get("email"))
    if rate_limited(("verify-ip", client_ip()), 30, 900):
        raise Invalid("Too many attempts. Wait a few minutes and try again.", "code", 429)
    purpose, extra = check_otp(email, p.get("code"), ("login", "signup"))
    user = find_user(email)
    if not user:
        if purpose != "signup":
            raise Invalid("No account for this email. Sign up instead.", "email")
        uid = new_user(email, extra.get("name") or email.split("@")[0], terms=extra.get("terms"))
        seed_workspace(Workspace(uid), extra.get("name", ""), email)
    else:
        uid = user["id"]
    M.users.update_one({"_id": uid}, {"$set": {"last_login": time.time()}})
    start_session(uid)
    return jsonify(ok=True)


@app.post("/api/auth/logout")
def logout():
    end_session()
    return jsonify(ok=True)


@app.post("/api/auth/logout-everywhere")
@login_required
def logout_everywhere(ws):
    """Sign out every device, including this one (e.g. after using a shared computer)."""
    n = M.sessions.delete_many({"uid": ws.uid}).deleted_count
    session.clear()
    return jsonify(ok=True, devices=n)


@app.get("/api/me")
@login_required
def me(ws):
    u = find_user(uid=ws.uid)
    return jsonify(name=u["name"], email=u["email"], is_admin=is_admin(ws.uid), stages=STAGES,
                   can_track_opens=can_track_opens(), daily_limit=DAILY_LIMIT, sent_today=ws.sent_today(),
                   min_wa_delay=MIN_WA_DELAY, max_files_mb=MAX_FILES_MB, max_file_mb=MAX_FILE_MB,
                   allowed_docs=sorted(ALLOWED_DOCS), whatsapp=WHATSAPP_ENABLED)


@app.post("/api/account/delete/request")
@login_required
def delete_account_request(ws):
    issue_otp(find_user(uid=ws.uid)["email"], "delete")
    return jsonify(ok=True, resend_in=OTP_COOLDOWN, dev=DEV_OTP)


@app.post("/api/account/delete")
@login_required
def delete_account(ws):
    check_otp(find_user(uid=ws.uid)["email"], body().get("code"), ("delete",))
    if ws.uid in JOBS:
        JOBS[ws.uid].stop.set()
    if ws.uid in WA_LINKS:
        WA_LINKS[ws.uid].cancel.set()
    delete_user_data(ws.uid)
    session.clear()
    return jsonify(ok=True)


# ---------------------------------------------------------------- helpers

def norm_header(h):
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # Excel stores phone numbers as floats
    return str(v).strip()


IMPORT_MAX_COLS = 40


def read_table(filename, data):
    """Return rows (list of dicts) from an uploaded .xlsx or .csv file."""
    name = filename.lower()
    try:
        if name.endswith((".xlsx", ".xlsm")):
            import zipfile
            from openpyxl import load_workbook
            # An .xlsx is a zip: check what it expands to before opening it (a tiny file can unpack to GBs).
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if len(infos) > 200 or sum(i.file_size for i in infos) > 60 * 1024 * 1024:
                    raise Invalid("That spreadsheet is too large to import. Save just the contacts sheet and try again.", "file")
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
            grid = []
            for row in sheet.iter_rows(values_only=True, max_col=IMPORT_MAX_COLS):  # streamed, stops at the cap
                grid.append([cell_text(c) for c in row])
                if len(grid) > MAX_CONTACTS + 1:
                    break
        elif name.endswith(".csv"):
            grid = []
            for row in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))):
                grid.append(row[:IMPORT_MAX_COLS])
                if len(grid) > MAX_CONTACTS + 1:
                    break
        else:
            raise Invalid("Upload an Excel (.xlsx) or CSV file.", "file")
    except Invalid:
        raise
    except Exception:
        raise Invalid("We couldn't read that file. Check it opens in Excel, then try again.", "file")
    grid = [r for r in grid if any(c.strip() for c in r)]
    if len(grid) < 2:
        raise Invalid("That file has no rows under the header.", "file")
    headers = []
    for h in grid[0]:
        n = norm_header(h)
        key = next((f for f, aliases in HEADER_ALIASES.items() if n in aliases), None)
        col = key or n.replace(" ", "_")
        headers.append(col if col and COLUMN_RE.match(col) and col not in STATUS_FIELDS and col != "id" else None)
    if "phone" not in headers and "email" not in headers:
        raise Invalid("We couldn't find a phone or email column. Name one of your columns “Phone” or “Email”.", "file")
    rows = []
    for r in grid[1:]:
        row = {}
        for h, v in zip(headers, r):
            if h and h not in row:
                row[h] = v.strip()[:500]
        rows.append(row)
    return rows


def phone_key(row, cc):
    p = row.get("phone", "")
    return wa.normalise_phone(p, cc) if p.strip() else ""


class Fields(dict):
    def __missing__(self, key):
        return "{" + key + "}"  # leave unknown placeholders untouched


def render(text, recipient, profile):
    values = Fields({k: v for k, v in recipient.items() if k not in STATUS_FIELDS and k != "id" and v})
    values.setdefault("name", "there")
    values.setdefault("company", "your company")
    for k in ("name", "phone", "email"):
        values[f"sender_{k}"] = (profile or {}).get(k, "")
    try:
        return string.Formatter().vformat(text, (), values).strip()
    except (ValueError, IndexError, AttributeError, KeyError):
        return text.strip()


# ---------------------------------------------------------------- WhatsApp browsers

SLOTS = threading.BoundedSemaphore(MAX_BROWSERS)
BUSY, BUSY_LOCK = set(), threading.Lock()


@contextmanager
def browser_slot(uid, say=None):
    """One WhatsApp browser per user, at most MAX_BROWSERS on the server."""
    with BUSY_LOCK:
        if uid in BUSY:
            raise RuntimeError("WhatsApp is busy for your account (linking or sending). Try again shortly.")
        BUSY.add(uid)
    try:
        if not SLOTS.acquire(timeout=1):
            if say:
                say("Server is busy – waiting for a free WhatsApp slot…")
            SLOTS.acquire()
        try:
            yield
        finally:
            SLOTS.release()
    finally:
        with BUSY_LOCK:
            BUSY.discard(uid)


def open_whatsapp(pw, folder):
    args = ["--disable-dev-shm-usage"] + (["--no-sandbox"] if NO_SANDBOX else [])
    ctx = pw.chromium.launch_persistent_context(str(folder), headless=HEADLESS, user_agent=USER_AGENT,
                                                viewport={"width": 1280, "height": 900}, args=args)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://web.whatsapp.com")
    return ctx, page


class WALink:
    def __init__(self):
        self.state, self.error, self.qr = "starting", "", None
        self.cancel = threading.Event()


WA_LINKS = {}


def link_whatsapp(ws, link):
    try:
        from playwright.sync_api import sync_playwright
        with browser_slot(ws.uid), wa_profile(ws, keep=lambda: link.state == "saving") as folder, \
                sync_playwright() as pw:
            ctx, page = open_whatsapp(pw, folder)
            try:
                deadline = time.time() + 180
                while time.time() < deadline and not link.cancel.is_set():
                    if page.locator(LOGGED_IN).count():
                        link.state = "saving"
                        page.wait_for_timeout(8000)  # let WhatsApp finish saving the new login
                        break
                    qr = page.locator("canvas").first
                    if qr.count() and qr.is_visible():
                        link.qr, link.state = qr.screenshot(), "qr"
                    page.wait_for_timeout(1500)
                else:
                    link.state, link.error = "error", ("Cancelled." if link.cancel.is_set()
                                                       else "The QR code expired. Try again.")
            finally:
                ctx.close()
        # wa_profile has encrypted and stored the login by now.
        if link.state == "saving":
            M.wa_sessions.update_one({"_id": ws.uid}, {"$set": {"created": time.time()}})  # a re-link starts a new 2 hours
            ws.update_settings(wa_connected=True, wa_linked_at=datetime.now().isoformat(timespec="seconds"))
            link.state = "connected"
    except Exception as e:
        link.state, link.error = "error", str(e).splitlines()[0]


# ---------------------------------------------------------------- deliverability: domain checks + bounces

def domain_problem(domain, cache):
    """Return a reason if the domain can't receive mail at all (checked once per domain per run)."""
    domain = domain.lower().strip(".")
    if domain in cache:
        return cache[domain]
    reason = ""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        if all(str(a.exchange).strip(".") == "" for a in answers):
            reason = "This domain says it doesn't accept email (null MX)."
    except dns.resolver.NXDOMAIN:
        reason = "This email's domain doesn't exist."
    except dns.resolver.NoAnswer:
        try:  # no MX: mail may still go to the domain's own address
            dns.resolver.resolve(domain, "A", lifetime=6)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            reason = "This email's domain has no mail server."
        except dns.exception.DNSException:
            pass
    except dns.exception.DNSException:
        pass  # DNS hiccup: don't block the send on a lookup failure
    cache[domain] = reason
    return reason


BOUNCE_REASONS = (  # (pattern, friendly reason); first match wins, most specific first
    # Microsoft 365 answers "5.4.1 Recipient address rejected: Access denied" when the address isn't in its directory.
    (r"5\.1\.1\b|5\.1\.10\b|5\.4\.1\b.*recipient address rejected|recipient address rejected: access denied|"
     r"does ?n.t exist|does not exist|no such user|user unknown|unknown user|recipient not found|address not found|"
     r"invalid recipient|mailbox unavailable|mailbox not found|not a valid|unrouteable", "Address doesn't exist"),
    (r"5\.7\.133|5\.7\.134|5\.7\.135|5\.7\.136|notauthenticatedforgroup|restricted to internal|only accepts mail from|"
     r"internal senders|not allowed to send to this (group|address)", "Internal-only address (accepts mail from their staff only)"),
    (r"5\.1\.2\b|domain not found|host not found|no mx|couldn.t be found", "Domain can't receive email"),
    (r"5\.2\.2\b|mailbox full|over quota|quota exceeded|insufficient storage", "Mailbox full"),
    (r"5\.4\.14|hop count exceeded|mail loop", "Mail loop at the recipient's server"),
    (r"5\.7\.\d+|blocked|block list|blacklist|spam|policy|denied|not authori[sz]ed|rejected|refused|access denied|restricted",
     "Blocked by the recipient's server"),
)


def bounce_reason(text):
    t = (text or "").lower()
    for pattern, label in BOUNCE_REASONS:
        if re.search(pattern, t):
            return label
    return "Couldn't be delivered"


def imap_host_for(profile):
    host = (profile.get("smtp_host") or "").lower()
    return {"smtp.gmail.com": "imap.gmail.com", "smtp.office365.com": "outlook.office365.com",
            "smtp-mail.outlook.com": "outlook.office365.com", "smtp.mail.yahoo.com": "imap.mail.yahoo.com",
            "smtp.zoho.com": "imap.zoho.com", "smtp.zoho.in": "imap.zoho.in"}.get(host, host.replace("smtp.", "imap.", 1))


def imap_connect(profile):
    """Log in to the sender's mailbox over IMAP with the saved app password (used read-only)."""
    if not profile.get("smtp_password"):
        raise Invalid("Set up email on the Profile page first.")
    host = public_host(imap_host_for(profile))
    try:
        imap = imaplib.IMAP4_SSL(host, 993, timeout=30, ssl_context=tls_context())
    except OSError:
        raise Invalid(f"Couldn't connect to your mailbox at {host}. Check the mail server address.")
    try:
        imap.login(profile.get("smtp_user") or profile["email"], profile["smtp_password"])
    except imaplib.IMAP4.error:
        raise Invalid("Your mail server refused the login. For Gmail, make sure IMAP is on "
                      "(Gmail → Settings → Forwarding and POP/IMAP) and the app password is current.")
    return imap


def parse_bounce(raw):
    """From one DSN email: [(failed recipient, status code, diagnostic, original Message-ID or '')]."""
    msg = email_lib.message_from_bytes(raw, policy=email_policy)
    original_mid, failures, text_bits = "", [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            blocks = part.get_payload()
            for block in blocks[1:] if isinstance(blocks, list) else []:
                action = str(block.get("Action", "")).lower()
                rcpt = str(block.get("Final-Recipient") or block.get("Original-Recipient") or "")
                rcpt = rcpt.split(";", 1)[-1].strip().strip("<>").lower()
                if rcpt and action.startswith("failed"):
                    failures.append([rcpt, str(block.get("Status", "")), str(block.get("Diagnostic-Code", ""))])
        elif ctype in ("message/rfc822", "text/rfc822-headers") and not original_mid:
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                original_mid = str(inner[0].get("Message-ID", ""))
            else:
                m = re.search(r"^Message-ID:\s*(<[^>]+>)", part.get_content() if ctype == "text/rfc822-headers" else "",
                              re.I | re.M)
                original_mid = m.group(1) if m else ""
        elif ctype == "text/plain" and not failures:
            try:
                text_bits.append(part.get_content()[:4000])
            except Exception:
                pass
    if not failures:  # some servers only use X-Failed-Recipients plus a plain-text explanation
        text = " ".join(text_bits)
        for rcpt in re.split(r"[,\s]+", str(msg.get("X-Failed-Recipients", ""))):
            if "@" in rcpt:
                failures.append([rcpt.strip("<>").lower(), "", text[:600]])
    if not original_mid:
        m = re.search(rb"^Message-ID:\s*(<[^>]+>)", raw.split(b"\r\n\r\n", 1)[-1], re.I | re.M)
        original_mid = m.group(1).decode(errors="ignore") if m else ""
    return [(r, st, diag, original_mid) for r, st, diag in failures]


def check_bounces(ws, days=14):
    """Read bounce notices from the sender's inbox (read-only) and mark matching sends as bounced."""
    profile = ws.profile()
    if not profile.get("smtp_password"):
        raise Invalid("Set up email on the Profile page first.")
    seen = set(ws.load("bounces_seen", []))
    found, checked = [], 0
    imap = imap_connect(profile)
    try:
        imap.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        ids = set()
        for crit in ('(SINCE {d} FROM "mailer-daemon")', '(SINCE {d} FROM "postmaster")',
                     '(SINCE {d} SUBJECT "Undeliverable")', '(SINCE {d} SUBJECT "Delivery Status Notification")',
                     '(SINCE {d} SUBJECT "Mail delivery failed")', '(SINCE {d} SUBJECT "Undelivered Mail")'):
            typ, data = imap.search(None, crit.format(d=since))
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        for num in sorted(ids, key=int):
            typ, data = imap.fetch(num, "(BODY.PEEK[]<0.400000>)")  # PEEK: doesn't mark the notice as read; first 400 KB only
            raw = next((d[1] for d in data if isinstance(d, tuple)), None)
            if not raw:
                continue
            key = lookup_hash("dsn:" + hashlib.sha256(raw).hexdigest())
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            for rcpt, status_code, diag, mid in parse_bounce(raw):
                if status_code.startswith("4"):
                    continue  # temporary: the mail server is still retrying
                row = None
                if mid:
                    row = M.send_log.find_one({"uid": ws.uid, "mid": lookup_hash("mid:" + mid.strip("<> ").lower())})
                if not row:  # older sends without our Message-ID: match the most recent email to that address
                    row = M.send_log.find_one({"uid": ws.uid, "to_hash": lookup_hash("to:" + rcpt)}, sort=[("ts", -1)]) \
                        or next((d for d in M.send_log.find({"uid": ws.uid, "rid": {"$ne": None}}).sort("ts", -1).limit(3000)
                                 if (unseal(d["data"], {}) or {}).get("to", "").lower() == rcpt
                                 and (unseal(d["data"], {}) or {}).get("status") == "sent"), None)
                if not row:
                    continue
                entry = unseal(row["data"], {})
                if entry.get("status") == "bounced":
                    continue
                reason = bounce_reason(f"{status_code} {diag}")
                detail = f"{reason}. {re.sub(r'\\s+', ' ', diag).strip()[:220]}".strip()
                entry.update(status="bounced", detail=detail)
                M.send_log.update_one({"_id": row["_id"]}, {"$set": {"data": seal(entry)}})
                if row.get("rid"):
                    ws.set_status(row["rid"], "email", "bounced")
                    ws.add_event(row["rid"], "bounced", f"Email bounced: {reason}")
                found.append({"to": rcpt, "reason": reason})
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        with ws.lock:  # union with what another check may have saved meanwhile
            seen |= set(ws.load("bounces_seen", []))
            ws.save("bounces_seen", sorted(seen)[-5000:])
        ws.update_settings(bounces_checked_at=datetime.now().isoformat(timespec="seconds"))
    if found and getattr(bridge_mod(), "notify", None):
        bridge_mod().notify(ws.uid, f"{len(found)} email{'s' if len(found) != 1 else ''} bounced",
                            ", ".join(f["to"] for f in found[:3]) + (" and more" if len(found) > 3 else ""), "#activity", "bounce")
    return {"checked": checked, "bounced": found}


BOUNCE_CHECK_SOON = {}   # uid -> when to run an extra check after a campaign


def bounce_watcher():
    """Background loop: checks accounts that sent email recently, every 10 minutes (sooner right after a run)."""
    last = {}
    while True:
        time.sleep(30)
        try:
            now = time.time()
            recent = M.send_log.distinct("uid", {"ts": {"$gt": now - 3 * 86400}, "mid": {"$exists": True}})
            for uid in recent:
                due = BOUNCE_CHECK_SOON.get(uid, 0)
                if (due and now >= due) or now - last.get(uid, 0) > 600:
                    if uid in JOBS and JOBS[uid].running:
                        continue
                    BOUNCE_CHECK_SOON.pop(uid, None)
                    last[uid] = now
                    try:
                        bridge_mod().with_deadline(240, check_bounces, Workspace(uid), days=4)
                    except Exception as e:
                        print(f"[Reachout] bounce check skipped for an account: {getattr(e, 'message', e)}", flush=True)
        except Exception as e:
            print(f"[Reachout] bounce watcher error: {e}", flush=True)


# ---------------------------------------------------------------- import past sends from the Sent folder

PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "rocketmail.com",
                    "outlook.com", "outlook.in", "hotmail.com", "hotmail.co.in", "live.com", "live.in", "msn.com",
                    "icloud.com", "me.com", "mac.com", "aol.com", "rediffmail.com", "proton.me", "protonmail.com",
                    "zoho.com", "zohomail.in", "gmx.com", "mail.com", "yandex.com"}
AUTOMATED = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?|bounce|alerts?|support|"
                       r"help|feedback|billing|invoices?)(\+[^@]*)?@|alert|\.coach\.|"
                       r"@(.+\.)?(greenhouse|lever|workday|myworkday|smartrecruiters|icims|taleo|naukri|linkedin|indeed|"
                       r"mail-tester|zendesk|freshdesk|helpscout|intercom-mail|mailchimp|sendgrid)\.", re.I)


def find_sent_folder(imap):
    typ, folders = imap.list()
    names = []
    for raw in folders or []:
        line = raw.decode(errors="ignore")
        m = re.search(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if "\\Sent" in m.group("flags"):
            return name
        names.append(name)
    for guess in ("[Gmail]/Sent Mail", "Sent Items", "Sent", "INBOX.Sent", "Sent Messages"):
        if guess in names:
            return guess
    raise Invalid("Couldn't find your Sent folder.")


def clean_display_name(name):
    """'Sangani, Mahesh' -> 'Mahesh Sangani'; drops '(Company)' suffixes and names that are just addresses."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip().strip('"\'')).strip()
    if "@" in name or not re.search(r"[A-Za-z]", name):
        return ""
    if re.fullmatch(r"[^,]+,\s*[^,]+", name):
        last, first = [x.strip() for x in name.split(",", 1)]
        name = f"{first} {last}"
    return name[:80]


def company_from_domain(domain):
    label = domain.lower().split(".")
    core = label[-3] if len(label) >= 3 and label[-2] in ("co", "com", "net", "org", "ac", "gov") else label[-2] if len(label) >= 2 else label[0]
    return core.replace("-", " ").title()


def already_logged(ws, addr, when_ts, rid=None):
    """True if history already has an email to this address within 15 minutes (e.g. a Reachout campaign send)."""
    q = {"uid": ws.uid, "ts": {"$gte": when_ts - 900, "$lte": when_ts + 900}}
    if rid:
        q["rid"] = rid
    for d in M.send_log.find(q, {"data": 1}):
        e = unseal(d["data"], {}) or {}
        if e.get("channel") == "email" and str(e.get("to", "")).lower() == addr:
            return True
    return False


def scan_sent(ws, days):
    """Group every work address you emailed (headers only: To, Cc, Date, Subject, Message-ID).

    days=0 scans the whole Sent folder. Personal mailboxes (gmail.com, yahoo.com, …) are left out entirely."""
    profile = ws.profile()
    me = {profile.get("email", "").lower(), (profile.get("smtp_user") or "").lower()}
    imap = imap_connect(profile)
    people = {}
    try:
        folder = find_sent_folder(imap)
        typ, _ = imap.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise Invalid("Couldn't open your Sent folder.")
        crit = f"(SINCE {(date.today() - timedelta(days=days)).strftime('%d-%b-%Y')})" if days else "ALL"
        typ, data = imap.search(None, crit)
        ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-20000:]
        for i in range(0, len(ids), 200):
            chunk = b",".join(ids[i:i + 200]).decode()
            typ, rows = imap.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (TO CC DATE SUBJECT MESSAGE-ID)])")
            for item in rows or []:
                if not isinstance(item, tuple):
                    continue
                h = email_lib.message_from_bytes(item[1], policy=email_policy)
                mid = str(h.get("Message-ID", "")).strip()
                if re.search(r"\.ro@", mid):
                    continue  # sent by Reachout itself: already in your history
                try:
                    when = parsedate_to_datetime(str(h.get("Date"))).astimezone().replace(tzinfo=None)
                except (TypeError, ValueError):
                    continue
                subject = re.sub(r"\s+", " ", str(h.get("Subject", "")))[:200]
                addrs = getaddresses([str(h.get("To", "")), str(h.get("Cc", ""))])
                for name, addr in addrs:
                    addr = addr.strip().lower()
                    if not EMAIL_RE.match(addr) or addr in me or AUTOMATED.search(addr) \
                            or addr.split("@")[1] in PERSONAL_DOMAINS:
                        continue
                    name = clean_display_name(name)
                    p = people.setdefault(addr, {"email": addr, "name": name, "count": 0, "sends": []})
                    p["count"] += 1
                    p["sends"].append({"date": when.isoformat(timespec="seconds"), "subject": subject, "mid": mid})
                    if name and not p["name"]:
                        p["name"] = name
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    known = {r.get("email", "").lower(): r for r in ws.load("recipients", []) if r.get("email")}
    imported = set(ws.load("sent_imported", []))
    out = []
    for p in people.values():
        p["sends"] = [x for x in p["sends"] if not already_logged(ws, p["email"], datetime.fromisoformat(x["date"]).timestamp())]
        if not p["sends"]:
            continue  # every email to this person is already in Reachout's history
        p["count"] = len(p["sends"])
        p["sends"].sort(key=lambda x: x["date"], reverse=True)
        domain = p["email"].split("@")[1]
        contact = known.get(p["email"])
        out.append({**p, "sends": p["sends"][:20], "last": p["sends"][0]["date"], "last_subject": p["sends"][0]["subject"],
                    "domain": domain, "personal": domain in PERSONAL_DOMAINS,
                    "company": (contact or {}).get("company") or ("" if domain in PERSONAL_DOMAINS else company_from_domain(domain)),
                    "contact": bool(contact),
                    "imported": all(lookup_hash("sent:" + s["mid"].lower()) in imported for s in p["sends"] if s["mid"])})
    out.sort(key=lambda x: x["last"], reverse=True)   # newest first…
    out.sort(key=lambda x: x["personal"])              # …with work addresses before personal ones (stable sort)
    return out


@app.post("/api/sentmail/scan")
@login_required
def sentmail_scan(ws):
    days = v_int(body().get("days", 0), "days", "Period", 0, 3650)  # 0 = whole Sent folder
    if rate_limited(("sent-scan", ws.uid), 10, 3600):
        raise Invalid("You've scanned several times recently. Try again in a few minutes.", status=429)
    items = scan_sent(ws, days)
    return jsonify(items=items, days=days)


@app.post("/api/sentmail/import")
@login_required
def sentmail_import(ws):
    p = body()
    items = p.get("items")
    # Off: people are added as fresh contacts that campaigns will email; earlier emails are kept as a note only.
    mark_contacted = bool(p.get("mark_contacted"))
    if not isinstance(items, list) or not items:
        raise Invalid("Select at least one person to import.")
    if len(items) > 2000:
        raise Invalid("Import at most 2,000 people at a time.")
    imported = set(ws.load("sent_imported", []))
    added = updated = history = 0
    with ws.lock:
        rows = ws.load("recipients", [])
        by_email = {r.get("email", "").lower(): r for r in rows if r.get("email")}
        touched = []
        for it in items:
            addr = v_email(it.get("email"))
            sends = [s for s in (it.get("sends") or [])[:20] if isinstance(s, dict)]
            if not sends:
                continue
            last = max(s.get("date", "") for s in sends)
            row = by_email.get(addr)
            if not row:
                if len(rows) >= MAX_CONTACTS:
                    raise Invalid(f"You've reached the limit of {MAX_CONTACTS} contacts.")
                row = {"id": new_id(), **clean_contact({"name": str(it.get("name") or "")[:80],
                                                         "company": str(it.get("company") or "")[:120], "email": addr}),
                       "stage": "contacted" if mark_contacted else "new", "list": "Imported from Sent mail", "added_at": now_iso()}
                rows.append(row); by_email[addr] = row; added += 1
            else:
                updated += 1
                if mark_contacted and row.get("stage", "new") == "new":
                    row["stage"] = "contacted"
            if mark_contacted:
                if row.get("email_status") not in ("bounced", "invalid"):
                    row["email_status"] = "sent"
                if last > (row.get("email_last") or ""):
                    row["email_last"] = last
            touched.append((row, sends))
        ws.save("recipients", rows)
    if not mark_contacted:
        # Keep a record of the earlier emails on each contact, without counting them as sends.
        for row, sends in touched:
            fresh = [s for s in sends if not s.get("mid") or lookup_hash("sent:" + str(s["mid"]).lower()) not in imported]
            if not fresh:
                continue
            lines = "; ".join(f"{str(s.get('date'))[:10]} \u201c{s.get('subject') or 'no subject'}\u201d" for s in fresh[:5])
            ws.add_event(row["id"], "note", f"You emailed this address {len(fresh)} time{'s' if len(fresh) != 1 else ''} "
                         f"before Reachout (from your Sent folder): {lines}.")
            imported.update(lookup_hash("sent:" + str(s["mid"]).lower()) for s in fresh if s.get("mid"))
        ws.save("sent_imported", sorted(imported)[-20000:])
        return jsonify(added=added, updated=updated, history=0, bounced=0)
    for row, sends in touched:
        new = []
        for s in sends:
            key = lookup_hash("sent:" + str(s.get("mid", "")).lower()) if s.get("mid") else None
            if key and key in imported:
                continue
            try:
                when = datetime.fromisoformat(str(s.get("date")))
            except ValueError:
                continue
            if already_logged(ws, row["email"].lower(), when.timestamp()):
                continue
            ws.log("email", row["email"], row.get("name", ""), "sent", "Sent from your own mailbox",
                   rid=row["id"], preview=str(s.get("subject", ""))[:200], message_id=s.get("mid") or None,
                   when=when, source="mailbox")
            if key:
                imported.add(key)
            new.append(s); history += 1
        if new:
            ws.add_event(row["id"], "note", f"Found {len(new)} earlier email{'s' if len(new) != 1 else ''} in your Sent folder "
                         f"(latest: \u201c{new[0].get('subject') or 'no subject'}\u201d, {str(new[0].get('date'))[:10]}).")
    ws.save("sent_imported", sorted(imported)[-20000:])
    # Earlier sends may have bounced too: look for their bounce notices over the same period.
    oldest = min((s.get("date", "") for _, ss in touched for s in ss), default="")
    bounced = 0
    if oldest:
        try:
            days = max(14, (datetime.now() - datetime.fromisoformat(oldest)).days + 2)
            bounced = len(check_bounces(ws, days=min(days, 3650))["bounced"])
        except Invalid:
            pass
    return jsonify(added=added, updated=updated, history=history, bounced=bounced)


# ---------------------------------------------------------------- send jobs

class Job:
    def __init__(self):
        self.stop = threading.Event()
        self.lines = []
        self.done = self.total = 0
        self.running = False
        self.sent = self.failed = self.skipped = 0
        self.current = ""        # who is being sent to right now
        self.phase = "starting"  # starting | sending | waiting | stopping | done | stopped | error
        self.next_at = None      # epoch seconds of the next send while waiting
        self.started = time.time()
        self.finished = None
        self.dry_run = False
        self.channels = []

    def say(self, text):
        self.lines.append(f"{datetime.now():%H:%M:%S}  {text}")
        self.lines = self.lines[-500:]

    def snapshot(self):
        return {"running": self.running, "done": self.done, "total": self.total, "lines": self.lines,
                "sent": self.sent, "failed": self.failed, "skipped": self.skipped, "current": self.current,
                "phase": self.phase, "next_at": self.next_at, "started": self.started, "finished": self.finished,
                "now": time.time(), "dry_run": self.dry_run, "channels": self.channels}


JOBS = {}
JOBS_LOCK = threading.Lock()


class Cleanup:
    """Runs cleanups in reverse order, ignoring their errors (a dead browser shouldn't mask the real error)."""

    def __init__(self):
        self.fns = []

    def enter(self, cm):
        value = cm.__enter__()
        self.fns.append(lambda: cm.__exit__(None, None, None))
        return value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for fn in reversed(self.fns):
            try:
                fn()
            except Exception:
                pass
        return False


def bridge_mod():
    return sys.modules.get("bridge")


def run_job(ws, job, cfg):
    try:
        _run_job(ws, job, cfg)
        job.phase = "stopped" if job.stop.is_set() else "done"
    except Exception as e:
        job.say(f"Error: {e}")
        job.phase, job.current = "error", str(e)
    finally:
        job.next_at, job.finished = None, time.time()
        job.running = False
        job.say("Finished.")
        if getattr(bridge_mod(), "notify", None) and not cfg.get("dry_run"):
            title = {"done": "Campaign finished", "stopped": "Campaign stopped", "error": "Campaign stopped with an error"}.get(job.phase, "Campaign finished")
            bridge_mod().notify(ws.uid, title, f"{job.done} of {job.total} sent." + (f" {job.current}" if job.phase == "error" else ""),
                                "#activity", "campaign")


def plan_job(ws, cfg):
    """Work list: (recipient, [channels still to do]); skips done ones, duplicates, and the daily cap."""
    cc = ws.settings()["country_code"]
    channels = ["whatsapp", "email"] if cfg["mode"] == "both" else [cfg["mode"]]
    rows = ws.load("recipients", [])
    if cfg["who"] == "selected":
        wanted = set(cfg["recipient_ids"])
        rows = [r for r in rows if r["id"] in wanted]
    todo, seen = [], set()
    for r in rows:
        chans = []
        for ch in channels:
            if ch == "whatsapp":
                key = phone_key(r, cc)
                if len(key) < 8 or ("wa", key) in seen:
                    continue
                if not cfg["resend"] and r.get("wa_status") in ("sent", "not_on_whatsapp"):
                    continue
                seen.add(("wa", key))
            else:
                key = r.get("email", "").strip().lower()
                if not EMAIL_RE.match(key) or ("em", key) in seen:
                    continue
                if r.get("email_status") in ("bounced", "invalid"):
                    continue  # address known to be undeliverable: never retried
                if not cfg["resend"] and r.get("email_status") == "sent":
                    continue
                if r.get("replied_at") and cfg["who"] != "selected":
                    continue  # they answered: follow up personally, not with a bulk send
                seen.add(("em", key))
            chans.append(ch)
        if chans:
            todo.append((r, chans))
    return channels, todo[: cfg["limit"]]


def _run_job(ws, job, cfg):
    cc = ws.settings()["country_code"]
    profile = ws.profile()
    channels, todo = plan_job(ws, cfg)
    if not todo:
        job.say("Nothing to send – everyone matching has been contacted already, or has no phone/email.")
        return
    if not cfg["dry_run"]:
        budget, capped, used = DAILY_LIMIT - ws.sent_today(), [], 0
        for r, chans in todo:
            if used + len(chans) > budget:
                break
            capped.append((r, chans))
            used += len(chans)
        if not capped:
            job.say(f"You've reached today's limit of {DAILY_LIMIT} messages. Try again tomorrow.")
            return
        todo = capped
    job.total = len(todo)
    job.dry_run, job.channels = cfg["dry_run"], channels
    min_delay = max(cfg["min_delay"], MIN_WA_DELAY) if "whatsapp" in channels else cfg["min_delay"]
    max_delay = max(min_delay, cfg["max_delay"])
    job.say(f"{len(todo)} people · {' + '.join(channels)} · {len(cfg['documents'])} attachment(s)"
            + (" · TEST RUN (nothing is sent)" if cfg["dry_run"] else ""))

    if cfg["dry_run"]:
        for r, chans in todo:
            job.say(f"— {r.get('name') or '(no name)'} · {r.get('company') or ''} → {', '.join(chans)}")
            if "email" in chans:
                job.say(f"   Subject: {render(cfg['subject'], r, profile)}")
            for line in render(cfg["body"], r, profile).splitlines():
                job.say(f"   {line}")
            job.done += 1
            job.sent += 1
        return

    with Cleanup() as cleanup:
        # Attachments are decrypted into a private temp folder only for the length of the run.
        tmp = Path(tempfile.mkdtemp(prefix="reachout-files-"))
        tmp.chmod(0o700)
        cleanup.fns.append(lambda: shutil.rmtree(tmp, ignore_errors=True))
        files = []
        for name in cfg["documents"]:
            data = ws.doc_bytes(name)
            if data is not None:
                (tmp / name).write_bytes(data)
                files.append(tmp / name)

        page = smtp = None
        if any("whatsapp" in c for _, c in todo):
            from playwright.sync_api import sync_playwright
            cleanup.enter(browser_slot(ws.uid, job.say))
            folder = cleanup.enter(wa_profile(ws))
            pw = cleanup.enter(sync_playwright())
            job.say("Opening WhatsApp…")
            ctx, page = open_whatsapp(pw, folder)
            cleanup.fns.append(ctx.close)
            try:
                page.wait_for_selector(LOGGED_IN, timeout=90_000)
            except Exception:
                ws.update_settings(wa_connected=False)
                raise RuntimeError("WhatsApp is logged out. Link it again on the WhatsApp page.")
            job.say("WhatsApp ready.")
        domain_cache = {}
        if any("email" in c for _, c in todo):
            smtp = smtp_connect(profile)
            cleanup.fns.append(lambda: smtp.quit())
            job.say(f"Connected to email as {profile['email']}.")

        for i, (r, chans) in enumerate(todo, 1):
            if job.stop.is_set():
                job.say("Stopped.")
                break
            if DELETED.get(ws.uid):
                break
            # Re-check the day's limit before every person: replies and scheduled emails sent while the
            # campaign runs count too.
            if ws.sent_today() + len(chans) > DAILY_LIMIT:
                job.say(f"Reached today's limit of {DAILY_LIMIT} messages. The rest can go tomorrow.")
                break
            who = r.get("name") or r.get("company") or r.get("phone") or r.get("email")
            job.phase, job.current, job.next_at = "sending", who, None
            text = render(cfg["body"], r, profile)
            for ch in chans:
                if ch == "whatsapp":
                    phone = phone_key(r, cc)
                    to = "+" + phone
                    try:
                        wa.send_one(page, phone, text, files)
                        status, detail = "sent", ""
                    except wa.NotOnWhatsApp:
                        status, detail = "not_on_whatsapp", ""
                    except Exception as e:
                        status, detail = "failed", str(e).splitlines()[0]
                else:
                    to = r["email"].strip()
                    token = secrets.token_urlsafe(18) if cfg.get("track_opens") else None
                    pixel = f"{cfg['public_url']}/o/{token}.gif" if token else None
                    msg = None
                    status, detail = "invalid", domain_problem(to.split("@")[-1], domain_cache)
                    if not detail:
                        try:
                            msg = build_email(profile, to, render(cfg["subject"], r, profile), text, files, pixel)
                            try:
                                smtp.send_message(msg)
                            except smtplib.SMTPServerDisconnected:
                                smtp = smtp_connect(profile)
                                smtp.send_message(msg)
                            status, detail = "sent", ""
                        except smtplib.SMTPRecipientsRefused as e:
                            code, why = next(iter(e.recipients.values()), (0, b""))
                            status = "bounced"
                            detail = f"Rejected by the mail server: {code} {why.decode(errors='replace')[:160]}"
                        except Exception as e:
                            status, detail = "failed", str(e).splitlines()[0]
                ws.set_status(r["id"], ch, status)
                ws.log(ch, to, r.get("name", ""), status, detail, rid=r["id"],
                       preview=render(cfg["subject"], r, profile) if ch == "email" else text,
                       track=lookup_hash("open:" + token) if ch == "email" and status == "sent" and token else None,
                       message_id=msg["Message-ID"] if ch == "email" and status == "sent" and msg else None)
                job.say(f"[{i}/{len(todo)}] {who} · {ch} → {status}" + (f" ({detail})" if detail else ""))
                if status == "sent":
                    job.sent += 1
                elif status in ("not_on_whatsapp", "invalid"):
                    job.skipped += 1
                else:
                    job.failed += 1
            job.done = i
            if i == len(todo) and any("email" in c for _, c in todo):
                BOUNCE_CHECK_SOON[ws.uid] = time.time() + 120  # look for bounce notices shortly after the run
            if i < len(todo) and not job.stop.is_set():
                wait = random.randint(min_delay, max_delay)
                job.say(f"Waiting {wait}s…")
                job.phase, job.current, job.next_at = "waiting", "", time.time() + wait
                job.stop.wait(wait)


# ---------------------------------------------------------------- pages

PREF_MAX_AGE = 365 * 86400
THEMES = ("system", "light", "dark")


def secure_cookies():
    return PRODUCTION or request.is_secure


def set_cookie(resp, name, value, max_age=PREF_MAX_AGE):
    # Every cookie is HttpOnly (scripts can't read it) and Secure whenever the site is served over HTTPS.
    resp.set_cookie(name, value, max_age=max_age, path="/", secure=secure_cookies(), httponly=True, samesite="Lax")


def read_source():
    """Where this visitor first came from (stored encrypted in an HttpOnly cookie)."""
    token = request.cookies.get("ro_src", "")
    return unseal(token.encode(), {}) if token else {}


def remember_source(resp):
    """On public pages: record the first external referrer and any utm_* tags, once per visitor."""
    utm = {k: v[:100] for k, v in request.args.items() if k.startswith("utm_") and k in (
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")}
    ref = request.referrer or ""
    external = ref and not ref.startswith(request.host_url)
    if (utm or external) and not request.cookies.get("ro_src"):
        set_cookie(resp, "ro_src", seal({"utm": utm, "referrer": ref[:300] if external else "",
                                         "landing": request.path}).decode(), max_age=90 * 86400)


def can_track_opens():
    """Recipients' mail apps must be able to reach the tracking image, so the app needs a public HTTPS address."""
    host = re.sub(r"^https?://", "", site_url()).split("/")[0].split(":")[0].lower()
    return site_url().startswith("https://") and host not in ("localhost", "127.0.0.1", "0.0.0.0") \
        and not host.endswith(".local") and not re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", host)


def site_url():
    return SITE_URL or request.host_url.rstrip("/")


def asset_version(match):
    path = WEB / "assets" / match.group(1)
    v = int(path.stat().st_mtime) if path.exists() else 0
    return f'/assets/{match.group(1)}?v={v:x}"'


def page(name, status=200, **values):
    if not (WEB / name).exists():
        # API-only deployment (the backend repo on its own; pages live on Netlify): no HTML pages here.
        if status == 404 or name == "404.html":
            return jsonify(error="Not found."), 404
        return jsonify(error="This server only provides the Reachout API."), 404
    doc = (WEB / name).read_text(encoding="utf-8").replace("{{SITE_URL}}", site_url())
    doc = doc.replace("{{OPERATOR}}", html.escape(OPERATOR_NAME)).replace("{{JURISDICTION}}", html.escape(JURISDICTION))
    # Preferences come from HttpOnly cookies and are handed to the page as attributes on <html>.
    theme = request.cookies.get("ro_theme")
    theme = theme if theme in THEMES else "system"
    flags = " data-popup=\"done\"" if request.cookies.get("ro_popup") else ""
    if request.cookies.get("ro_sidebar") == "collapsed":
        flags += " data-sidebar=\"collapsed\""
    closed = [g for g in (request.cookies.get("ro_nav") or "").split(".") if g in NAV_GROUPS]
    if closed:
        flags += f' data-nav-closed="{" ".join(closed)}"'
    doc = doc.replace('<html lang="en">', f'<html lang="en" data-theme-choice="{theme}"{flags}>', 1)
    # Versioned asset URLs, so browsers can cache them for a week yet pick up changes immediately.
    doc = re.sub(r'/assets/([\w.-]+\.(?:css|js))"', asset_version, doc)
    for key, value in values.items():
        doc = doc.replace("{{" + key + "}}", value)
    doc = add_nonce(doc)
    resp = make_response(doc, status)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"  # pages carry per-visitor preferences
    if name in ("landing.html", "contact.html"):
        remember_source(resp)
    return resp


NAV_GROUPS = ("outreach", "career", "website")  # sidebar sections that can be folded away


@app.get("/api/prefs")
def read_prefs():
    """Saved look (theme, sidebar, folded menu groups) for pages that aren't rendered by this server."""
    theme = request.cookies.get("ro_theme")
    return jsonify(theme=theme if theme in THEMES else "system",
                   sidebar="collapsed" if request.cookies.get("ro_sidebar") == "collapsed" else "open",
                   nav_closed=[x for x in (request.cookies.get("ro_nav") or "").split(".") if x in NAV_GROUPS])


@app.post("/api/prefs")
def save_prefs():
    p = body()
    resp = jsonify(ok=True)
    if "theme" in p:
        if p["theme"] not in THEMES:
            raise Invalid("Unknown theme.", "theme")
        set_cookie(resp, "ro_theme", p["theme"])
    if p.get("popup_seen"):
        set_cookie(resp, "ro_popup", "1")
    if "sidebar" in p:
        if p["sidebar"] not in ("open", "collapsed"):
            raise Invalid("Unknown sidebar state.", "sidebar")
        set_cookie(resp, "ro_sidebar", p["sidebar"])
    if "nav_closed" in p:
        groups = [g for g in (p["nav_closed"] or []) if g in NAV_GROUPS] if isinstance(p["nav_closed"], list) else []
        set_cookie(resp, "ro_nav", ".".join(groups))
    return resp


@app.get("/")
def landing():
    return page("landing.html")


@app.get("/contact")
def contact_page():
    return page("contact.html")


@app.get("/privacy")
def privacy_page():
    return page("privacy.html")


@app.get("/terms")
def terms_page():
    return page("terms.html")


@app.get("/robots.txt")
def robots():
    if LANDING_URL and LANDING_URL != SITE_URL:  # app site: the landing site has its own robots.txt
        text = (f"User-agent: *\nAllow: /signup\nAllow: /p/\nDisallow: /app\nDisallow: /api/\nDisallow: /login\n"
                f"Disallow: /site-preview/\n\nSitemap: {site_url()}/sitemap.xml\n")
        return text, 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=86400"}
    text = (f"User-agent: *\nAllow: /$\nAllow: /signup\nAllow: /contact\nAllow: /assets/\n"
            f"Allow: /p/\nDisallow: /app\nDisallow: /api/\n\nSitemap: {site_url()}/sitemap.xml\n")
    return text, 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=86400"}


@app.get("/sitemap.xml")
def sitemap():
    landing_file = WEB / "landing.html"
    day = (datetime.fromtimestamp(landing_file.stat().st_mtime) if landing_file.exists() else datetime.now()).date().isoformat()
    pages = (("/signup", "0.6"),) if LANDING_URL and LANDING_URL != SITE_URL else (("/", "1.0"), ("/contact", "0.7"), ("/signup", "0.6"), ("/privacy", "0.3"), ("/terms", "0.3"))
    urls = "".join(f"<url><loc>{site_url()}{path}</loc><lastmod>{day}</lastmod><priority>{pri}</priority></url>"
                   for path, pri in pages)
    urls += "".join(f"<url><loc>{html.escape(loc)}</loc><lastmod>{mod}</lastmod><priority>0.8</priority></url>" for loc, mod in feature_site.sitemap_entries())
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return xml, 200, {"Content-Type": "application/xml; charset=utf-8", "Cache-Control": "public, max-age=86400"}


@app.get("/site.webmanifest")
def manifest():
    return jsonify(name="Reachout", short_name="Reachout", start_url="/app", display="standalone",
                   background_color="#fbfaf7", theme_color="#0f6b54",
                   icons=[{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}])


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return page("404.html", 404)


# The workspace and sign-in screens are the React app in web/dist (built from frontend/ with `npm run build`).


@app.get("/login")
@app.get("/signup")
def auth_page():
    if current_uid():
        return redirect("/app")
    signup = request.path == "/signup"
    title, robots = ("Create a free account · Reachout", "index, follow") if signup else ("Log in · Reachout", "noindex, follow")
    return page("dist/index.html", TITLE=title, ROBOTS=robots)


@app.get("/app")
@app.get("/app/<path:subpath>")
def app_page(subpath=""):
    """The workspace. Pages have real paths (/app/campaign, /app/applications/<id>) handled by the React router."""
    if not current_uid():
        return redirect("/login")
    return page("dist/index.html", TITLE="Reachout", ROBOTS="noindex, nofollow")


@app.get("/dist/<path:name>")
def dist_assets(name):
    resp = send_from_directory(WEB / "dist", name)
    if name.startswith("static/"):  # hashed file names: safe to cache for a long time
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/assets/<path:name>")
def assets(name):
    return send_from_directory(WEB / "assets", name)


PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@app.get("/o/<token>.gif")
def open_pixel(token):
    """Record that a tracked email was opened (its images were loaded)."""
    if re.fullmatch(r"[\w-]{16,64}", token):
        h, now = lookup_hash("open:" + token), time.time()
        first = M.send_log.find_one_and_update({"track": h, "first_open": None}, {"$set": {"first_open": now}})
        M.send_log.update_one({"track": h}, {"$inc": {"opens": 1}, "$set": {"last_open": now}})
        if first and first.get("rid"):
            ws = Workspace(first["uid"])
            if ws.mark_opened(first["rid"], datetime.fromtimestamp(now).isoformat(timespec="seconds")):
                secs = int(now - first["ts"])
                ws.add_event(first["rid"], "opened", "Email opened" + (" (within a minute of sending, possibly an automatic security scan)" if secs < 60 else ""))
    resp = make_response(PIXEL)
    resp.headers.update({"Content-Type": "image/gif", "Cache-Control": "no-store, no-cache, must-revalidate, private",
                         "X-Robots-Tag": "noindex"})
    return resp


@app.post("/api/client-errors")
def client_error():
    """Crashes in the browser, reported to the server log (and Sentry when configured)."""
    if rate_limited(("client-error", client_ip()), 20, 3600):
        return jsonify(ok=True)
    p = request.get_json(silent=True) or {}
    msg = str(p.get("message") or "")[:300]
    where = str(p.get("where") or "")[:200]
    stack = str(p.get("stack") or "")[:2000]
    app.logger.warning("Browser error at %s: %s", where, msg)
    if SENTRY_DSN:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("source", "browser")
            scope.set_context("browser", {"where": where, "stack": stack})
            sentry_sdk.capture_message(f"Browser: {msg}", level="error")
    return jsonify(ok=True)


@app.get("/healthz")
def healthz():
    """For uptime monitors: the app is up AND can reach its database."""
    try:
        M.client.admin.command("ping")
    except Exception:
        return "database unreachable", 503
    return "ok"


# ---------------------------------------------------------------- data API

@app.get("/api/state")
@login_required
def state(ws):
    job = JOBS.get(ws.uid) or Job()
    return jsonify({
        "recipients": ws.load("recipients", []),
        "templates": ws.load("templates", []),
        "profile": public_profile(ws.profile()),
        "documents": ws.documents(),
        "settings": ws.settings(),
        "job": job.snapshot(),
    })


@app.post("/api/recipients")
@login_required
def add_recipient(ws):
    fields = clean_contact(body())
    cc = ws.settings()["country_code"]
    with ws.lock:
        rows = ws.load("recipients", [])
        if len(rows) >= MAX_CONTACTS:
            raise Invalid(f"You've reached the limit of {MAX_CONTACTS} contacts.")
        pk, ek = phone_key(fields, cc), fields.get("email", "")
        for r in rows:
            if pk and phone_key(r, cc) == pk:
                raise Invalid("A contact with this phone number already exists.", "phone")
            if ek and r.get("email", "").lower() == ek:
                raise Invalid("A contact with this email already exists.", "email")
        row = {"id": new_id(), **fields, "added_at": now_iso()}
        rows.append(row)
        ws.save("recipients", rows)
    return jsonify(row)


@app.put("/api/recipients/<rid>")
@login_required
def update_recipient(ws, rid):
    changes = clean_contact(body(), partial=True)
    with ws.lock:
        rows = ws.load("recipients", [])
        row = next((r for r in rows if r["id"] == rid), None)
        if not row:
            raise Invalid("That contact no longer exists.", status=404)
        merged = {**row, **changes}
        if not merged.get("phone") and not merged.get("email"):
            raise Invalid("A contact needs a phone number or an email address.", next(iter(changes), "phone"))
        old_stage, old_follow = row.get("stage", "new"), row.get("follow_up", "")
        row.update(changes)
        ws.save("recipients", rows)
    if "stage" in changes and changes["stage"] != old_stage:
        ws.add_event(rid, "stage", f"Stage changed from {STAGES[old_stage]} to {STAGES[changes['stage']]}")
    if "follow_up" in changes and changes["follow_up"] != old_follow:
        d = date.fromisoformat(changes["follow_up"]) if changes["follow_up"] else None
        ws.add_event(rid, "follow_up", f"Follow-up set for {d.day} {d.strftime('%b %Y')}" if d else "Follow-up cleared")
    return jsonify(row)


@app.get("/api/contacts/<rid>")
@login_required
def contact_detail(ws, rid):
    row = next((r for r in ws.load("recipients", []) if r["id"] == rid), None)
    if not row:
        raise Invalid("That contact no longer exists.", status=404)
    return jsonify(contact=row, events=ws.events(rid), messages=ws.contact_log(row))


@app.post("/api/contacts/<rid>/notes")
@login_required
def add_note(ws, rid):
    if not any(r["id"] == rid for r in ws.load("recipients", [])):
        raise Invalid("That contact no longer exists.", status=404)
    text = v_text(body().get("text"), "text", "Note", 2000, required=True)
    return jsonify(ws.add_event(rid, "note", text))


@app.delete("/api/contacts/<rid>/notes/<nid>")
@login_required
def delete_note(ws, rid, nid):
    with ws.lock:
        ws.save(f"contact:{rid}", [e for e in ws.events(rid) if not (e["id"] == nid and e["type"] == "note")])
    return jsonify(ok=True)


def id_list():
    ids = body().get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise Invalid("Select at least one contact.")
    return set(ids)


@app.post("/api/recipients/delete")
@login_required
def delete_recipients(ws):
    ids = id_list()
    with ws.lock:
        ws.save("recipients", [r for r in ws.load("recipients", []) if r["id"] not in ids])
    ws.drop_events(ids)
    return jsonify(ok=True)


@app.post("/api/recipients/reset")
@login_required
def reset_recipients(ws):
    ids = id_list()
    with ws.lock:
        rows = ws.load("recipients", [])
        for r in rows:
            if r["id"] in ids:
                for k in STATUS_FIELDS:
                    r[k] = ""
        ws.save("recipients", rows)
    return jsonify(ok=True)


@app.post("/api/recipients/import")
@login_required
def import_recipients(ws):
    f = request.files.get("file")
    if not f or not f.filename:
        raise Invalid("Choose a file to import.", "file")
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        raise Invalid("That file is larger than 5 MB. Split it into smaller files.", "file")
    incoming = read_table(f.filename, data)
    cc = ws.settings()["country_code"]
    added = duplicates = invalid = 0
    replace = request.form.get("replace") == "1"
    with ws.lock:
        old_ids = [r["id"] for r in ws.load("recipients", [])] if replace else []
        rows = [] if replace else ws.load("recipients", [])
        keys = {phone_key(r, cc) for r in rows} | {r.get("email", "").lower() for r in rows}
        keys.discard("")
        for r in incoming:
            try:
                r = clean_contact(r)
            except Invalid:
                invalid += 1
                continue
            pk, ek = phone_key(r, cc), r.get("email", "")
            if (pk and pk in keys) or (ek and ek in keys):
                duplicates += 1
                continue
            if len(rows) >= MAX_CONTACTS:
                invalid += 1
                continue
            keys |= {pk, ek} - {""}
            rows.append({"id": new_id(), **r, "added_at": now_iso()})
            added += 1
        ws.save("recipients", rows)
        if old_ids:  # replaced contacts: their notes/history go with them
            ws.drop_events(old_ids)
    return jsonify(added=added, duplicates=duplicates, invalid=invalid)


def xlsx_response(headers, rows, filename):
    from openpyxl import Workbook
    wb = Workbook()
    sheet = wb.active
    sheet.append(headers)
    for r in rows:
        sheet.append(r)
        for cell in sheet[sheet.max_row]:
            # Text is always stored as text, never as a formula, whatever it starts with (=, +, -, @, tab…),
            # so a visitor can't plant a live formula in your export through a contact form.
            if isinstance(cell.value, str):
                cell.data_type = "s"
                if cell.value[:1] in ("=", "+", "-", "@", "\t", "\r", "|"):
                    cell.value = "'" + cell.value
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/recipients/export")
@login_required
def export_recipients(ws):
    rows = ws.load("recipients", [])
    extra = sorted({k for r in rows for k in r} - set(CORE_FIELDS) - set(STATUS_FIELDS) - set(CRM_FIELDS) - {"id"})
    headers = CORE_FIELDS + extra + CRM_FIELDS + STATUS_FIELDS
    return xlsx_response(headers, [[r.get(h, "") for h in headers] for r in rows], "contacts.xlsx")


@app.get("/api/recipients/sample")
def sample_sheet():
    return xlsx_response(["name", "company", "phone", "email"],
                         [["Priya Sharma", "Acme Pvt Ltd", "9876543210", "priya@acme.com"],
                          ["", "Globex", "+91 98123 45678", ""]], "contacts_sample.xlsx")


@app.post("/api/templates")
@login_required
def save_template(ws):
    p = body()
    item = {"name": v_text(p.get("name"), "name", "Template name", 80, required=True),
            "subject": v_template_text(p.get("subject"), "subject", "Email subject", 200, False),
            "body": v_template_text(p.get("body"), "body", "Message", 5000, True)}
    with ws.lock:
        items = ws.load("templates", [])
        if any(t["name"].lower() == item["name"].lower() and t["id"] != p.get("id") for t in items):
            raise Invalid("You already have a template with this name.", "name")
        existing = next((t for t in items if p.get("id") and t["id"] == p["id"]), None)
        if existing:
            existing.update(item)
            item = existing
        else:
            if len(items) >= 50:
                raise Invalid("You can keep up to 50 templates. Delete one first.")
            item["id"] = new_id()
            items.append(item)
        ws.save("templates", items)
    return jsonify(item)


@app.delete("/api/templates/<tid>")
@login_required
def delete_template(ws, tid):
    with ws.lock:
        items = ws.load("templates", [])
        if len(items) <= 1:
            raise Invalid("Keep at least one template.")
        ws.save("templates", [t for t in items if t["id"] != tid])
    return jsonify(ok=True)


@app.put("/api/profile")
@login_required
def save_profile(ws):
    p = body()
    with ws.lock:
        profile = ws.profile()
        profile.update({
            "name": v_text(p.get("name"), "name", "Your name", 80, required=True, min_len=2),
            "phone": v_phone(p.get("phone")),
            "email": v_email(p.get("email"), label="Email"),
            "smtp_host": v_text(p.get("smtp_host"), "smtp_host", "Mail server", 120),
            "smtp_port": v_int(p.get("smtp_port") or 465, "smtp_port", "Port", 1, 65535),
            "smtp_user": v_text(p.get("smtp_user"), "smtp_user", "Login", 254),
        })
        if profile["smtp_host"] and not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", profile["smtp_host"]):
            raise Invalid("Enter a mail server like smtp.gmail.com.", "smtp_host")
        if profile["smtp_port"] not in MAIL_PORTS:
            raise Invalid("Use port 465 (SSL) or 587 (STARTTLS). Your provider's help page lists the right one.", "smtp_port")
        if profile["smtp_host"]:
            public_host(profile["smtp_host"])
        old_password = profile.get("smtp_password", "")
        password = re.sub(r"\s", "", str(p.get("smtp_password") or ""))
        if password:  # blank = keep the saved password
            if len(password) > 200:
                raise Invalid("That password is too long.", "smtp_password")
            profile["smtp_password"] = password
        if p.get("clear_password"):
            profile["smtp_password"] = ""
        first_password = bool(password) and not old_password
        ws.save("profile", profile)
    if first_password:  # mailbox just connected: read it once so every page has data
        from features import apps as feature_apps
        threading.Thread(target=feature_apps.sync_all, args=(ws.uid, False, "first"), daemon=True).start()
    return jsonify(public_profile(profile))


@app.post("/api/profile/test")
@login_required
def test_profile(ws):
    if rate_limited(("smtp-test", ws.uid), 10, 3600):
        raise Invalid("Too many test emails. Try again later.", status=429)
    profile = ws.profile()
    try:
        server = smtp_connect(profile)
        server.send_message(build_email(profile, profile["email"], "Reachout – test email",
                                        "Your email settings work. You can now send outreach emails.", []))
        server.quit()
    except smtplib.SMTPAuthenticationError:
        raise Invalid("The mail server rejected the login. Check the login and app password.", "smtp_password")
    except (OSError, smtplib.SMTPException) as e:
        # A friendly category only: echoing the raw error would leak what other servers/ports answer.
        kind = ("the server's security certificate isn't valid" if "CERTIFICATE" in str(e).upper()
                else "the connection timed out" if isinstance(e, TimeoutError)
                else "the server refused the connection" if isinstance(e, ConnectionRefusedError)
                else "the server didn't accept the connection")
        raise Invalid(f"Couldn't connect to the mail server: {kind}. Check the server address and port.", "smtp_host")
    except RuntimeError as e:
        raise Invalid(str(e), "smtp_password")
    return jsonify(ok=True)


@app.post("/api/documents")
@login_required
def upload_documents(ws):
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        raise Invalid("Choose at least one file.", "files")
    existing = {d["name"]: d["size"] for d in ws.documents()}
    prepared, total = [], sum(existing.values())
    for f in uploads:
        name = secure_filename(f.filename) or "document"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_DOCS:
            raise Invalid(f"“{f.filename}” isn't a supported file type. Use PDF, Word, Excel, PowerPoint, "
                          f"text or image files.", "files")
        data = f.read()
        if not data:
            raise Invalid(f"“{f.filename}” is empty.", "files")
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise Invalid(f"“{f.filename}” is larger than {MAX_FILE_MB} MB.", "files")
        total += len(data) - existing.get(name, 0)
        prepared.append((name, data))
    if total > MAX_FILES_MB * 1024 * 1024:
        raise Invalid(f"That would go over your {MAX_FILES_MB} MB storage. Delete some files first.", "files")
    if len(set(existing) | {n for n, _ in prepared}) > MAX_FILES:
        raise Invalid(f"You can keep up to {MAX_FILES} files. Delete some first.", "files")
    for name, data in prepared:
        ws.save_doc(name, data)
    return jsonify(ok=True, replaced=[n for n, _ in prepared if n in existing])


@app.get("/api/documents/<name>")
@login_required
def view_document(ws, name):
    data = ws.doc_bytes(name)
    if data is None:
        return jsonify(error="File not found."), 404
    return send_file(io.BytesIO(data), download_name=name, mimetype=guess_type(name)[0] or "application/octet-stream")


@app.delete("/api/documents/<name>")
@login_required
def delete_document(ws, name):
    ws.delete_doc(name)
    return jsonify(ok=True)


@app.post("/api/settings")
@login_required
def save_settings(ws):
    p = body()
    cc = re.sub(r"\D", "", str(p.get("country_code", "")))
    if not 1 <= len(cc) <= 3:
        raise Invalid("Country code must be 1–3 digits, like 91 for India or 1 for the US.", "country_code")
    ws.update_settings(country_code=cc)
    return jsonify(ws.settings())


@app.post("/api/preview")
@login_required
def preview(ws):
    p = body()
    rows = ws.load("recipients", [])
    r = next((x for x in rows if x["id"] == p.get("recipient_id")), rows[0] if rows else {})
    profile = ws.profile()
    return jsonify(subject=render(str(p.get("subject", ""))[:200], r, profile),
                   body=render(str(p.get("body", ""))[:5000], r, profile),
                   recipient=r.get("name") or r.get("company") or r.get("phone") or r.get("email") or "")


def validate_send(ws, p):
    cfg = {
        "mode": p.get("mode"), "who": p.get("who"),
        "subject": "", "body": v_template_text(p.get("body"), "body", "Message", 5000, True),
        "limit": v_int(p.get("limit"), "limit", "Max people", 1, 500),
        "min_delay": v_int(p.get("min_delay"), "min_delay", "Min gap", 0, 3600),
        "max_delay": v_int(p.get("max_delay"), "max_delay", "Max gap", 0, 3600),
        "resend": bool(p.get("resend")), "dry_run": bool(p.get("dry_run")),
        "track_opens": bool(p.get("track_opens")), "public_url": site_url(),
        "template_id": str(p.get("template_id") or ""), "recipient_ids": [], "documents": [],
    }
    if cfg["mode"] not in ("whatsapp", "email", "both"):
        raise Invalid("Choose WhatsApp, email or both.", "mode")
    if cfg["mode"] != "email" and not WHATSAPP_ENABLED:
        raise Invalid("WhatsApp sending isn't available here. Send by email instead.", "mode")
    if cfg["who"] not in ("all", "selected"):
        raise Invalid("Choose who to send to.", "who")
    if cfg["track_opens"] and cfg["mode"] != "whatsapp" and not can_track_opens():
        raise Invalid("Open tracking only works once the app is online at a public address (set SITE_URL).", "track_opens")
    if cfg["max_delay"] < cfg["min_delay"]:
        raise Invalid("Max gap must be the same as or more than the min gap.", "max_delay")
    if cfg["mode"] != "whatsapp":
        cfg["subject"] = v_template_text(p.get("subject"), "subject", "Email subject", 200, True)
    if cfg["who"] == "selected":
        ids = p.get("recipient_ids")
        if not isinstance(ids, list) or not ids:
            raise Invalid("Select contacts on the Contacts page first.", "who")
        cfg["recipient_ids"] = [str(i) for i in ids]
    docs = p.get("documents") or []
    names = {d["name"] for d in ws.documents()}
    if not isinstance(docs, list) or any(d not in names for d in docs):
        raise Invalid("One of the selected files no longer exists. Refresh the page.", "documents")
    cfg["documents"] = docs
    if not cfg["dry_run"]:
        if cfg["mode"] != "whatsapp" and not ws.profile().get("smtp_password"):
            raise Invalid("Set up email on the Profile page before sending email.", "mode")
        if cfg["mode"] != "email" and not ws.settings().get("wa_connected"):
            raise Invalid("Link your WhatsApp first.", "mode")
    if not plan_job(ws, cfg)[1]:
        raise Invalid("Nobody to send to: everyone matching has been contacted already, or has no phone/email.",
                      "who")
    return cfg


@app.post("/api/send")
@login_required
def start_send(ws):
    cfg = validate_send(ws, body())
    with JOBS_LOCK:  # check-and-start in one step, so two quick clicks can't start two campaigns
        if JOBS.get(ws.uid) and JOBS[ws.uid].running:
            raise Invalid("A send is already running.", status=409)
        job = JOBS[ws.uid] = Job()
        job.running = True
    ws.update_settings(**{k: cfg[k] for k in ("mode", "template_id", "documents", "min_delay", "max_delay", "limit")})
    threading.Thread(target=run_job, args=(ws, job, cfg), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/bounces/check")
@login_required
def bounces_check(ws):
    if rate_limited(("bounce-check", ws.uid), 12, 3600):
        raise Invalid("You've checked a lot recently. Try again in a few minutes.", status=429)
    return jsonify(check_bounces(ws))


@app.get("/api/job")
@login_required
def job_status(ws):
    return jsonify((JOBS.get(ws.uid) or Job()).snapshot())


@app.post("/api/job/stop")
@login_required
def stop_job(ws):
    job = JOBS.get(ws.uid)
    if job and job.running:
        job.stop.set()
        job.phase = "stopping"
        job.say("Stopping after the current message…")
    return jsonify(ok=True)


@app.get("/api/history")
@login_required
def history(ws):
    return jsonify(ws.history())


@app.get("/api/whatsapp")
@login_required
def wa_status(ws):
    if not WHATSAPP_ENABLED:
        return jsonify(state="disabled", error="", linked_at="")
    link = WA_LINKS.get(ws.uid)
    st = ws.settings()
    if link and link.state in ("starting", "qr", "saving"):
        state = link.state
    else:
        state = "connected" if st.get("wa_connected") else "disconnected"
    expires = wa_expires_at(ws.uid) if state == "connected" else None
    return jsonify(state=state, error=link.error if link and link.state == "error" else "",
                   linked_at=st.get("wa_linked_at", ""), hours=WA_SESSION_HOURS,
                   expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else "")


@app.before_request
def whatsapp_switch():
    if not WHATSAPP_ENABLED and request.path.startswith("/api/whatsapp/"):
        return jsonify(error="WhatsApp isn't available here."), 404


@app.post("/api/whatsapp/link")
@login_required
def wa_link(ws):
    current = WA_LINKS.get(ws.uid)
    if current and current.state in ("starting", "qr", "saving"):
        return jsonify(ok=True)
    if JOBS.get(ws.uid) and JOBS[ws.uid].running:
        raise Invalid("Wait for the current send to finish.", status=409)
    link = WA_LINKS[ws.uid] = WALink()
    threading.Thread(target=link_whatsapp, args=(ws, link), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/whatsapp/qr")
@login_required
def wa_qr(ws):
    link = WA_LINKS.get(ws.uid)
    if not link or not link.qr:
        return jsonify(error="No QR code yet."), 404
    return send_file(io.BytesIO(link.qr), mimetype="image/png")


@app.post("/api/whatsapp/cancel")
@login_required
def wa_cancel(ws):
    if ws.uid in WA_LINKS:
        WA_LINKS[ws.uid].cancel.set()
    return jsonify(ok=True)


@app.post("/api/whatsapp/unlink")
@login_required
def wa_unlink(ws):
    if ws.uid in BUSY:
        raise Invalid("WhatsApp is in use right now. Try again when it has finished.", status=409)
    row = M.wa_sessions.find_one_and_delete({"_id": ws.uid})
    if row:
        drop_file(row["file_id"])
    ws.update_settings(wa_connected=False, wa_linked_at="")
    WA_LINKS.pop(ws.uid, None)
    return jsonify(ok=True)


# ---------------------------------------------------------------- website enquiries (leads)

def is_admin(uid):
    user = find_user(uid=uid)
    if not user:
        return False
    if ADMIN_EMAILS:
        return user["email"] in ADMIN_EMAILS
    if PRODUCTION:
        return False
    first = M.users.find_one({}, {"_id": 1}, sort=[("created", ASCENDING)])
    return bool(first) and first["_id"] == uid


def admin_required(fn):
    @functools.wraps(fn)
    @login_required
    def wrapper(ws, *args, **kwargs):
        if not is_admin(ws.uid):
            return jsonify(error="Only the site owner can see this."), 403
        return fn(ws, *args, **kwargs)
    return wrapper


def lead_out(row):
    return {"id": row["_id"], "created": row["created"], "status": row["status"], **unseal(row["data"], {})}


def notify_admins(lead):
    to = sorted(ADMIN_EMAILS) or ([MAIL_FROM] if MAIL_FROM else [])
    if not to and not DEV_OTP:
        return
    text = (f"New enquiry on Reachout\n\nName: {lead['name']}\nEmail: {lead['email']}\n"
            f"Phone: {lead.get('phone') or '-'}\nCompany: {lead.get('company') or '-'}\nTopic: {lead['topic']}\n"
            f"From: {lead.get('source') or '-'}\n\n{lead['message']}\n\nOpen the Leads inbox in Reachout to reply.")
    for addr in to or ["(site owner)"]:
        try:
            send_system_email(addr, f"New enquiry: {lead['topic']} from {lead['name']}", text)
        except Exception as e:
            print(f"[Reachout] Couldn't email enquiry alert: {e}", flush=True)


@app.post("/api/leads")
def create_lead():
    p = body()
    # Bots fill the hidden "website" field or submit instantly; accept quietly and store nothing.
    if p.get("website") or safe_int(p.get("elapsed_ms")) < 2500:
        return jsonify(ok=True)
    if rate_limited(("lead-ip", client_ip()), 5, 3600):
        raise Invalid("You've sent several messages already. Please try again in an hour.", status=429)
    lead = {
        "name": v_text(p.get("name"), "name", "Your name", 80, required=True, min_len=2),
        "email": v_email(p.get("email")),
        "phone": v_phone(p.get("phone")),
        "company": v_text(p.get("company"), "company", "Company", 120),
        "topic": p.get("topic") if p.get("topic") in LEAD_TOPICS else "",
        "message": v_text(p.get("message"), "message", "Message", 2000, required=True, min_len=10),
    }
    if not lead["topic"]:
        raise Invalid("Choose what your message is about.", "topic")
    if not p.get("consent"):
        raise Invalid("Please agree to be contacted about your enquiry.", "consent")
    if rate_limited(("lead-email", lookup_hash(lead["email"])), 3, 3600):
        raise Invalid("We've already received your messages. We'll be in touch soon.", status=429)
    origin = read_source()
    page_path = (request.referrer or "").removeprefix(request.host_url.rstrip("/")) if (request.referrer or "").startswith(request.host_url) else ""
    lead.update({
        "source": p.get("source") if p.get("source") in ("popup", "contact_page") else "",
        "page": page_path.split("?")[0][:300],
        "referrer": origin.get("referrer", ""),
        "landing": origin.get("landing", ""),
        "utm": origin.get("utm", {}),
        "notes": [],
    })
    lid = uuid.uuid4().hex[:12]
    M.leads.insert_one({"_id": lid, "created": time.time(), "status": "new", "data": seal(lead)})
    threading.Thread(target=notify_admins, args=(lead,), daemon=True).start()
    resp = jsonify(ok=True)
    set_cookie(resp, "ro_popup", "1")  # don't pop the form open again for someone who has written to us
    return resp


def lead_scope(ws):
    """Whose enquiries a request is about: your own website's (default), or Reachout's own site (admins only)."""
    if request.args.get("scope") == "site":
        if not is_admin(ws.uid):
            raise Invalid("Only the site owner can see this.", status=403)
        return {"uid": {"$exists": False}}
    return {"uid": ws.uid}


@app.get("/api/leads")
@login_required
def list_leads(ws):
    rows = M.leads.find(lead_scope(ws)).sort("created", DESCENDING).limit(5000)
    return jsonify(leads=[lead_out(r) for r in rows], statuses=LEAD_STATUSES, is_admin=is_admin(ws.uid),
                   site_new=M.leads.count_documents({"uid": {"$exists": False}, "status": "new"}) if is_admin(ws.uid) else 0)


@app.put("/api/leads/<lid>")
@login_required
def update_lead(ws, lid):
    p = body()
    row = M.leads.find_one({"_id": lid, **lead_scope(ws)})
    if not row:
        raise Invalid("That enquiry no longer exists.", status=404)
    lead = lead_out(row)
    status = p.get("status", lead["status"])
    if status not in LEAD_STATUSES:
        raise Invalid("Choose a valid status.", "status")
    data = {k: v for k, v in lead.items() if k not in ("id", "created", "status")}
    who = find_user(uid=ws.uid)["name"]
    now = datetime.now().isoformat(timespec="seconds")
    if status != lead["status"]:
        data["notes"].insert(0, {"id": new_id(), "type": "status", "by": who, "at": now,
                                 "text": f"Status changed from {LEAD_STATUSES[lead['status']]} to {LEAD_STATUSES[status]}"})
    if p.get("note") is not None:
        data["notes"].insert(0, {"id": new_id(), "type": "note", "by": who, "at": now,
                                 "text": v_text(p.get("note"), "note", "Note", 2000, required=True)})
    M.leads.update_one({"_id": lid}, {"$set": {"status": status, "data": seal(data)}})
    return jsonify({"id": lid, "created": lead["created"], "status": status, **data})


@app.delete("/api/leads/<lid>")
@login_required
def delete_lead(ws, lid):
    M.leads.delete_one({"_id": lid, **lead_scope(ws)})
    return jsonify(ok=True)


@app.get("/api/leads/export")
@login_required
def export_leads(ws):
    rows = M.leads.find(lead_scope(ws)).sort("created", DESCENDING)
    headers = ["received", "status", "name", "email", "phone", "company", "topic", "message", "source", "page",
               "referrer", "utm_source", "utm_medium", "utm_campaign"]
    out = []
    for r in rows:
        d = unseal(r["data"], {})
        out.append([datetime.fromtimestamp(r["created"]).isoformat(timespec="minutes"), LEAD_STATUSES.get(r["status"], ""),
                    *[d.get(k, "") for k in headers[2:11]], *[d.get("utm", {}).get(k, "") for k in headers[11:]]])
    return xlsx_response(headers, out, "enquiries.xlsx")


# ---------------------------------------------------------------- migrations + CLI

def migrate_sqlite():
    """Copy the previous SQLite store into MongoDB. Encrypted values are copied as-is (same key)."""
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts = {}
    try:
        users = src.execute("SELECT * FROM users").fetchall() if "users" in tables else []
        for u in users:
            M.users.insert_one({"_id": u["id"], "email_hash": u["email_hash"], "data": u["data"], "created": u["created"],
                                "last_login": u["last_login"]})
        counts["accounts"] = len(users)
        if "kv" in tables:
            rows = src.execute("SELECT * FROM kv").fetchall()
            if rows:
                M.kv.insert_many([{"_id": f"{r['uid']}:{r['key']}", "uid": r["uid"], "key": r["key"], "data": r["data"]}
                                  for r in rows])
        if "send_log" in tables:
            cols = {r[1] for r in src.execute("PRAGMA table_info(send_log)")}
            rows = src.execute("SELECT * FROM send_log ORDER BY id").fetchall()
            if rows:
                def when(r):
                    stamp = (unseal(r["data"], {}) or {}).get("timestamp") or r["day"]
                    try:
                        return datetime.fromisoformat(stamp).timestamp()
                    except ValueError:
                        return 0
                M.send_log.insert_many([{"uid": r["uid"], "rid": r["rid"] if "rid" in cols else None, "day": r["day"],
                                         "ts": when(r), "data": r["data"]} for r in rows])
            counts["messages"] = len(rows)
        if "documents" in tables:
            rows = src.execute("SELECT * FROM documents").fetchall()
            for r in rows:
                data = fernet.decrypt(r["data"])
                M.documents.insert_one({"uid": r["uid"], "name_hash": r["name_hash"], "size": r["size"], "meta": r["meta"],
                                        "file_id": put_file(data, r["uid"], "document"), "created": r["created"]})
            counts["files"] = len(rows)
        if "wa_sessions" in tables:
            for r in src.execute("SELECT * FROM wa_sessions").fetchall():
                store_wa_blob(r["uid"], fernet.decrypt(r["data"]))
        if "leads" in tables:
            rows = src.execute("SELECT * FROM leads").fetchall()
            if rows:
                M.leads.insert_many([{"_id": r["id"], "created": r["created"], "status": r["status"], "data": r["data"]}
                                     for r in rows])
            counts["enquiries"] = len(rows)
    except Exception:
        # Leave MongoDB empty so the copy runs again cleanly next start; the SQLite file is untouched.
        for name in ("users", "kv", "send_log", "documents", "wa_sessions", "leads", "files.files", "files.chunks"):
            M.db[name].delete_many({})
        raise
    finally:
        src.close()
    backup = SQLITE_PATH.with_name("app.db.migrated-to-mongodb")
    SQLITE_PATH.rename(backup)
    for extra in ("-wal", "-shm"):
        side = SQLITE_PATH.with_name("app.db" + extra)
        if side.exists():
            side.rename(backup.with_name(backup.name + extra))
    print("Copied into MongoDB: " + ", ".join(f"{v} {k}" for k, v in counts.items())
          + f". The old file was kept as {backup.name}.", flush=True)



init_storage()

# Feature modules (GitHub workspace, job matches + hiring posts + send queue, LinkedIn, applications tracker,
# notifications) get this module passed in, so they share the same database handles and helpers instead of
# importing app.py again.
import bridge  # noqa: E402
from features import apps as feature_apps  # noqa: E402
from features import inbox as feature_inbox  # noqa: E402
from features import github as feature_github  # noqa: E402
from features import jobs as feature_jobs  # noqa: E402
from features import linkedin as feature_linkedin  # noqa: E402
from features import notify as feature_notify  # noqa: E402
from features import finder as feature_finder  # noqa: E402
from features import ai_jobs as feature_ai_jobs  # noqa: E402
from features import portfolio as feature_portfolio  # noqa: E402
from features import site as feature_site  # noqa: E402
from features import replies as feature_replies  # noqa: E402

bridge.setup(sys.modules[__name__])
feature_notify.init()
feature_inbox.ensure_indexes()
feature_replies.init()
for _bp in (feature_github.bp, feature_jobs.bp, feature_linkedin.bp, feature_apps.bp, feature_notify.bp, feature_inbox.bp, feature_replies.bp, feature_portfolio.bp, feature_site.bp, feature_finder.bp, feature_ai_jobs.bp):
    app.register_blueprint(_bp)

if os.environ.get("BOUNCE_WATCH", "1") == "1":
    threading.Thread(target=bounce_watcher, daemon=True, name="bounce-watcher").start()
    threading.Thread(target=wa_expiry_watcher, daemon=True, name="wa-expiry").start()
    feature_jobs.start_workers()
    feature_apps.start_workers()
    feature_replies.start_workers()

if __name__ == "__main__":
    if sys.argv[1:] == ["move-files-to-cloudinary"]:
        move_files_to_cloudinary()
        sys.exit(0)
    port = env_int("PORT", 5050)
    if DEV_OTP:
        print("Email isn't configured (SMTP_HOST), so login codes will be printed here.", flush=True)
    if os.environ.get("OPEN_BROWSER", "1") == "1":
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, threaded=True)
