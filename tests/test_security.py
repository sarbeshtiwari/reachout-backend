"""Regression tests for the security audit: each test replays an attack or abuse case."""

import io
import json
import re
import time
import zipfile
from email.message import EmailMessage

import pytest
from conftest import A, H, needs_pages, client_for, make_user
from features import apps as FA
from features import jobs as FJ
from features import notify as FN
from features import portfolio as FP


# ---------------------------------------------------------------- injected links (XSS)

def test_job_alert_drops_javascript_and_lookalike_links():
    m = EmailMessage()
    m["From"], m["Subject"] = "jobs@naukri.com", "New jobs"
    m.add_alternative('<a href="javascript:fetch(1)//naukri.com/job-listings-x">Senior SDE</a>'
                      '<a href="https://evil.com/naukri.com/job-listings-y">Fake</a>'
                      '<a href="https://www.naukri.com/job-listings-real-123">Real Job</a>', subtype="html")
    jobs = FJ.parse_alert(m, "Naukri", FJ.SOURCES["naukri.com"][1])
    assert [j["url"] for j in jobs] == ["https://www.naukri.com/job-listings-real-123"]


def test_stored_javascript_job_link_is_never_returned(user):
    user["ws"].save("jobs", {"x": {"id": "x", "title": "Old", "url": "javascript:alert(1)", "state": "new",
                                   "source": "Naukri", "company": "", "location": "", "received": "2026-09-01"}})
    assert user["client"].get("/api/jobs").get_json()["jobs"][0]["url"] == ""


def test_portfolio_public_items_strip_unsafe_links(user):
    ws = user["ws"]
    c = FP.cfg(ws)
    c["items"] = [{"repo": "a/b", "title": "T", "live_url": "javascript:alert(1)", "repo_url": "https://github.com/a/b",
                   "image": "data:text/html,x"}]
    ws.save("portfolio", c)
    it = FP.public_items(FP.cfg(ws))[0]
    assert it["live_url"] == "" and it["image"] == "" and it["repo_url"].startswith("https://")


# ---------------------------------------------------------------- denial of service

def test_notification_stream_refused_when_slots_are_full(user):
    FN.STREAMS.clear()
    FN.STREAMS.update({"other1": 2, "other2": 2})
    try:
        body = user["client"].get("/api/notifications/stream").get_data(as_text=True)
        assert "retry: 30000" in body
    finally:
        FN.STREAMS.clear()


@pytest.mark.parametrize("bad", ["<!--" * 75000, "<blockquote" * 60000, "<" * 300000])
def test_hostile_html_is_parsed_quickly(bad):
    from features import replies as FR
    t = time.time()
    FA.html_to_text(bad)
    FR.html_reply_text(bad)
    assert time.time() - t < 2


def test_zip_bomb_spreadsheet_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", "0" * (80 * 1024 * 1024))
    with pytest.raises(A.Invalid, match="too large"):
        A.read_table("bomb.xlsx", buf.getvalue())


def test_oversized_document_fails_clearly(user):
    with pytest.raises(A.Invalid) as e:
        user["ws"].save("big", ["x" * 1000] * 13000)
    assert e.value.status == 413


# ---------------------------------------------------------------- server-side requests (SSRF)

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1.nip.io", "169.254.169.254.nip.io", "10.0.0.5.nip.io"])
def test_private_mail_hosts_refused(host, monkeypatch):
    import socket
    ips = {"localhost": "127.0.0.1", "127.0.0.1.nip.io": "127.0.0.1", "169.254.169.254.nip.io": "169.254.169.254", "10.0.0.5.nip.io": "10.0.0.5"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", (ips[h], 0))])
    with pytest.raises(A.Invalid):
        A.public_host(host)


def test_public_mail_host_allowed(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("142.250.4.108", 0))])
    assert A.public_host("smtp.gmail.com") == "smtp.gmail.com"


def test_unusual_mail_port_refused(user):
    r = user["client"].put("/api/profile", json={"name": "Test User", "email": user["email"], "smtp_host": "smtp.gmail.com",
                                                 "smtp_port": 27017}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "smtp_port"


@pytest.mark.parametrize("endpoint,ok", [
    ("https://127.0.0.1/x", False), ("https://fcm.googleapis.com.evil.com/x", False),
    ("http://fcm.googleapis.com/x", False), ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True)])
def test_push_endpoints_limited_to_real_push_services(endpoint, ok):
    assert FN.push_host_ok(endpoint) is ok


def test_github_dot_names_refused():
    from features import github as G
    with pytest.raises(A.Invalid):
        G.repo_path("..", "..")


# ---------------------------------------------------------------- data integrity

def test_excel_export_never_contains_formulas():
    from openpyxl import load_workbook
    rows = [["=1+HYPERLINK(\"http://x\")", "+cmd", "@SUM(1)", "-2+3", "normal"]]
    with A.app.test_request_context():
        resp = A.xlsx_response(list("abcde"), rows, "t.xlsx")
        resp.direct_passthrough = False
        data = resp.get_data()
    cells = load_workbook(io.BytesIO(data)).active[2]
    assert all(c.data_type == "s" for c in cells)
    assert cells[0].value.startswith("'=") and cells[4].value == "normal"


