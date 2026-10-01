"""Inbox insights: every email in the inbox grouped by the company it's from, sorted into categories, with
charts and a per-company drill-down.

The whole message is read (read-only, BODY.PEEK, so nothing is marked as read) because the sender alone is
often a hiring system or a no-reply address: the body names the actual company and says whether it's an
application update, an interview invite, a recruiter reaching out, a job alert or a newsletter.

Each email becomes one encrypted row in `mail_index` (sender, subject, a short snippet, company, category).
The first sync reads the whole inbox; later syncs only read what's new, and the morning sync runs it too.
"""

import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from flask import Blueprint, jsonify, request

import bridge
from features import apps as fa
from bridge import login_required

bp = Blueprint("inbox", __name__)


def C():
    return bridge.C


CATEGORIES = {  # key: (label, colour, job-related?)
    "offer": ("Offers", "#16a34a", True),
    "interview": ("Interviews", "#0d9488", True),
    "assessment": ("Assessments & tests", "#8b5cf6", True),
    "application": ("Application updates", "#2563eb", True),
    "rejection": ("Rejections", "#ef4444", True),
    "recruiter": ("Recruiters & HR", "#f59e0b", True),
    "job_alert": ("Job alerts", "#6366f1", True),
    "newsletter": ("Newsletters & promotions", "#94a3b8", False),
    "account": ("Accounts & security", "#64748b", False),
    "finance": ("Orders, bills & banking", "#0ea5e9", False),
    "exams": ("Exams & government", "#d97706", False),
    "learning": ("Courses & learning", "#ec4899", False),
    "social": ("Social & community", "#a855f7", False),
    "personal": ("Personal", "#22c55e", False),
    "other": ("Other", "#cbd5e1", False),
}
APP_TO_CAT = {"offer": "offer", "interview": "interview", "assessment": "assessment", "shortlisted": "interview",
              "rejected": "rejection", "closed": "rejection", "withdrawn": "rejection", "applied": "application",
              "in_review": "application", "incomplete": "application"}

JOB_ALERT = re.compile(r"new jobs? posted|job alert|jobs? (?:for you|you (?:may|might))|recommended jobs|jobs? matching|"
                       r"be first to apply|check out jobs|handpicked|urgently hiring|top openings|urgent requirement|"
                       r"new (?:job )?opportunit|hiring for|jobs? in |openings? (?:at|for)|walk-?in|^job \||latest .{0,20}jobs|"
                       r"similar jobs|jobs? posted|apply now|is hiring|are hiring|hiring now|(?:immediate|urgent) requirement|"
                       r"requirement for|jobs? you might|recommended for you|matches your profile", re.I)
JOB_BOARDS = re.compile(r"@(?:[\w-]+\.)*(?:naukri\.com|linkedin\.com|indeed\.com|indeedemail\.com|glassdoor\.|foundit\.in|"
                        r"monsterindia|shine\.com|instahyre\.com|wellfound\.com|cutshort\.io|hirist\.|iimjobs\.com|"
                        r"apna\.co|internshala\.com|unstop\.com|jobs2web\.com|timesjobs\.com|workindia|joinsuperset\.com)", re.I)
RECRUITER_BODY = re.compile(r"(?:your|came across your) (?:profile|resume|cv)|(?:opportunity|opening|position|role) (?:with|at|for)|"
                            r"(?:are|is) (?:looking|hiring) for|(?:share|send) (?:me )?your (?:updated )?(?:resume|cv)|"
                            r"current (?:ctc|salary)|expected (?:ctc|salary)|notice period|job description|\bjd\b|"
                            r"interested in (?:this|the) (?:role|position|opportunity)|let me know if you(?:'re| are) interested", re.I)
EXAMS = re.compile(r"admit card|\bexam\b|examination|\bcbt\b|computer based test|hall ticket|\bgate[- ]?20\d\d|afcat|"
                   r"\bacio\b|\bupsc\b|\buppsc\b|\bssc\b|\bibps\b|\bnta\b|registration (?:number|no)|\botr\b|scorecard|"
                   r"result (?:declared|announced)", re.I)
EXAMS_FROM = re.compile(r"@(?:[\w-]+\.)*(?:nic\.in|gov\.in|digialm\.com|ibps\.in|cdac\.in|nta\.ac\.in|upsc\.gov\.in|"
                        r"iitg\.ac\.in|iitk\.ac\.in|ssc\.nic\.in)$", re.I)
