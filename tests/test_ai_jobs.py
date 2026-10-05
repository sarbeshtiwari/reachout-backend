"""Reachout AI job match: the ranking engine, the LangChain pipeline, keys, consent and auto-apply rules."""

import time

import pytest
from conftest import A, H

from features import ai_jobs as AI


RESUME = "Full stack developer, 3 years of experience. Python, React, Node.js, TypeScript, MongoDB, AWS, Docker."
PROFILE = {"role": "Full Stack Developer", "years": 3.0, "company": "", "location": "", "resume": RESUME,
           "skills": ["Python", "React", "Node.js", "TypeScript", "MongoDB", "AWS", "Docker"]}


def job(i, title, text, location="Remote", ats="lever"):
    return {"id": f"lv:acme:{i}", "ats": ats, "board": "acme", "title": title, "company": "Acme", "location": location,
            "url": f"https://jobs.lever.co/acme/{i:036d}", "text": text, "posted": "2026-10-01"}


JOBS = [
    job(1, "Full Stack Engineer", "We use React, Node.js, TypeScript and MongoDB on AWS. 2-4 years of experience."),
    job(2, "Senior Staff Engineer", "Lead architecture. 12+ years of experience. Go, Kubernetes."),
    job(3, "Strategic Finance, AI Partnerships", "Finance role. Excel, modelling. 3+ years."),
    job(4, "Frontend Developer", "React and TypeScript. 1+ years of experience."),
]


def test_engine_ranks_the_real_fit_first_and_explains_it():
    ranked = AI.rank(PROFILE, [j for j in JOBS if AI.role_fit(PROFILE["role"], j["title"])])
    assert ranked[0]["title"] == "Full Stack Engineer" and ranked[0]["score"] >= 75
    assert any("React" in r for r in ranked[0]["reasons"]) and ranked[0]["can_apply"]
    assert "Strategic Finance, AI Partnerships" not in [r["title"] for r in ranked]


@pytest.mark.parametrize("text,ask", [("3+ years of experience", (3, None)), ("2-4 years", (2, 4)), ("5 to 8 yrs", (5, 8)),
                                      ("no experience needed", None), ("we were founded 30 years ago and need 4+ years", (4, None))])
def test_years_asked(text, ask):
    assert AI.years_asked(text) == ask


def test_senior_roles_are_flagged_for_junior_profiles():
    r = AI.rank(PROFILE, [JOBS[1]])[0]
    assert "Very senior title" in r["gaps"] and r["score"] < 50


