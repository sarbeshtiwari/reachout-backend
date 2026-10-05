"""Reachout Autofill (Chrome extension) API.

The extension fills job application forms in the user's own browser, with their details, resume and approved
answers. The user reviews the form and presses Submit themselves: the extension never submits anything.

It authenticates with a personal extension key (created and revoked in Reachout), never with the login cookie.
The key only reaches these endpoints: read the user's application details, download their resume, match answers
to a form's questions, and save answers they typed for their review.
"""

import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

import bridge
from bridge import login_required

bp = Blueprint("extension", __name__)


def C():
    return bridge.C


def AI():
    from features import ai_jobs
    return ai_jobs


def key_hash(secret):
    return C().lookup_hash("ext-key:" + secret)


def ext_workspace():
    """The workspace for the key in `Authorization: Bearer <uid>.<secret>`, or a 401 error."""
    core = C()
    m = re.fullmatch(r"Bearer ([0-9a-f]{32})\.([\w-]{30,80})", request.headers.get("Authorization", ""))
    if not m:
        raise core.Invalid("Add your Reachout extension key in the extension.", status=401)
    uid, secret = m.groups()
    if core.rate_limited(("ext", uid), 600, 3600):
        raise core.Invalid("Too many requests from the extension. Try again in a while.", status=429)
    if not core.M.users.find_one({"_id": uid}, {"_id": 1}):
        raise core.Invalid("This extension key doesn't work any more. Create a new one in Reachout.", status=401)
    ws = core.Workspace(uid)
    saved = ws.settings().get("ext_key_hash", "")
    if not saved or not hmac.compare_digest(saved, key_hash(secret)):
        raise core.Invalid("This extension key doesn't work any more. Create a new one in Reachout.", status=401)
    return ws


# ----------------------------------------------------------------- managed from the app (normal login)

@bp.post("/api/extension/key")
@login_required
def new_key(ws):
    """A new key (shown once); any previous key stops working."""
    secret = secrets.token_urlsafe(32)
    ws.update_settings(ext_key_hash=key_hash(secret), ext_key_created=time.time())
    return jsonify(key=f"{ws.uid}.{secret}")


@bp.delete("/api/extension/key")
@login_required
def revoke_key(ws):
    ws.update_settings(ext_key_hash="", ext_key_created=0)
    return jsonify(ok=True)


@bp.get("/api/extension")
@login_required
def ext_status(ws):
    st = ws.settings()
    return jsonify(has_key=bool(st.get("ext_key_hash")), created=st.get("ext_key_created") or 0,
                   last_used=st.get("ext_last_used") or 0)


# ----------------------------------------------------------------- used by the extension (extension key)

def company_for(ws, url, title=""):
    """The company behind an application page: from your matched jobs, else from the job board's address."""
    for r in ws.load("ai_match", {}).get("results", []):
        jid = r["id"].split(":")[-1]
        if jid and jid in url:
            return r["company"]
    parts = urlsplit(url or "")
    path = [x for x in parts.path.split("/") if x]
    host = (parts.hostname or "").lower()
    q = dict(x.split("=", 1) for x in parts.query.split("&") if "=" in x)
    slug = q.get("for") or (path[0] if path and any(h in host for h in ("lever.co", "ashbyhq.com", "greenhouse.io")) else "")
    if slug:
        return slug.replace("softwareprivatelimited", "").replace("-", " ").title()
    m = re.search(r"(?:at|@|-|\|)\s*([A-Z][\w&. ]{1,40})\s*$", title or "")
    return m.group(1).strip() if m else ""


@bp.post("/api/ext/autofill")
def autofill():
    """Your details plus the answers to this form's questions (approved ones; drafts are marked)."""
    core = C()
    ws = ext_workspace()
    p = request.get_json(silent=True) or {}
    url, title = str(p.get("url") or "")[:500], str(p.get("title") or "")[:200]
    company = str(p.get("company") or "").strip()[:80] or company_for(ws, url, title)
    ai = AI()
    v = ai.apply_values(ws)
    fields = {k: v.get(k, "") for k in ("name", "first", "last", "email", "phone", "dial", "country", "location", "linkedin",
                                        "github", "portfolio", "current_company", "notice")}
    bank = ws.load("ai_answers", {})
    answers = {}
    for q in [str(x)[:300] for x in (p.get("questions") or [])][:120]:
        hit = ai.answer_for(bank, q, company)
        if hit:
            answers[q] = {"answer": hit, "draft": False}
            continue
        for a in bank.values():  # an unapproved draft: offered, clearly marked, never filled without a click
            if a.get("status") == "review" and a.get("answer") and (not a.get("company") or ai.same_company(a["company"], company)) \
                    and ai.q_score(ai.norm_q(q, company), a.get("norm") or "") >= 0.8:
                answers[q] = {"answer": a["answer"].replace("{company}", company or "your company"), "draft": True}
                break
    resume, data = ai.resume_file(ws)
    ws.update_settings(ext_last_used=time.time())
    return jsonify(company=company, fields=fields, answers=answers, resume={"name": resume, "size": len(data or b"")} if data else None,
                   field_map=[[pat, key] for pat, key in ai.FIELD_MAP])


@bp.get("/api/ext/resume")
def resume():
    ws = ext_workspace()
    name, data = AI().resume_file(ws)
    if not data:
        raise C().Invalid("Upload your resume as a PDF in Reachout first.", status=404)
    return Response(data, mimetype="application/pdf", headers={"X-Filename": re.sub(r"[^\w.\-]", "_", name) or "resume.pdf",
                                                               "Cache-Control": "no-store"})


@bp.post("/api/ext/learn")
def learn():
    """Answers the user typed on this page, saved to My answers for their review (never used before approval)."""
    core = C()
    ws = ext_workspace()
    p = request.get_json(silent=True) or {}
    ai = AI()
    url, title = str(p.get("url") or "")[:500], str(p.get("title") or "")[:200]
    company = str(p.get("company") or "").strip()[:80] or company_for(ws, url, title)
    n = 0
    for pair in (p.get("pairs") or [])[:120]:
        if not (isinstance(pair, list) and len(pair) == 2):
            continue
        q, a = str(pair[0]).strip()[:300], str(pair[1]).strip()[:4000]
        ql = q.lower().strip(" ?:*")
        if not q or not a or any(re.search(pat, ql) for pat, _ in ai.FIELD_MAP) or re.search(r"resume|cover letter|password|captcha", ql):
            continue
        specific = bool(company) and company.lower() in q.lower()
        ai.save_answer(ws, q, a, company if specific else "", status="review", source="learned",
                       learned_from=f"{company or urlsplit(url).hostname or 'a form'} (browser extension)")
        n += 1
    return jsonify(saved=n)