ACCOUNT = re.compile(r"\botp\b|one[- ]time (?:password|code)|single-use code|verification code|verify (?:your|email)|"
                     r"security info|profile photo|confirm your (?:email|account)|"
                     r"password|sign[- ]?in|log[- ]?in (?:attempt|alert|code)|security alert|new device|2-step|"
                     r"two[- ]factor|account (?:created|activated|update)|welcome to|activate your", re.I)
FINANCE = re.compile(r"invoice|receipt|you will be charged|subscription is confirmed|plan (?:renew|expir)|payment|paid|order (?:confirm|#|placed|shipped|delivered)|your order|shipped|"
                     r"delivery|statement|transaction|debited|credited|refund|bill\b|subscription (?:renew|confirm)|"
                     r"upi|bank|credit card|emi\b|salary (?:slip|credited)|payslip|tax", re.I)
LEARNING = re.compile(r"course|webinar|certificat|workshop|bootcamp|lecture|class\b|learn(?:ing)?\b|tutorial|"
                      r"hackathon|contest|quiz|challenge", re.I)
SOCIAL_FROM = re.compile(r"@(?:[\w-]+\.)*(?:facebookmail|instagram|twitter|x\.com|discord|reddit|quora|medium|github|"
                         r"slack|meetup|whatsapp|telegram|youtube|pinterest|substack)\.", re.I)
LEARNING_FROM = re.compile(r"@(?:[\w-]+\.)*(?:coursera|udemy|edx|leetcode|geeksforgeeks|hackerrank|codechef|codeforces|"
                           r"scaler|simplilearn|upgrad|great ?learning|nptel|swayam|datacamp|freecodecamp|educative|"
                           r"(?:ac|edu)(?:\.[a-z]{2})?)\b", re.I)


def categorize(subject, body, addr, name, has_unsub):
    """(category, application status or '') for one email."""
    dom = addr.partition("@")[2].lower()
    personal = dom in C().PERSONAL_DOMAINS
    st, _ = fa.classify(subject, body[:6000])
    job_board = bool(JOB_BOARDS.search("@" + dom))
    # Real application events (a confirmation, a rejection, an interview) beat everything else.
    if st and not (fa.NOISE_SUBJECT.search(subject) or fa.NOISE_FROM.search(f"{name} <{addr}>")):
        if not (job_board and st in ("applied", "in_review") and JOB_ALERT.search(subject)):
            return APP_TO_CAT[st], st
    if job_board and (JOB_ALERT.search(subject) or has_unsub):
        return "job_alert", ""
    if JOB_ALERT.search(subject) and (has_unsub or job_board):
        return "job_alert", ""
    if EXAMS_FROM.search("@" + dom) or (EXAMS.search(subject) and not job_board):
        return "exams", ""
    if ACCOUNT.search(subject):
        return "account", ""
    if FINANCE.search(subject):
        return "finance", ""
    if not has_unsub and RECRUITER_BODY.search(body[:4000]) and not job_board:
        return "recruiter", ""
    if SOCIAL_FROM.search("@" + dom):
        return "social", ""
    if LEARNING_FROM.search("@" + dom) or (has_unsub and LEARNING.search(subject)):
        return "learning", ""
    if has_unsub:
        return "newsletter", ""
    if personal:
        return "personal", ""
    return "other", ""


SECOND_LEVEL = {"co", "com", "net", "org", "ac", "gov", "edu", "nic", "res", "gen", "ind", "firm"}


def root_domain(dom):
    """careers.hsbc.com -> hsbc.com, qazmail.quesscorp.com -> quesscorp.com, iitg.ac.in -> iitg.ac.in."""
    parts = dom.lower().split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2 else 2
    return ".".join(parts[-n:])


def is_portal(dom):
    return any(dom == d or dom.endswith("." + d) for d in fa.PORTALS) or dom.endswith(("jobs2web.com", "yello.co", "onwingspan.com"))


