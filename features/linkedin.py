"""LinkedIn via the official API: sign in (OpenID Connect) and publish / schedule posts.

LinkedIn only offers these to third-party apps ("Sign In with LinkedIn using OpenID Connect" and "Share on
LinkedIn"). Feed, messages, profile editing and job applications are not available through its API, so
the UI links out to linkedin.com for those instead of automating them.

The site owner registers a LinkedIn app once (client ID + secret, stored encrypted); each user then
connects their own LinkedIn account. Their access token is stored encrypted in their workspace.
"""

import re
import secrets
import time
from datetime import date, datetime
from urllib.parse import quote, urlencode

from flask import Blueprint, jsonify, redirect, request, session

import bridge
from bridge import login_required

bp = Blueprint("linkedin", __name__)
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPES = "openid profile email w_member_social"
SIGNIN_SCOPES = "openid profile email"  # when the LinkedIn app doesn't have "Share on LinkedIn" (yet)


def C():
    return bridge.C


def app_config():
    """LinkedIn app credentials: env vars win, else what the site owner saved in the app."""
    import os
    if os.environ.get("LINKEDIN_CLIENT_ID") and os.environ.get("LINKEDIN_CLIENT_SECRET"):
        return {"client_id": os.environ["LINKEDIN_CLIENT_ID"], "client_secret": os.environ["LINKEDIN_CLIENT_SECRET"], "env": True}
    d = C().M.site.find_one({"_id": "linkedin"})
    return (C().unseal(d["data"], {}) if d else {}) or {}


def redirect_uri():
    return f"{C().site_url()}/api/li/callback"


def account(ws):
    li = ws.load("linkedin", {})
    if li.get("token") and li.get("expires_at", 0) < time.time():
        li["expired"] = True
    return li


@bp.get("/api/li/status")
@login_required
def status(ws):
    cfg, li = app_config(), account(ws)
    return jsonify(configured=bool(cfg.get("client_id")), is_admin=C().is_admin(ws.uid), redirect_uri=redirect_uri(),
                   connected=bool(li.get("token")) and not li.get("expired"), expired=bool(li.get("expired")),
                   profile={k: li.get(k) for k in ("name", "given_name", "picture", "email", "sub")} if li.get("token") else None,
                   expires_at=li.get("expires_at"), posts=ws.load("li_posts", [])[:30], env=bool(cfg.get("env")),
                   can_post=bool(li.get("token")) and "w_member_social" in (li.get("scope") or ""))


@bp.put("/api/li/config")
@login_required
def save_config(ws):
    if not C().is_admin(ws.uid):
        raise C().Invalid("Only the site owner can set up the LinkedIn app.", status=403)
    p = C().body()
    cid, secret = str(p.get("client_id") or "").strip(), str(p.get("client_secret") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", cid):
        raise C().Invalid("That doesn't look like a LinkedIn Client ID.", "client_id")
    if not re.fullmatch(r"[A-Za-z0-9._=\-]{10,120}", secret):
        raise C().Invalid("That doesn't look like a LinkedIn Client Secret.", "client_secret")
    C().M.site.replace_one({"_id": "linkedin"}, {"_id": "linkedin", "data": C().seal({"client_id": cid, "client_secret": secret})},
                           upsert=True)
    return jsonify(ok=True)


@bp.get("/api/li/connect")
@login_required
def connect(ws):
    cfg = app_config()
    if not cfg.get("client_id"):
        return redirect("/app#linkedin?error=" + quote("LinkedIn isn't set up on this site yet."))
    state = secrets.token_urlsafe(24)
    session["li_state"] = state
    # Ask for posting permission unless we already know this LinkedIn app doesn't have it.
    if request.args.get("share") == "1":  # "Reconnect with posting": try again after enabling the product
        session.pop("li_no_share", None)
    share = request.args.get("share") != "0" and not session.get("li_no_share")
    session["li_scope"] = SCOPES if share else SIGNIN_SCOPES
    q = urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri(),
                   "state": state, "scope": session["li_scope"]})
    return redirect(f"{AUTH_URL}?{q}")


@bp.get("/api/li/callback")
@login_required
def callback(ws):
    def back(err=""):
        return redirect("/app#linkedin" + (f"?error={quote(err)}" if err else "?connected=1"))
    state, expected = request.args.get("state", ""), session.pop("li_state", None)
    if not expected or not secrets.compare_digest(state, expected):
        return back("The sign-in link expired. Please try again.")
    if request.args.get("error") == "unauthorized_scope_error" and "w_member_social" in request.args.get("error_description", ""):
        # The LinkedIn app lacks "Share on LinkedIn": sign in without posting permission instead of failing.
        session["li_no_share"] = True
        return redirect("/api/li/connect?share=0")
    if request.args.get("error"):
        return back(request.args.get("error_description") or "LinkedIn sign-in was cancelled.")
    cfg = app_config()
    r = bridge.http("POST", TOKEN_URL, data={"grant_type": "authorization_code", "code": request.args.get("code", ""),
                                             "redirect_uri": redirect_uri(), "client_id": cfg.get("client_id", ""),
                                             "client_secret": cfg.get("client_secret", "")})
    if r.status_code != 200:
        return back("LinkedIn didn't accept the sign-in. Check the app's redirect URL and products.")
    tok = r.json()
    u = bridge.http("GET", "https://api.linkedin.com/v2/userinfo", token=tok["access_token"])
    if u.status_code != 200:
        return back("Couldn't read your LinkedIn profile. Make sure the app has 'Sign In with LinkedIn using OpenID Connect'.")
    info = u.json()
    ws.save("linkedin", {"token": tok["access_token"], "expires_at": time.time() + int(tok.get("expires_in", 5184000)),
                         "scope": tok.get("scope") or session.pop("li_scope", ""), "sub": info.get("sub"), "name": info.get("name"),
                         "given_name": info.get("given_name"), "picture": info.get("picture"), "email": info.get("email")})
    return back()


