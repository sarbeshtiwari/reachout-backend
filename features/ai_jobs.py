"""Reachout AI job match: read the user's resume, search for openings, rank them, and (when allowed) apply.

The pipeline is a LangChain runnable chain:

    profile (resume + what they want) -> search (LangChain tools) -> rank (Reachout's own engine)
                                      -> optional review by a local open-source model (Ollama)

* Matching runs on our own engine, on our server, with no outside AI service: skills found in the resume vs.
  the job, role family, years of experience asked for, and how closely the resume's wording matches the
  description (TF-IDF cosine). When an Ollama server is reachable (OLLAMA_URL), a local open-source model
  also reads the top matches and writes a short verdict; nothing leaves your own machines either way.
* Search sources, picked by the user: company career sites (public Greenhouse, Lever and Ashby job boards,
  no key needed), Tavily and Brave (each user adds their own key, stored encrypted).
* Auto-apply only touches public application forms on Greenhouse, Lever and Ashby, only for jobs the
  user approved, never logs in anywhere, never guesses answers to questions it can't fill from the user's
  own details, and stops at any CAPTCHA, handing the job back to the user.
"""

import html
import math
import os
import re
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

import requests
from flask import Blueprint, jsonify

import bridge
from bridge import login_required

bp = Blueprint("ai_jobs", __name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "" if os.environ.get("APP_ENV") == "production" else "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
APPLY_PER_DAY = 20
RUN_SECONDS = 8 * 60
MAX_RESULTS = 40
UA = {"User-Agent": "ReachoutJobMatch/1.0 (+https://reachout-web.netlify.app)"}

# Public job boards checked when no company is named (verified to exist; a missing one is simply skipped).
BOARDS = [("greenhouse", s) for s in ("razorpaysoftwareprivatelimited", "groww", "druva", "hackerrank", "gitlab", "stripe",
                                      "databricks", "airbnb", "coinbase", "figma", "dropbox", "twilio", "mongodb", "elastic",
                                      "samsara", "cloudflare", "okta", "datadog", "zscaler")] + \
         [("lever", s) for s in ("meesho", "cred", "paytm", "zeta", "spotify", "palantir")] + \
         [("ashby", s) for s in ("sarvam", "notion", "openai", "linear", "ramp", "supabase", "replit", "posthog", "cohere",
                                 "elevenlabs", "perplexity")]
ATS_URL = re.compile(r"https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?P<gh>[\w-]+)/jobs/(?P<ghid>\d+)"
                     r"|https?://jobs\.lever\.co/(?P<lv>[\w-]+)/(?P<lvid>[0-9a-f-]{36})"
                     r"|https?://jobs\.ashbyhq\.com/(?P<ab>[\w.-]+)/(?P<abid>[0-9a-f-]{36})", re.I)

RUNS, APPLYING = {}, {}
APPLY_LOCK = threading.Lock()      # one browser at a time on this server


def C():
    return bridge.C


def J():
    from features import jobs
    return jobs


def plain(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text)))
    return re.sub(r"\s+", " ", text).strip()


def get_json(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=15, **kw)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


# ================================================================= sources (LangChain tools)