def company_for(subject, name, addr, body, category, me_names):
    """(display name, grouping key, via) — the organisation an email is really from.

    Mail from a company's own domain is grouped by that domain (so "Quess", "Quess Corp" and
    "QUESS CORP Communications" are one company). Mail sent through a hiring system or job board is
    grouped by the company named in the email instead."""
    dom = addr.partition("@")[2].lower()
    if dom in C().PERSONAL_DOMAINS:
        who = fa.tidy(name or addr.partition("@")[0], 60)
        return who or addr, "person:" + addr.lower(), "Personal"
    if dom.endswith("jobs2web.com"):  # "New jobs posted from careers.wipro.com" / "… from Capgemini Group"
        m = re.search(r"posted from\s+(.+)$", subject, re.I)
        src = (m.group(1).strip() if m else "")
        comp = fa.company_from_domain("x@" + src) if re.search(r"\.\w{2,}$", src) else fa.good_company(src)
        if not comp and (m2 := re.match(r"^([A-Z][\w&.' -]{2,40}?)\s*:", subject)):  # "Standard Chartered: We have new …"
            comp = fa.good_company(m2.group(1))
        comp = comp or fa.good_company(re.sub(r"(?:limit\w*|-?jobnotification.*|\d+$|p\d+$|l$|tecp\d+$)", "", addr.partition("@")[0]))
        if comp:
            return comp, "co:" + fa.norm_company(comp), "Jobs2Web"
    if category in ("offer", "interview", "assessment", "application", "rejection", "recruiter") and is_portal(dom):
        info = fa.extract(subject, name, addr, body, me_names)
        if info["company"]:
            return info["company"], "co:" + fa.norm_company(info["company"]), info["portal"]
    if not is_portal(dom):
        comp, _, _ = fa.company_from_sender(name, addr)
        comp = comp or fa.company_from_domain(addr)
        root = root_domain(dom)
        if not comp:
            comp = root.split(".")[0].title()
        return comp, "dom:" + root, ""
    comp, portal, tenant = fa.company_from_sender(name, addr)
    comp = comp or tenant or fa.company_from_domain(addr)
    if portal and not comp:
        comp = portal
    if not comp:  # fall back to the registrable part of the domain
        parts = [p for p in dom.split(".") if p not in ("com", "co", "in", "org", "net", "io", "ai", "mail", "email", "e", "m", "info", "news")]
        comp = parts[-1].title() if parts else dom
    key = fa.norm_company(comp) or dom
    return comp, "co:" + key, portal


# ================================================================= sync

LOCK = {}
PROGRESS = {}


def ensure_indexes():
    M = C().M
    M.mail_index.create_index([("uid", 1), ("mid", 1)], unique=True)
    M.mail_index.create_index([("uid", 1), ("key", 1), ("ts", -1)])
    M.mail_index.create_index([("uid", 1), ("ts", -1)])


