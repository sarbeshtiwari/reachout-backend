"""Notifications: an in-app list that updates live (Server-Sent Events), browser push notifications that
arrive even when the Reachout tab is closed, and — when Reachout runs on your own Mac — native macOS
alerts that show even when the browser isn't open at all.

Everything is stored encrypted in MongoDB (collection `notifications`, 90-day expiry); push subscriptions
live in `push_subs`, also encrypted. VAPID keys for Web Push are generated once and kept in `site`.
"""

import base64
import json
import os
import platform
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context

import bridge
from bridge import login_required

bp = Blueprint("notify", __name__)
STREAMS = {}  # uid -> number of open live connections (a page is open: it shows alerts itself)
STREAM_SEEN = {}  # uid -> last time a live connection was open (streams are short and reconnect)
STREAM_LOCK = threading.Lock()
# Each live connection holds a server thread, so they are capped and short: otherwise a handful of open
# tabs could take every thread and freeze the whole app for everyone.
STREAM_PER_USER = 2
STREAM_TOTAL = int(os.environ.get("STREAM_SLOTS", "4"))
STREAM_SECONDS = int(os.environ.get("STREAM_SECONDS", "50"))  # keep under any proxy timeout (Netlify: ~26 s)
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
              "notify.windows.com", "push.apple.com")
MAX_PUSH_DEVICES = 10
_vapid_lock = threading.Lock()


def C():
    return bridge.C


def init():
    M = C().M
    M.notifications.create_index([("uid", 1), ("at", -1)])
    M.notifications.create_index("expire_at", expireAfterSeconds=0)
    M.push_subs.create_index("uid")
    bridge.notify = notify


def prefs(ws):
    return {"sound": True, "mac": False, **ws.load("notify_prefs", {})}


def mac_available():
    return platform.system() == "Darwin" and not C().PRODUCTION and os.environ.get("MAC_ALERTS", "1") == "1"


# ---------------------------------------------------------------- sending

def notify(uid, title, body="", link="", kind="info"):
    """Record a notification for one account and fan it out. Never raises."""
    try:
        now = time.time()
        item = {"title": str(title)[:140], "body": str(body)[:400], "link": str(link)[:300], "kind": kind}
        C().M.notifications.insert_one({"uid": uid, "at": now, "read": False, "data": C().seal(item),
                                        "expire_at": datetime.now(timezone.utc) + timedelta(days=90)})
        p = prefs(C().Workspace(uid))
        if p["mac"] and mac_available():
            threading.Thread(target=mac_alert, args=(item["title"], item["body"], p["sound"]), daemon=True).start()
        if not STREAMS.get(uid) and time.time() - STREAM_SEEN.get(uid, 0) > 20:  # no open tab: the page can't show it, so push
            threading.Thread(target=push_all, args=(uid, {**item, "tag": kind}), daemon=True).start()
    except Exception as e:
        print(f"[Reachout] notify: {e}", flush=True)