def norm_gh(board, j):
    return {"id": f"gh:{board}:{j['id']}", "ats": "greenhouse", "board": board, "title": j.get("title", ""),
            "company": board.replace("softwareprivatelimited", "").capitalize(), "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""), "text": plain(j.get("content", "")), "posted": (j.get("updated_at") or "")[:10]}


def norm_lever(board, j):
    cat = j.get("categories") or {}
    return {"id": f"lv:{board}:{j['id']}", "ats": "lever", "board": board, "title": j.get("text", ""), "company": board.capitalize(),
            "location": ", ".join(filter(None, [cat.get("location", ""), j.get("workplaceType", "") if j.get("workplaceType") == "remote" else ""])),
            "url": j.get("hostedUrl", ""), "apply_url": j.get("applyUrl", ""),
            "text": plain(" ".join([j.get("descriptionPlain", ""), *[f"{x.get('text', '')} {plain(x.get('content', ''))}" for x in j.get("lists", [])]])),
            "posted": datetime.fromtimestamp(j.get("createdAt", 0) / 1000).date().isoformat() if j.get("createdAt") else ""}


def norm_ashby(board, j):
    return {"id": f"ab:{board}:{j['id']}", "ats": "ashby", "board": board, "title": j.get("title", ""), "company": board.capitalize(),
            "location": ", ".join(filter(None, [j.get("location", ""), "Remote" if j.get("isRemote") else ""])),
            "url": j.get("jobUrl", ""), "apply_url": j.get("applyUrl", ""), "text": plain(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
            "posted": (j.get("publishedAt") or "")[:10]}


def board_jobs(ats, board):
    if ats == "greenhouse":
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
        return [norm_gh(board, j) for j in (d or {}).get("jobs", [])]
    if ats == "lever":
        d = get_json(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"})
        return [norm_lever(board, j) for j in d] if isinstance(d, list) else []
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    return [norm_ashby(board, j) for j in (d or {}).get("jobs", []) if j.get("isListed", True)]


def company_boards(company):
    """Likely board names for a company: 'Acme Labs' -> acme, acmelabs, acme-labs."""
    words = re.findall(r"[a-z0-9]+", company.lower())
    words = [w for w in words if w not in ("pvt", "ltd", "private", "limited", "inc", "llc", "technologies", "india")] or words
    names = {"".join(words), "-".join(words), words[0]} if words else set()
    return [(ats, n) for n in names for ats in ("greenhouse", "lever", "ashby")]


def from_ats_url(url):
    """A job found by web search, read from its job board when it's on Greenhouse, Lever or Ashby."""
    m = ATS_URL.search(url or "")
    if not m:
        return None
    if m.group("gh"):
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group('gh')}/jobs/{m.group('ghid')}")
        return norm_gh(m.group("gh"), d) if d and d.get("id") else None
    if m.group("lv"):
        d = get_json(f"https://api.lever.co/v0/postings/{m.group('lv')}/{m.group('lvid')}")
        return norm_lever(m.group("lv"), d) if d and d.get("id") else None
    jobs = board_jobs("ashby", m.group("ab"))
    return next((j for j in jobs if j["id"].endswith(m.group("abid"))), None)


SEARCH_SITES = "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com)"


def search_query(want):
    parts = [f'"{want["role"]}"', want.get("company", ""), want.get("location", "")]
    return " ".join(p for p in parts if p) + " " + SEARCH_SITES


def web_hits_to_jobs(hits):
    out, seen = [], set()
    for h in hits:
        url = h.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        job = from_ats_url(url)
        if job:
            out.append(job)
        elif re.match(r"https?://", url):  # a job page elsewhere: shown, but applied to by the user
            out.append({"id": "web:" + C().lookup_hash(url)[:16], "ats": "", "board": "", "title": h.get("title", "")[:160],
                        "company": urlsplit(url).hostname.removeprefix("www.").split(".")[0].capitalize(), "location": "",
                        "url": url, "text": plain(h.get("content", ""))[:4000], "posted": ""})
    return out


def make_tools(keys):
    """The search sources as LangChain tools, so the chain (or an agent) can call them the same way."""
    from langchain_core.tools import tool

    @tool
    def career_sites(role: str, company: str = "") -> list:
        """Open positions from public company career sites on Greenhouse, Lever and Ashby."""
        boards = company_boards(company) if company else BOARDS
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [j for js in pool.map(lambda b: board_jobs(*b), boards) for j in js]

    @tool
    def tavily_search(query: str) -> list:
        """Web search with Tavily (user's own key)."""
        r = requests.post("https://api.tavily.com/search", timeout=30, headers={"Authorization": f"Bearer {keys['tavily']}"},
                          json={"query": query, "max_results": 20, "search_depth": "advanced"})
        if r.status_code in (401, 403):
            raise RuntimeError("Tavily didn't accept your API key.")
        r.raise_for_status()
        return web_hits_to_jobs(r.json().get("results", []))

    @tool
    def brave_search(query: str) -> list:
        """Web search with Brave Search (user's own key)."""
        r = requests.get("https://api.search.brave.com/res/v1/web/search", timeout=30, params={"q": query, "count": 20},
                         headers={"X-Subscription-Token": keys["brave"], "Accept": "application/json"})
        if r.status_code in (401, 403, 422):
            raise RuntimeError("Brave didn't accept your API key.")
        r.raise_for_status()
        return web_hits_to_jobs([{"url": x.get("url"), "title": x.get("title"), "content": x.get("description")}
                                 for x in r.json().get("web", {}).get("results", [])])

    return {"career_sites": career_sites, "tavily": tavily_search, "brave": brave_search}


# ================================================================= Reachout's own matching engine

STOP = set("a an and are as at be by for from has have in is it of on or our that the this to we will with you your "
           "who what work working team teams role job about across able experience years year using use".split())


def tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9+#.]{1,30}", (text or "").lower()) if w not in STOP]


def cosine(a, b, idf):
    va = {w: c * idf.get(w, 1.0) for w, c in Counter(a).items()}
    vb = {w: c * idf.get(w, 1.0) for w, c in Counter(b).items()}
    dot = sum(v * vb.get(w, 0) for w, v in va.items())
    na, nb = math.sqrt(sum(v * v for v in va.values())), math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def years_asked(text):
    """Smallest 'N+ years' / 'N-M years' the description asks for, or None."""
    found = []
    for lo, hi in re.findall(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)", text.lower()):
        lo = int(lo)
        if lo <= 20:
            found.append((lo, int(hi) if hi and int(hi) <= 25 else None))
    return min(found, key=lambda x: x[0]) if found else None


GENERIC_TECH = re.compile(r"\b(software|developer|sde|programmer|full[ -]?stack|front[ -]?end|back[ -]?end|web|mobile|platform|"
                          r"product engineer|application engineer)\b", re.I)


NON_TECH = re.compile(r"\b(finance|financial analyst|account(ing|ant)?|sales|marketing|recruit(er|ing)|legal|counsel|partnerships?|"
                      r"business development|strategic|operations manager|customer success|support specialist|hr|people partner)\b", re.I)


def role_fit(wanted, title, want_fam=None):
    """30 = the same kind of role, 15 = a related engineering role, 0 = something else."""
    jm = J()
    if NON_TECH.search(title) and not NON_TECH.search(wanted):
        return 0
    want_fam = want_fam if want_fam is not None else jm.role_families(wanted)
    have = jm.role_families(title)
    words = [w for w in tokens(wanted) if len(w) > 2 and w not in ("developer", "engineer", "senior", "junior")]
    if (want_fam & have) - {"software engineer"} or (words and all(w in title.lower() for w in words)):
        return 30
    if GENERIC_TECH.search(title) and ("software engineer" in want_fam or want_fam & {"full stack", "frontend", "backend"}):
        return 15
    return 0


def rank(profile, jobs):
    """Score every job 0–100 with reasons and gaps (our own engine, no outside service)."""
    jm = J()
    resume_tok = tokens(profile["resume"])
    docs = [tokens(f"{j['title']} {j['text']}") for j in jobs]
    df = Counter(w for d in docs + [resume_tok] for w in set(d))
    n = len(docs) + 1
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
    want_fam = jm.role_families(profile["role"])
    have_skills = set(profile["skills"])
    years = profile["years"]
    loc = profile.get("location", "").lower()
    out = []
    for j, d in zip(jobs, docs):
        title, body = j["title"], j["text"]
        reasons, gaps = [], []
        role = role_fit(profile["role"], title, want_fam)
        if role >= 30:
            reasons.append("Same kind of role")
        elif role:
            reasons.append("Related role")
        job_skills = set(jm.skill_hits(f"{title} {body}", jm.SKILLS)) - {"C", "R"}
        common = sorted(job_skills & have_skills)
        missing = sorted(job_skills - have_skills)
        skills = round(35 * (len(common) / (len(job_skills) + 1)) ** 0.6) if job_skills else 10
        if common:
            reasons.append("Your skills: " + ", ".join(common[:5]))
        gaps += missing[:4]
        ask = years_asked(body)
        if ask is None:
            exp = 12
        elif ask[0] <= years + 0.5 and (ask[1] is None or years <= ask[1] + 1.5):
            exp = 20; reasons.append(f"Asks {ask[0]}{'–' + str(ask[1]) if ask[1] else '+'} yrs: fits")
        elif ask[0] <= years + 1.5:
            exp = 10; gaps.append(f"Asks {ask[0]}+ yrs")
        else:
            exp = 0; gaps.append(f"Needs {ask[0]}+ yrs")
        if ask and years > (ask[1] or ask[0]) + 5:
            exp = min(exp, 8); gaps.append("May be junior for you")
        sim = round(15 * min(cosine(resume_tok, d, idf) / 0.25, 1))
        if re.search(r"\b(principal|staff|director|head of|vp|vice president|chief|distinguished|architect)\b", title, re.I) and years < 8:
            role, gaps = role // 3, gaps + ["Very senior title"]
        elif re.search(r"\b(senior|sr\.?|lead|manager)\b", title, re.I) and years < 4:
            role, gaps = role // 2, gaps + ["Senior title"]
        elif re.search(r"\b(intern|internship|graduate|fresher|new grad)\b", title, re.I) and years >= 2:
            role, gaps = role // 3, gaps + ["Entry-level role"]
        where = j.get("location", "").lower()
        if loc and not (loc in where or "remote" in where or not where):
            sim = max(sim - 6, 0); gaps.append(f"Based in {j['location']}")
        bonus = 5 if profile.get("company") and profile["company"].lower()[:5] in j["company"].lower() else 0
        age = (datetime.now() - datetime.fromisoformat(j["posted"])).days if re.fullmatch(r"\d{4}-\d{2}-\d{2}", j.get("posted") or "") else None
        if age is not None and age > 120:
            bonus -= 8; gaps.append("Posted over 4 months ago")
        score = max(min(role + skills + exp + sim + bonus, 100), 0)
        out.append({**{k: v for k, v in j.items() if k != "text"}, "score": score, "reasons": reasons, "gaps": gaps,
                    "can_apply": j["ats"] in ("greenhouse", "lever", "ashby"), "summary": j["text"][:400]})
    out.sort(key=lambda x: -x["score"])
    return out


def ollama_ready():
    if not OLLAMA_URL:
        return False
    try:
        tags = requests.get(OLLAMA_URL + "/api/tags", timeout=2).json().get("models", [])
        return any(m.get("name", "").split(":")[0] == OLLAMA_MODEL.split(":")[0] for m in tags)
    except (requests.RequestException, ValueError):
        return False


def llm_review(profile, ranked, top=6):
    """A local open-source model reads the best matches and writes a one-line verdict for each (optional)."""
    from langchain_core.output_parsers import JsonOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_ollama import ChatOllama
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a careful career advisor. Judge how well a candidate fits a job. Answer only with JSON: "
                   '{{"fit": <0-100>, "verdict": "<one honest sentence, max 25 words>"}}. Do not invent facts.'),
        ("human", "Candidate wants: {role}, {years} years of experience.\nResume (excerpt):\n{resume}\n\n"
                  "Job: {title} at {company} ({location})\nDescription (excerpt):\n{text}")])
    chain = prompt | ChatOllama(base_url=OLLAMA_URL, model=OLLAMA_MODEL, temperature=0, num_predict=200, format="json", reasoning=False, keep_alive="10m") | JsonOutputParser()
    for j in ranked[:top]:
        try:
            r = chain.invoke({"role": profile["role"], "years": profile["years"], "resume": profile["resume"][:2500], "title": j["title"],
                              "company": j["company"], "location": j["location"] or "not stated", "text": j["summary"] + " " + j.get("_text", "")[:1500]})
            fit = max(0, min(100, int(r.get("fit", j["score"]))))
            j["ai_verdict"] = str(r.get("verdict", ""))[:200]
            j["score"] = round(0.6 * j["score"] + 0.4 * fit)
        except Exception as e:
            print(f"[Reachout] local model review skipped: {e}", flush=True)
            break
    ranked.sort(key=lambda x: -x["score"])
    return ranked


def build_chain(keys, sources):
    """LangChain pipeline: profile -> search (tools) -> rank -> optional local-model review."""
    from langchain_core.runnables import RunnableLambda
    tools = make_tools(keys)

    def search(state):
        found, errors = [], []
        for src in sources:
            try:
                if src == "career_sites":
                    found += tools["career_sites"].invoke({"role": state["profile"]["role"], "company": state["profile"].get("company", "")})
                else:
                    found += tools[src].invoke({"query": search_query(state["profile"])})
            except Exception as e:
                errors.append(f"{ {'tavily': 'Tavily', 'brave': 'Brave'}.get(src, 'Career sites') }: {str(e)[:120]}")
        uniq = {}
        for j in found:
            uniq.setdefault(j["id"], j)
        jobs = list(uniq.values())
        return {**state, "jobs": jobs, "errors": errors}

    def prefilter(state):
        """Cheap first cut so the engine and the model spend their time on plausible jobs."""
        jm, p = J(), state["profile"]
        fam = jm.role_families(p["role"])
        fits = [(role_fit(p["role"], j["title"], fam), j) for j in state["jobs"]]
        keep = [j for f, j in sorted(fits, key=lambda x: -x[0]) if f]
        return {**state, "jobs": keep[:500], "considered": len(state["jobs"])}

    def score(state):
        texts = {j["id"]: j["text"] for j in state["jobs"]}
        ranked = rank(state["profile"], state["jobs"])[:MAX_RESULTS]
        for r in ranked:
            r["_text"] = texts.get(r["id"], "")
        return {**state, "ranked": ranked}

    def review(state):
        engine = "Reachout AI"
        if state.get("use_llm") and ollama_ready():
            state["ranked"] = llm_review(state["profile"], state["ranked"])
            engine = f"Reachout AI + {OLLAMA_MODEL} (local)"
        for r in state["ranked"]:
            r.pop("_text", None)
        return {**state, "engine": engine}

    return RunnableLambda(search) | RunnableLambda(prefilter) | RunnableLambda(score) | RunnableLambda(review)


# ================================================================= runs

def run_match(uid, want, sources, use_llm):
    ws = C().Workspace(uid)
    run = RUNS[uid]
    try:
        resume = J().resume_text(ws) if not want.get("resume") else pdf_text(ws, want["resume"])
        profile = {**want, "resume": resume, "skills": [s for s in J().skill_hits(resume, J().SKILLS) if s not in ("C", "R")]}
        run["skills"] = profile["skills"]
        run["stage"] = "Searching"
        keys = ws.load("ai_keys", {})
        state = build_chain(keys, sources).invoke({"profile": profile, "use_llm": use_llm})
        run.update(state="done", stage="Done", results=state["ranked"], errors=state["errors"], engine=state["engine"],
                   considered=state.get("considered", 0))
    except Exception as e:
        print(f"[Reachout] AI match failed: {e}", flush=True)
        run.update(state="error", stage="Error", error="Something went wrong while matching. Please try again.")
    run["finished"] = time.time()
    with ws.lock:
        ws.save("ai_match", {k: v for k, v in run.items() if k != "thread"})
    from features.notify import notify
    good = sum(1 for r in run.get("results", []) if r["score"] >= 70)
    notify(uid, f"AI job match: {good} strong match{'es' if good != 1 else ''}", f"For {want['role']}. Open the page to review them.",
           "/app/ai-jobs", "info")


def pdf_text(ws, name):
    import io
    from pypdf import PdfReader
    data = ws.doc_bytes(name)
    if not data:
        raise C().Invalid("That resume file no longer exists.", "resume")
    try:
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        raise C().Invalid("We couldn't read that PDF. Upload a text-based resume (not a scanned image).", "resume")


def public_state(ws):
    run = RUNS.get(ws.uid) or ws.load("ai_match", None) or {"state": "idle"}
    keys = ws.load("ai_keys", {})
    applied = ws.load("ai_applied", {})
    out = {k: v for k, v in run.items() if k != "thread"}
    for r in out.get("results", []):
        r["apply"] = applied.get(r["id"])
    return out | {"keys": {"tavily": bool(keys.get("tavily")), "brave": bool(keys.get("brave"))}, "local_model": ollama_ready(),
                  "model_name": OLLAMA_MODEL, "resumes": [d["name"] for d in ws.documents() if d["name"].lower().endswith(".pdf")],
                  "details": ws.load("ai_apply_profile", {}), "consent": bool(ws.settings().get("auto_apply_consent")),
                  "applying": APPLYING.get(ws.uid, {}), "apply_per_day": APPLY_PER_DAY}


@bp.get("/api/ai-jobs")
@login_required
def status(ws):
    return jsonify(public_state(ws))