def scan(ws, full=False):
    core = C()
    lock = LOCK.setdefault(ws.uid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise core.Invalid("Inbox sync is already running.", status=409)
    PROGRESS[ws.uid] = {"stage": "Connecting to your mailbox…", "done": 0, "total": 0}
    state = ws.load("inbox_sync", {})
    try:
        profile = ws.profile()
        me = (profile.get("email") or "").lower()
        me_names = fa.my_name_parts(profile)
        fa.ME_NAMES.set(me_names)
        imap = core.imap_connect(profile)
        added = 0
        try:
            typ, data = imap.select("INBOX", readonly=True)
            validity = ""
            typ, resp = imap.response("UIDVALIDITY")
            if resp and resp[0]:
                validity = resp[0].decode()
            start_uid = 1
            if not full and state.get("validity") == validity and state.get("last_uid"):
                start_uid = int(state["last_uid"]) + 1
            PROGRESS[ws.uid]["stage"] = "Finding emails…"
            typ, data = imap.uid("SEARCH", None, f"UID {start_uid}:*")
            uids = [u for u in (data[0].split() if typ == "OK" and data and data[0] else []) if int(u) >= start_uid]
            if start_uid == 1:  # full read: rebuild, so emails deleted from the inbox drop out too
                core.M.mail_index.delete_many({"uid": ws.uid})
            known = {r["mid"] for r in core.M.mail_index.find({"uid": ws.uid}, {"mid": 1})}
            PROGRESS[ws.uid].update(stage="Reading emails…", total=len(uids))
            last_uid = int(state.get("last_uid") or 0) if not full else 0
            for i in range(0, len(uids), 40):
                chunk = uids[i:i + 40]
                typ, parts = imap.uid("FETCH", b",".join(chunk).decode(), "(UID BODY.PEEK[]<0.300000>)")
                rows = []
                for item in parts if typ == "OK" else []:
                    if not isinstance(item, tuple):
                        continue
                    m = re.search(rb"UID (\d+)", item[0])
                    if not m:
                        continue
                    last_uid = max(last_uid, int(m.group(1)))
                    try:
                        row = index_row(ws, item[1], me, me_names)
                    except Exception as e:  # one odd email mustn't stop the scan
                        print(f"[Reachout] inbox: skipped a message ({e})", flush=True)
                        continue
                    if row and row["mid"] not in known:
                        known.add(row["mid"])
                        rows.append(row)
                if rows:
                    try:
                        core.M.mail_index.insert_many(rows, ordered=False)
                    except Exception:
                        pass  # duplicates from a concurrent run
                    added += len(rows)
                PROGRESS[ws.uid]["done"] = min(len(uids), i + 40)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        now = datetime.now().isoformat(timespec="seconds")
        state.update(last_ok=now, last_error="", validity=validity, last_uid=last_uid, added=added,
                     full_done=True if (full or start_uid == 1) else state.get("full_done", False))
        ws.save("inbox_sync", state)
        return {"added": added, "total": core.M.mail_index.count_documents({"uid": ws.uid})}
    except Exception as e:
        state.update(last_error=str(e) if isinstance(e, core.Invalid) else f"Sync failed: {e}")
        ws.save("inbox_sync", state)
        raise
    finally:
        PROGRESS.pop(ws.uid, None)
        lock.release()


def index_row(ws, raw, me, me_names):
    core = C()
    msg = core.email_lib.message_from_bytes(raw, policy=core.email_policy)
    sender = str(msg.get("From") or "")
    name, addr = parseaddr(sender)
    addr = addr.lower()
    if not addr or (me and addr == me):
        return None
    subject = fa.tidy(str(msg.get("Subject") or ""), 200)
    try:
        when = core.parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        when = datetime.now(timezone.utc)
    body = fa.body_text(msg)
    has_unsub = bool(msg.get("List-Unsubscribe")) or bool(re.search(r"\bunsubscribe\b", body[-3000:], re.I))
    cat, app_status = categorize(subject, body, addr, name, has_unsub)
    company, key, via = company_for(subject, name, addr, body, cat, me_names)
    mid = str(msg.get("Message-ID") or f"{addr}|{subject}|{when.isoformat()}").strip()
    return {"uid": ws.uid, "mid": hashlib.sha256(mid.encode()).hexdigest()[:24], "ts": when.timestamp(),
            "key": core.lookup_hash("mailco:" + key), "cat": cat,
            "data": core.seal({"company": company, "from": fa.tidy(name, 80), "addr": addr, "subject": subject,
                               "snippet": fa.tidy(re.sub(r"https?://\S+", "", body), 240), "via": via or "",
                               "app_status": app_status, "msgid": mid})}


def run_in_background(ws, full=False):
    def go():
        try:
            scan(ws, full=full)
        except Exception as e:
            print(f"[Reachout] inbox sync: {e}", flush=True)
    threading.Thread(target=go, daemon=True, name=f"inbox-sync-{ws.uid[:6]}").start()


# ================================================================= API

def rows_for(ws, since=None):
    core = C()
    q = {"uid": ws.uid}
    if since:
        q["ts"] = {"$gte": since}
    for r in core.M.mail_index.find(q, {"_id": 0, "ts": 1, "key": 1, "cat": 1, "data": 1}).sort("ts", -1).limit(50000):
        d = core.unseal(r["data"], {}) or {}
        yield r, d


@bp.get("/api/inbox")
@login_required
def summary(ws):
    core = C()
    months_back = core.v_int(request.args.get("months", 12), "months", "Months", 0, 240)
    since = (datetime.now(timezone.utc) - timedelta(days=31 * months_back)).timestamp() if months_back else None
    overrides = ws.load("inbox_companies", {})  # key -> {"name", "hidden"}
    apps = ws.load("applications", {})
    app_by_co = {}
    for a in sorted(apps.values(), key=lambda a: a.get("updated_at", "")):
        app_by_co[fa.norm_company(a.get("company", ""))] = a
    companies, cats, months = {}, {k: 0 for k in CATEGORIES}, {}
    total = 0
    for r, d in rows_for(ws, since):
        total += 1
        k = r["key"]
        o = overrides.get(k, {})
        c = companies.get(k)
        if not c:
            c = companies[k] = {"key": k, "name": o.get("name") or d.get("company") or "Unknown", "count": 0, "cats": {},
                                "last": r["ts"], "first": r["ts"], "via": set(), "senders": set(), "hidden": bool(o.get("hidden")),
                                "person": False, "last_subject": d.get("subject", ""), "names": {}}
        c["count"] += 1
        nm = d.get("company") or ""
        c["names"][nm] = c["names"].get(nm, 0) + 1
        c["cats"][r["cat"]] = c["cats"].get(r["cat"], 0) + 1
        c["first"] = min(c["first"], r["ts"])
        if d.get("via"):
            c["via"].add(d["via"])
        c["senders"].add(d.get("addr", ""))
        c["person"] = c["person"] or d.get("via") == "Personal"
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
        m = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m")
        months.setdefault(m, {})
        months[m][r["cat"]] = months[m].get(r["cat"], 0) + 1
    out = []
    for c in companies.values():
        top = max(c["cats"].items(), key=lambda kv: kv[1])[0]
        job = sum(n for k, n in c["cats"].items() if CATEGORIES[k][2])
        names = c.pop("names")
        if not overrides.get(c["key"], {}).get("name"):
            c["name"] = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] or c["name"]
        a = app_by_co.get(fa.norm_company(c["name"]))
        out.append({**c, "via": sorted(c["via"])[:3], "senders": len(c["senders"]), "top": top, "job": job,
                    "app": {"id": a["id"], "status": a.get("status", ""), "role": a.get("role", "")} if a else None})
    out.sort(key=lambda c: -c["count"])
    state = ws.load("inbox_sync", {})
    return jsonify(companies=out, categories={k: {"label": v[0], "color": v[1], "job": v[2]} for k, v in CATEGORIES.items()},
                   by_category=cats, months=[{"month": m, **v} for m, v in sorted(months.items())][-24:], total=total,
                   sync={**{k: state.get(k) for k in ("last_ok", "last_error", "full_done", "added")},
                         "running": ws.uid in PROGRESS, "progress": PROGRESS.get(ws.uid)},
                   email_ready=bool(ws.profile().get("smtp_password")))


