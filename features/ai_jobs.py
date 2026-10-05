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
LIVE = {}                          # uid -> {"frame": jpeg bytes, "log": [...], "job": title, "at": t}
HANDOVER = {}                      # uid -> job id of the window left open on the user's Mac
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
                  "applying": APPLYING.get(ws.uid, {}), "apply_per_day": APPLY_PER_DAY,
                  "can_hand_over": can_hand_over(), "handover": HANDOVER.get(ws.uid, ""),
                  "documents": [d["name"] for d in ws.documents() if COVER_DOC.search(d["name"])], "cover_letter": cover_file(ws)[0]}


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
    cover = str(p.get("cover_letter", (ws.load("ai_apply_profile", {}) or {}).get("cover_letter", "auto")))[:200]
    if cover not in ("auto", "none") and cover not in {d["name"] for d in ws.documents()}:
        raise core.Invalid("Pick a cover letter from your files.", "cover_letter")
    out["cover_letter"] = cover
    consent = p.get("consent")
    with ws.lock:
        ws.save("ai_apply_profile", out)
    if consent is not None:
        ws.update_settings(auto_apply_consent=datetime.now().isoformat(timespec="seconds") if consent else "")
    return jsonify(details=out, consent=bool(ws.settings().get("auto_apply_consent")))


# ================================================================= auto-apply (Playwright, public forms only)

# (label pattern, value key). Whole words only and checked against short labels, so a question like
# "employed by us in any capacity?" can never match "city".
FIELD_MAP = [
    (r"^(legal |preferred )?first name$|^given name$", "first"), (r"^(legal )?last name$|^surname$|^family name$", "last"),
    (r"^(full |your )?name$", "name"), (r"^e-?mail( address)?$", "email"),
    (r"^(mobile |cell )?phone( number)?$|^mobile( number)?$|^contact number$", "phone"),
    (r"^((a )?link to |url (of|to) )?(your )?linkedin( profile)?( url| link| page)?$", "linkedin"),
    (r"^((a )?link to |url (of|to) )?(your )?github( profile)?( url| link| page)?$", "github"),
    (r"^((a )?link to |url (of|to) )?(your )?(portfolio|personal website|website|personal site)( url| link)?$", "portfolio"),
    (r"^current (company|employer)( name)?$", "current_company"),
    (r"^(current )?(location|city)( \(city\))?$|^where are you (based|located)\??$", "location"),
    (r"^notice period( \(.*\))?$", "notice")]
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


APPLY_HOSTS = {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.greenhouse.io", "boards.eu.greenhouse.io",
               "jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}


def apply_url(job):
    """The job board's own application form. Many companies show their Greenhouse jobs on their own careers site
    (coinbase.com/careers/…), so Greenhouse jobs always go to Greenhouse's hosted form for that job instead."""
    if job["ats"] == "greenhouse":
        _, board, jid = job["id"].split(":", 2)
        eu = ".eu" if ".eu.greenhouse.io" in job.get("url", "") else ""
        return f"https://job-boards{eu}.greenhouse.io/embed/job_app?for={board}&token={jid}"
    if job["ats"] == "lever":
        return job.get("apply_url") or job["url"].rstrip("/") + "/apply"
    if job["ats"] == "ashby":
        return job["url"].rstrip("/") + "/application"
    return job["url"]


TEXT_BOXES = ("input:not([type=file]):not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])"
              ":not([type=search]):not([role=combobox]):not([aria-autocomplete]):not([aria-haspopup]):not([readonly]), textarea")
COUNTRIES = {"91": "India", "1": "United States", "44": "United Kingdom", "971": "United Arab Emirates", "65": "Singapore",
             "61": "Australia", "49": "Germany", "33": "France", "31": "Netherlands", "353": "Ireland", "966": "Saudi Arabia",
             "974": "Qatar", "60": "Malaysia", "81": "Japan", "86": "China", "880": "Bangladesh", "92": "Pakistan",
             "94": "Sri Lanka", "977": "Nepal", "27": "South Africa", "234": "Nigeria", "254": "Kenya", "55": "Brazil"}


def pick(page, box, text):
    """Type into a searchable dropdown and choose the option that matches the user's own value; never a guess."""
    try:
        box.click(timeout=4000)
        box.fill(text, timeout=4000)
        page.wait_for_timeout(1800)  # options load as you type (city lists come from a server)
        opt = page.locator("[role=option]:visible, [class*=select__option]:visible").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\b", re.I)).first
        if opt.count():
            opt.click(timeout=4000)
            page.wait_for_timeout(600)
            return True
        box.fill("", timeout=2000)
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def fill_pickers(page, values):
    """Country, city and the phone's country code, from the user's own details."""
    done = []
    country, city = values.get("country", ""), values.get("location", "").split(",")[0].strip()
    # Read the dropdowns first, then act on each by id: choosing an option re-renders the form.
    boxes = page.evaluate("""() => [...document.querySelectorAll('input[class*=select__input], input[role=combobox]')]
        .filter(e => e.id && e.offsetParent !== null)
        .map(e => [e.id, (""" + LABEL_JS + """)(e).toLowerCase(),
                   !!e.closest('[class*=select__control], [class*=-control]')?.querySelector('[class*=single-value], [class*=singleValue]')])""")
    for bid, label, already in boxes:
        label = label.strip(" ?:")
        if already:
            continue
        box = page.locator(f"[id='{bid}']")
        if re.fullmatch(r"country( of residence)?", label) and country and pick(page, box, country):
            done.append("country")
        elif re.fullmatch(r"(current )?(location|city)( \(city\))?", label) and city and pick(page, box, city):
            done.append("city")
    flag = page.locator(".iti__selected-country, .iti__selected-flag, .iti__flag-container button").first
    if country and values.get("dial") and flag.count():
        try:
            if f"+{values['dial']}" not in (flag.get_attribute("title") or "") + flag.inner_text():
                flag.click(timeout=4000)
                page.locator(".iti__search-input").first.fill(country, timeout=4000)
                page.wait_for_timeout(500)
                item = page.locator(f".iti__country[data-dial-code='{values['dial']}']").first
                if item.count():
                    item.click(timeout=4000); done.append("phone country code")
                else:
                    page.keyboard.press("Escape")
        except Exception:
            pass
    return done


class Live:
    """Shows what the browser is doing: a screenshot after each step, plus a short log (for the live view)."""

    def __init__(self, uid, job):
        self.uid = uid
        LIVE[uid] = {"frame": b"", "log": [], "job": f"{job['title']} · {job['company']}", "at": time.time(), "url": ""}

    def __call__(self, page, step=""):
        st = LIVE.get(self.uid)
        if st is None:
            return
        if step:
            st["log"] = (st["log"] + [step])[-14:]
        try:
            st["frame"] = page.screenshot(type="jpeg", quality=55, timeout=5000)
            st["url"] = page.url
        except Exception:
            pass
        st["at"] = time.time()


REMAINING_JS = """(mark) => [...document.querySelectorAll('input[required], textarea[required], select[required], [aria-required=true]')]
        .filter(e => {
            if (e.type === 'file' || e.type === 'hidden') return false;
            const pickerHasValue = x => !!x.closest('[class*=select__control], [class*=-control]')?.querySelector('[class*=single-value], [class*=singleValue], [class*=multi-value]');
            if (e.matches('[class*=select__input], [role=combobox]') && pickerHasValue(e)) return false;
            if (e.getAttribute('aria-hidden') === 'true' && e.parentElement && pickerHasValue(e.parentElement.querySelector('input:not([aria-hidden])') || e)) return false;
            if (!e.matches('input, textarea, select'))  // a group, like an upload box: answered when a file is attached
                return !([...e.querySelectorAll('input[type=file]')].some(f => f.files.length) || /\\.(pdf|docx?|txt|rtf)\\b/i.test(e.innerText));
            if (e.offsetParent === null && e.getAttribute('aria-hidden') !== 'true') return false;
            return !(e.type === 'checkbox' || e.type === 'radio' ? document.querySelector(`input[name="${e.name}"]:checked`) : e.value);
        })
        .map(e => { if (mark) (e.closest('.select-shell, .field, .file-upload, fieldset, li') || e.parentElement || e).style.outline = '2px solid #e5484d';
            return (" + LABEL_JS + ")(e).slice(0, 70) + (e.getAttribute('aria-hidden') === 'true' ? ' (pick from the list)' : '') })""".replace('" + LABEL_JS + "', LABEL_JS)


# ================================================================= the user's answer bank (supervised)
# Answers the user wrote, or answers Reachout learned from forms they filled in themselves. Learned and drafted
# answers wait for the user's approval ("review"); only approved answers are ever typed into a form.

ANS_STOP = set("a an the to of in for and or your you are is do does did have has with this that what which how why "
               "please describe tell us about at on any".split())


def norm_q(text, company=""):
    t = (text or "").lower().replace("✱", " ").replace("*", " ")
    t = re.sub(r"\((optional|required)\)|select\.\.\.", " ", t)
    if company and len(company) > 2:
        t = re.sub(rf"\b{re.escape(company.lower())}\b", " {company} ", t)
    t = re.sub(r"[^a-z0-9{} ]+", " ", t)
    return " ".join(t.split())


def q_score(a, b):
    if a == b:
        return 1.0
    from difflib import SequenceMatcher
    ta = {w for w in a.split() if w not in ANS_STOP and len(w) > 1}
    tb = {w for w in b.split() if w not in ANS_STOP and len(w) > 1}
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return max(jac, SequenceMatcher(None, a, b).ratio() * 0.95)


def same_company(a, b):
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    return bool(a and b and (a in b or b in a))


def answer_for(bank, label, company):
    """The user's approved answer to this question, or None. Company-specific answers only go to that company."""
    q = norm_q(label, company)
    if len(q) < 2:
        return None
    best, best_s = None, 0.0
    for a in bank.values():
        if a.get("status") != "approved" or not a.get("answer"):
            continue
        if a.get("company") and not same_company(a["company"], company):
            continue
        sc = q_score(q, a.get("norm") or norm_q(a["question"], a.get("company", "")))
        if sc > best_s:
            best, best_s = a, sc
    if best and best_s >= 0.8:
        return best["answer"].replace("{company}", company or "your company")
    return None


def save_answer(ws, question, answer, company="", status="approved", source="you", learned_from="", aid=None):
    with ws.lock:
        bank = ws.load("ai_answers", {})
        norm = norm_q(question, company)
        if not aid:  # same question already saved: update it instead of adding a copy
            aid = next((k for k, v in bank.items() if v.get("norm") == norm and (v.get("company") or "").lower() == (company or "").lower()), None)
        if aid and aid in bank and source == "learned" and bank[aid].get("status") == "approved" and bank[aid].get("answer") == answer:
            return bank[aid]
        aid = aid or C().new_id()
        old = bank.get(aid, {})
        if source == "learned" and old.get("status") == "approved" and old.get("answer") != answer:
            status = "review"  # the user answered differently this time: ask which one to keep
        bank[aid] = {"id": aid, "question": question[:300], "answer": answer[:4000], "company": company[:80], "norm": norm,
                     "status": status, "source": source, "learned_from": learned_from[:120], "updated": time.time(),
                     "uses": old.get("uses", 0)}
        ws.save("ai_answers", bank)
        return bank[aid]


SNAPSHOT_JS = """() => {
  const t = x => (x || '').replace(/[✱*]/g, '').replace(/\\s+/g, ' ').trim();
  const lab = e => (""" + LABEL_JS + """)(e);
  const out = [];
  document.querySelectorAll('textarea, input[type=text], input:not([type])').forEach(e => {
    if (e.offsetParent === null || e.matches('[class*=select__input], [role=combobox], [type=search]') || !e.value.trim()) return;
    out.push([lab(e), e.value.trim()]);
  });
  document.querySelectorAll('[class*=select__control], [class*=-control]').forEach(c => {
    const v = c.querySelector('[class*=single-value], [class*=singleValue]'); const i = c.querySelector('input');
    if (v && i && !c.closest('.iti')) out.push([lab(i), t(v.innerText)]);
  });
  document.querySelectorAll('select').forEach(e => { if (e.offsetParent !== null && e.value && e.selectedIndex > 0) out.push([lab(e), t(e.options[e.selectedIndex].text)]); });
  const seen = new Set();
  document.querySelectorAll('input[type=radio]:checked, input[type=checkbox]:checked').forEach(e => {
    const box = e.closest('fieldset, [role=radiogroup], [role=group], li, [class*=question], [class*=field]');
    const q = box ? t((box.querySelector('legend, label, [class*=label]') || {}).innerText) : '';
    const a = e.type === 'checkbox' && box && box.querySelectorAll('input[type=checkbox]').length === 1 ? 'Yes' : t(e.labels && e.labels[0] ? e.labels[0].innerText : e.value);
    const k = q + '|' + e.name; if (!q || seen.has(k)) return; seen.add(k); out.push([q, a]);
  });
  document.querySelectorAll('button[aria-pressed=true], [role=radio][aria-checked=true]').forEach(e => {
    const box = e.closest('fieldset, [role=radiogroup], [class*=question], [class*=field]');
    const q = box ? t((box.querySelector('legend, label, [class*=label]') || {}).innerText) : '';
    if (q) out.push([q, t(e.innerText)]);
  });
  return out.filter(([q, a]) => q && a && q.length < 300);
}"""


def learn_from_page(ws, page, job):
    """Remember the answers the user typed themselves in this form, for their review (never used before approval)."""
    try:
        pairs = page.evaluate(SNAPSHOT_JS)
    except Exception:
        return 0
    n = 0
    for q, a in pairs:
        ql = q.lower().strip(" ?:")
        if any(re.search(pat, ql) for pat, _ in FIELD_MAP) or re.search(r"resume|cover letter|password|captcha", ql):
            continue  # contact details and files come from the profile already
        specific = bool(job.get("company")) and job["company"].lower() in q.lower()
        save_answer(ws, q, a, job["company"] if specific else "", status="review", source="learned",
                    learned_from=f"{job['company']} · {job['title']}")
        n += 1
    return n


def fill_from_bank(page, answers, company, live):
    """Answer questions with the user's approved answers: text, dropdowns, radio groups and Yes/No buttons."""
    done = []
    if not answers:
        return done
    for el in page.locator(TEXT_BOXES).all():
        try:
            if not el.is_visible() or el.input_value():
                continue
            label = el.evaluate("e => (" + LABEL_JS + ")(e)") or ""
            ans = answer_for(answers, label, company)
            if ans:
                el.scroll_into_view_if_needed(timeout=2000)
                el.fill(ans, timeout=5000); done.append(label[:60]); live(page, f"Answered “{label[:50]}”")
        except Exception:
            continue
    boxes = page.evaluate("""() => [...document.querySelectorAll('input[class*=select__input], input[role=combobox]')]
        .filter(e => e.id && e.offsetParent !== null && !e.closest('.iti')
                && !e.closest('[class*=select__control], [class*=-control]')?.querySelector('[class*=single-value], [class*=singleValue]'))
        .map(e => [e.id, (""" + LABEL_JS + """)(e)])""")
    for bid, label in boxes:
        ans = answer_for(answers, label, company)
        if ans and pick(page, page.locator(f"[id='{bid}']"), ans):
            done.append(label[:60]); live(page, f"Answered “{label[:50]}”")
    for sel in page.locator("select").all():
        try:
            if not sel.is_visible() or sel.evaluate("e => e.selectedIndex > 0"):
                continue
            label = sel.evaluate("e => (" + LABEL_JS + ")(e)") or ""
            ans = answer_for(answers, label, company)
            if ans:
                sel.select_option(label=ans, timeout=3000); done.append(label[:60]); live(page, f"Answered “{label[:50]}”")
        except Exception:
            continue
    groups = page.evaluate("""() => {
        const t = x => (x || '').replace(/[✱*]/g, '').replace(/\\s+/g, ' ').trim(); const out = []; let n = 0;
        document.querySelectorAll('fieldset, [role=radiogroup]').forEach(g => {
          if (g.offsetParent === null || g.querySelector('input:checked, [aria-checked=true], [aria-pressed=true]')) return;
          const q = t((g.querySelector('legend, label, [class*=label]') || {}).innerText);
          if (!q) return; g.setAttribute('data-ro-group', String(++n)); out.push([String(n), q]);
        });
        return out; }""")
    for gid, label in groups:
        ans = answer_for(answers, label, company)
        if not ans:
            continue
        g = page.locator(f"[data-ro-group='{gid}']")
        opt = g.locator("label, button, [role=radio]").filter(has_text=re.compile(rf"^\s*{re.escape(ans)}\s*$", re.I)).first
        try:
            if opt.count():
                opt.scroll_into_view_if_needed(timeout=2000); opt.click(timeout=3000)
                done.append(label[:60]); live(page, f"Answered “{label[:50]}”")
            elif re.fullmatch(r"yes|i agree|agree|true", ans.strip(), re.I) and g.locator("input[type=checkbox]").count() == 1:
                g.locator("input[type=checkbox]").first.check(timeout=3000); done.append(label[:60])
        except Exception:
            continue
    return done


def questions_left(page, mark=False):
    """Required questions still unanswered, one entry per question (a dropdown's hidden copy isn't counted twice)."""
    out = {}
    missing = page.evaluate(REMAINING_JS, mark)
    for m in missing:
        hidden = m.endswith(" (pick from the list)")
        base = re.sub(r"\s*select\.\.\.$", "", m.removesuffix(" (pick from the list)"), flags=re.I).strip()
        if base == "A question" and len(missing) > 1:
            continue
        key = base.lower()
        if hidden and key in out:
            continue
        out.setdefault(key, base + (" (choose from the list)" if hidden else ""))
    return sorted(out.values())


def file_input_label(f):
    try:
        return (" ".join(filter(None, [f.get_attribute("name"), f.get_attribute("id"), f.get_attribute("aria-label")])) + " "
                + (f.evaluate("e => (" + LABEL_JS + ")(e)") or "")).lower()
    except Exception:
        return ""


def fill_form(page, values, resume_path, live=lambda page, step="": None, answers=None, company="", cover=None):
    """Fill what we can from the user's own details; return the labels of required questions left empty.

    cover = {"path": file to attach, "text": the letter's text} when the user has a cover letter to send."""
    filled = []
    files = page.locator("input[type=file]")
    for i in range(files.count()):
        f = files.nth(i)
        attrs = file_input_label(f)
        if "cover" in attrs:
            continue
        if "resume" in attrs or "cv" in attrs or i == 0:
            f.set_input_files(resume_path); filled.append("Resume"); live(page, "Attached your resume"); break
    if cover and cover.get("path"):
        for i in range(files.count()):
            f = files.nth(i)
            try:
                if "cover" in file_input_label(f) and not f.evaluate("e => e.files.length"):
                    f.set_input_files(cover["path"]); filled.append("Cover letter"); live(page, "Attached your cover letter"); break
            except Exception:
                continue
    if filled:  # some forms read the resume and refill (and reset) the fields; let that finish first
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    for el in page.locator(TEXT_BOXES).all():
        try:
            if not el.is_visible() or el.input_value():
                continue
            if el.evaluate("e => e.type !== 'tel' && !!e.closest('[class*=select__], [class*=-select], [class*=dropdown], [role=listbox]')"):
                continue  # a dropdown that only looks like a text box: answers there are the user's call
            label = (el.evaluate("e => (" + LABEL_JS + ")(e)") or "").lower().strip(" ?:")
        except Exception:
            continue
        if cover and cover.get("text") and re.fullmatch(r"(your |a )?cover letter( \(optional\))?|cover letter / message|message to (the )?hiring (team|manager)", label):
            try:
                el.scroll_into_view_if_needed(timeout=2000)
                el.fill(cover["text"], timeout=8000); filled.append("Cover letter (pasted)"); live(page, "Pasted your cover letter")
            except Exception:
                pass
            continue
        if len(label) > 40:
            continue  # a real question, not a contact field
        for pat, key in FIELD_MAP:
            if re.search(pat, label) and values.get(key):
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.fill(values[key], timeout=5000); filled.append(label)
                    live(page, f"Filled {label}")
                except Exception:
                    pass
                break
    picked = fill_pickers(page, values)
    filled += picked
    if picked:
        live(page, "Chose " + ", ".join(picked))
    from_bank = fill_from_bank(page, answers, company, live)
    filled += [f"answer: {x}" for x in from_bank]
    missing = questions_left(page)
    return filled, missing


def apply_values(ws):
    prof, extra = ws.profile(), ws.load("ai_apply_profile", {})
    name = prof.get("name", "").strip()
    first, _, last = name.partition(" ")
    dial = str(ws.settings().get("country_code") or "").lstrip("+")
    return {"name": name, "first": first, "last": last or first, "email": prof.get("email") or C().find_user(uid=ws.uid)["email"],
            "phone": prof.get("phone", ""), "dial": dial, "country": COUNTRIES.get(dial, ""), **extra}


COVER_DOC = re.compile(r"\.(pdf|docx?|txt|rtf)$", re.I)


def cover_file(ws):
    """(name, bytes) of the cover letter to attach, or ("", None). 'auto' = a file in Files with "cover" in its name."""
    choice = (ws.load("ai_apply_profile", {}) or {}).get("cover_letter", "auto")
    if choice == "none":
        return "", None
    names = [d["name"] for d in ws.documents()]
    name = choice if choice in names else next((n for n in names if "cover" in n.lower() and COVER_DOC.search(n)), "")
    return (name, ws.doc_bytes(name)) if name else ("", None)


def cover_text(name, data):
    """The letter's text, for forms that ask you to paste it instead of attaching a file."""
    import io
    if not data:
        return ""
    try:
        if name.lower().endswith(".pdf"):
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        elif name.lower().endswith(".txt"):
            text = data.decode("utf-8", errors="replace")
        else:
            return ""
    except Exception:
        return ""
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text)).strip()[:6000]