@bp.post("/api/ai-jobs/match")
@login_required
def start(ws):
    core = C()
    p = core.body()
    want = {"role": core.v_text(p.get("role"), "role", "Job you want", 80, required=True),
            "years": float(core.v_int(p.get("years", 0), "years", "Years of experience", 0, 40)),
            "company": core.v_text(p.get("company"), "company", "Company", 80),
            "location": core.v_text(p.get("location"), "location", "Location", 80),
            "resume": core.v_text(p.get("resume"), "resume", "Resume", 200)}
    if want["resume"] and want["resume"] not in {d["name"] for d in ws.documents()}:
        raise core.Invalid("Pick a resume from your files.", "resume")
    if not want["resume"] and not any(d["name"].lower().endswith(".pdf") for d in ws.documents()):
        raise core.Invalid("Upload your resume as a PDF on the Files page first.", "resume")
    sources = [s for s in (p.get("sources") or ["career_sites"]) if s in ("career_sites", "tavily", "brave")] or ["career_sites"]
    keys = ws.load("ai_keys", {})
    for s in sources:
        if s in ("tavily", "brave") and not keys.get(s):
            raise core.Invalid(f"Add your {s.capitalize()} API key first, or untick it.", "sources")
    if (RUNS.get(ws.uid) or {}).get("state") == "running":
        raise core.Invalid("A search is already running.", status=409)
    if core.rate_limited(("ai-match", ws.uid), 30, 86400):
        raise core.Invalid("You've run 30 searches today. Try again tomorrow.", status=429)
    RUNS[ws.uid] = {"state": "running", "stage": "Reading your resume", "want": want, "sources": sources, "started": time.time(), "results": []}
    threading.Thread(target=run_match, args=(ws.uid, want, sources, bool(p.get("use_llm", True))), daemon=True, name="ai-match").start()
    return jsonify(ok=True)


@bp.put("/api/ai-jobs/keys")
@login_required
def save_keys(ws):
    p = C().body()
    with ws.lock:
        keys = ws.load("ai_keys", {})
        for k in ("tavily", "brave"):
            if k in p:
                v = str(p[k] or "").strip()
                if v and not re.fullmatch(r"[\w\-]{10,200}", v):
                    raise C().Invalid("That doesn't look like an API key.", k)
                keys[k] = v
        ws.save("ai_keys", keys)
    return jsonify(tavily=bool(keys.get("tavily")), brave=bool(keys.get("brave")))


DETAIL_FIELDS = {"linkedin": "LinkedIn", "github": "GitHub", "portfolio": "Portfolio", "current_company": "Current company",
                 "location": "Location", "notice": "Notice period"}


@bp.put("/api/ai-jobs/details")
@login_required
def save_details(ws):
    core, p = C(), C().body()
    out = {}
    for k, label in DETAIL_FIELDS.items():
        v = core.v_text(p.get(k), k, label, 200)
        if v and k in ("linkedin", "github", "portfolio") and not re.match(r"^https://[^\s<>\"']+$", v):
            raise core.Invalid(f"{label} should be a full https:// link.", k)
        out[k] = v
    consent = p.get("consent")
    with ws.lock:
        ws.save("ai_apply_profile", out)
    if consent is not None:
        ws.update_settings(auto_apply_consent=datetime.now().isoformat(timespec="seconds") if consent else "")
    return jsonify(details=out, consent=bool(ws.settings().get("auto_apply_consent")))


# ================================================================= auto-apply (Playwright, public forms only)

FIELD_MAP = [  # (label pattern, value key)
    (r"^first\s*name|given name", "first"), (r"^last\s*name|surname|family name", "last"), (r"^(full\s*)?name\b|^your name", "name"),
    (r"e-?mail", "email"), (r"phone|mobile|contact number", "phone"), (r"linkedin", "linkedin"), (r"github", "github"),
    (r"portfolio|personal (site|website)|^website", "portfolio"), (r"current (company|employer)|^company$|organi[sz]ation", "current_company"),
    (r"^(current )?location|city|where are you (based|located)", "location"), (r"notice period", "notice")]
LABEL_JS = """(e => { const t = x => (x || '').replace(/[✱*]/g, '').replace(/\\s+/g, ' ').trim();
      if (e.labels && e.labels[0] && t(e.labels[0].innerText)) return t(e.labels[0].innerText);
      if (e.getAttribute('aria-label')) return t(e.getAttribute('aria-label'));
      const by = e.getAttribute('aria-labelledby'); if (by && document.getElementById(by)) return t(document.getElementById(by).innerText);
      const box = e.closest('li, fieldset, .application-question, [class*=question], [class*=field], [class*=Field]');
      const head = box && box.querySelector('label, legend, .application-label, .text, [class*=label], [class*=Label]');
      return t(head ? head.innerText : '') || t(e.getAttribute('placeholder')) || 'A question' })"""
CAPTCHA = "iframe[src*='captcha'], iframe[title*='captcha' i], iframe[title*='challenge' i]"