def mac_alert(title, body, sound=True):
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    script = f"display notification {q(body or ' ')} with title \"Reachout\" subtitle {q(title)}"
    if sound:
        script += ' sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=8, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def vapid():
    """(Vapid instance, public key in base64url) — created once per site."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    with _vapid_lock:
        row = C().M.site.find_one({"_id": "vapid"})
        keys = C().unseal(row["data"], None) if row else None
        if not keys:
            v = Vapid01()
            v.generate_keys()
            keys = {"pem": v.private_pem().decode()}
            C().M.site.replace_one({"_id": "vapid"}, {"_id": "vapid", "data": C().seal(keys)}, upsert=True)
        v = Vapid01.from_pem(keys["pem"].encode())
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return v, base64.urlsafe_b64encode(raw).decode().rstrip("=")


def push_all(uid, payload):
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return
    subs = list(C().M.push_subs.find({"uid": uid}))
    if not subs:
        return
    v, _ = vapid()
    user = C().find_user(uid=uid) or {}
    claims = {"sub": "mailto:" + (os.environ.get("VAPID_EMAIL") or user.get("email") or "admin@localhost")}
    for row in subs:
        sub = C().unseal(row["data"], None)
        if not sub:
            continue
        try:
            if not push_host_ok(sub.get("endpoint", "")):
                C().M.push_subs.delete_one({"_id": row["_id"]})
                continue
            webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=v, vapid_claims=dict(claims), ttl=86400, timeout=10)
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):  # unsubscribed / expired
                C().M.push_subs.delete_one({"_id": row["_id"]})
        except Exception as e:
            print(f"[Reachout] push: {e}", flush=True)


# ---------------------------------------------------------------- API

def item_of(row):
    d = C().unseal(row["data"], {}) or {}
    return {"id": str(row["_id"]), "at": row["at"], "read": row.get("read", False), **d}


@bp.get("/api/notifications")
@login_required
def list_notes(ws):
    M = C().M
    rows = M.notifications.find({"uid": ws.uid}).sort("at", -1).limit(40)
    _, public_key = vapid()
    return jsonify(items=[item_of(r) for r in rows], unread=M.notifications.count_documents({"uid": ws.uid, "read": False}),
                   prefs=prefs(ws), mac_available=mac_available(), push_key=public_key,
                   push_devices=M.push_subs.count_documents({"uid": ws.uid}))


@bp.post("/api/notifications/read")
@login_required
def mark_read(ws):
    p = C().body()
    q = {"uid": ws.uid, "read": False}
    if not p.get("all"):
        try:
            q["_id"] = {"$in": [ObjectId(i) for i in (p.get("ids") or [])[:100]]}
        except (InvalidId, TypeError):
            raise C().Invalid("Unknown notification.")
    C().M.notifications.update_many(q, {"$set": {"read": True}})
    return jsonify(unread=C().M.notifications.count_documents({"uid": ws.uid, "read": False}))


@bp.post("/api/notifications/clear")
@login_required
def clear(ws):
    C().M.notifications.delete_many({"uid": ws.uid})
    return jsonify(ok=True)


@bp.put("/api/notifications/prefs")
@login_required
def save_prefs(ws):
    p, cur = C().body(), prefs(ws)
    for k in ("sound", "mac"):
        if k in p:
            cur[k] = bool(p[k])
    if cur["mac"] and not mac_available():
        raise C().Invalid("Mac alerts only work when Reachout runs on your own Mac.", "mac")
    ws.save("notify_prefs", cur)
    return jsonify(prefs=cur)


def push_host_ok(endpoint):
    """Only real browser push services: this server POSTs to the endpoint, so anything else could be used
    to make it reach internal addresses."""
    from urllib.parse import urlsplit
    u = urlsplit(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and u.port in (None, 443) and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


@bp.post("/api/notifications/subscribe")
@login_required
def subscribe(ws):
    sub = C().body().get("subscription") or {}
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not push_host_ok(endpoint) or len(endpoint) > 1000 or not keys.get("p256dh") or not keys.get("auth"):
        raise C().Invalid("The browser sent an invalid push subscription.")
    sid = C().lookup_hash("push:" + endpoint)
    other = C().M.push_subs.find_one({"_id": sid}, {"uid": 1})
    if other and other["uid"] != ws.uid:
        raise C().Invalid("This browser is already linked to another account. Log out there first.")
    if not other and C().M.push_subs.count_documents({"uid": ws.uid}) >= MAX_PUSH_DEVICES:
        raise C().Invalid(f"You can get notifications on up to {MAX_PUSH_DEVICES} browsers. Turn them off on one first.")
    C().M.push_subs.replace_one({"_id": sid}, {"_id": sid, "uid": ws.uid, "created": time.time(),
                                               "data": C().seal({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}})},
                                upsert=True)
    return jsonify(ok=True, devices=C().M.push_subs.count_documents({"uid": ws.uid}))


@bp.post("/api/notifications/unsubscribe")
@login_required
def unsubscribe(ws):
    endpoint = str(C().body().get("endpoint") or "")
    if endpoint:
        C().M.push_subs.delete_one({"_id": C().lookup_hash("push:" + endpoint), "uid": ws.uid})
    else:
        C().M.push_subs.delete_many({"uid": ws.uid})
    return jsonify(ok=True, devices=C().M.push_subs.count_documents({"uid": ws.uid}))


@bp.post("/api/notifications/test")
@login_required
def test(ws):
    if C().rate_limited(("notify-test", ws.uid), 10, 600):
        raise C().Invalid("That's enough tests for now. Try again in a few minutes.", status=429)
    mode = C().body().get("mode")
    if mode == "push":  # prove the closed-tab path works, even while this tab is open
        item = {"title": "Test notification", "body": "Browser notifications are working.", "link": "#dashboard", "kind": "info"}
        threading.Thread(target=push_all, args=(ws.uid, {**item, "tag": "test"}), daemon=True).start()
        return jsonify(ok=True)
    notify(ws.uid, "Test notification", "Notifications are working.", "#dashboard", "info")
    return jsonify(ok=True)


@bp.get("/api/notifications/stream")
@login_required
def stream(ws):
    """Live feed for an open page. Holds one connection; the browser reconnects on its own."""
    uid = ws.uid
    try:
        last = float(request.args.get("since") or time.time())
    except ValueError:
        last = time.time()

    with STREAM_LOCK:
        busy = STREAMS.get(uid, 0) >= STREAM_PER_USER or sum(STREAMS.values()) >= STREAM_TOTAL
        if not busy:
            STREAMS[uid] = STREAMS.get(uid, 0) + 1
    if busy:  # no free slot: tell the browser to come back later instead of holding a thread
        STREAM_SEEN[uid] = time.time()
        return Response("retry: 30000\n\n", mimetype="text/event-stream", headers={"Cache-Control": "no-store"})

    def gen():
        nonlocal last
        try:
            yield "retry: 3000\n\n"
            started, beat = time.time(), time.time()
            while time.time() - started < STREAM_SECONDS:
                STREAM_SEEN[uid] = time.time()
                rows = list(C().M.notifications.find({"uid": uid, "at": {"$gt": last}}).sort("at", 1).limit(20))
                if rows:
                    last = rows[-1]["at"]
                    unread = C().M.notifications.count_documents({"uid": uid, "read": False})
                    for r in rows:
                        yield f"event: notify\ndata: {json.dumps({**item_of(r), 'unread': unread})}\n\n"
                    beat = time.time()
                elif time.time() - beat > 20:
                    yield ": ping\n\n"
                    beat = time.time()
                time.sleep(2)
        finally:
            with STREAM_LOCK:
                STREAMS[uid] = max(0, STREAMS.get(uid, 1) - 1)
            STREAM_SEEN[uid] = time.time()

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@bp.get("/sw.js")
def service_worker():
    path = C().WEB / "dist" / "sw.js"  # built from frontend/public/sw.js
    if not path.exists():
        return Response("Not found", 404)
    resp = send_file(path, mimetype="text/javascript", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp
