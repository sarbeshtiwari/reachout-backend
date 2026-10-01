"""Core flows: sign-in, contacts, website builder, leads, SEO, deployment switches."""

import base64
import hmac
import json
import time

import pytest
from conftest import A, H, needs_pages, client_for, make_user


def test_health(anon):
    assert anon.get("/healthz").get_data(as_text=True) == "ok"


def test_signup_and_login_with_code(anon, monkeypatch):
    sent = {}
    monkeypatch.setattr(A, "send_system_email", lambda to, subject, text: sent.update(to=to, text=text))
    email = "new-person@example.com"
    r = anon.post("/api/auth/request-code", json={"email": email, "intent": "signup", "name": "New Person", "agree": True}, headers=H)
    assert r.status_code == 200
    code = next(w for w in sent["text"].split() if w.rstrip(".").isdigit() and len(w.rstrip(".")) == 6).rstrip(".")
    r = anon.post("/api/auth/verify-code", json={"email": email, "code": code}, headers=H)
    assert r.status_code == 200
    me = anon.get("/api/me").get_json()
    assert me["email"] == email and me["name"] == "New Person"
    row = A.M.users.find_one({"email_hash": A.lookup_hash(email)})
    assert row["terms_accepted"]["version"] == A.TERMS_VERSION


def test_signup_requires_agreement(anon):
    r = anon.post("/api/auth/request-code", json={"email": "x@example.com", "intent": "signup", "name": "X Y"}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "agree"


def test_wrong_code_is_rejected(anon, monkeypatch):
    monkeypatch.setattr(A, "send_system_email", lambda *a: None)
    uid, email = make_user()
    anon.post("/api/auth/request-code", json={"email": email}, headers=H)
    r = anon.post("/api/auth/verify-code", json={"email": email, "code": "000000"}, headers=H)
    assert r.status_code == 400


def test_contacts_are_private_per_user(user):
    c = user["client"]
    r = c.post("/api/recipients", json={"name": "Riya", "company": "Northwind", "email": "riya@northwind.example"}, headers=H)
    assert r.status_code == 200
    other_uid, _ = make_user()
    other = client_for(other_uid)
    assert other.get("/api/state").get_json()["recipients"] == []
    assert len(c.get("/api/state").get_json()["recipients"]) == 1


def test_contact_import_csv(user):
    data = b"name,email,company\nAsha,asha@corp.example,Corp\nDup,asha@corp.example,Corp\n"
    r = user["client"].post("/api/recipients/import", data={"file": (__import__("io").BytesIO(data), "c.csv")},
                            headers=H, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["added"] == 1 and r.get_json()["duplicates"] == 1


def test_website_publish_contact_and_leads(user, anon):
    c = user["client"]
    slug = "w-" + user["uid"][:6]
    c.put("/api/portfolio/settings", json={"slug": slug}, headers=H)
    c.get("/api/site")
    assert c.post("/api/site/publish", json={}, headers=H).get_json()["online"]
    html = anon.get(f"/p/{slug}").get_data(as_text=True)
    assert "Powered by <b>Reachout</b>" in html and 'name="subject"' in html
    r = anon.post(f"/api/site/public/{slug}/contact", json={"name": "Asha HR", "email": "asha@corp.example", "subject": "SDE role",
                                                           "message": "Hi, we have an opening for you.", "elapsed_ms": 5000}, headers=H)
    assert r.status_code == 200
    leads = c.get("/api/leads").get_json()["leads"]
    assert leads[0]["name"] == "Asha HR" and leads[0]["source"] == "website"
    assert c.post("/api/site/unpublish", json={}, headers=H).status_code == 200
    assert anon.get(f"/p/{slug}").status_code == 404


def test_bots_on_contact_form_store_nothing(user, anon):
    c = user["client"]
    slug = "b-" + user["uid"][:6]
    c.put("/api/portfolio/settings", json={"slug": slug}, headers=H)
    c.get("/api/site")
    c.post("/api/site/publish", json={}, headers=H)
    before = A.M.leads.count_documents({"uid": user["uid"]})
    anon.post(f"/api/site/public/{slug}/contact", json={"name": "Bot", "email": "b@x.example", "message": "x" * 20, "elapsed_ms": 50}, headers=H)
    assert A.M.leads.count_documents({"uid": user["uid"]}) == before


def test_site_seo_tags(user, anon):
    c = user["client"]
    slug = "s-" + user["uid"][:6]
    c.put("/api/portfolio/settings", json={"slug": slug}, headers=H)
    c.get("/api/site")
    c.post("/api/site/publish", json={}, headers=H)
    html = anon.get(f"/p/{slug}").get_data(as_text=True)
    assert html.count("<h1") == 1 and 'rel="canonical"' in html and "og:title" in html and "application/ld+json" in html


@needs_pages
def test_legal_pages(anon):
    for path in ("/privacy", "/terms"):
        r = anon.get(path)
        assert r.status_code == 200 and "{{" not in r.get_data(as_text=True)


def test_whatsapp_switch(user, monkeypatch):
    monkeypatch.setattr(A, "WHATSAPP_ENABLED", False)
    c = user["client"]
    assert c.get("/api/me").get_json()["whatsapp"] is False
    assert c.get("/api/whatsapp").get_json()["state"] == "disabled"
    assert c.post("/api/whatsapp/link", json={}, headers=H).status_code == 404
    r = c.post("/api/send", json={"mode": "whatsapp", "who": "all", "body": "Hi {name}", "limit": 5, "min_delay": 0, "max_delay": 0}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "mode"


# ---------------------------------------------------------------- Netlify signed proxy

def _sign(secret, exp_in=60, alg="HS256"):
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    h = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    c = b64(json.dumps({"iss": "netlify", "exp": int(time.time()) + exp_in}).encode())
    return f"{h}.{c}.{b64(hmac.new(secret.encode(), f'{h}.{c}'.encode(), 'sha256').digest())}"


@pytest.mark.parametrize("token,ok", [
    (None, False), (_sign("wrong"), False), (_sign("s3cret", exp_in=-600), False),
    (_sign("s3cret", alg="none"), False), (_sign("s3cret"), True)])
def test_only_signed_netlify_requests_get_through(anon, monkeypatch, token, ok):
    monkeypatch.setattr(A, "NETLIFY_PROXY_SECRET", "s3cret")
    headers = {"X-Nf-Sign": token} if token else {}
    assert (anon.get("/api/prefs", headers=headers).status_code == 200) is ok
    assert anon.get("/healthz").status_code == 200  # health checks always work


def test_client_ip_from_signed_netlify_request(monkeypatch):
    monkeypatch.setattr(A, "NETLIFY_PROXY_SECRET", "s3cret")
    with A.app.test_request_context(headers={"X-Nf-Sign": _sign("s3cret"), "X-Nf-Client-Connection-Ip": "203.0.113.7"}):
        assert A.client_ip() == "203.0.113.7"
    with A.app.test_request_context(headers={"X-Nf-Client-Connection-Ip": "203.0.113.7"}):
        assert A.client_ip() != "203.0.113.7"  # unsigned: header ignored


def test_system_email_failure_is_a_clean_error(anon, monkeypatch):
    def boom(*a, **k):
        raise OSError(101, "Network is unreachable")
    monkeypatch.setattr(A, "DEV_OTP", False)
    monkeypatch.setattr(A, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(A, "_send_via_smtp", boom)
    uid, email = make_user()
    r = anon.post("/api/auth/request-code", json={"email": email}, headers=H)
    assert r.status_code == 503 and "couldn't send" in r.get_json()["error"]



def test_new_contacts_record_when_they_were_added(user):
    from datetime import date
    r = user["client"].post("/api/recipients", json={"name": "Dated", "email": "dated@example.com"}, headers=H)
    assert r.status_code == 200 and r.get_json()["added_at"].startswith(date.today().isoformat())
    r = user["client"].put(f"/api/recipients/{r.get_json()['id']}", json={"name": "Dated Again", "added_at": "2000-01-01"}, headers=H)
    row = next(x for x in user["ws"].load("recipients", []) if x["email"] == "dated@example.com")
    assert row["added_at"].startswith(date.today().isoformat())  # can't be overwritten by an edit