def challenge_visible(page):
    """True when a CAPTCHA puzzle is actually on screen. Invisible checks that run in the background are the site's
    own decision; a visible challenge is for a person, so the job is handed back, never solved or bypassed."""
    for f in page.locator(CAPTCHA).all():
        try:
            box = f.bounding_box()
            if f.is_visible() and box and box["height"] > 120 and box["width"] > 120:
                return True
        except Exception:
            pass
    return False


def apply_url(job):
    if job["ats"] == "lever":
        return job.get("apply_url") or job["url"].rstrip("/") + "/apply"
    if job["ats"] == "ashby":
        return job["url"].rstrip("/") + "/application"
    return job["url"]


def fill_form(page, values, resume_path):
    """Fill what we can from the user's own details; return the labels of required questions left empty."""
    filled = []
    files = page.locator("input[type=file]")
    for i in range(files.count()):
        f = files.nth(i)
        attrs = " ".join(filter(None, [f.get_attribute("name"), f.get_attribute("id"), f.get_attribute("aria-label")])).lower()
        if "cover" in attrs:
            continue
        if "resume" in attrs or "cv" in attrs or i == 0:
            f.set_input_files(resume_path); filled.append("Resume"); break
    for el in page.locator("input:not([type=file]):not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea").all():
        try:
            if not el.is_visible() or el.input_value():
                continue
            label = (el.evaluate("e => (" + LABEL_JS + ")(e)") or "").lower()
        except Exception:
            continue
        for pat, key in FIELD_MAP:
            if re.search(pat, label) and values.get(key):
                el.fill(values[key]); filled.append(label[:40]); break
    missing = page.evaluate("""() => [...document.querySelectorAll('input[required], textarea[required], select[required], [aria-required=true]')]
        .filter(e => e.offsetParent !== null && e.type !== 'file' && e.type !== 'hidden' && !(e.type === 'checkbox' || e.type === 'radio'
                ? document.querySelector(`input[name="${e.name}"]:checked`) : e.value))
        .map(e => (" + LABEL_JS + ")(e).slice(0, 80))""".replace('" + LABEL_JS + "', LABEL_JS))
    return filled, sorted(set(missing))


