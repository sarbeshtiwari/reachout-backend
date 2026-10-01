"""Job matches (from job-alert emails), hiring-post replies, and the scheduled-send queue.

- Job matches: reads job-alert emails (Naukri, LinkedIn, Indeed, Glassdoor…) from the user's own mailbox,
  read-only, and scores each job against their resume and preferences. Nothing is done on job sites.
- Hiring posts: the user pastes (or sends via bookmarklet) a post that asks for CVs by email; the address,
  role and requested subject line are extracted, a reply is drafted, and it is queued to send later.
- Queue: a background loop sends due items (emails, scheduled LinkedIn posts) and records the result.
"""

import hashlib
import html as html_lib
import io
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, jsonify

import bridge
from bridge import login_required

bp = Blueprint("jobs", __name__)


def C():
    return bridge.C


# ================================================================= resume → preferences

SKILLS = ["Python", "JavaScript", "TypeScript", "Java", "C", "C++", "C#", "Go", "Rust", "Dart", "Kotlin", "Swift", "PHP", "Ruby",
          "Scala", "R", "SQL", "React", "Next.js", "Angular", "Vue", "Node.js", "Express", "FastAPI", "Django", "Flask",
          "Spring", "Spring Boot", ".NET", "Flutter", "React Native", "HTML", "CSS", "Tailwind", "Redux", "GraphQL",
          "REST", "MySQL", "PostgreSQL", "MongoDB", "Redis", "Elasticsearch", "Kafka", "AWS", "Azure", "GCP", "Docker",
          "Kubernetes", "Terraform", "CI/CD", "Jenkins", "GitHub Actions", "Linux", "Nginx", "Git", "Machine Learning",
          "Deep Learning", "NLP", "Computer Vision", "LLM", "Generative AI", "GenAI", "Prompt Engineering", "RAG",
          "LangChain", "PyTorch", "TensorFlow", "scikit-learn", "Pandas", "NumPy", "Data Science", "Data Engineering",
          "Spark", "Airflow", "Power BI", "Tableau", "Selenium", "Playwright", "Jest", "Microservices", "System Design",
          "AI Agents", "MLOps", "Hugging Face", "OpenAI", "Figma"]
SKILL_ALIASES = {"Node.js": ["node", "nodejs", "node js"], "Next.js": ["nextjs", "next js"], "React": ["reactjs", "react js", "react.js"],
                 "JavaScript": ["js"], "TypeScript": ["ts"], "PostgreSQL": ["postgres"], "Machine Learning": ["ml"],
                 "Generative AI": ["gen ai", "genai", "generative"], "LLM": ["llms", "large language model", "large language models"],
                 "Kubernetes": ["k8s"], "CI/CD": ["cicd", "ci cd"], "AWS": ["amazon web services"], "Deep Learning": ["dl"],
                 "Express": ["express.js", "expressjs"], "Vue": ["vue.js", "vuejs"], "Angular": ["angularjs"], "C#": ["dotnet c#"]}
CITIES = ["Noida", "Gurugram", "Gurgaon", "Delhi", "New Delhi", "Delhi NCR", "Bengaluru", "Bangalore", "Hyderabad", "Pune",
          "Mumbai", "Navi Mumbai", "Chennai", "Kolkata", "Ahmedabad", "Jaipur", "Lucknow", "Indore", "Chandigarh", "Mohali",
          "Kochi", "Coimbatore", "Trivandrum", "Bhubaneswar", "Nagpur", "Vadodara", "Surat", "Mysore", "Dehradun"]
CITY_SAME = {"gurgaon": "gurugram", "bangalore": "bengaluru", "new delhi": "delhi", "delhi ncr": "delhi"}


def norm_city(c):
    c = c.lower().strip()
    return CITY_SAME.get(c, c)


def skill_hits(text, skills):
    t = f" {re.sub(r'[^a-z0-9+#./ ]+', ' ', (text or '').lower())} "
    out = []
    for s in skills:
        names = [s.lower()] + [a.lower() for a in SKILL_ALIASES.get(s, [])]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", t) for n in names if len(n) > 1 or n in ("c", "r")):
            out.append(s)
    return out


def resume_text(ws):
    from pypdf import PdfReader
    for d in ws.documents():
        if d["name"].lower().endswith(".pdf") and "cover" not in d["name"].lower():
            data = ws.doc_bytes(d["name"])
            try:
                return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
            except Exception:
                return ""
    return ""


def default_prefs(ws):
    text = resume_text(ws)
    skills = [s for s in skill_hits(text, SKILLS) if s not in ("C", "R", "GenAI")][:25]
    years = 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years? of experience", text, re.I)
    if m:
        years = float(m.group(1))
    roles = []
    head = "\n".join(text.splitlines()[:4])
    for part in re.split(r"[|•·/\n]", head):
        part = part.strip()
        if re.search(r"engineer|developer|scientist|analyst|architect|designer", part, re.I) and len(part) < 40:
            roles.append(part)
    return {"roles": roles or ["Software Engineer"], "skills": skills, "years": years, "locations": [],
            "remote_ok": True, "exclude": ["Senior Manager", "Director", "Intern", "Sales", "Recruiter"],
            "min_score": 55, "auto_scan": True}


