"""Test setup: a throwaway MongoDB database and a temporary data folder, created per test session.

Needs MongoDB on 127.0.0.1:27017 (or MONGODB_URI). Never touches the real "reachout" database: the name
is random and dropped afterwards, and the test refuses to run against a database called "reachout".
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

DB_NAME = f"reachout_test_{uuid.uuid4().hex[:8]}"
os.environ.update({
    "MONGODB_DB": DB_NAME,
    "DATA_DIR": tempfile.mkdtemp(prefix="reachout-test-"),
    "SECRET_KEY": "test-secret-key-for-the-test-suite-only-0123456789",
    "BOUNCE_WATCH": "0",
    "NO_WORKERS": "1",
    "OPEN_BROWSER": "0",
    "HOST": "127.0.0.1",
})
for k in ("SMTP_HOST", "APP_ENV", "COOKIE_SECURE", "NETLIFY_PROXY_SECRET", "SITE_URL", "LANDING_URL", "SENTRY_DSN", "WHATSAPP_ENABLED"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
assert DB_NAME != "reachout"

import app as A  # noqa: E402  (configured by the environment above)

H = {"X-Requested-With": "fetch"}
# The HTML pages live in the web/ folder (or on Netlify). Backend-only checkouts skip page tests.
needs_pages = pytest.mark.skipif(not (A.WEB / "privacy.html").exists(), reason="web/ pages not present (API-only repo)")


@pytest.fixture(scope="session", autouse=True)
def _database():
    yield
    A.M.client.drop_database(DB_NAME)


def make_user(email=None, name="Test User"):
    email = email or f"user-{uuid.uuid4().hex[:6]}@example.com"
    uid = A.new_user(email, name)
    return uid, email


def client_for(uid):
    c = A.app.test_client()
    with A.app.test_request_context():
        A.start_session(uid)
        from flask import session
        sid = session["sid"]
    with c.session_transaction() as s:
        s["sid"] = sid
    return c


@pytest.fixture
def user():
    uid, email = make_user()
    return {"uid": uid, "email": email, "client": client_for(uid), "ws": A.Workspace(uid)}


@pytest.fixture
def anon():
    return A.app.test_client()