def apply_one(ws, job, submit=True):
    """Open the job's public application form, fill it and (when nothing is left to answer) submit it."""
    from playwright.sync_api import sync_playwright
    prof, extra = ws.profile(), ws.load("ai_apply_profile", {})
    name = prof.get("name", "").strip()
    first, _, last = name.partition(" ")
    values = {"name": name, "first": first, "last": last or first, "email": prof.get("email") or C().find_user(uid=ws.uid)["email"], "phone": prof.get("phone", ""), **extra}
    resume = ws.load("ai_match", {}).get("want", {}).get("resume") or next(
        (d["name"] for d in ws.documents() if d["name"].lower().endswith(".pdf") and "cover" not in d["name"].lower()), "")
    data = ws.doc_bytes(resume) if resume else None
    if not data:
        return {"state": "needs_you", "detail": "Upload your resume as a PDF first."}
    url = apply_url(job)
    from features.finder import host_is_public
    if not ATS_URL.search(job["url"]) or not host_is_public(urlsplit(url).hostname or ""):
        return {"state": "needs_you", "detail": "This job isn't on a supported application site."}
    with tempfile.TemporaryDirectory(prefix="reachout-apply-") as tmp:
        path = os.path.join(tmp, re.sub(r"[^\w.\-]", "_", resume) or "resume.pdf")
        with open(path, "wb") as f:
            f.write(data)
        with APPLY_LOCK, sync_playwright() as pw:
            args = ["--no-sandbox"] if C().NO_SANDBOX else []
            browser = pw.chromium.launch(headless=True, args=args)
            try:
                page = browser.new_page(user_agent=C().USER_AGENT)
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector("input[type=file]", state="attached", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                if re.search(r"page not found|no longer (open|available|accepting)|position has been filled|job (is )?closed",
                             page.locator("body").inner_text()[:3000], re.I):
                    return {"state": "closed", "url": url, "detail": "This job is no longer open."}
                if job["ats"] == "greenhouse" and page.locator("button:has-text('Apply')").count() and not page.locator("input[type=file]").count():
                    page.locator("button:has-text('Apply')").first.click(); page.wait_for_timeout(1500)
                filled, missing = fill_form(page, values, path)
                filled = [re.sub(r"\s+", " ", f).strip() for f in filled]
                if "Resume" not in filled:
                    return {"state": "needs_you", "detail": "Couldn't find where to attach your resume.", "url": url}
                if missing:
                    return {"state": "needs_you", "url": url, "detail": "Has questions only you can answer: " + "; ".join(missing[:4])}
                if challenge_visible(page):
                    return {"state": "needs_you", "url": url, "detail": "The form shows a CAPTCHA, so finish it yourself."}
                if not submit:
                    return {"state": "ready", "url": url, "detail": f"Filled: {', '.join(filled)}."}
                btn = page.locator("button[type=submit], input[type=submit], button:has-text('Submit application'), button:has-text('Submit')").first
                btn.click()
                page.wait_for_timeout(6000)
                body = page.locator("body").inner_text().lower()
                if re.search(r"thank you|thanks for applying|application (has been )?(received|submitted)|we('ve| have) received", body):
                    return {"state": "applied", "url": url, "detail": "Application submitted."}
                if challenge_visible(page):
                    return {"state": "needs_you", "url": url, "detail": "A CAPTCHA appeared on submit, so finish it yourself."}
                return {"state": "needs_you", "url": url, "detail": "Submitted, but no confirmation appeared. Check the page."}
            finally:
                browser.close()


def record_application(ws, job):
    from features import apps
    with ws.lock:
        rows = ws.load("applications", {})
        aid = apps.app_id(job["company"], job["title"], job["id"])
        now = datetime.now().isoformat(timespec="seconds")
        rows[aid] = {"id": aid, "company": job["company"], "role": job["title"], "job_id": "", "url": job["url"],
                     "portal": f"Auto-apply ({job['ats'].capitalize()})", "created": now,
                     "history": [{"status": "applied", "at": now, "source": "you", "subject": "Applied by Reachout AI"}]}
        apps.recompute(rows[aid])
        ws.save("applications", rows)


def run_apply(uid, picks):
    ws = C().Workspace(uid)
    st = APPLYING[uid]
    try:
        for job in picks:
            st["current"] = job["title"]
            try:
                res = apply_one(ws, job, submit=True)
            except Exception as e:
                print(f"[Reachout] auto-apply error: {e}", flush=True)
                res = {"state": "needs_you", "detail": "The application page didn't load as expected. Apply on the site.", "url": job["url"]}
            res["at"] = time.time()
            with ws.lock:
                done = ws.load("ai_applied", {})
                done[job["id"]] = res
                ws.save("ai_applied", done)
            if res["state"] == "applied":
                record_application(ws, job)
            st["done"] += 1
    finally:
        st["state"] = "done"
        from features.notify import notify
        ok = sum(1 for j in picks if (ws.load("ai_applied", {}).get(j["id"]) or {}).get("state") == "applied")
        notify(uid, f"Reachout AI applied to {ok} job{'s' if ok != 1 else ''}", f"{len(picks) - ok} need you to finish them.",
               "/app/ai-jobs", "application")


@bp.post("/api/ai-jobs/apply")
@login_required
def apply(ws):
    core = C()
    if not ws.settings().get("auto_apply_consent"):
        raise core.Invalid("Turn on auto-apply and confirm first.", "consent")
    ids = [str(x) for x in (core.body().get("ids") or [])][:APPLY_PER_DAY]
    run = ws.load("ai_match", {})
    done = ws.load("ai_applied", {})
    picks = [r for r in run.get("results", []) if r["id"] in ids and r.get("can_apply") and (done.get(r["id"]) or {}).get("state") != "applied"]
    if not picks:
        raise core.Invalid("Pick at least one job that can be applied to automatically.")
    if not ws.profile().get("phone"):
        raise core.Invalid("Add your phone number on the Profile page first; application forms ask for it.", "phone")
    if (APPLYING.get(ws.uid) or {}).get("state") == "running":
        raise core.Invalid("Already applying. Wait for it to finish.", status=409)
    for _ in picks:
        if core.rate_limited(("ai-apply", ws.uid), APPLY_PER_DAY, 86400):
            raise core.Invalid(f"Reachout AI applies to at most {APPLY_PER_DAY} jobs a day.", status=429)
    APPLYING[ws.uid] = {"state": "running", "total": len(picks), "done": 0, "current": ""}
    threading.Thread(target=run_apply, args=(ws.uid, picks), daemon=True, name="ai-apply").start()
    return jsonify(ok=True, total=len(picks))