@bp.get("/api/inbox/company/<key>")
@login_required
def company(ws, key):
    core = C()
    if not re.fullmatch(r"[0-9a-f]{16,64}", key):
        raise core.Invalid("Unknown company.", status=404)
    items = []
    for r in core.M.mail_index.find({"uid": ws.uid, "key": key}).sort("ts", -1).limit(300):
        d = core.unseal(r["data"], {}) or {}
        items.append({"ts": r["ts"], "cat": r["cat"], **{k: d.get(k, "") for k in ("company", "from", "addr", "subject", "snippet",
                                                                                      "via", "app_status", "msgid")}})
    if not items:
        raise core.Invalid("No emails from this company.", status=404)
    o = ws.load("inbox_companies", {}).get(key, {})
    return jsonify(name=o.get("name") or items[0]["company"], hidden=bool(o.get("hidden")), items=items)


@bp.put("/api/inbox/company/<key>")
@login_required
def edit_company(ws, key):
    core, p = C(), C().body()
    if not re.fullmatch(r"[0-9a-f]{16,64}", key):
        raise core.Invalid("Unknown company.", status=404)
    with ws.lock:
        ov = ws.load("inbox_companies", {})
        cur = ov.get(key, {})
        if "name" in p:
            cur["name"] = core.v_text(p.get("name"), "name", "Company name", 80, required=True)
        if "hidden" in p:
            cur["hidden"] = bool(p["hidden"])
        ov[key] = cur
        ws.save("inbox_companies", ov)
    return jsonify(ok=True, **cur)


@bp.post("/api/inbox/sync")
@login_required
def sync(ws):
    if not ws.profile().get("smtp_password"):
        raise C().Invalid("Set up email on the Profile page first, so Reachout can read your inbox.")
    if ws.uid not in PROGRESS:
        run_in_background(ws, full=bool(C().body().get("full")))
        time.sleep(0.3)
    return jsonify(ok=True, running=True)


@bp.get("/api/inbox/progress")
@login_required
def progress(ws):
    return jsonify(running=ws.uid in PROGRESS, progress=PROGRESS.get(ws.uid),
                   last_error=ws.load("inbox_sync", {}).get("last_error", ""))