def write_cover(ws, tmp):
    name, data = cover_file(ws)
    if not data:
        return None
    path = os.path.join(tmp, re.sub(r"[^\w.\-]", "_", name) or "cover-letter.pdf")
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "text": cover_text(name, data), "name": name}


def resume_file(ws):
    resume = ws.load("ai_match", {}).get("want", {}).get("resume") or next(
        (d["name"] for d in ws.documents() if d["name"].lower().endswith(".pdf") and "cover" not in d["name"].lower()), "")
    return resume, (ws.doc_bytes(resume) if resume else None)


def can_hand_over():
    """A visible browser window can only be opened when Reachout runs on the user's own computer."""
    import platform
    return not C().PRODUCTION and (platform.system() in ("Darwin", "Windows") or bool(os.environ.get("DISPLAY")))


# The site decided the submission came from an automated browser. Reachout respects that: it stops, never retries or
# hides itself, remembers the site, and hands the user a kit to apply from their own browser.
BLOCKED = re.compile(r"flagged as (possible )?spam|couldn.t submit your application|unusual (activity|traffic)|"
                     r"automated (requests|traffic|submission)|are you a (robot|human)|verify you are human|access denied", re.I)
BLOCKED_RESULT = {"state": "blocked", "detail": "This site flagged the automated submission. Apply from your own browser; "
                                                "Reachout has your answers ready to copy."}


