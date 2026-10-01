"""Replies: notices when someone answers an email you sent from Reachout, and reads what they wrote.

Matching: every email Reachout sends has its own Message-ID, which a reply carries in In-Reply-To /
References, so a reply in the same thread is tied to the exact email and contact it answers. Replies sent
as a new email are matched by the sender's address (only if Reachout has emailed that address).

Reading: the reply's own text is separated from the quoted history, then read for what the person means
(interested, not interested, asks for your resume or CTC, proposes a call, refers you to someone, …) and
for concrete details: meeting time and link, phone numbers, other email addresses, questions asked, and
the sender's signature (name, title, company). Out-of-office auto-replies are recognised and kept apart.

The mailbox is only read (BODY.PEEK in a read-only folder); nothing is marked as read. New mail is checked
every 3 minutes, from the last message seen.
"""

import re
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import getaddresses, parseaddr
from pathlib import Path

from flask import Blueprint, jsonify, request

import bridge
from features import apps as fa
from bridge import login_required

bp = Blueprint("replies", __name__)
LOCKS = {}


def C():
    return bridge.C


def init():
    C().M.replies.create_index([("uid", 1), ("ts", -1)])
    C().M.replies.create_index([("uid", 1), ("mid", 1)], unique=True)
    C().M.replies.create_index([("uid", 1), ("rid", 1)])


# ================================================================= reading the reply

QUOTE_START = re.compile(
    r"^\s*(?:On\s.{6,200}?wrote:\s*$|On\s.{6,120}$\n^.{0,120}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|"
    r"_{10,}|From:\s.+$\n^(?:Sent|Date):\s|Le\s.+a écrit\s*:|Am\s.+schrieb.+:|"
    r"-{2,}\s*Forwarded message\s*-{2,}|Sent from my (?:iPhone|Android|mobile)|Get Outlook for)", re.I | re.M)
SIGN_OFF = re.compile(r"^\s*(?:regards|best regards|kind regards|warm regards|thanks(?: and| &) regards|thanks|thank you|"
                      r"cheers|best|sincerely|yours (?:truly|sincerely)|with regards|br)\s*,?\s*$", re.I | re.M)


def plain_and_html(msg):
    plain = html = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, AssertionError):
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = content
        elif part.get_content_type() == "text/html" and not html:
            html = content
    return plain, html


QUOTE_START = re.compile(r"(?i)<div[^>]{0,300}(?:class=\"?gmail_(?:quote|extra)|id=\"?(?:divRplyFwdMsg|appendonsend))|<hr\b|<blockquote\b")


def html_reply_text(html):
    # Gmail / Outlook put the quoted history after a known marker: keep only what comes before the first
    # one. A single forward search with bounded attribute length: linear time even on hostile input.
    html = str(html or "")[:fa.HTML_MAX]
    m = QUOTE_START.search(html)
    return fa.html_to_text(html[:m.start()] if m else html)


def reply_text(msg):
    """(the new text the person wrote, the full text) with quoted history, signatures' tails and noise removed."""
    plain, html = plain_and_html(msg)
    full = plain if len(plain.strip()) > 20 or not html else fa.html_to_text(html)
    text = plain if plain.strip() else html_reply_text(html)
    text = text.replace("\r\n", "\n")
    m = QUOTE_START.search(text)
    if m:
        text = text[:m.start()]
    text = "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith(">"))
    text = re.sub(r"[ \t\xa0​]+", " ", text)
    # HTML mail turns every <p>/<div> into a blank line; keep one line break unless the text is truly paragraphed.
    lines = [ln.strip() for ln in text.split("\n")]
    short = sum(1 for ln in lines if ln and len(ln) < 90)
    text = re.sub(r"\n\s*\n+", "\n" if short > len([ln for ln in lines if ln]) * 0.6 else "\n\n", "\n".join(lines)).strip()
    return text[:4000], re.sub(r"\s+", " ", full)[:8000]


AUTO_SUBJECT = re.compile(r"^(?:automatic reply|auto(?:matic)?[- ]?reply|auto:|out of (?:the )?office|ooo\b|away from|"
                          r"on leave|vacation|autoreply|thank you for (?:your )?(?:email|message|contacting)|"
                          r"we(?:'ve| have) received your (?:email|message|query|request))", re.I)