@pytest.mark.parametrize("url,ok", [("https://jobs.lever.co/acme/" + "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12, True),
                                    ("https://job-boards.greenhouse.io/acme/jobs/123456", True),
                                    ("https://jobs.ashbyhq.com/acme/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, True),
                                    ("https://www.linkedin.com/jobs/view/123", False), ("https://evil.example/jobs.lever.co/x", False)])
def test_only_supported_application_sites_are_recognised(url, ok):
    assert bool(AI.ATS_URL.match(url)) is ok


def test_pipeline_with_career_sites(monkeypatch):
    monkeypatch.setattr(AI, "board_jobs", lambda ats, board: JOBS if board == "acme" else [])
    monkeypatch.setattr(AI, "BOARDS", [("lever", "acme")])
    st = AI.build_chain({}, ["career_sites"]).invoke({"profile": PROFILE, "use_llm": False})
    assert st["ranked"][0]["title"] == "Full Stack Engineer" and st["engine"] == "Reachout AI" and st["considered"] == 4


def test_web_search_results_are_read_from_their_job_board(monkeypatch):
    monkeypatch.setattr(AI, "from_ats_url", lambda u: JOBS[0] if "lever" in u else None)
    out = AI.web_hits_to_jobs([{"url": "https://jobs.lever.co/acme/x"}, {"url": "https://careers.other.example/job/1", "title": "Dev"}])
    assert out[0]["ats"] == "lever" and out[1]["ats"] == "" and out[1]["url"].startswith("https://careers.other")


def test_keys_are_stored_but_never_sent_back(user):
    c = user["client"]
    r = c.put("/api/ai-jobs/keys", json={"tavily": "tvly-abcdefghijklmnop"}, headers=H).get_json()
    assert r == {"tavily": True, "brave": False}
    assert "tvly-" not in c.get("/api/ai-jobs").get_data(as_text=True)
    assert c.put("/api/ai-jobs/keys", json={"brave": "<script>"}, headers=H).status_code == 400


def test_search_needs_a_resume_and_a_key_for_web_sources(user):
    c = user["client"]
    r = c.post("/api/ai-jobs/match", json={"role": "Developer", "years": 2}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "resume"
    user["ws"].save_doc("resume.pdf", b"%PDF-1.4 x")
    r = c.post("/api/ai-jobs/match", json={"role": "Developer", "years": 2, "sources": ["brave"]}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "sources"


def test_full_run_from_the_api(user, monkeypatch):
    monkeypatch.setattr(AI, "board_jobs", lambda ats, board: JOBS)
    monkeypatch.setattr(AI, "BOARDS", [("lever", "acme")])
    monkeypatch.setattr(AI, "pdf_text", lambda ws, name: RESUME)
    monkeypatch.setattr(AI, "ollama_ready", lambda: False)
    user["ws"].save_doc("resume.pdf", b"%PDF-1.4 x")
    c = user["client"]
    assert c.post("/api/ai-jobs/match", json={"role": "Full Stack Developer", "years": 3, "resume": "resume.pdf"}, headers=H).status_code == 200
    for _ in range(100):
        st = c.get("/api/ai-jobs").get_json()
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["results"][0]["title"] == "Full Stack Engineer"


def test_auto_apply_needs_consent_and_records_the_application(user, monkeypatch):
    c, ws = user["client"], user["ws"]
    ws.save("ai_match", {"results": [{**JOBS[0], "can_apply": True, "score": 90}]})
    r = c.post("/api/ai-jobs/apply", json={"ids": [JOBS[0]["id"]]}, headers=H)
    assert r.status_code == 400 and r.get_json()["field"] == "consent"
    assert c.put("/api/ai-jobs/details", json={"linkedin": "https://linkedin.com/in/me", "consent": True}, headers=H).get_json()["consent"]
    assert c.put("/api/ai-jobs/details", json={"linkedin": "javascript:alert(1)"}, headers=H).status_code == 400
    ws.save("profile", {**ws.profile(), "phone": "+919999999999"})
    monkeypatch.setattr(AI, "apply_one", lambda ws, job, submit=True: {"state": "applied", "detail": "Application submitted.", "url": job["url"]})
    assert c.post("/api/ai-jobs/apply", json={"ids": [JOBS[0]["id"]]}, headers=H).status_code == 200
    for _ in range(100):
        if AI.APPLYING[ws.uid]["state"] != "running":
            break
        time.sleep(0.05)
    assert ws.load("ai_applied", {})[JOBS[0]["id"]]["state"] == "applied"
    assert any(a["portal"] == "Auto-apply (Lever)" for a in ws.load("applications", {}).values())
    r = c.post("/api/ai-jobs/apply", json={"ids": [JOBS[0]["id"]]}, headers=H)  # never applies twice
    assert r.status_code == 400


def test_greenhouse_jobs_apply_on_greenhouse_even_when_shown_on_the_company_site():
    j = {"id": "gh:coinbase:7985187", "ats": "greenhouse", "url": "https://www.coinbase.com/careers/positions/7985187?gh_jid=7985187"}
    url = AI.apply_url(j)
    assert url == "https://job-boards.greenhouse.io/embed/job_app?for=coinbase&token=7985187"
    assert AI.urlsplit(url).hostname in AI.APPLY_HOSTS


@pytest.mark.parametrize("label,key", [("first name", "first"), ("email", "email"), ("phone", "phone"), ("location (city)", "location"),
                                       ("linkedin profile url", "linkedin"),
                                       ("have you previously been employed by coinbase in any capacity", None),
                                       ("are you a close relative of a government official", None)])
def test_only_contact_fields_are_filled_never_questions(label, key):
    hit = next((k for pat, k in AI.FIELD_MAP if AI.re.search(pat, label)), None)
    assert hit == key