def board_key(job):
    return f"{job.get('ats', '')}:{job.get('board', '')}"


def mark_blocked(ws, job):
    with ws.lock:
        b = ws.load("ai_blocked_boards", {})
        b[board_key(job)] = time.time()
        ws.save("ai_blocked_boards", b)


def is_blocked_board(ws, job):
    return board_key(job) in ws.load("ai_blocked_boards", {})


CONFIRMED = re.compile(r"thank you for applying|thanks for applying|application (has been )?(received|submitted)|"
                       r"we('ve| have) received your application|successfully submitted", re.I)


def open_form(page, job, url, live):
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    live(page, "Opened the application form")
    try:  # the form only reacts to a file or typing once its scripts have loaded
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    try:
        page.wait_for_selector("input[type=file]", state="attached", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1000)
    if re.search(r"page not found|no longer (open|available|accepting)|position has been filled|job (is )?closed",
                 page.locator("body").inner_text()[:3000], re.I):
        return False
    if job["ats"] == "greenhouse" and page.locator("button:has-text('Apply')").count() and not page.locator("input[type=file]").count():
        page.locator("button:has-text('Apply')").first.click(); page.wait_for_timeout(1500)
    return True


def apply_one(ws, job, submit=True, visible=False):
    """Open the job's public application form, fill it and (when nothing is left to answer) submit it.

    visible=True (only when Reachout runs on the user's computer) does it in a browser window they can watch."""
    from playwright.sync_api import sync_playwright
    if is_blocked_board(ws, job):
        return {**BLOCKED_RESULT, "url": job["url"]}
    values = apply_values(ws)
    resume, data = resume_file(ws)
    if not data:
        return {"state": "needs_you", "detail": "Upload your resume as a PDF first."}
    url = apply_url(job)
    from features.finder import host_is_public
    if (urlsplit(url).hostname or "") not in APPLY_HOSTS or not host_is_public(urlsplit(url).hostname or ""):
        return {"state": "needs_you", "detail": "This job isn't on a supported application site."}
    live = Live(ws.uid, job)
    with tempfile.TemporaryDirectory(prefix="reachout-apply-") as tmp:
        path = os.path.join(tmp, re.sub(r"[^\w.\-]", "_", resume) or "resume.pdf")
        with open(path, "wb") as f:
            f.write(data)
        cover = write_cover(ws, tmp)
        with APPLY_LOCK, sync_playwright() as pw:
            args = ["--no-sandbox"] if C().NO_SANDBOX else []
            browser = pw.chromium.launch(headless=not (visible and can_hand_over()), args=args, slow_mo=120 if visible else 0)
            try:
                page = browser.new_page(user_agent=C().USER_AGENT, viewport={"width": 1100, "height": 860})
                if not open_form(page, job, url, live):
                    live(page, "This job is no longer open")
                    return {"state": "closed", "url": url, "detail": "This job is no longer open."}
                filled, missing = fill_form(page, values, path, live, ws.load("ai_answers", {}), job.get("company", ""), cover)
                filled = [re.sub(r"\s+", " ", f).strip() for f in filled]
                if "Resume" not in filled:
                    live(page, "Couldn't find where to attach your resume")
                    return {"state": "needs_you", "detail": "Couldn't find where to attach your resume.", "url": url}
                if missing:
                    more = f" and {len(missing) - 4} more" if len(missing) > 4 else ""
                    live(page, f"Stopped: {len(missing)} question{'s' if len(missing) != 1 else ''} only you can answer")
                    return {"state": "needs_you", "url": url, "questions": missing[:40], "filled": filled,
                            "detail": f"Filled {len(filled)} fields. Questions only you can answer: " + "; ".join(missing[:4]) + more}
                if challenge_visible(page):
                    live(page, "Stopped: the form shows a CAPTCHA")
                    return {"state": "needs_you", "url": url, "detail": "The form shows a CAPTCHA, so finish it yourself."}
                if not submit:
                    live(page, "Everything is filled")
                    return {"state": "ready", "url": url, "detail": f"Filled: {', '.join(filled)}."}
                btn = page.locator("button[type=submit], input[type=submit], button:has-text('Submit application'), button:has-text('Submit')").first
                btn.scroll_into_view_if_needed(timeout=3000)
                live(page, "Submitting…")
                btn.click()
                page.wait_for_timeout(6000)
                live(page, "")
                body = page.locator("body").inner_text()
                if CONFIRMED.search(body):
                    live(page, "Submitted ✓")
                    return {"state": "applied", "url": url, "detail": "Application submitted."}
                if BLOCKED.search(body):
                    live(page, "Stopped: the site flagged the automated submission")
                    mark_blocked(ws, job)
                    return {**BLOCKED_RESULT, "url": job["url"]}
                if challenge_visible(page):
                    live(page, "Stopped: a CAPTCHA appeared on submit")
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