def test_mailbox_scan_merge_keeps_your_edits():
    cur = {"a1": {"id": "a1", "company": "X", "history": [{"at": "2026-09-01T10:00:00", "status": "applied"}],
                  "hidden": True, "manual_status": "offer"}}
    scanned = {"a1": {"id": "a1", "company": "X", "role": "SDE", "hidden": False,
                      "history": [{"at": "2026-09-01T10:00:00", "status": "applied"},
                                  {"at": "2026-09-05T10:00:00", "status": "interview", "mid": "m1"}]},
               "a2": {"id": "a2", "company": "Y", "history": [{"at": "2026-09-02T10:00:00", "status": "applied"}]},
               "gone": {"id": "gone", "company": "Z", "history": [{"at": "2026-09-02T10:00:00", "status": "applied"}]}}
    merged = FA.merge_scanned(cur, scanned, {"a1", "gone"})
    a1 = merged["a1"]
    assert a1["hidden"] and a1["status"] == "offer" and len(a1["history"]) == 2 and a1["role"] == "SDE"
    assert "a2" in merged and "gone" not in merged


def test_account_deletion_cancels_queue_and_blocks_late_writes():
    uid, _ = make_user()
    A.M.queue.insert_one({"_id": "q-" + uid, "uid": uid, "kind": "email", "status": "queued", "due": 0, "data": A.seal({})})
    A.delete_user_data(uid)
    A.Workspace(uid).save("recipients", [{"x": 1}])  # a background job finishing after the deletion
    assert A.M.queue.count_documents({"uid": uid}) == 0
    assert A.M.kv.count_documents({"uid": uid}) == 0


def test_replies_count_toward_daily_limit(user, monkeypatch):
    monkeypatch.setattr(A.Workspace, "sent_today", lambda self: A.DAILY_LIMIT)
    A.M.replies.insert_one({"_id": "r-" + user["uid"], "uid": user["uid"], "key": "k1",
                            "data": A.seal({"from": "hr@x.com", "subject": "Hi", "mid": "<a@b>", "text": "hello"})})
    r = user["client"].post(f"/api/replies/r-{user['uid']}/send",
                            json={"to": "hr@x.com", "subject": "Re: Hi", "body": "Thanks for reaching out"}, headers=H)
    assert r.status_code in (404, 429)
    if r.status_code == 429:
        assert "limit" in r.get_json()["error"]


# ---------------------------------------------------------------- sign-in

def test_unknown_email_gets_same_cooldown(anon):
    email = "nobody-here@example.com"
    anon.post("/api/auth/request-code", json={"email": email, "purpose": "login"}, headers=H)
    r = anon.post("/api/auth/request-code", json={"email": email, "purpose": "login"}, headers=H)
    assert r.status_code == 429


def test_email_normalisation():
    assert A.v_email("ｔｅｓｔ@example.com") == "test@example.com"
    assert A.v_email("someone@gmаil.com").endswith("@xn--gmil-63d.com")  # Cyrillic "а" → punycode, not gmail.com


def test_logout_everywhere_ends_all_sessions():
    uid, _ = make_user()
    a, b = client_for(uid), client_for(uid)
    assert b.post("/api/auth/logout-everywhere", json={}, headers=H).status_code == 200
    assert a.get("/api/me").status_code == 401


def test_state_changing_calls_need_the_fetch_header(user):
    r = user["client"].post("/api/auth/logout-everywhere", json={})
    assert r.status_code == 400


def test_bad_numbers_dont_crash(anon):
    r = anon.post("/api/leads", json={"name": "a", "elapsed_ms": "abc"}, headers=H)
    assert r.status_code < 500


def test_unknown_api_is_json_404(anon):
    r = anon.get("/api/nope")
    assert r.status_code == 404 and r.is_json


# ---------------------------------------------------------------- headers + public sites

@needs_pages
def test_app_pages_carry_csp(anon):
    csp = anon.get("/login").headers.get("Content-Security-Policy", "")
    assert "script-src" in csp and "object-src 'none'" in csp


def test_reserved_and_taken_site_addresses(user):
    assert user["client"].put("/api/portfolio/settings", json={"slug": "api"}, headers=H).status_code == 400
    slug = "site-" + user["uid"][:6]
    assert user["client"].put("/api/portfolio/settings", json={"slug": slug}, headers=H).status_code == 200
    assert FP.claim("someone-else", slug) is False and FP.claim(user["uid"], slug) is True


def test_published_site_csp_jsonld_and_304(user, anon):
    c = user["client"]
    slug = "pub-" + user["uid"][:6]
    c.put("/api/portfolio/settings", json={"slug": slug}, headers=H)
    d = c.get("/api/site").get_json()["draft"]
    d["profile"]["name"] = 'X<!--<script>alert(1)</script>'
    assert c.put("/api/site", json={"draft": d}, headers=H).status_code == 200
    assert c.post("/api/site/publish", json={}, headers=H).status_code == 200
    r = anon.get(f"/p/{slug}", headers={"User-Agent": "Mozilla/5.0"})
    html = r.get_data(as_text=True)
    nonce = re.search(r"nonce-([a-f0-9]+)", r.headers["Content-Security-Policy"]).group(1)
    assert f'nonce="{nonce}"' in html and "onerror=" not in html and "<script>alert(1)" not in html
    ld = re.search(r'ld\+json">(.*?)</script>', html, re.S).group(1)
    assert "<" not in ld and json.loads(ld)["@graph"][1]["name"].startswith("X<!--")
    assert anon.get(f"/p/{slug}", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304


def test_rate_limits_are_persistent():
    assert A.rate_limited(("test-key",), 1, 60) is False
    assert A.rate_limited(("test-key",), 1, 60) is True
    assert A.M.ratelimits.count_documents({}) > 0