def prefs(ws):
    p = ws.load("job_prefs", None)
    if p is None:
        p = default_prefs(ws)
        ws.save("job_prefs", p)
    return p


# ================================================================= alert-email parsing

TRACKER_HOSTS = ["indeed.co.in", "glassdoor.co.in", "glassdoor.com"]  # job sites that link from sibling domains
SOURCES = {  # sender domain -> (label, job-link pattern)
    "naukri.com": ("Naukri", re.compile(r"naukri\.com/(?:jd/)?job-listings-", re.I)),
    "linkedin.com": ("LinkedIn", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/\d+", re.I)),
    "indeed.com": ("Indeed", re.compile(r"indeed\.(?:com|co\.in)/(?:rc/clk|viewjob|pagead/clk|m/viewjob)", re.I)),
    "glassdoor.com": ("Glassdoor", re.compile(r"glassdoor\.[a-z.]+/(?:partner/jobListing|job-listing|Job/)", re.I)),
    "glassdoor.co.in": ("Glassdoor", re.compile(r"glassdoor\.[a-z.]+/(?:partner/jobListing|job-listing|Job/)", re.I)),
    "foundit.in": ("foundit", re.compile(r"foundit\.in/(?:job|seeker/job)", re.I)),
    "instahyre.com": ("Instahyre", re.compile(r"instahyre\.com/(?:job|candidate/opportunities)", re.I)),
    "wellfound.com": ("Wellfound", re.compile(r"wellfound\.com/(?:jobs|company/[^/]+/jobs)", re.I)),
}
NOISE = re.compile(r"^(view( all)?( jobs?| details| recommendations)?|apply( now)?|see (all|more)( jobs)?|easy apply|save|"
                   r"are these jobs relevant\??|yes|no|unsubscribe|report a problem|get app|update profile|new|promoted|"
                   r"actively recruiting|\d+(\.\d+)?|\d+ (applicants?|days? ago|hours? ago)|be an early applicant)$", re.I)


class _Walk(HTMLParser):
    """Flatten an email's HTML into [(text, href_or_None)] in reading order."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.href, self.skip = [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        if tag == "a":
            self.href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        if tag == "a":
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        t = re.sub(r"\s+", " ", data).strip()
        if t:
            self.items.append((t, self.href))


def exp_range(*texts):
    for t in texts:
        if not t:
            continue
        m = re.search(r"(\d{1,2})[-_ ]?(?:to|-|–)[-_ ]?(\d{1,2})[-_ ]?(?:yrs?|years?)", t, re.I)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d{1,2})\s*\+\s*(?:yrs?|years?)", t, re.I)
        if m:
            return int(m.group(1)), None
    return None, None


def job_key(source, url):
    parts = urlsplit(url)
    ident = re.search(r"(\d{6,})", parts.path)
    base = f"{source}:{ident.group(1) if ident else parts.netloc + parts.path}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def job_url_ok(href, link_re):
    """A job link must be a plain web link whose host belongs to a known job site. The sender name of an
    email is easy to fake, so the link itself is checked; anything else (javascript:, data:, lookalike
    hosts) is dropped."""
    from urllib.parse import urlsplit
    u = urlsplit(href)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or re.search(r"[\s\"'<>]", href):
        return False
    if not any(host == d or host.endswith("." + d) for d in list(SOURCES) + TRACKER_HOSTS):
        return False
    return bool(link_re.search(u.netloc + u.path + ("?" + u.query if u.query else "")))


def parse_alert(msg, source, link_re):
    """Pull (title, company, location, url, experience) out of one job-alert email."""
    body = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
    if not body:
        return []
    w = _Walk()
    try:
        w.feed(body.get_content())
    except Exception:
        return []
    jobs, cur = [], None
    for text, href in w.items:
        href = html_lib.unescape(href).strip() if href else href
        if href and job_url_ok(href, link_re):
            if NOISE.match(text) or len(text) < 3:
                continue
            if cur and cur["url"] == href:
                continue
            cur = {"title": text[:160], "url": href, "extra": []}
            jobs.append(cur)
        elif cur is not None and len(cur["extra"]) < 4 and not NOISE.match(text) and len(text) < 120:
            cur["extra"].extend(x.strip() for x in re.split(r"\s+[·•|]\s+", text) if x.strip())
    out = []
    for j in jobs:
        extra = [x for x in j["extra"] if not re.fullmatch(r"[\d.,%()+\- ]+", x)]
        company = extra[0] if extra else ""
        location = next((x for x in extra[1:] if re.search("|".join(CITIES + ["remote", "hybrid", "india", "on-?site", "wfh"]), x, re.I)),
                        extra[1] if len(extra) > 1 else "")
        lo, hi = exp_range(urlsplit(j["url"]).path, " ".join(j["extra"]))
        out.append({"title": j["title"], "company": company[:120], "location": location[:120], "url": j["url"][:1500],
                    "exp_min": lo, "exp_max": hi, "source": source})
    return out


def parse_recruiter_mail(msg, source):
    """Naukri-style direct recruiter emails: 'Job | <title> in <location>'."""
    subj = str(msg.get("Subject", ""))
    m = re.search(r"(?:Job|Walk-in interview)\s*\|\s*(.+?)(?:\s+in\s+(.+))?$", subj)
    if not m:
        return []
    text_part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None)
    html_part = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
    body_text = ""
    try:
        if text_part:
            body_text = text_part.get_content()
        elif html_part:
            w = _Walk(); w.feed(html_part.get_content()); body_text = " ".join(t for t, _ in w.items)
    except Exception:
        pass
    links = re.findall(r"https?://[^\s\"'<>]*naukri\.com/[^\s\"'<>]+", html_part.get_content() if html_part else body_text)
    url = next((l for l in links if "job-listings" in l or "/jd/" in l), links[0] if links else "")
    title, _, company = m.group(1).partition(" | ")   # "Title | Company" when the company is given
    lo, hi = exp_range(body_text, url)
    return [{"title": title.strip()[:160], "company": company.strip()[:120], "location": (m.group(2) or "")[:120],
             "url": url[:1500], "exp_min": lo, "exp_max": hi, "source": f"{source} recruiter",
             "snippet": re.sub(r"\s+", " ", body_text)[:1500], "direct": True}]


def source_for(from_header):
    m = re.search(r"@([\w.-]+)", from_header or "")
    dom = (m.group(1) if m else "").lower()
    for d, (label, rx) in SOURCES.items():
        if dom == d or dom.endswith("." + d):
            return label, rx
    return None, None


def scan_alerts(ws, days=7, limit=300):
    """Read job-alert emails (read-only) and store new jobs; returns counts."""
    core = C()
    imap = core.imap_connect(ws.profile())
    seen = set(ws.load("jobs_seen_mail", []))
    jobs = ws.load("jobs", {})
    added = mails = 0
    try:
        imap.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        ids = set()
        for dom in sorted(set(SOURCES)):
            typ, data = imap.search(None, f'(SINCE {since} FROM "{dom}")')
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        for num in sorted(ids, key=int)[-limit:]:
            typ, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)])")
            raw_h = next((d[1] for d in hdr if isinstance(d, tuple)), b"")
            h = core.email_lib.message_from_bytes(raw_h, policy=core.email_policy)
            key = core.lookup_hash("jobmail:" + str(h.get("Message-ID", num)))
            if key in seen:
                continue
            seen.add(key)
            label, rx = source_for(str(h.get("From", "")))
            subj = str(h.get("Subject", ""))
            is_recruiter = bool(re.search(r"(?:Job|Walk-in interview)\s*\|", subj))
            is_alert = bool(re.search(r"job|opening|opportunit|hiring|role|position|recommend", subj, re.I))
            if not label or not (is_alert or is_recruiter):
                continue
            typ, data = imap.fetch(num, "(BODY.PEEK[]<0.500000>)")  # first 500 KB: alerts are small; a huge mail can't eat memory
            raw = next((d[1] for d in data if isinstance(d, tuple)), None)
            if not raw:
                continue
            msg = core.email_lib.message_from_bytes(raw, policy=core.email_policy)
            try:
                received = core.parsedate_to_datetime(str(h.get("Date"))).astimezone().replace(tzinfo=None)
            except (TypeError, ValueError):
                received = datetime.now()
            found = parse_recruiter_mail(msg, label) if is_recruiter else parse_alert(msg, label, rx)
            mails += 1
            for j in found:
                if not j["title"]:
                    continue
                jid = job_key(j["source"], j["url"] or j["title"] + j["company"])
                if jid in jobs:
                    continue
                j.update(id=jid, received=received.isoformat(timespec="seconds"), state="new")
                jobs[jid] = j
                added += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        ws.save("jobs_seen_mail", sorted(seen)[-8000:])
    # The scan took a while: merge into the latest saved list under the lock, so states you changed
    # meanwhile (saved / applied / dismissed) are kept. The scan only ever adds jobs.
    with ws.lock:
        current = ws.load("jobs", {})
        for jid, j in jobs.items():
            current.setdefault(jid, j)
        jobs = current
        if len(jobs) > 1500:  # keep the newest
            jobs = dict(sorted(jobs.items(), key=lambda kv: kv[1].get("received", ""), reverse=True)[:1500])
        ws.save("jobs", jobs)
    ws.update_settings(jobs_scanned_at=datetime.now().isoformat(timespec="seconds"))
    return {"emails": mails, "added": added, "total": len(jobs)}


# ================================================================= scoring

ROLE_SYNONYMS = {
    "full stack": ["full stack", "fullstack", "full-stack", "mern", "mean"],
    "software engineer": ["software engineer", "software developer", "software development engineer", "sde", "sde-1", "sde-2",
                          "sde 1", "sde 2", "developer", "programmer", "engineer"],
    "ai/ml": ["ai", "ml", "machine learning", "llm", "genai", "gen ai", "generative ai", "data scientist", "ai engineer",
              "ml engineer", "nlp", "deep learning"],
    "frontend": ["frontend", "front end", "front-end", "react", "ui developer"],
    "backend": ["backend", "back end", "back-end", "api", "node", "python developer", "java developer"],
    "data": ["data engineer", "data analyst", "data scientist", "analytics"],
}


FAMILY_LABEL = {"full stack": "Full Stack", "ai/ml": "AI/ML", "frontend": "Frontend", "backend": "Backend", "data": "Data"}


def role_families(text):
    t = f" {text.lower()} "
    return {fam for fam, words in ROLE_SYNONYMS.items()
            if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t) for w in words)}


def score_job(j, p):
    title = j.get("title", "")
    hay = f"{title} {j.get('snippet', '')} {j.get('company', '')}"
    reasons, flags = [], []
    if any(x.strip() and x.lower() in title.lower() for x in p.get("exclude", [])):
        return 0, ["Excluded by your filters"], ["excluded"], False
    want = set().union(*[role_families(r) for r in p.get("roles", [])]) if p.get("roles") else set()
    have = role_families(title)
    specific = (want & have) - {"software engineer"}
    if specific:
        role = 35; reasons.append("Role: " + ", ".join(FAMILY_LABEL.get(f, f) for f in sorted(specific)))
    elif want & have:
        role = 22; reasons.append("Software role")
    else:
        role = 0; flags.append("Different role")
    sk = skill_hits(hay, p.get("skills", []))
    skills = 30 if len(sk) >= 3 else 22 if len(sk) == 2 else 15 if sk else 0
    if sk:
        reasons.append("Skills: " + ", ".join(sk[:4]))
    yrs, lo, hi = float(p.get("years") or 0), j.get("exp_min"), j.get("exp_max")
    eligible = True
    if lo is None:
        exp = 12
    elif lo <= yrs + 0.5 and (hi is None or hi + 1 >= yrs):
        exp = 25; reasons.append(f"Experience {lo}–{hi if hi is not None else '+'} yrs fits")
    elif lo <= yrs + 1.5:
        exp = 12; flags.append(f"Asks {lo}+ yrs (slight stretch)")
    else:
        exp = 0; eligible = False; flags.append(f"Needs {lo}+ yrs")
    locs = [norm_city(x) for x in p.get("locations", []) if x.strip()]
    jl = j.get("location", "").lower()
    remote = bool(re.search(r"remote|work from home|wfh", jl))
    if not locs:
        loc = 10
    elif (remote and p.get("remote_ok", True)) or any(c in {norm_city(x) for x in re.split(r"[,/;]|\s-\s", jl)} or c in jl for c in locs):
        loc = 10; reasons.append("Remote" if remote else "Your location")
    else:
        loc = 0; flags.append("Other location")
    score = role + skills + exp + loc
    return score, reasons, flags, eligible and score >= int(p.get("min_score", 55))


# ================================================================= job routes

@bp.get("/api/jobs")
@login_required
def list_jobs(ws):
    p = prefs(ws)
    jobs = ws.load("jobs", {})
    out = []
    for j in jobs.values():
        s, reasons, flags, ok = score_job(j, p)
        url = j.get("url") or ""
        if url and not re.match(r"^https?://[^\s\"'<>]+$", url):
            url = ""  # stored before links were checked: never hand an unsafe link to the page
        out.append({k: v for k, v in j.items() if k != "snippet"} | {"url": url, "score": s, "reasons": reasons, "flags": flags,
                                                                     "eligible": ok})
    out.sort(key=lambda x: (x["state"] == "dismissed", -x["score"], x.get("received", "")), reverse=False)
    return jsonify(jobs=out, prefs=p, scanned_at=ws.settings().get("jobs_scanned_at"))


@bp.post("/api/jobs/scan")
@login_required
def jobs_scan(ws):
    days = C().v_int(C().body().get("days", 7), "days", "Days", 1, 60)
    if C().rate_limited(("jobs-scan", ws.uid), 12, 3600):
        raise C().Invalid("You've scanned several times recently. Try again in a few minutes.", status=429)
    prefs(ws)
    return jsonify(scan_alerts(ws, days))


@bp.put("/api/jobs/prefs")
@login_required
def save_prefs(ws):
    p = C().body()
    def words(key, n=40, ln=60):
        v = p.get(key, [])
        if isinstance(v, str):
            v = re.split(r"[,\n]", v)
        return [str(x).strip()[:ln] for x in v if str(x).strip()][:n]
    out = {"roles": words("roles", 10), "skills": words("skills", 60, 40), "locations": words("locations", 20, 40),
           "exclude": words("exclude", 30, 40), "remote_ok": bool(p.get("remote_ok", True)), "auto_scan": bool(p.get("auto_scan", True)),
           "min_score": C().v_int(p.get("min_score", 55), "min_score", "Match threshold", 20, 95)}
    try:
        out["years"] = max(0.0, min(40.0, float(p.get("years") or 0)))
    except (TypeError, ValueError):
        raise C().Invalid("Years of experience must be a number, e.g. 2 or 2.5.", "years")
    if not out["roles"]:
        raise C().Invalid("Add at least one role you're looking for.", "roles")
    ws.save("job_prefs", out)
    return jsonify(out)


@bp.post("/api/jobs/prefs/reset")
@login_required
def reset_prefs(ws):
    p = default_prefs(ws)
    ws.save("job_prefs", p)
    return jsonify(p)


@bp.put("/api/jobs/<jid>")
@login_required
def set_job_state(ws, jid):
    state = C().body().get("state")
    if state not in ("new", "saved", "applied", "dismissed"):
        raise C().Invalid("Invalid state.")
    with ws.lock:
        jobs = ws.load("jobs", {})
        if jid not in jobs:
            raise C().Invalid("That job is no longer in your list.", status=404)
        jobs[jid]["state"] = state
        if state == "applied":
            jobs[jid]["applied_at"] = datetime.now().isoformat(timespec="seconds")
        ws.save("jobs", jobs)
    return jsonify(ok=True)


def auto_scan_all():
    core = C()
    for u in core.M.users.find({}, {"_id": 1}):
        ws = core.Workspace(u["_id"])
        p = ws.load("job_prefs", None)
        if not p or not p.get("auto_scan") or not ws.profile().get("smtp_password"):
            continue
        last = ws.settings().get("jobs_scanned_at")
        if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < 3300:
            continue
        try:
            r = bridge.with_deadline(300, scan_alerts, ws, days=3, limit=150)
            if r["added"] and getattr(bridge, "notify", None):
                jobs, pf = ws.load("jobs", {}), prefs(ws)
                good = [j for j in jobs.values() if j.get("state") == "new" and score_job(j, pf)[3]]
                if good:
                    bridge.notify(ws.uid, f"{r['added']} new job{'s' if r['added'] != 1 else ''} from your alerts",
                                  f"{len(good)} match your profile.", "#jobs", "job")
        except Exception as e:
            print(f"[Reachout] job scan skipped for an account: {getattr(e, 'message', e)}", flush=True)


# ================================================================= hiring posts

HIRE_ROLE = re.compile(
    r"(?:we(?:'re| are)\s+hiring(?:\s+for)?|hiring(?:\s+for)?|looking\s+for(?:\s+an?)?|opening\s+for(?:\s+an?)?|"
    r"opportunity\s+for(?:\s+an?)?|position\s*[:\-–]|role\s*[:\-–]|job\s+title\s*[:\-–]|designation\s*[:\-–])\s*"
    r"(?:an?\s+|the\s+)?(?:#)?([A-Za-z][A-Za-z0-9/+.#&() \-]{2,70}?)"
    r"(?=\s*(?:\(|\||,|!|\.\s|\n|–|—|\s-\s|\bat\b|\bin\b|\bwith\b|\bfor\b|\bto\b|$))", re.I)
ROLE_WORD = re.compile(r"\b(engineer|developer|analyst|scientist|designer|manager|intern|architect|lead|sde|consultant|"
                       r"administrator|specialist|tester|qa|devops|sre)\b", re.I)
ROLE_PHRASE = re.compile(r"((?:[A-Z][\w/+.#&-]*\s+){0,3}(?:engineer|developer|analyst|scientist|designer|architect|"
                        r"consultant|tester|administrator|specialist|intern|sde(?:[- ]?\d)?|lead)\b)", re.I)
SUBJECT_QUOTED = re.compile(r"mention\s+[\"“'‘]([^\"”'’\n]{3,80})[\"”'’]\s+(?:in|as)\s+(?:the\s+)?subject", re.I)
SUBJECT_HINT = re.compile(r"(?:subject(?:\s+line)?|mention(?:\s+in\s+(?:the\s+)?subject)?)\s*(?:as|:|-|–)?\s*[\"“'‘]?"
                          r"([^\"”'’\n]{4,90}?)[\"”'’]?(?:\s*(?:\n|$|\.(?:\s|$)))", re.I)


def parse_post(text):
    text = str(text or "")[:8000]
    core = C()
    emails = []
    for e in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        e = e.lower().rstrip(".")
        if e not in emails:
            emails.append(e)
    emails.sort(key=lambda e: 0 if re.match(r"(hr|career|job|talent|recruit|hiring|resume|cv|people)", e) else 1)
    role = next((m.group(1) for m in HIRE_ROLE.finditer(text) if ROLE_WORD.search(m.group(1))), "")
    if not role:
        m = ROLE_PHRASE.search(text)
        role = m.group(1) if m else ""
    # drop leading filler words the looser patterns can pick up ("for an AI/ML Engineer" -> "AI/ML Engineer")
    role = re.sub(r"(?i)^(?:(?:looking|hiring|opening|urgent|requirement|for|an?|the|with|of|as|our|new|role|position|post)\s+)+", "", role.strip())
    role = re.sub(r"\s+", " ", re.sub(r"(?i)^(an?|the)\s+", "", role)).strip(" -–:.,")[:80]
    lo, hi = exp_range(text)
    loc = [c for c in CITIES if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    if re.search(r"\bremote\b|work from home|\bwfh\b", text, re.I):
        loc.append("Remote")
    company = ""
    cm = re.search(r"\b(?:at|@|join)\s+([A-Z][A-Za-z0-9&.]+(?:\s+[A-Z][A-Za-z0-9&.]+){0,3})", text)
    if cm and not ROLE_WORD.search(cm.group(1)):
        company = cm.group(1)
    elif emails and emails[0].split("@")[1] not in core.PERSONAL_DOMAINS:
        company = core.company_from_domain(emails[0].split("@")[1])
    sh = SUBJECT_QUOTED.search(text) or SUBJECT_HINT.search(text)
    return {"emails": emails, "email": emails[0] if emails else "", "role": role, "company": company.strip()[:80],
            "location": ", ".join(dict.fromkeys(loc))[:120], "exp_min": lo, "exp_max": hi,
            "subject_hint": sh.group(1).strip() if sh else ""}


POST_TEMPLATE = {
    "name": "Reply to a hiring post",
    "subject": "Application for {role} – {sender_name}",
    "body": """Hi {name},

I came across your post about the {role} opening{at_company} and I'd like to be considered.

<one or two lines about your experience and what you'd bring to the role>

I've attached my resume and cover letter. I'd be glad to share anything else you need.

Best regards,
{sender_name}
{sender_phone} | {sender_email}""",
}


def post_template(ws):
    t = ws.load("post_template", None)
    if not t:
        t = dict(POST_TEMPLATE)
        ws.save("post_template", t)
    return t


def draft_for(ws, info):
    core = C()
    t = post_template(ws)
    fields = {"name": info.get("name") or "", "company": info.get("company") or "", "role": info.get("role") or "the",
              "at_company": f" at {info['company']}" if info.get("company") else ""}
    if not info.get("role"):
        fields["role"] = "open"
    subject = info.get("subject_hint") or core.render(t["subject"], fields, ws.profile())
    return {"subject": subject[:200], "body": core.render(t["body"], fields, ws.profile())}


@bp.post("/api/posts/parse")
@login_required
def posts_parse(ws):
    p = C().body()
    info = parse_post(p.get("text"))
    info["post_url"] = str(p.get("url") or "")[:500] if re.match(r"https?://", str(p.get("url") or "")) else ""
    known = {r.get("email", "").lower(): r for r in ws.load("recipients", []) if r.get("email")}
    c = known.get(info["email"])
    info["already"] = ({"status": c.get("email_status") or "", "when": c.get("email_last") or ""} if c else None)
    if info["email"]:
        info["domain_problem"] = C().domain_problem(info["email"].split("@")[1], {})
    info["draft"] = draft_for(ws, info)
    info["documents"] = [d["name"] for d in ws.documents()]
    return jsonify(info)


@bp.post("/api/posts/draft")
@login_required
def posts_redraft(ws):
    p = C().body()
    return jsonify(draft_for(ws, {k: str(p.get(k) or "")[:120] for k in ("name", "company", "role", "subject_hint")}))


@bp.get("/api/posts/template")
@login_required
def get_post_template(ws):
    return jsonify(post_template(ws))


@bp.put("/api/posts/template")
@login_required
def set_post_template(ws):
    p = C().body()
    t = {"name": POST_TEMPLATE["name"],
         "subject": C().v_template_text(p.get("subject"), "subject", "Subject", 200, True),
         "body": C().v_template_text(p.get("body"), "body", "Message", 5000, True)}
    ws.save("post_template", t)
    return jsonify(t)


# ================================================================= send queue

def queue_doc(ws, kind, due, payload, summary):
    core = C()
    qid = uuid.uuid4().hex[:12]
    core.M.queue.insert_one({"_id": qid, "uid": ws.uid, "kind": kind, "due": due, "status": "queued", "created": time.time(),
                             "attempts": 0, "data": core.seal(payload), "summary": core.seal(summary)})
    return qid


def queue_out(d):
    core = C()
    return {"id": d["_id"], "kind": d["kind"], "due": d["due"], "status": d["status"], "created": d["created"],
            "sent_at": d.get("sent_at"), "detail": d.get("detail", ""), **(core.unseal(d["summary"], {}) or {})}


def parse_due(p):
    if p.get("send_at"):
        try:
            when = datetime.fromisoformat(str(p["send_at"]))
        except ValueError:
            raise C().Invalid("Pick a valid date and time.", "send_at")
        due = when.timestamp()
    else:
        mins = C().v_int(p.get("delay_minutes", 60), "delay_minutes", "Delay", 0, 60 * 24 * 14)
        due = time.time() + mins * 60
    if due > time.time() + 60 * 86400:
        raise C().Invalid("Schedule at most 60 days ahead.", "send_at")
    return max(due, time.time())


@bp.post("/api/queue/email")
@login_required
def queue_email(ws):
    core, p = C(), C().body()
    to = core.v_email(p.get("to"), "to", label="Recipient email")
    subject = core.v_text(p.get("subject"), "subject", "Subject", 200, required=True)
    text = core.v_text(p.get("body"), "body", "Message", 10000, required=True)
    if re.search(r"\{[a-z_]+\}", subject + text):
        raise core.Invalid("The draft still has {placeholders}. Fill them in first.", "body")
    if re.search(r"<[a-z][^<>]{8,}>", text):
        raise core.Invalid("Replace the <…> part of the message with your own words first.", "body")
    docs = [d for d in (p.get("documents") or []) if isinstance(d, str)]
    names = {d["name"] for d in ws.documents()}
    if any(d not in names for d in docs):
        raise core.Invalid("One of the selected files no longer exists.", "documents")
    problem = core.domain_problem(to.split("@")[1], {})
    if problem:
        raise core.Invalid(problem, "to")
    if not ws.profile().get("smtp_password"):
        raise core.Invalid("Set up email on the Profile page first.")
    fields = {"name": core.v_text(p.get("name"), "name", "Name", 120), "company": core.v_text(p.get("company"), "company", "Company", 120),
              "role": core.v_text(p.get("role"), "role", "Role", 120), "post_url": str(p.get("post_url") or "")[:500]}
    with ws.lock:  # make sure the person is in contacts so history and statuses line up
        rows = ws.load("recipients", [])
        row = next((r for r in rows if r.get("email", "").lower() == to), None)
        if row and row.get("email_status") in ("bounced", "invalid"):
            raise core.Invalid("Email to this address bounced before, so it won't be sent again.", "to")
        if not row and len(rows) >= core.MAX_CONTACTS:
            raise core.Invalid(f"You have {core.MAX_CONTACTS} contacts, the most one account can hold. Remove some first.", "to")
        if not row:
            row = {"id": core.new_id(), "name": fields["name"], "company": fields["company"], "email": to,
                   "role": fields["role"], "stage": "new", "list": "Hiring posts"}
            if fields["post_url"]:
                row["post_url"] = fields["post_url"]
            rows.append(row)
            ws.save("recipients", rows)
    due = parse_due(p)
    qid = queue_doc(ws, "email", due, {"to": to, "subject": subject, "body": text, "documents": docs, "rid": row["id"]},
                    {"to": to, "subject": subject, "company": fields["company"], "role": fields["role"], "rid": row["id"]})
    ws.add_event(row["id"], "note", f"Reply to a hiring post queued for {datetime.fromtimestamp(due):%d %b %Y, %H:%M}: “{subject}”.")
    return jsonify(id=qid, due=due, rid=row["id"])


@bp.get("/api/queue")
@login_required
def list_queue(ws):
    rows = C().M.queue.find({"uid": ws.uid}).sort("due", -1).limit(300)
    return jsonify(items=[queue_out(d) for d in rows], now=time.time())


@bp.get("/api/queue/<qid>")
@login_required
def get_queue_item(ws, qid):
    d = C().M.queue.find_one({"_id": qid, "uid": ws.uid})
    if not d:
        raise C().Invalid("Not found.", status=404)
    return jsonify(queue_out(d) | {"payload": C().unseal(d["data"], {})})


@bp.put("/api/queue/<qid>")
@login_required
def edit_queue_item(ws, qid):
    core, p = C(), C().body()
    d = core.M.queue.find_one({"_id": qid, "uid": ws.uid, "status": "queued"})
    if not d:
        raise core.Invalid("This item was already sent or cancelled.", status=409)
    payload, summary = core.unseal(d["data"], {}), core.unseal(d["summary"], {})
    if d["kind"] == "email":
        if "subject" in p:
            payload["subject"] = summary["subject"] = core.v_text(p["subject"], "subject", "Subject", 200, required=True)
        if "body" in p:
            payload["body"] = core.v_text(p["body"], "body", "Message", 10000, required=True)
    elif d["kind"] == "linkedin_post" and "text" in p:
        payload["text"] = core.v_text(p["text"], "text", "Post", 3000, required=True)
        summary["text"] = payload["text"][:140]
    upd = {"data": core.seal(payload), "summary": core.seal(summary)}
    if p.get("send_at") or "delay_minutes" in p:
        upd["due"] = parse_due(p)
    core.M.queue.update_one({"_id": qid, "status": "queued"}, {"$set": upd})
    return jsonify(ok=True)


@bp.post("/api/queue/<qid>/cancel")
@login_required
def cancel_queue_item(ws, qid):
    r = C().M.queue.update_one({"_id": qid, "uid": ws.uid, "status": "queued"}, {"$set": {"status": "cancelled"}})
    if not r.modified_count:
        raise C().Invalid("This item was already sent or cancelled.", status=409)
    return jsonify(ok=True)


@bp.post("/api/queue/<qid>/now")
@login_required
def send_now(ws, qid):
    r = C().M.queue.update_one({"_id": qid, "uid": ws.uid, "status": "queued"}, {"$set": {"due": time.time()}})
    if not r.modified_count:
        raise C().Invalid("This item was already sent or cancelled.", status=409)
    # Send just this item, in the background: the request returns at once instead of holding a server thread.
    threading.Thread(target=run_queue, kwargs={"only": qid}, daemon=True).start()
    d = C().M.queue.find_one({"_id": qid})
    return jsonify(queue_out(d))


def send_queued_email(ws, payload):
    core = C()
    profile = ws.profile()
    if ws.sent_today() >= core.DAILY_LIMIT:
        return None, "Daily sending limit reached; will retry tomorrow"
    tmp = Path(core.tempfile.mkdtemp(prefix="reachout-q-"))
    try:
        files = []
        for name in payload.get("documents", []):
            data = ws.doc_bytes(name)
            if data is not None:
                (tmp / name).write_bytes(data)
                files.append(tmp / name)
        to = payload["to"]
        problem = core.domain_problem(to.split("@")[1], {})
        if problem:
            status, detail, msg = "invalid", problem, None
        else:
            msg = core.build_email(profile, to, payload["subject"], payload["body"], files)
            try:
                smtp = core.smtp_connect(profile)
                try:
                    smtp.send_message(msg)
                finally:
                    smtp.quit()
                status, detail = "sent", ""
            except core.smtplib.SMTPRecipientsRefused as e:
                code, why = next(iter(e.recipients.values()), (0, b""))
                status, detail = "bounced", f"Rejected by the mail server: {code} {why.decode(errors='replace')[:160]}"
            except Exception as e:
                status, detail = "failed", str(e).splitlines()[0]
    finally:
        core.shutil.rmtree(tmp, ignore_errors=True)
    rid = payload.get("rid")
    if rid:
        ws.set_status(rid, "email", status)
    ws.log("email", payload["to"], "", status, detail, rid=rid, preview=payload["subject"],
           message_id=msg["Message-ID"] if status == "sent" and msg else None)
    if status == "sent":
        core.BOUNCE_CHECK_SOON[ws.uid] = time.time() + 120
    return status == "sent", detail or "Sent"


bridge.QUEUE_HANDLERS["email"] = send_queued_email


def run_queue(only=None):
    core = C()
    now = time.time()
    while True:
        d = core.M.queue.find_one_and_update({"status": "queued", "due": {"$lte": now}, **({"_id": only} if only else {})},
                                             {"$set": {"status": "sending", "claimed": now}, "$inc": {"attempts": 1}},
                                             sort=[("due", 1)])
        if not d:
            break
        handler = bridge.QUEUE_HANDLERS.get(d["kind"])
        try:
            ok, detail = (bridge.with_deadline(180, handler, core.Workspace(d["uid"]), core.unseal(d["data"], {}))
                          if handler else (False, "Unknown item"))
        except bridge.Timeout:
            ok, detail = False, "The mail server didn't respond in time. Check your Sent folder before retrying."
        except Exception as e:
            ok, detail = False, str(getattr(e, "message", e)).splitlines()[0][:300]
        if ok is None:  # try again later (e.g. daily limit)
            tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=9, minute=30)
            core.M.queue.update_one({"_id": d["_id"]}, {"$set": {"status": "queued", "due": tomorrow.timestamp(), "detail": detail}})
            continue
        core.M.queue.update_one({"_id": d["_id"]}, {"$set": {"status": "sent" if ok else "failed", "detail": detail,
                                                              "sent_at": time.time()}})
        if getattr(bridge, "notify", None):
            meta = core.unseal(d.get("summary"), {}) or {}
            what = "LinkedIn post" if d["kind"] == "linkedin_post" else f"Email to {meta.get('company') or meta.get('to') or 'recipient'}"
            bridge.notify(d["uid"], f"{what} {'sent' if ok else 'failed'}", detail if not ok else (meta.get("subject") or ""),
                          "#posts", "queue" if ok else "error")
    # Items stuck in "sending" (server stopped mid-send) may or may not have gone out. Re-sending could
    # email someone twice, so they're marked failed with a note instead; you can resend after checking.
    core.M.queue.update_many({"status": "sending", "claimed": {"$lt": now - 600}},
                             {"$set": {"status": "failed", "detail": "Interrupted while sending. Check your Sent folder, then send again if needed."}})


def start_workers():
    C().M.queue.create_index([("status", 1), ("due", 1)])
    C().M.queue.create_index("uid")
    bridge.every(20, run_queue, "send-queue")
    bridge.every(600, auto_scan_all, "job-alert-scan")