def is_auto(msg, subject, text):
    auto = str(msg.get("Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Auto-Response-Suppress") and AUTO_SUBJECT.search(subject):
        return True
    if str(msg.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    if AUTO_SUBJECT.search(re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", subject, flags=re.I)):
        return True
    return bool(re.search(r"\b(?:i am|i'm|i will be) (?:currently )?(?:out of (?:the )?office|on (?:leave|vacation|holiday)|away)\b|"
                          r"this is an automated (?:reply|response|message)", text[:600], re.I))


INTENTS = [  # key, label, pattern — checked against the person's own words only
    ("not_interested", "Not interested / no opening",
     r"\b(?:no (?:current )?(?:openings?|vacanc\w+|positions?|requirements?)|not (?:currently )?hiring|(?:role|position) (?:has been|is) (?:filled|closed)|"
     r"not (?:a (?:good )?(?:fit|match)|interested)|(?:won't|will not|cannot|can't|unable to) (?:be able to )?(?:move forward|proceed|consider)|"
     r"regret to inform|unfortunately|we (?:have )?decided to (?:go|move) (?:ahead|forward) with (?:other|another)|"
     r"do not have (?:any )?(?:suitable )?(?:openings?|roles?|positions?)|keep your (?:resume|cv|profile) (?:on file|in our database))"),
    ("interview", "Wants to interview / talk",
     r"\b(?:schedule (?:a|an|your) (?:call|interview|meeting|chat|discussion)|(?:available|free|convenient) (?:for (?:a )?(?:call|chat|discussion))|"
     r"interview (?:is )?(?:scheduled|on|at|slot)|let(?:'s| us) (?:connect|talk|speak|have a call)|(?:can|could) (?:we|you) (?:connect|talk|speak|have a call|join)|"
     r"(?:please )?(?:join|attend) (?:the )?(?:call|meeting|interview)|when (?:are|would) you (?:be )?(?:available|free)|share your availability|"
     r"meet\.google\.com|zoom\.us/j|teams\.microsoft\.com|calendly\.com)"),
    ("asks_resume", "Asked for your resume",
     r"\b(?:(?:share|send|attach|forward|email|mail) (?:me |us )?(?:your |an? )?(?:updated |latest |recent )?(?:resume|cv|profile|portfolio)|"
     r"(?:resume|cv) (?:is )?(?:not )?attached\?|could(?:n't| not) (?:find|open) (?:the |your )?(?:resume|cv|attachment))"),
    ("asks_details", "Asked for details (CTC, notice period…)",
     r"\b(?:current (?:ctc|salary|compensation)|expected (?:ctc|salary|compensation)|notice period|(?:how soon|when) can you (?:join|start)|"
     r"total (?:years of )?experience|relevant experience|current location|(?:are you )?(?:open|willing) to relocat\w+|"
     r"(?:share|send|provide|fill) (?:the |your |following )?(?:details|information|form))"),
    ("referral", "Referred you / apply via portal",
     r"\b(?:(?:i have|i've|have) (?:forwarded|referred|shared|passed) your|(?:forwarded|referred|shared|passed) (?:it|your (?:resume|cv|profile|email)) (?:to|with)|"
     r"(?:reach out|write|connect|get in touch) (?:to|with) (?:\w+ ){0,3}(?:at|on)\s|(?:cc(?:'d|ed)?|copied|looping in|adding)\s+\w+|"
     r"(?:the right|a better) person (?:to|for)|(?:apply|submit (?:your|an) application)\b[^.\n]{0,30}\b(?:through|via|on|at)\b[^.\n]{0,25}\b(?:careers?|portal|website|job (?:site|board)|link)\b)"),
    ("interested", "Interested",
     r"\b(?:(?:we are|we're|i am|i'm) (?:interested|impressed)|(?:your )?profile (?:looks|seems) (?:good|interesting|great|relevant)|"
     r"(?:you have been|you've been|you are) shortlisted|(?:would|will) (?:like|love) to (?:take this forward|move forward|proceed|discuss)|"
     r"(?:thanks|thank you) for (?:reaching out|your interest)[^.]{0,40}\b(?:we|i) (?:will|would|shall)|(?:let|will) (?:me |us )?(?:get back|revert))"),
    ("question", "Asked you a question", r"\?\s*(?:\n|$)"),
    ("thanks", "Acknowledged", r"^\s*(?:thanks|thank you|noted|received|got it|acknowledged)\b[^?]{0,80}$"),
]
INTENTS = [(k, lbl, re.compile(p, re.I | re.M)) for k, lbl, p in INTENTS]
INTENT_LABEL = {k: lbl for k, lbl, _ in INTENTS} | {"auto_reply": "Auto-reply"}

PHONE = re.compile(r"(?<![\w/])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)|(?<![\w/])\+\d{1,3}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MEET = re.compile(r"https?://(?:meet\.google\.com/[\w-]+|[\w.-]*zoom\.us/[jw]/\S+|teams\.microsoft\.com/l/meetup-join/\S+|"
                  r"teams\.live\.com/meet/\S+|calendly\.com/\S+|[\w.-]*webex\.com/\S+)", re.I)
LINK = re.compile(r"https?://[^\s<>\"')\]]+")
WHEN = re.compile(
    r"\b(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
    r"(?:,?\s+\d{4})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|today|tomorrow|(?:this|next)\s+(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|"
    r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?)"
    r"(?:\s*(?:,|at|@|from|between)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|hrs|ist)?(?:\s*(?:-|to|–)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|ist)?)?)?",
    re.I)
TIME_ONLY = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)(?:\s*ist)?\b", re.I)
TITLE = re.compile(r"\b(?:recruiter|talent acquisition|hr|human resources|hiring manager|manager|lead|director|head|founder|"
                   r"co-founder|ceo|cto|partner|engineer|specialist|executive|associate|consultant|vp)\b", re.I)


def signature(text, sender_name):
    """Name / title / company from the last lines after a sign-off, if present."""
    m = None
    for m in SIGN_OFF.finditer(text):
        pass
    tail = text[m.end():] if m else "\n".join(text.split("\n")[-6:])
    lines = [ln.strip(" |-–•") for ln in tail.split("\n") if ln.strip()][:6]
    lines = [ln for ln in lines if len(ln) <= 80 and not EMAIL.search(ln) and not PHONE.search(ln) and not LINK.search(ln)]
    out = {}
    if lines and m:
        first = lines[0]
        if re.fullmatch(r"[A-Z][a-zA-Z.'’-]+(?:\s+[A-Z][a-zA-Z.'’-]+){0,3}", first):
            out["name"] = first
            lines = lines[1:]
    for ln in lines[:3]:
        if "title" not in out and TITLE.search(ln) and len(ln.split()) <= 8:
            parts = re.split(r"\s*(?:\||,|@|\bat\b|–|-)\s*", ln, maxsplit=1)
            out["title"] = fa.tidy(parts[0], 60)
            if len(parts) > 1 and fa.good_company(parts[1]):
                out["company"] = fa.good_company(parts[1])
        elif ("company" not in out and fa.good_company(ln) and len(ln.split()) <= 6 and not TITLE.search(ln)
              and not re.search(r"\b(?:people|operations|team|department|desk|support|recruitment|recruiting)\b", ln, re.I)):
            out["company"] = fa.good_company(ln)
    if "name" not in out and sender_name:
        out["name"] = fa.tidy(sender_name, 60)
    return out


def understand(text, full, msg, sender_name, sender_addr, me):
    """Intents + concrete details from the person's own words (quoted history excluded)."""
    own = text or full[:1500]
    intents = [k for k, _, rx in INTENTS if rx.search(LINK.sub(" ", own) if k == "question" else own)]
    # Sharper than the reply patterns for formal HR language ("regret", "shortlisted", "interview on …").
    st, _ = fa.classify("", own)
    extra = {"rejected": "not_interested", "closed": "not_interested", "interview": "interview", "shortlisted": "interested",
             "offer": "interested", "assessment": "interview"}.get(st)
    if extra and extra not in intents:
        intents.insert(0, extra)
    if "not_interested" in intents:
        intents = [i for i in intents if i not in ("interested", "thanks")]
    if len(intents) > 1 and "thanks" in intents:
        intents.remove("thanks")
    me_l = (me or "").lower()
    emails = sorted({e.lower() for e in EMAIL.findall(own)} - {sender_addr, me_l})[:5]
    phones = sorted({re.sub(r"[\s-]", "", p) for p in PHONE.findall(own)})[:4]
    meet = sorted(set(MEET.findall(full)))[:3]
    links = [lk for lk in dict.fromkeys(LINK.findall(own)) if lk not in meet and not re.search(r"unsubscribe|mailtrack|track", lk, re.I)][:5]
    whens = []
    for m in WHEN.finditer(own):
        w = fa.tidy(m.group(0), 60)
        if len(w) > 4 and (TIME_ONLY.search(w) or re.search(r"\d|today|tomorrow|next|this", w, re.I)) and w.lower() not in [x.lower() for x in whens]:
            whens.append(w)
    if not whens:
        whens = [fa.tidy(t, 20) for t in dict.fromkeys(TIME_ONLY.findall(own))][:2]
    cal = fa.calendar_start(msg)
    no_links = LINK.sub(" ", re.sub(r"<https?://[^>]*>", " ", own))
    questions = [fa.tidy(q, 200) for q in re.findall(r"[^.!?\n]{8,200}\?", no_links)][:5]
    asks = []
    for lbl, rx in (("Resume / CV", r"\b(?:resume|cv)\b"), ("Current CTC", r"current (?:ctc|salary)"), ("Expected CTC", r"expected (?:ctc|salary)"),
                    ("Notice period", r"notice period"), ("Availability", r"availab\w+|convenient time|free (?:for|on)"),
                    ("Portfolio / GitHub", r"portfolio|github|projects?"), ("Location / relocation", r"relocat\w+|current location|preferred location"),
                    ("Experience", r"years of experience|total experience|relevant experience")):
        if re.search(rx, own, re.I) and (questions or re.search(r"share|send|let (?:me|us) know|provide|mention|confirm", own, re.I)):
            asks.append(lbl)
    return {"intents": intents or ["replied"], "emails": emails, "phones": phones, "meeting_links": meet, "links": links,
            "when": whens[:3], "calendar": cal.isoformat(timespec="minutes") if cal else "", "questions": questions,
            "asks": asks, "signature": signature(own, sender_name)}


# ================================================================= matching + scanning

def refs_of(msg):
    ids = re.findall(r"<[^<>\s]+>", f"{msg.get('In-Reply-To') or ''} {msg.get('References') or ''}")
    return [i.strip("<>").lower() for i in reversed(ids)]  # nearest first


SENT_CACHE = {}  # uid -> (time, [(row, to, subject, root domain)]) — email sends, newest first


def sent_emails(ws):
    core = C()
    hit = SENT_CACHE.get(ws.uid)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    out = []
    for row in core.M.send_log.find({"uid": ws.uid, "ts": {"$gt": time.time() - 120 * 86400}}).sort("ts", -1).limit(3000):
        e = core.unseal(row["data"], {}) or {}
        to = (e.get("to") or "").lower()
        if e.get("channel", "email") != "email" or "@" not in to or e.get("status") not in ("sent", None):
            continue
        out.append((row, to, norm_subject(e.get("preview") or ""), root_of(to)))
    SENT_CACHE[ws.uid] = (time.time(), out)
    return out


def norm_subject(s):
    s = re.sub(r"^(?:(?:re|fw|fwd|aw|sv|antw)\s*:\s*|\[[^\]]*\]\s*)+", "", s or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def root_of(addr):
    dom = addr.partition("@")[2]
    parts = dom.split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "ac", "gov") and len(parts[-1]) == 2 else 2
    return ".".join(parts[-n:])


def find_sent(ws, msg, addr):
    """The send_log row this email answers, or None.

    1. thread headers (In-Reply-To / References) point at an email Reachout sent;
    2. the sender is an address Reachout emailed;
    3. the sender is at the same company domain and the subject quotes what was sent
       (you wrote to careers@x.com, people@hr.x.com answered "Re: <your subject>")."""
    core = C()
    for ref in refs_of(msg)[:20]:
        row = core.M.send_log.find_one({"uid": ws.uid, "mid": core.lookup_hash("mid:" + ref)})
        if row:
            return row, "thread"
    sends = sent_emails(ws)
    for row, to, _, _ in sends:
        if to == addr:
            return row, "address"
    root = root_of(addr)
    if addr.partition("@")[2] in C().PERSONAL_DOMAINS:
        return None, ""
    subj = norm_subject(str(msg.get("Subject") or ""))
    for row, to, sent_subj, sent_root in sends:
        if sent_root == root and len(sent_subj) >= 15 and sent_subj in subj:
            return row, "company"
    return None, ""


def contact_for(ws, row, addr):
    recips = ws.load("recipients", [])
    rid = row.get("rid") if row else None
    if rid and any(r["id"] == rid for r in recips):
        return rid
    return next((r["id"] for r in recips if (r.get("email") or "").strip().lower() == addr), None)


STAGE_ORDER = {"new": 0, "contacted": 1, "replied": 2, "interested": 3, "not_interested": 3, "won": 4}


def apply_to_contact(ws, rid, rec):
    """Mark the contact Replied (or Interested / Not interested), store reply fields, add a timeline entry."""
    if not rid:
        return None
    info = rec["info"]
    intents = info["intents"]
    target = ("not_interested" if "not_interested" in intents else
              "interested" if {"interested", "interview", "asks_resume", "asks_details"} & set(intents) else "replied")
    with ws.lock:
        rows = ws.load("recipients", [])
        r = next((x for x in rows if x["id"] == rid), None)
        if not r:
            return None
        cur = r.get("stage") or "new"
        if not rec["auto"] and STAGE_ORDER.get(target, 2) > STAGE_ORDER.get(cur, 0) and cur != "won":
            r["stage"] = target
        if not rec["auto"]:
            r["replied_at"] = rec["at"]
            r["reply_intent"] = intents[0]
            r["reply_count"] = int(r.get("reply_count") or 0) + 1
            when = info.get("calendar") or ""
            if when:
                r["follow_up"] = when[:10]
            elif {"asks_resume", "asks_details", "question", "interview"} & set(intents) and not r.get("follow_up"):
                r["follow_up"] = date.today().isoformat()
        ws.save("recipients", rows)
        name = r.get("name") or r.get("email")
    with ws.lock:
        events = ws.events(rid)
        events.insert(0, {"id": C().new_id(), "type": "auto_reply" if rec["auto"] else "replied", "at": datetime.now().isoformat(timespec="seconds"),
                          "text": ("Auto-reply: " if rec["auto"] else "Replied: ") + ", ".join(INTENT_LABEL.get(i, "Replied") for i in intents[:3]),
                          "body": rec["text"][:1500], "reply_id": rec["mid"]})
        ws.save(f"contact:{rid}", events[:500])
    return name


def scan(ws, first_days=21):
    core = C()
    lock = LOCKS.setdefault(ws.uid, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"found": 0, "skipped": "busy"}
    state = ws.load("replies_sync", {})
    found = []
    try:
        profile = ws.profile()
        me = (profile.get("email") or "").lower()
        imap = core.imap_connect(profile)
        try:
            imap.select("INBOX", readonly=True)
            typ, resp = imap.response("UIDVALIDITY")
            validity = resp[0].decode() if resp and resp[0] else ""
            if state.get("validity") == validity and state.get("last_uid"):
                typ, data = imap.uid("SEARCH", None, f"UID {int(state['last_uid']) + 1}:*")
            else:
                since = (date.today() - timedelta(days=first_days)).strftime("%d-%b-%Y")
                typ, data = imap.uid("SEARCH", None, f"SINCE {since}")
            uids = [u for u in (data[0].split() if typ == "OK" and data and data[0] else [])
                    if int(u) > int(state.get("last_uid") or 0) or state.get("validity") != validity]
            last_uid = int(state.get("last_uid") or 0) if state.get("validity") == validity else 0
            for i in range(0, len(uids), 100):
                chunk = uids[i:i + 100]
                typ, parts = imap.uid("FETCH", b",".join(chunk).decode(),
                                      "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO REFERENCES MESSAGE-ID DATE)])")
                wanted = []
                for item in parts if typ == "OK" else []:
                    if not isinstance(item, tuple):
                        continue
                    um = re.search(rb"UID (\d+)", item[0])
                    if not um:
                        continue
                    uid_n = int(um.group(1))
                    last_uid = max(last_uid, uid_n)
                    h = core.email_lib.message_from_bytes(item[1], policy=core.email_policy)
                    _, addr = parseaddr(str(h.get("From") or ""))
                    addr = addr.lower()
                    if not addr or addr == me or re.match(r"(?:mailer-daemon|postmaster)@", addr):
                        continue
                    row, how = find_sent(ws, h, addr)
                    if row:
                        wanted.append((uid_n, row, how))
                for uid_n, row, how in wanted:
                    typ, d = imap.uid("FETCH", str(uid_n), "(BODY.PEEK[]<0.400000>)")
                    raw = next((x[1] for x in d if isinstance(x, tuple)), None) if typ == "OK" else None
                    if raw:
                        rec = ingest(ws, raw, row, how, me)
                        if rec:
                            found.append(rec)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        state.update(validity=validity, last_uid=last_uid, last_ok=datetime.now().isoformat(timespec="seconds"), last_error="")
        ws.save("replies_sync", state)
    except Exception as e:
        state.update(last_error=str(getattr(e, "message", e))[:300])
        ws.save("replies_sync", state)
        raise
    finally:
        lock.release()
    announce(ws, found)
    return {"found": len(found)}


def ingest(ws, raw, row, how, me):
    core = C()
    msg = core.email_lib.message_from_bytes(raw, policy=core.email_policy)
    mid = str(msg.get("Message-ID") or "").strip() or f"{msg.get('From')}|{msg.get('Date')}"
    key = core.lookup_hash("reply:" + mid.lower())
    if core.M.replies.find_one({"uid": ws.uid, "mid": key}, {"_id": 1}):
        return None
    name, addr = parseaddr(str(msg.get("From") or ""))
    addr = addr.lower()
    subject = fa.tidy(str(msg.get("Subject") or ""), 200)
    sent = core.unseal(row["data"], {}) or {}
    # Address-only match must look like an answer, not a newsletter from the same address.
    if how != "thread" and msg.get("List-Unsubscribe"):
        return None
    try:
        when = core.parsedate_to_datetime(str(msg.get("Date"))).astimezone()
    except (TypeError, ValueError):
        when = datetime.now().astimezone()
    if when.timestamp() < row.get("ts", 0) - 60:
        return None  # older than what we sent: not a reply to it
    text, full = reply_text(msg)
    auto = is_auto(msg, subject, text)
    info = understand(text, full, msg, name, addr, me) if not auto else {
        "intents": ["auto_reply"], "emails": [], "phones": [], "meeting_links": [], "links": [], "when": [],
        "calendar": "", "questions": [], "asks": [],
        "signature": {"name": fa.tidy(name, 60)}, "back_on": next(iter(understand(text, full, msg, name, addr, me)["when"]), "")}
    ccs = [a.lower() for _, a in getaddresses([str(msg.get("Cc") or "")]) if a and a.lower() not in (me, addr)]
    rec = {"mid": key, "msgid": mid, "at": when.isoformat(timespec="seconds"), "auto": auto, "how": how, "text": text or full[:1500],
           "info": info, "from": fa.tidy(name, 80), "addr": addr, "subject": subject, "cc": ccs[:5],
           "sent_to": sent.get("to", ""), "sent_subject": sent.get("preview", ""), "sent_at": sent.get("timestamp", ""),
           "refs": list(reversed(refs_of(msg)))[-10:]}
    rid = contact_for(ws, row, addr)
    rec["rid"] = rid
    rec["contact"] = apply_to_contact(ws, rid, rec) if rid else None
    if not auto:
        core.M.send_log.update_one({"_id": row["_id"]}, {"$set": {"replied_at": when.timestamp()}})
    try:
        core.M.replies.insert_one({"uid": ws.uid, "mid": key, "rid": rid, "ts": when.timestamp(), "auto": auto, "handled": False,
                                   "intent": info["intents"][0], "data": core.seal(rec)})
    except Exception:
        return None
    return rec


def announce(ws, found):
    notify = getattr(bridge, "notify", None)
    if not notify:
        return
    real = [r for r in found if not r["auto"]]
    for r in real[:6]:
        who = r.get("contact") or r["info"]["signature"].get("name") or r["from"] or r["addr"]
        comp = r["info"]["signature"].get("company") or fa.company_from_domain(r["addr"])
        labels = [INTENT_LABEL.get(i) for i in r["info"]["intents"] if i in INTENT_LABEL][:2]
        body = (" · ".join(labels) + ": " if labels else "") + fa.tidy(r["text"], 140)
        kind = "offer" if "interested" in r["info"]["intents"] or "interview" in r["info"]["intents"] else "reply"
        notify(ws.uid, f"{who}{f' ({comp})' if comp and comp.lower() not in who.lower() else ''} replied", body,
               f"#replies/{r['mid']}", kind)
    if len(real) > 6:
        notify(ws.uid, f"{len(real) - 6} more replies", "Open Replies to read them.", "#replies", "reply")


# ================================================================= background worker

LAST = {}


def tick():
    core = C()
    now = time.time()
    recent = set(core.M.send_log.distinct("uid", {"ts": {"$gt": now - 60 * 86400}}))
    for uid in recent:
        if now - LAST.get(uid, 0) < 170:
            continue
        job = core.JOBS.get(uid)
        if job and job.running:
            continue  # don't compete with a campaign for the mailbox
        ws = core.Workspace(uid)
        if not ws.profile().get("smtp_password"):
            continue
        LAST[uid] = now
        try:
            bridge.with_deadline(240, scan, ws)
        except Exception as e:
            print(f"[Reachout] reply check skipped for an account: {getattr(e, 'message', e)}", flush=True)


def start_workers():
    bridge.every(60, tick, "reply-watcher")


# ================================================================= API

def out(row):
    d = C().unseal(row["data"], {}) or {}
    return {"id": row["mid"], "handled": row.get("handled", False), "ts": row["ts"], **{k: d.get(k) for k in (
        "at", "auto", "how", "text", "info", "from", "addr", "subject", "cc", "sent_to", "sent_subject", "sent_at", "rid", "contact", "msgid",
        "answered")}}


@bp.get("/api/replies")
@login_required
def list_replies(ws):
    core = C()
    q = {"uid": ws.uid}
    f = request.args.get("f", "")
    if f == "open":
        q.update(handled=False, auto=False)
    elif f == "auto":
        q["auto"] = True
    elif f in INTENT_LABEL:
        q["intent"] = f
    rows = [out(r) for r in core.M.replies.find(q).sort("ts", -1).limit(300)]
    counts = {"all": core.M.replies.count_documents({"uid": ws.uid, "auto": False}),
              "open": core.M.replies.count_documents({"uid": ws.uid, "auto": False, "handled": False}),
              "auto": core.M.replies.count_documents({"uid": ws.uid, "auto": True})}
    for k in INTENT_LABEL:
        if k != "auto_reply":
            counts[k] = core.M.replies.count_documents({"uid": ws.uid, "auto": False, "intent": k})
    sent = core.M.send_log.count_documents({"uid": ws.uid, "mid": {"$exists": True}})
    replied = core.M.send_log.count_documents({"uid": ws.uid, "replied_at": {"$exists": True}})
    st = ws.load("replies_sync", {})
    return jsonify(items=rows, counts=counts, labels=INTENT_LABEL, sent=sent, replied=replied,
                   rate=round(100 * replied / sent) if sent else 0, sync={k: st.get(k) for k in ("last_ok", "last_error")},
                   email_ready=bool(ws.profile().get("smtp_password")))


@bp.post("/api/replies/check")
@login_required
def check_now(ws):
    if not ws.profile().get("smtp_password"):
        raise C().Invalid("Set up email on the Profile page first.")
    if C().rate_limited(("reply-check", ws.uid), 20, 600):
        raise C().Invalid("Checked a lot just now. Try again in a few minutes.", status=429)
    LAST[ws.uid] = time.time()
    r = scan(ws)
    return jsonify(r)


@bp.put("/api/replies/<rid_>")
@login_required
def mark(ws, rid_):
    if not re.fullmatch(r"[0-9a-f]{16,64}", rid_):
        raise C().Invalid("Unknown reply.", status=404)
    handled = bool(C().body().get("handled", True))
    r = C().M.replies.update_one({"uid": ws.uid, "mid": rid_}, {"$set": {"handled": handled}})
    if not r.matched_count:
        raise C().Invalid("Unknown reply.", status=404)
    return jsonify(ok=True, handled=handled)


# ================================================================= answering from Reachout

DETAIL_FIELDS = [  # key, label, how to recognise it being asked for
    ("experience", "Total experience", r"(?:years? of |total |relevant |work )experience|\bexperience\b"),
    ("current_location", "Current location", r"current (?:location|city)|where are you (?:based|located)|\blocation\b"),
    ("preferred_location", "Preferred location", r"preferred (?:job )?location|relocat\w+|open to (?:move|relocat)"),
    ("current_ctc", "Current CTC", r"current (?:ctc|salary|compensation|package)|\bcctc\b"),
    ("expected_ctc", "Expected CTC", r"expected (?:ctc|salary|compensation|package)|\bectc\b|salary expectation"),
    ("notice_period", "Notice period", r"notice period|how soon can you (?:join|start)|when can you (?:join|start)|joining date|\blwd\b"),
    ("availability", "Availability for a call", r"availab\w+|convenient (?:time|slot)|free (?:for|on)|time slot"),
    ("portfolio", "Portfolio / GitHub", r"portfolio|github|projects? (?:link|you)|work samples"),
    ("linkedin", "LinkedIn", r"linkedin"),
]
DETAIL_RX = [(k, lbl, re.compile(rx, re.I)) for k, lbl, rx in DETAIL_FIELDS]


def resume_links(ws):
    """LinkedIn / GitHub / portfolio links embedded in the resume PDF (its clickable links)."""
    import io
    from pypdf import PdfReader
    out = {}
    doc = next((d["name"] for d in ws.documents() if re.search(r"resume|cv", d["name"], re.I)), None)
    if not doc:
        return out
    try:
        for pg in PdfReader(io.BytesIO(ws.doc_bytes(doc))).pages:
            for a in pg.get("/Annots") or []:
                u = str(((a.get_object().get("/A") or {}).get("/URI")) or "")
                if "linkedin.com/in/" in u:
                    out.setdefault("linkedin", u)
                elif "github.com/" in u:
                    out.setdefault("github", u)
                elif u.startswith("http") and not re.search(r"mailto:|google\.|linkedin|github", u):
                    out.setdefault("portfolio", u)
    except Exception:
        pass
    return out


def my_details(ws):
    """What we know about you for answering recruiters: saved answers first, then resume / job preferences."""
    saved = ws.load("reply_details", {})
    if saved.get("_seeded"):
        return saved
    from features import jobs as feature_jobs
    jp = ws.load("job_prefs", {}) or {}
    links = resume_links(ws)
    years = jp.get("years") or 0
    seed = {"experience": f"{years:g}+ years" if years else "", "current_location": "", "preferred_location": ", ".join(jp.get("locations") or []),
            "current_ctc": "", "expected_ctc": "", "notice_period": "", "availability": "Weekdays, 10 am – 7 pm IST",
            "portfolio": " | ".join(v for k, v in links.items() if k in ("portfolio", "github")), "linkedin": links.get("linkedin", ""),
            "_seeded": True}
    try:
        t = feature_jobs.resume_text(ws)
        m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years? of experience", t, re.I)
        if m and not years:
            seed["experience"] = f"{m.group(1)}+ years"
    except Exception:
        pass
    seed.update({k: v for k, v in saved.items() if v})
    ws.save("reply_details", seed)
    return seed


def asked_details(text):
    """The details a reply asks for, in the order the person listed them."""
    hits = []
    for k, lbl, rx in DETAIL_RX:
        m = rx.search(text)
        if m:
            hits.append((m.start(), k, lbl))
    # "Current Location" also matches the generic \blocation\b; keep the first key per position.
    seen, out = set(), []
    for pos, k, lbl in sorted(hits):
        if k not in seen:
            seen.add(k)
            out.append((k, lbl))
    return out


GENERIC_NAME = re.compile(r"\b(?:team|talent|acquisition|recruit\w*|careers?|hr|human resources|people|support|hiring|"
                          r"operations|candidate|global|noreply|no-reply)\b", re.I)


def greeting(rec):
    name = (rec["info"].get("signature") or {}).get("name") or rec.get("from") or ""
    first = name.split()[0] if name and not GENERIC_NAME.search(name) and re.fullmatch(r"[A-Za-z.'’ -]{2,40}", name) else ""
    if first and len(first) > 1:
        return f"Hi {first.title() if first.isupper() or first.islower() else first},"
    comp = (rec["info"].get("signature") or {}).get("company") or fa.company_from_domain(rec.get("addr", ""))
    return f"Hi {comp} team," if comp else "Hello,"


def draft_reply(ws, rec):
    """Subject, body, attachments and missing details for a suggested answer to one reply."""
    profile = ws.profile()
    info, intents = rec["info"], set(rec["info"].get("intents") or [])
    mine = my_details(ws)
    subj = rec.get("subject") or rec.get("sent_subject") or ""
    subject = subj if re.match(r"^\s*re\s*:", subj, re.I) else f"Re: {subj}".strip()
    lines = [greeting(rec), ""]
    missing, docs = [], []
    resume = next((d["name"] for d in ws.documents() if re.search(r"resume|cv", d["name"], re.I)), None)
    wanted = asked_details(rec.get("text") or "") if {"asks_details", "question", "interview"} & intents or info.get("asks") else []
    if "not_interested" in intents:
        lines += ["Thank you for letting me know, and for taking the time to consider my application.",
                  "I'd be glad to be considered for any future openings that match my profile, so please do keep me in mind.",
                  "Wishing you and the team all the best."]
    else:
        opener = ("Thank you for getting back to me, and for considering my profile." if {"interested", "interview", "asks_details", "asks_resume"} & intents
                  else "Thank you for your reply.")
        if not ("referral" in intents and not {"interested", "interview", "asks_details", "asks_resume"} & intents):
            lines += [opener, ""]
        if "interview" in intents:
            if info.get("calendar") or info.get("when"):
                slot = info.get("calendar") and datetime.fromisoformat(info["calendar"]).strftime("%A, %d %B at %I:%M %p").replace(" 0", " ") or info["when"][0]
                lines.append(f"{slot} works well for me, and I'll be there" + (" using the meeting link you shared." if info.get("meeting_links") else ". Please share the meeting link or details when convenient."))
            else:
                avail = mine.get("availability") or ""
                lines.append("I'd be happy to have a conversation." + (f" I'm generally available {avail[0].lower() + avail[1:]}; please let me know a time that suits you." if avail
                                                                          else " Please let me know a time that suits you."))
                if not avail:
                    missing.append("availability")
            lines.append("")
        wanted_keys = [k for k, _ in wanted if k != "availability" or "interview" not in intents]
        if wanted_keys:
            lines.append("Please find the details below:" if len(wanted_keys) > 1 else "Here are the details you asked for:")
            lines.append("")
            for k in wanted_keys:
                lbl = dict((a, b) for a, b, _ in DETAIL_FIELDS)[k]
                val = mine.get(k) or ""
                if not val:
                    missing.append(k)
                    val = f"[add your {lbl if 'CTC' in lbl else lbl.lower()}]"
                lines.append(f"• {lbl}: {val}")
            lines.append("")
        if "asks_resume" in intents or re.search(r"\b(?:resume|cv)\b", " ".join(info.get("asks") or []), re.I):
            if resume:
                docs.append(resume)
                lines += ["I've attached my updated resume for your reference.", ""]
            else:
                missing.append("resume")
        if "referral" in intents:
            lines += ["Thank you for your reply and for pointing me in the right direction. I'll apply through the careers page as suggested"
                      + (" and would appreciate it if you could keep an eye out for my application." if "@" in rec.get("addr", "") else "."), ""]
        if info.get("questions") and not wanted_keys and "interview" not in intents:
            lines += ["To answer your question: [write your answer here]", ""]
            missing.append("answer")
        if {"interested", "asks_details", "asks_resume"} & intents and "interview" not in intents:
            lines.append("I'm very interested in the opportunity and would be glad to discuss how I can contribute. Please let me know the next steps.")
        elif "interview" not in intents and not wanted_keys:
            lines.append("Please let me know if you need anything else from me.")
    sig = [ln for ln in ("", "Best regards,", profile.get("name", ""), " | ".join(x for x in (profile.get("phone"), profile.get("email")) if x),
                         mine.get("linkedin", "")) if ln is not None]
    body = "\n".join(lines).rstrip() + "\n" + "\n".join(sig).rstrip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    to = rec["addr"]
    return {"to": to, "cc": [c for c in (rec.get("cc") or []) if c != to], "subject": subject[:200], "body": body,
            "documents": docs, "missing": missing, "asked": [k for k, _ in wanted], "details": {k: v for k, v in mine.items() if not k.startswith("_")},
            "detail_labels": {k: lbl for k, lbl, _ in DETAIL_FIELDS}}


def reply_row(ws, key):
    core = C()
    if not re.fullmatch(r"[0-9a-f]{16,64}", key or ""):
        raise core.Invalid("Unknown reply.", status=404)
    row = core.M.replies.find_one({"uid": ws.uid, "mid": key})
    if not row:
        raise core.Invalid("That reply no longer exists.", status=404)
    return row, core.unseal(row["data"], {}) or {}


@bp.get("/api/replies/<key>/draft")
@login_required
def get_draft(ws, key):
    _, rec = reply_row(ws, key)
    d = draft_reply(ws, rec)
    return jsonify(**d, available_docs=[x["name"] for x in ws.documents()], original={k: rec.get(k) for k in ("text", "from", "addr", "at", "subject")},
                   answered=rec.get("answered"))


@bp.put("/api/replies/details")
@login_required
def save_details(ws):
    core, p = C(), C().body()
    with ws.lock:
        cur = my_details(ws)
        for k, lbl, _ in DETAIL_FIELDS:
            if k in p:
                cur[k] = core.v_text(p.get(k), k, lbl, 200)
        ws.save("reply_details", cur)
    return jsonify(details={k: v for k, v in cur.items() if not k.startswith("_")})


@bp.post("/api/replies/<key>/send")
@login_required
def send_reply(ws, key):
    """Send your answer from your own mailbox, in the same thread (In-Reply-To / References)."""
    core, p = C(), C().body()
    row, rec = reply_row(ws, key)
    profile = ws.profile()
    if not profile.get("smtp_password"):
        raise core.Invalid("Set up email on the Profile page first.")
    to = core.v_email(p.get("to"), "to", label="To")
    cc = []
    for raw in re.split(r"[,;\s]+", str(p.get("cc") or "")):
        if raw.strip():
            cc.append(core.v_email(raw.strip(), "cc", label="Cc"))
    if len(cc) > 10:
        raise core.Invalid("Up to 10 people in Cc.", "cc")
    subject = core.v_text(p.get("subject"), "subject", "Subject", 200, required=True)
    text = core.v_text(p.get("body"), "body", "Message", 20000, required=True, min_len=5)
    if re.search(r"\[(?:add|write) your [^\]]*\]", text, re.I):
        raise core.Invalid("Fill in the parts in [brackets] first.", "body")
    names = {d["name"] for d in ws.documents()}
    docs = [d for d in (p.get("documents") or []) if isinstance(d, str)]
    if any(d not in names for d in docs):
        raise core.Invalid("One of the selected files no longer exists.", "documents")
    if core.rate_limited(("reply-send", ws.uid), 60, 3600):
        raise core.Invalid("You've sent a lot of replies in the last hour. Try again later.", status=429)
    if ws.sent_today() + 1 + len(cc) > core.DAILY_LIMIT:  # replies count toward the same daily limit as campaigns
        raise core.Invalid(f"You've reached today's limit of {core.DAILY_LIMIT} emails (each person in Cc counts). "
                           "It resets at midnight.", status=429)
    if p.get("quote") and rec.get("text"):
        when = datetime.fromisoformat(rec["at"]).strftime("%a, %d %b %Y at %I:%M %p") if rec.get("at") else ""
        who = rec.get("from") or rec.get("addr")
        text = text.rstrip() + f"\n\nOn {when}, {who} <{rec.get('addr')}> wrote:\n" + "\n".join("> " + ln for ln in rec["text"].split("\n"))
    tmp = Path(core.tempfile.mkdtemp(prefix="reachout-r-"))
    try:
        files = []
        for name in docs:
            data = ws.doc_bytes(name)
            if data is not None:
                (tmp / name).write_bytes(data)
                files.append(tmp / name)
        msg = core.build_email(profile, to, subject, text, files)
        if cc:
            msg["Cc"] = ", ".join(cc)
        orig = (rec.get("msgid") or "").strip()
        if orig:
            orig = orig if orig.startswith("<") else f"<{orig}>"
            msg["In-Reply-To"] = orig
            chain = [f"<{r}>" for r in rec.get("refs") or [] if f"<{r}>" != orig]
            msg["References"] = " ".join(chain[-9:] + [orig])
        try:
            smtp = core.smtp_connect(profile)
            try:
                smtp.send_message(msg)
            finally:
                smtp.quit()
        except core.smtplib.SMTPRecipientsRefused as e:
            code, why = next(iter(e.recipients.values()), (0, b""))
            raise core.Invalid(f"The mail server refused this address: {code} {why.decode(errors='replace')[:160]}", "to")
        except core.smtplib.SMTPAuthenticationError:
            raise core.Invalid("Your mail server refused the login. Check the app password on the Profile page.")
        except (OSError, core.smtplib.SMTPException) as e:
            raise core.Invalid(f"Couldn't send: {str(e).splitlines()[0][:200]}")
    finally:
        core.shutil.rmtree(tmp, ignore_errors=True)
    rid = rec.get("rid")
    ws.log("email", to, rec.get("contact") or rec.get("from") or "", "sent", "Reply sent from Reachout", rid=rid, preview=subject,
           message_id=msg["Message-ID"])
    now = datetime.now().isoformat(timespec="seconds")
    rec["answered"] = {"at": now, "subject": subject, "body": text[:4000], "to": to, "cc": cc, "documents": docs}
    core.M.replies.update_one({"_id": row["_id"]}, {"$set": {"data": core.seal(rec), "handled": True}})
    if rid:
        with ws.lock:
            events = ws.events(rid)
            events.insert(0, {"id": core.new_id(), "type": "answered", "at": now, "text": f"You replied: {subject}", "body": text[:1500]})
            ws.save(f"contact:{rid}", events[:500])
    core.BOUNCE_CHECK_SOON[ws.uid] = time.time() + 120
    SENT_CACHE.pop(ws.uid, None)
    return jsonify(ok=True, at=now)