@bp.post("/api/li/disconnect")
@login_required
def disconnect(ws):
    ws.save("linkedin", {})
    return jsonify(ok=True)


LITTLE_TEXT = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def publish(ws, text, visibility):
    """Publish a text post as the connected member. Returns (url, urn)."""
    li = account(ws)
    if not li.get("token") or li.get("expired"):
        raise C().Invalid("Connect LinkedIn first (or reconnect: your sign-in expired).")
    if "w_member_social" not in (li.get("scope") or ""):
        raise C().Invalid("Posting isn't enabled yet: add the 'Share on LinkedIn' product to your LinkedIn app, then reconnect.")
    author = f"urn:li:person:{li['sub']}"
    today = date.today()
    y, m = (today.year, today.month - 2) if today.month > 2 else (today.year - 1, today.month + 10)
    headers = {"LinkedIn-Version": f"{y}{m:02d}", "X-Restli-Protocol-Version": "2.0.0"}
    body = {"author": author, "commentary": LITTLE_TEXT.sub(r"\\\1", text), "visibility": visibility,
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    r = bridge.http("POST", "https://api.linkedin.com/rest/posts", token=li["token"], headers=headers, json=body)
    if r.status_code in (400, 426) and "version" in r.text.lower():
        ugc = {"author": author, "lifecycleState": "PUBLISHED",
               "specificContent": {"com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": text}, "shareMediaCategory": "NONE"}},
               "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility}}
        r = bridge.http("POST", "https://api.linkedin.com/v2/ugcPosts", token=li["token"],
                        headers={"X-Restli-Protocol-Version": "2.0.0"}, json=ugc)
    if r.status_code == 401:
        raise C().Invalid("LinkedIn sign-in expired. Reconnect on the LinkedIn page.")
    if r.status_code == 403:
        raise C().Invalid("LinkedIn refused the post. The LinkedIn app needs the 'Share on LinkedIn' product.")
    if r.status_code not in (200, 201):
        raise C().Invalid(f"LinkedIn returned an error ({r.status_code}): {r.text[:200]}")
    urn = r.headers.get("x-restli-id") or r.headers.get("X-RestLi-Id") or (r.json().get("id") if r.content else "")
    url = f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
    with ws.lock:
        posts = ws.load("li_posts", [])
        posts.insert(0, {"urn": urn, "url": url, "text": text[:3000], "visibility": visibility,
                         "at": datetime.now().isoformat(timespec="seconds")})
        ws.save("li_posts", posts[:200])
    return url, urn


def check_post(p):
    text = str(p.get("text") or "").strip()
    if not text:
        raise C().Invalid("Write something to post.", "text")
    if len(text) > 3000:
        raise C().Invalid("LinkedIn posts can be up to 3,000 characters.", "text")
    vis = p.get("visibility", "PUBLIC")
    if vis not in ("PUBLIC", "CONNECTIONS"):
        raise C().Invalid("Choose who can see the post.", "visibility")
    return text, vis


@bp.post("/api/li/post")
@login_required
def post_now(ws):
    if C().rate_limited(("li-post", ws.uid), 10, 3600):
        raise C().Invalid("You've posted a lot in the last hour. Try again later.", status=429)
    text, vis = check_post(C().body())
    url, urn = publish(ws, text, vis)
    return jsonify(ok=True, url=url, urn=urn)


@bp.post("/api/li/schedule")
@login_required
def schedule(ws):
    from features import jobs as feature_jobs
    p = C().body()
    text, vis = check_post(p)
    li = account(ws)
    if not li.get("token"):
        raise C().Invalid("Connect LinkedIn first.")
    due = feature_jobs.parse_due(p)
    if li.get("expires_at", 0) < due:
        raise C().Invalid("Your LinkedIn sign-in expires before that time. Reconnect first or pick an earlier time.", "send_at")
    qid = feature_jobs.queue_doc(ws, "linkedin_post", due, {"text": text, "visibility": vis},
                                 {"text": text[:140], "visibility": vis})
    return jsonify(id=qid, due=due)


def run_scheduled_post(ws, payload):
    url, _ = publish(ws, payload["text"], payload.get("visibility", "PUBLIC"))
    return True, url or "Posted"


bridge.QUEUE_HANDLERS["linkedin_post"] = run_scheduled_post