OVERLAY_JS = r"""
(() => {
  if (window.__ro_show) return;
  window.__ro_action = "";
  window.__ro_show = (msg, buttons) => {
    const mount = () => {
      let bar = document.getElementById("__reachout_bar");
      if (!bar) {
        bar = document.createElement("div");
        bar.id = "__reachout_bar";
        bar.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:2147483647;display:flex;gap:10px;"
          + "align-items:center;max-width:min(760px,94vw);padding:10px 12px 10px 16px;border-radius:14px;color:#fff;"
          + "font:600 14px/1.35 -apple-system,system-ui,sans-serif;background:linear-gradient(120deg,#7a4b8f,#0f6b54);box-shadow:0 10px 30px rgba(0,0,0,.25)";
        document.documentElement.appendChild(bar);
      }
      bar.innerHTML = "";
      const t = document.createElement("span"); t.textContent = "✦ Reachout AI · " + msg; t.style.flex = "1"; bar.appendChild(t);
      for (const [id, label] of buttons || []) {
        const b = document.createElement("button"); b.textContent = label; b.type = "button";
        b.style.cssText = "border:0;border-radius:9px;padding:7px 12px;font:600 13px system-ui;cursor:pointer;"
          + (id === "continue" ? "background:#fff;color:#0f6b54" : "background:rgba(255,255,255,.18);color:#fff");
        b.onclick = e => { e.preventDefault(); window.__ro_action = id; };
        bar.appendChild(b);
      }
    };
    document.body ? mount() : addEventListener("DOMContentLoaded", mount);
  };
})();
"""
LOGIN_WALL = re.compile(r"\b(sign in|log ?in|create (an |your )?account|sign up|register to apply|verify your email)\b", re.I)


class Watch:
    """A visible browser on the user's computer: each job opens in a new tab, Reachout fills and submits, and pauses
    with a bar at the bottom of the page whenever the user is needed (sign-in, account creation, questions, CAPTCHA)."""

    def __init__(self, pw, uid):
        self.uid = uid
        profile = C().DATA / "apply-browser"  # remembers sign-ins between runs, on this computer only
        profile.mkdir(parents=True, exist_ok=True)
        self.ctx = pw.chromium.launch_persistent_context(str(profile), headless=False, slow_mo=140, viewport=None,
                                                         user_agent=C().USER_AGENT, args=["--start-maximized"])
        self.ctx.add_init_script(OVERLAY_JS)
        self.stop = False

    def bar(self, page, msg, buttons=()):
        try:
            page.evaluate("([m, b]) => { window.__ro_show && window.__ro_show(m, b) }", [msg, list(buttons)])
        except Exception:
            pass

    def action(self, page):
        try:
            a = page.evaluate("() => { const a = window.__ro_action || ''; window.__ro_action = ''; return a }")
        except Exception:
            return ""
        return a

    def wait_for_user(self, page, live, msg, done=None, minutes=30):
        """Show the bar and wait until the user presses Continue/Skip, `done(page)` becomes true, or the tab closes."""
        live(page, "Waiting for you: " + msg)
        end = time.time() + minutes * 60
        while time.time() < end:
            if page.is_closed():
                return "closed"
            self.bar(page, msg, [("continue", "Continue"), ("skip", "Skip this job")])
            a = self.action(page)
            if a in ("continue", "skip"):
                return a
            try:
                if done and done(page):
                    return "done"
            except Exception:
                pass
            page.wait_for_timeout(1000)
            if int(time.time()) % 4 == 0:
                live(page, "")
        return "timeout"


def login_wall(page):
    """A sign-in or sign-up step stands between us and the form (a password box, no upload field)."""
    try:
        if page.locator("input[type=password]:visible").count():
            return True
        return not page.locator("input[type=file]").count() and bool(LOGIN_WALL.search(page.locator("body").inner_text(timeout=3000)[:4000]))
    except Exception:
        return False


def watch_apply(w, ws, job, values, path):
    """Apply to one job in a new visible tab, pausing for the user where needed. Returns the result dict."""
    if is_blocked_board(ws, job):
        return {**BLOCKED_RESULT, "url": job["url"]}
    live = Live(ws.uid, job)
    url = apply_url(job)
    page = w.ctx.new_page()
    page.bring_to_front()
    try:
        if not open_form(page, job, url, live):
            live(page, "This job is no longer open")
            page.wait_for_timeout(2500)
            return {"state": "closed", "url": url, "detail": "This job is no longer open."}
        w.bar(page, "Filling this application for you…")
        if login_wall(page):
            a = w.wait_for_user(page, live, "This site needs you to sign in or create an account. Do it here, then press Continue.",
                                done=lambda p: not login_wall(p) and p.locator("input[type=file]").count() > 0)
            if a in ("skip", "closed", "timeout"):
                return {"state": "needs_you", "url": url, "detail": "Skipped at the sign-in step."}
            if not page.locator("input[type=file]").count():
                open_form(page, job, url, live)
        filled, missing = fill_form(page, values, path, live, ws.load("ai_answers", {}), job.get("company", ""), values.get("_cover"))
        used = [f for f in filled if f.startswith("answer: ")]
        if used:
            w.bar(page, f"Used {len(used)} of your saved answers")
        if "Resume" not in filled:
            a = w.wait_for_user(page, live, "Attach your resume in this form, then press Continue.")
            if a in ("skip", "closed", "timeout"):
                return {"state": "needs_you", "url": url, "detail": "Couldn't find where to attach your resume."}
        while True:
            page.evaluate("() => document.querySelectorAll('[style*=\"e5484d\"]').forEach(e => e.style.outline = '')")
            left = len(questions_left(page, mark=True))
            if left == 0 and not challenge_visible(page):
                break
            page.evaluate("() => document.querySelector('[style*=\"e5484d\"]')?.scrollIntoView({behavior: 'smooth', block: 'center'})")
            msg = (f"{left} question{'s' if left != 1 else ''} only you can answer (outlined in red). Answer them, then press Continue."
                   if left else "Solve the CAPTCHA, then press Continue.")
            snap = {"n": 0}

            def submitted(p, snap=snap):
                body = p.locator("body").inner_text(timeout=3000)
                if CONFIRMED.search(body) or BLOCKED.search(body):
                    return True
                snap["n"] += 1
                if snap["n"] % 5 == 0:  # keep a recent copy of what the user typed, in case they submit themselves
                    learn_from_page(ws, p, job)
                return False
            a = w.wait_for_user(page, live, msg, done=submitted)
            if a == "continue":
                n = learn_from_page(ws, page, job)
                if n:
                    live(page, f"Saved {n} of your answers for review")
            if a == "done":
                if BLOCKED.search(page.locator("body").inner_text(timeout=3000)):
                    mark_blocked(ws, job)
                    live(page, "Stopped: the site flagged the automated submission")
                    return {**BLOCKED_RESULT, "url": job["url"]}
                live(page, "Submitted ✓")
                return {"state": "applied", "url": url, "detail": "You submitted it in the Reachout window."}
            if a in ("skip", "closed", "timeout"):
                return {"state": "needs_you", "url": url, "questions": missing[:40], "detail": "Skipped: questions were left unanswered."}
        btn = page.locator("button[type=submit], input[type=submit], button:has-text('Submit application'), button:has-text('Submit')").first
        btn.scroll_into_view_if_needed(timeout=3000)
        w.bar(page, "Submitting your application…")
        live(page, "Submitting…")
        btn.click()
        page.wait_for_timeout(6000)
        body = page.locator("body").inner_text()
        if BLOCKED.search(body):
            mark_blocked(ws, job)
            w.bar(page, "This site flagged the automated submission. Reachout stopped; apply from your own browser.")
            live(page, "Stopped: the site flagged the automated submission")
            page.wait_for_timeout(4000)
            return {**BLOCKED_RESULT, "url": job["url"]}
        if CONFIRMED.search(body):
            w.bar(page, "Submitted ✓ Moving on…")
            live(page, "Submitted ✓")
            page.wait_for_timeout(2000)
            return {"state": "applied", "url": url, "detail": "Application submitted."}
        a = w.wait_for_user(page, live, "No confirmation yet. Fix anything the form points out and submit, or press Continue to move on.",
                            done=lambda p: bool(CONFIRMED.search(p.locator("body").inner_text(timeout=3000))))
        if a == "done":
            return {"state": "applied", "url": url, "detail": "Application submitted."}
        return {"state": "needs_you", "url": url, "detail": "Submitted, but no confirmation appeared. Check the page."}
    finally:
        try:
            page.close()
        except Exception:
            pass


def run_watch(uid, picks):
    """Visible mode on the user's computer: one browser window, a tab per job, pausing for the user when needed."""
    from playwright.sync_api import sync_playwright
    ws = C().Workspace(uid)
    st = APPLYING[uid]
    values = apply_values(ws)
    resume, data = resume_file(ws)
    try:
        with tempfile.TemporaryDirectory(prefix="reachout-apply-") as tmp, APPLY_LOCK, sync_playwright() as pw:
            path = os.path.join(tmp, re.sub(r"[^\w.\-]", "_", resume) or "resume.pdf")
            with open(path, "wb") as f:
                f.write(data or b"")
            values["_cover"] = write_cover(ws, tmp)
            w = Watch(pw, uid)
            try:
                for job in picks:
                    st["current"] = job["title"]
                    try:
                        res = watch_apply(w, ws, job, values, path)
                    except Exception as e:
                        print(f"[Reachout] watch apply error: {e}", flush=True)
                        res = {"state": "needs_you", "url": job["url"], "detail": "The window was closed or the page changed. Apply on the site."}
                    res["at"] = time.time()
                    with ws.lock:
                        done = ws.load("ai_applied", {})
                        done[job["id"]] = res
                        ws.save("ai_applied", done)
                    if res["state"] == "applied":
                        record_application(ws, job)
                    st["done"] += 1
            finally:
                try:
                    w.ctx.close()
                except Exception:
                    pass
    finally:
        st["state"] = "done"
        HANDOVER.pop(uid, None)
        from features.notify import notify
        ok = sum(1 for j in picks if (ws.load("ai_applied", {}).get(j["id"]) or {}).get("state") == "applied")
        notify(uid, f"Reachout AI applied to {ok} job{'s' if ok != 1 else ''}", f"{len(picks) - ok} still need you.",
               "/app/ai-jobs", "application")


def run_apply(uid, picks, visible=False):
    if visible:
        return run_watch(uid, picks)
    ws = C().Workspace(uid)
    st = APPLYING[uid]
    try:
        for job in picks:
            st["current"] = job["title"]
            try:
                res = apply_one(ws, job, submit=True, visible=visible)
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
    visible = bool(core.body().get("watch")) and can_hand_over()
    APPLYING[ws.uid] = {"state": "running", "total": len(picks), "done": 0, "current": "", "visible": visible}
    threading.Thread(target=run_apply, args=(ws.uid, picks, visible), daemon=True, name="ai-apply").start()
    return jsonify(ok=True, total=len(picks))


@bp.get("/api/ai-jobs/live")
@login_required
def live_view(ws):
    """The latest picture of the application browser and what it just did (polled by the page while applying)."""
    import base64
    st = LIVE.get(ws.uid)
    if not st:
        return jsonify(active=False)
    busy = (APPLYING.get(ws.uid) or {}).get("state") == "running" or ws.uid in HANDOVER
    return jsonify(active=busy or time.time() - st["at"] < 120, job=st["job"], log=st["log"], at=st["at"],
                   frame=("data:image/jpeg;base64," + base64.b64encode(st["frame"]).decode()) if st["frame"] else "")


@bp.post("/api/ai-jobs/finish/<jid>")
@login_required
def finish(ws, jid):
    """Open a browser window on this computer with the form filled in, for the user to answer the rest and submit."""
    core = C()
    if not can_hand_over():
        raise core.Invalid("This works when Reachout runs on your own computer. Open the form on the site instead.", status=400)
    if not ws.settings().get("auto_apply_consent"):
        raise core.Invalid("Turn on auto-apply and confirm first.", "consent")
    job = next((r for r in ws.load("ai_match", {}).get("results", []) if r["id"] == jid and r.get("can_apply")), None)
    if not job:
        raise core.Invalid("That job isn't in your matches any more.", status=404)
    if ws.uid in HANDOVER or (APPLYING.get(ws.uid) or {}).get("state") == "running":
        raise core.Invalid("A form is already open. Finish or close that window first.", status=409)
    if not resume_file(ws)[1]:
        raise core.Invalid("Upload your resume as a PDF first.", "resume")
    HANDOVER[ws.uid] = jid
    APPLYING[ws.uid] = {"state": "running", "total": 1, "done": 0, "current": job["title"], "visible": True}
    threading.Thread(target=run_watch, args=(ws.uid, [job]), daemon=True, name="ai-watch").start()
    return jsonify(ok=True)


def answer_out(a):
    return {k: a.get(k) for k in ("id", "question", "answer", "company", "status", "source", "learned_from", "updated")}


@bp.get("/api/ai-jobs/answers")
@login_required
def list_answers(ws):
    bank = ws.load("ai_answers", {})
    rows = sorted(bank.values(), key=lambda a: (a.get("status") != "review", -a.get("updated", 0)))
    return jsonify(answers=[answer_out(a) for a in rows])


@bp.post("/api/ai-jobs/answers")
@login_required
def add_answer(ws):
    core, p = C(), C().body()
    q = core.v_text(p.get("question"), "question", "Question", 300, required=True)
    a = core.v_text(p.get("answer"), "answer", "Answer", 4000, required=True)
    company = core.v_text(p.get("company"), "company", "Company", 80)
    if len(ws.load("ai_answers", {})) >= 500:
        raise core.Invalid("You can save up to 500 answers. Remove some first.")
    return jsonify(answer=answer_out(save_answer(ws, q, a, company)))


@bp.put("/api/ai-jobs/answers/<aid>")
@login_required
def edit_answer(ws, aid):
    core, p = C(), C().body()
    bank = ws.load("ai_answers", {})
    if aid not in bank:
        raise core.Invalid("That answer no longer exists.", status=404)
    cur = bank[aid]
    q = core.v_text(p.get("question", cur["question"]), "question", "Question", 300, required=True)
    a = core.v_text(p.get("answer", cur["answer"]), "answer", "Answer", 4000, required=True)
    company = core.v_text(p.get("company", cur.get("company", "")), "company", "Company", 80)
    status = "approved" if p.get("approve", cur["status"] == "approved") else cur["status"]
    row = save_answer(ws, q, a, company, status=status, source=cur.get("source", "you"), learned_from=cur.get("learned_from", ""), aid=aid)
    return jsonify(answer=answer_out(row))


@bp.post("/api/ai-jobs/answers/approve-all")
@login_required
def approve_all(ws):
    with ws.lock:
        bank = ws.load("ai_answers", {})
        n = 0
        for a in bank.values():
            if a.get("status") == "review" and a.get("answer"):
                a["status"], n = "approved", n + 1
        ws.save("ai_answers", bank)
    return jsonify(approved=n)


@bp.delete("/api/ai-jobs/answers/<aid>")
@login_required
def delete_answer(ws, aid):
    with ws.lock:
        bank = ws.load("ai_answers", {})
        bank.pop(aid, None)
        ws.save("ai_answers", bank)
    return jsonify(ok=True)


@bp.get("/api/ai-jobs/kit/<jid>")
@login_required
def apply_kit(ws, jid):
    """Everything needed to apply by hand: the link, contact details and the answers for this job's questions."""
    core = C()
    job = next((r for r in ws.load("ai_match", {}).get("results", []) if r["id"] == jid), None)
    if not job:
        raise core.Invalid("That job isn't in your matches any more.", status=404)
    v = apply_values(ws)
    labels = [("First name", "first"), ("Last name", "last"), ("Email", "email"), ("Phone", "phone"), ("Location", "location"),
              ("LinkedIn", "linkedin"), ("GitHub", "github"), ("Portfolio", "portfolio"), ("Current company", "current_company"),
              ("Notice period", "notice")]
    fields = [{"label": l, "value": v[k]} for l, k in labels if v.get(k)]
    bank = ws.load("ai_answers", {})
    company = job.get("company", "")
    questions = (ws.load("ai_applied", {}).get(jid) or {}).get("questions") or []
    answers, seen = [], set()
    for q in questions:
        q = q.removesuffix(" (choose from the list)")
        hit = answer_for(bank, q, company)
        draft = None
        if not hit:  # show a draft too, clearly marked, so the user can check it before using it
            for a in bank.values():
                if a.get("status") == "review" and (not a.get("company") or same_company(a["company"], company)) \
                        and q_score(norm_q(q, company), a.get("norm") or "") >= 0.8:
                    draft = a["answer"].replace("{company}", company)
        answers.append({"question": q, "answer": hit or draft or "", "draft": bool(draft and not hit)})
        seen.add(norm_q(q, company))
    for a in bank.values():  # other answers written for this company
        if a.get("company") and same_company(a["company"], company) and a.get("norm") not in seen:
            answers.append({"question": a["question"], "answer": a["answer"].replace("{company}", company), "draft": a.get("status") != "approved"})
    resume = resume_file(ws)[0]
    return jsonify(url=job["url"], apply_url=apply_url(job), title=job["title"], company=company, resume=resume, fields=fields, answers=answers)
