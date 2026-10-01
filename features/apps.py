"""Applications tracker: finds the jobs you applied for in your mailbox and follows each one's status.

Sources are the emails companies' career sites send (Workday, Lever, Greenhouse, iCIMS, SuccessFactors, …),
Naukri's application and status emails, LinkedIn's "application sent / viewed" emails, and recruiters
writing to you directly. Each email becomes an event (applied, in review, assessment, shortlisted,
interview, offer, rejected, position closed, …); events are grouped into one application per company +
role, so every application has a current status, the previous one, and its full history.

Mail is read with BODY.PEEK in a read-only folder, so nothing is marked as read. A daily sync (06:00 in
the user's time zone by default, with catch-up if the app wasn't running then) keeps everything current.
"""

import hashlib
from html.parser import HTMLParser
import contextvars
import re
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import bridge
from bridge import login_required

bp = Blueprint("apps", __name__)


def C():
    return bridge.C


# ================================================================= statuses

STATUSES = {  # key: (label, rank in the pipeline, terminal?)
    "applied": ("Applied", 1, False),
    "incomplete": ("Application incomplete", 0, False),
    "in_review": ("In review", 2, False),
    "assessment": ("Assessment", 3, False),
    "shortlisted": ("Shortlisted", 4, False),
    "interview": ("Interview", 5, False),
    "offer": ("Offer", 7, True),
    "rejected": ("Not selected", 6, True),
    "closed": ("Position closed", 6, True),
    "withdrawn": ("Withdrawn", 6, True),
}
ACTIVE = {k for k, v in STATUSES.items() if not v[2]}

# Ordered strongest first. Each: (status, subject pattern, body pattern). Body patterns are strict phrases
# so boilerplate ("only shortlisted candidates will be contacted") doesn't trigger them.
RULES = [
    ("offer",
     r"\boffer letter\b|\boffer (?:id|of employment)\b|offer\b.*\baccepted\b|\byour .{0,30}offer\b|\bdigital offer\b",
     r"pleased to (?:offer|extend)|offer of employment|attached (?:is )?(?:your|the) offer letter|"
     r"congratulations[^.\n]{0,80}\b(?:selected|offer)\b"),
    ("rejected",
     r"\bregret\b|\bunsuccessful\b|\bnot (?:been )?selected\b|\bnot shortlisted\b",
     r"regret to inform|(?:decided|chosen|elected) (?:not )?to (?:move|proceed|go) (?:forward|ahead)? ?with (?:other|another)|"
     r"(?:pursue|progress|proceed with|consider) other candidates|other candidates (?:whose|who)|"
     r"not (?:to )?(?:pursue|proceed with|progress|move forward with) your (?:candidacy|application|profile)|"
     r"(?:will|would) not be (?:moving|proceeding|progressing) (?:forward )?with your|"
     r"(?:have|has) not been (?:selected|shortlisted|successful)|(?:were|was|is) (?:not )?unsuccessful (?:on this|at this|in)|"
     r"no longer (?:under|being) consider|unable to (?:offer you|move forward|progress your)|not (?:been )?selected for|"
     r"your application (?:was|has been) (?:declined|rejected)|not a match for (?:this|the) (?:role|position)"),
    ("closed",
     r"\b(?:role|position|opening|requisition|job)\b[^|]{0,40}?\b(?:has been |is |was )?(?:filled|closed|cancel+ed)\b",
     r"(?:role|position|opening|requisition|vacancy|job)[^.\n]{0,50}?(?:has|have) (?:now )?been (?:filled|closed|cancel+ed|put on hold)|"
     r"(?:role|position|opening) is no longer (?:open|available)|no longer accepting applications"),
    ("withdrawn",
     r"\bwithdra(?:wn|wal)\b",
     r"(?:your )?application (?:has been|was) withdrawn|you (?:have )?withdrawn"),
    ("incomplete",
     r"\bincomplete (?:application|skills test)\b|\bfinish your application\b|\bcomplete your application\b",
     r"did not (?:finish|complete) your .{0,30}application|application is (?:still )?incomplete|"
     r"haven'?t (?:finished|completed) your application"),
    ("interview",
     r"\binterview\b(?!.*\btrend\b)|^invitation:.*@|\b(?:meet|meeting|call) (?:with|scheduled)\b",
     r"(?:invite|invited|invitation|like to (?:schedule|invite)|scheduled?|shortlisted) (?:you )?(?:for|to) (?:an? |the |your )?"
     r"(?:\w+ )?(?:round of )?(?:interview|discussion|conversation|call)|your interview (?:is|has been) (?:scheduled|confirmed|booked)|"
     r"interview (?:details|schedule|confirmation|slot|invite)"),
    ("assessment",
     r"\bassessment\b|\bskills? test\b|\bonline test\b|\bcoding (?:test|challenge|round)\b|\baptitude\b|\btest details\b|"
     r"\bhackerrank\b|\bcodility\b|\bhackerearth\b|\bmettl\b|\bshl\b|\btestgorilla\b|\bhirepro\b",
     r"(?:appear|complete|take|attempt) (?:for |in )?(?:the |an? |your )?(?:online |skill |coding |technical )?"
     r"(?:assessment|test|challenge)|(?:assessment|test) link|hck\.re/|hackerrank\.com/test|app\.codility|mettl\.com"),
    ("shortlisted",
     r"\bshortlist(?:ed)?\b|\bnext steps?\b|\bmoving forward\b|\bselected for\b",
     r"(?:your )?(?:profile|application|resume|cv) (?:has been|was|is) (?:shortlisted|selected|moved forward)|"
     r"move (?:you )?(?:forward|ahead) (?:to|in) the (?:next|following)|pleased to inform you that you have been shortlisted|"
     r"would (?:like|love) to (?:speak|talk|connect|chat|discuss) (?:with you|further)|next (?:step|round) (?:in|of) (?:the|our) (?:process|hiring)"),
    ("in_review",
     r"\bstatus of your (?:job )?application has (?:changed|been updated)\b|\bapplication (?:is )?(?:under|in) review\b|"
     r"\bviewed your application\b|\bfollow your application\b|\bapplication status\b|\bupdate on your .{0,40}application\b|"
     r"\bupdate (?:for you )?(?:from|on)\b.*\bapplication\b",
     r"(?:is|are) (?:currently )?(?:under|being) review|(?:is|are) reviewing your (?:application|profile)|"
     r"recruiter activity on your|recruiter (?:viewed|has viewed)|application (?:is )?in process"),
    ("applied",
     r"\bthank(?:s| you)?\b.{0,20}\b(?:applying|application|interest|apply)\b|\bapplication (?:has been )?(?:received|submitted|sent)\b|"
     r"\bsuccessfully (?:submitted|applied)\b|\breceived your application\b|\byou applied for\b|\byour application (?:for|to|at|with|was sent)\b|"
     r"\bjourney at\b.*\bbegins\b|\byour candidature\b|\bapplied (?:to|for|successfully)\b|\bapplication confirmation\b",
     r"thank(?:s| you) for (?:applying|your application|submitting|your interest in)|"
     r"(?:we'?ve|we have) (?:successfully )?received your application|application (?:has been|was) (?:successfully )?(?:received|submitted)|"
     r"received your application for"),
]
RULES = [(s, re.compile(sp, re.I), re.compile(bp_, re.I)) for s, sp, bp_ in RULES]
STRONG_BODY = {"offer", "rejected", "closed", "withdrawn", "incomplete"}


CONDITIONAL = re.compile(r"\b(?:if|even if|in case|should|whether|unless|once|when|until)\b")
NEGATED = re.compile(r"\b(?:not|no|never|nor)\b(?:\s+\S+){0,4}\s*$")
POSITIVE = {"offer", "interview", "assessment", "shortlisted"}


def guarded(rx, text, status):
    """First match that isn't hypothetical ("If you are not selected…") or negated ("doesn't create an offer")."""
    for m in rx.finditer(text):
        pre = text[max(0, m.start() - 160):m.start()].lower()
        clause = re.split(r"[.!?\n;]\s", pre)[-1]
        if CONDITIONAL.search(clause) or (status in POSITIVE and NEGATED.search(clause)):
            continue
        return m
    return None


def classify(subject, body):
    """(status, matched phrase) for one email, or (None, '') if it isn't about an application."""
    s_hit = next(((st, m.group(0)) for st, sp, _ in RULES if (m := guarded(sp, subject, st))), None)
    b_hit = next(((st, m.group(0)) for st, _, bpat in RULES if (m := guarded(bpat, body, st))), None)
    if b_hit and b_hit[0] in STRONG_BODY:  # a rejection hidden behind "An update on your application"
        return b_hit
    if s_hit and s_hit[0] == "in_review" and b_hit and STATUSES[b_hit[0]][1] > STATUSES["in_review"][1]:
        return b_hit  # generic "status update" subject, specific body
    return s_hit or b_hit or (None, "")


# ================================================================= noise

NOISE_SUBJECT = re.compile(
    r"new jobs? posted|latest .{0,20}jobs|job alert|jobs? (?:for you|you (?:may|might))|recommended (?:jobs|for you)|"
    r"recommendations|webinar|newsletter|leetcode|contest|be first to apply|check out jobs|handpicked|urgently hiring|"
    r"top openings|urgent requirement|unlock your potential|rank higher|interview trend|invited to apply|"
    r"verify your (?:candidate )?(?:account|email)|reset your password|password|activate your|sign[- ]?in code|"
    r"join our talent (?:community|network)|similar jobs|who viewed|profile views|premium|subscription|"
    r"^✉️|walk-?in interview \||^job \||hiring for .{0,40}\|\s|admit card|\bgate[- ]?20\d\d\b|"
    r"interview (?:questions|tips|prep|advice|guide)|mock interview|resume (?:score|review|tips)|what'?s new in your|"
    r"your weekly|daily digest|jobs? matching|people also applied|applied by other|similar (?:roles|openings)|"
    r"\botp\b|verification (?:code|needed)|verify your|email verification|confirm your .{0,30}email|login details|"
    r"candidate portal|one more step|seat booking|ticket .{0,20} solved|saved as a draft|continue to apply|"
    r"let'?s stay in touch|registration procedure|stand out from the crowd|new comment for|about case \d+|"
    r"support id|link not active|unresponsive|request for new|\bconsent to represent\b", re.I)
NOISE_FROM = re.compile(
    r"@(?:[\w-]+\.)*(?:digialm\.com|nic\.in|gov\.in|ibps\.in|leetcode\.com|geeksforgeeks\.org|coursera|udemy|"
    r"unstop\.com|ambitionbox\.com|glassdoor\.|medium\.com|substack\.com|quora\.com|youtube\.com|"
    r"minis\.naukri\.com|jobs2web\.com|facebookmail\.com|accounts\.google\.com)|"
    r"@(?:[\w-]+\.)*(?:ac|edu)(?:\.[a-z]{2})?>?$|jobalerts?|job-?notification|newsletter|marketing|promo", re.I)

# ATS / job-board sender domains: the company is not the sender's domain.
PORTALS = {
    "myworkday.com": "Workday", "workday.com": "Workday", "lever.co": "Lever", "greenhouse-mail.io": "Greenhouse",
    "greenhouse.io": "Greenhouse", "icims.com": "iCIMS", "smartrecruiters.com": "SmartRecruiters",
    "successfactors.com": "SuccessFactors", "successfactors.eu": "SuccessFactors", "brassring.com": "BrassRing",
    "avature.net": "Avature", "ashbyhq.com": "Ashby", "rippling.com": "Rippling", "jobvite.com": "Jobvite",
    "taleo.net": "Taleo", "oraclecloud.com": "Oracle Recruiting", "workablemail.com": "Workable", "workable.com": "Workable",
    "naukri.com": "Naukri", "linkedin.com": "LinkedIn", "indeed.com": "Indeed", "indeedemail.com": "Indeed",
    "njoyn.com": "Njoyn", "testedrecruits.com": "TestedRecruits", "darwinbox.in": "Darwinbox", "darwinbox.com": "Darwinbox",
    "zohorecruit.com": "Zoho Recruit", "zohorecruit.in": "Zoho Recruit", "freshteam.com": "Freshteam", "keka.com": "Keka",
    "kekamail.com": "Keka", "joinsuperset.com": "Superset", "wellfound.com": "Wellfound", "instahyre.com": "Instahyre",
    "cutshort.io": "Cutshort", "foundit.in": "foundit", "internshala.com": "Internshala", "hirist.tech": "Hirist",
    "hackerrank.com": "HackerRank", "hackerearth.com": "HackerEarth", "testgorilla.com": "TestGorilla",
    "talentrecruitmail.com": "Talent Recruit", "recruitee.com": "Recruitee", "teamtailor-mail.com": "Teamtailor",
    "personio.de": "Personio", "bamboohr.com": "BambooHR", "hirepro.in": "HirePro", "mettl.com": "Mercer Mettl",
    "shl.com": "SHL", "apna.co": "apna", "uplers.com": "Uplers", "turing.com": "Turing", "crossinghurdles.com": "Crossing Hurdles",
    "micro1.ai": "micro1",
}
SENDER_WORDS = re.compile(
    r"\b(?:workday|notifications?|notification|recruiting|recruitment|recruiters?|talent acquisition|talent|careers?|"
    r"hiring|hr|team|people services|people|resourcing|human resources|jobs|job|do-?not-?reply|no-?reply|noreply|"
    r"action may be required|global talent acquisition|p&o|india|inc|the|joining)\b|@\s*icims|\bat\b|[-–|:]|\s{2,}", re.I)
PLATFORMS = re.compile(r"\b(?:turbohire|ripplehire|expertia(?: ai)?|talenttitan|superset|codesignal|testgorilla|zwayam|"
                       r"hackerrank|hackerearth|mettl|keka|darwinbox|naukri|linkedin|indeed|internshala|unstop|wellfound|"
                       r"instahyre|cutshort|workday|lever|greenhouse|icims|successfactors|smartrecruiters|brassring|"
                       r"avature|taleo|jobvite|zoho recruit|freshteam|talentrecruit|system|systems administrator)\b", re.I)
ROLE_WORDS = re.compile(r"\b(?:engineer|engg|engr|developer|analyst|intern|internship|trainee|manager|architect|specialist|"
                        r"associate|consultant|designer|tester|sdet|sde|support|executive|representative|programmer|lead|"
                        r"officer|scientist|apprentice|graduate|qa|devops|administrator|process|operations|product|data|"
                        r"front ?end|back ?end|full ?stack|fullstack|technician|staff|customer|voice|analytics|stack|"
                        r"software|security|platform|cloud|mern|react|java|python|flutter|\.net|node|ai|ml|fresher|"
                        r"coordinator|member|contributor|expert|benchmarking|agent|advisor|writer|editor|researcher)\b", re.I)
COMPANY_ROLE = re.compile(r"\b(?:engineers?|developers?|analyst|intern|internship|trainee|manager|architect|specialist|"
                          r"consultant|designer|tester|sdet|sde|executive|representative|programmer|officer|scientist|"
                          r"apprentice|graduate|devops|administrator|technician|fresher|coordinator|contributor|associate)\b", re.I)
NOT_COMPANY = re.compile(r"^(?:action required|reminder|important|invitation|interview|update|status(?: update)?|next steps?|"
                         r"shortlisted|incomplete|assessment|completed?|kind regards|regards|welcome|congratulations|"
                         r"application|thank you|thanks|candidate|confirmation|acknowledgement|campus|india|remote|urgent|"
                         r"hiring|new|re|fw|fwd|reg|hello|hi|dear|sorry|greetings|notice|alert|result|results|offer|"
                         r"bangalore|bengaluru|noida|gurugram|gurgaon|hyderabad|pune|chennai|mumbai|delhi|kolkata|"
                         r"navi mumbai|ncr|usa|uk|us|job|jobs|opportunity|career|careers)\b", re.I)
GENERIC = {"", "central", "unknown sender", "an unknown sender", "workday", "lever", "greenhouse", "icims", "recruiting", "naukri", "linkedin", "indeed", "careers", "talent",
           "hr", "team", "sap successfactors", "successfactors", "info", "mail", "support", "admin", "the company", "us",
           "our company", "your", "we"}

COMPANY_SUFFIX = re.compile(
    r"[\s,]+(?:pvt\.?|private|ltd\.?|limited|llp|llc|inc\.?|corp\.?|corporation|co\.|plc|gmbh|s\.?a\.?|"
    r"technologies|technology|solutions|services|software|systems|consulting|global|group|india|international|associates|"
    r"team|careers|labs|"
    r"& co\.?|and company)\b\.?", re.I)


def norm_company(name):
    n = (name or "").lower().replace("&", " and ")
    prev = None
    while prev != n:
        prev, n = n, COMPANY_SUFFIX.sub("", " " + n).strip(" .,-")
    return re.sub(r"[^a-z0-9]+", "", n)


def norm_role(role):
    r = re.sub(r"\([^)]*\)|\b(?:i{1,3}|iv|[1-4])\b$|[^a-z0-9 ]+", " ", (role or "").lower())
    return re.sub(r"\s+", " ", r).strip()


def tidy(text, n=80):
    t = re.sub(r"[​-‏⁠﻿]", "", text or "")
    t = re.sub(r"\s+", " ", t).strip(" .,:;!-–—|'\"*")
    return t[:n].strip()


# The signed-in person's own name parts, set per scan: their name is never taken for a company.
ME_NAMES = contextvars.ContextVar("me_names", default=())


def my_name_parts(profile):
    return tuple(n for n in re.split(r"\s+", (profile.get("name") or "")) if len(n) >= 3)


def good_company(c):
    c = tidy(re.sub(r"\([^)]*\)?|\s+via\s+.*$|^@|@.*$", " ", c or "", flags=re.I), 60)
    c = re.sub(r"^.*\bfrom\s+", "", c) if re.search(r"\bfrom\s+\S", c) else c
    c = tidy(re.sub(r"\s+(?:team|careers?|recruiting|recruitment|hiring|hr|talent acquisition|people services|support|notifications?|system)$", "", c, flags=re.I), 60)
    if (not c or c.lower() in GENERIC or len(c) < 2 or NOT_COMPANY.match(c) or COMPANY_ROLE.search(c) or PLATFORMS.search(c)
            or re.search(r"\b(?:you|your|our|we|position|role|application|job|id|candidate)\b|\d{4,}|\.(?:com|in|io|ai)$", c, re.I)
            or any(re.search(rf"\b{re.escape(n)}\b", c, re.I) for n in ME_NAMES.get())
            or re.fullmatch(r"(?:do-?not-?reply|no-?reply|noreply|donotreply)\w*", c, re.I)):
        return ""
    if re.fullmatch(r"[a-z0-9]+", c):  # 'barclays' from a tenant name
        c = c.upper() if len(c) <= 3 else c.title()
    return c


def good_role(r, me=()):
    r = tidy(r, 140)
    for n in me:  # "Jane Doe - JR0295478 IT Security Analyst I"
        r = re.sub(rf"\b{re.escape(n)}\b", " ", r, flags=re.I)
    r = re.sub(r"^\W*(?:role|position|job title|designation)\s*[:-]\s*|^(?:job|post|position|role) of\s+|^position\s+", "",
               tidy(r, 140), flags=re.I)
    r = re.sub(r"\s+(?:at|@)\s+[A-Z][\w&. -]*$|^[A-Z][\w&.]*\s*\|\s*|\s+-\s+application form$", "", r)
    r = re.sub(r"^(?:the|a|an|our)\s+|\b(?:role|position|opening|opportunity|job|post)\b\s*$", "", tidy(r, 140), flags=re.I)
    r = re.sub(r"^[a-z][a-z ]{0,20}\s+-\s+", "", tidy(r, 140))  # "completion - 36556 - Software Developer"
    r = tidy(re.sub(r"^(?:[-–—]\s*)?(?:R|JR|REQ|R-)?[-_]?\d{4,}[\w-]*\s*[-–—]?\s*", "", r), 90)
    words = r.split()
    if (not r or len(words) > 12 or len(r) < 3 or not ROLE_WORDS.search(r) or "#" in r
            or re.search(r"\b(?:you|your|we|our|us|thank|thanks|application|interest|applying|apply|interview|link|verification|"
                         r"represent|started|track|status|career|update|received|submitted)\b|[;{}]|background-", r, re.I)):
        return ""
    return r


# ================================================================= extraction

HTML_MAX = 200_000  # characters of email HTML we ever look at


class _Text(HTMLParser):
    """Linear-time HTML → text (no regex backtracking, so a hostile email can't stall the server)."""
    BREAKS = {"br", "p", "div", "tr", "td", "li", "a", "table", "span", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html):
    p = _Text()
    try:
        p.feed(str(html or "")[:HTML_MAX])
        p.close()
    except Exception:
        pass
    return " ".join(p.out).replace(" \n ", "\n")


def body_text(msg):
    plain = html = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, AssertionError):
            continue
        if ctype == "text/plain" and not plain:
            plain = content
        elif ctype == "text/html" and not html:
            html = content
    # Plain parts are sometimes a one-line stub ("view this email in a browser"); prefer the HTML then.
    text = plain if len(plain.strip()) > 200 or not html else html_to_text(html)
    text = re.sub(r"[​-‏⁠﻿\xa0\t ]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def calendar_start(msg):
    """DTSTART of an attached calendar invite, as a naive local datetime."""
    for part in msg.walk():
        if part.get_content_type() in ("text/calendar", "application/ics"):
            try:
                ics = part.get_content()
            except Exception:
                ics = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
            m = re.search(r"DTSTART(?:;TZID=([^:;]+))?(?:;VALUE=DATE-TIME)?:(\d{8}T\d{4,6})(Z?)", ics or "")
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(2)[:13], "%Y%m%dT%H%M")
                if m.group(3) == "Z":
                    dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone().replace(tzinfo=None)
                elif m.group(1):
                    dt = dt.replace(tzinfo=ZoneInfo(m.group(1).strip('"'))).astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                return None
    return None


SUBJ_COMPANY = [
    re.compile(r"(?:applying|applied|application|apply|interest|interested|candidature|candidacy|journey|career opportunity)"
               r"\s+(?:to|at|with|in)\s+(?!the\b|our\b|us\b|a\b|an\b)([A-Z][\w&.,'’ -]{1,48}?)(?:\s*[!.|:,–—-]|\s+for\b|\s+-\s|$)"),
    re.compile(r"\bat\s+([A-Z][\w&.'’ -]{1,40}?)\s*[!.]?$"),
    re.compile(r"\bwith\s+([A-Z][\w&.'’ -]{1,40}?)\s*[!.]?$"),
    re.compile(r"^(?:\[)?([A-Z][\w&.'’ -]{1,32}?)\]?\s*(?::|\||::|–|—)\s"),
    re.compile(r"\bYour\s+([A-Z][\w&.'’ -]{1,30}?)\s+(?:Job\s+)?Application\b"),
    re.compile(r"^([A-Z][\w&.'’ -]{1,30}?)\s+(?:Recruiting|Hiring|Careers?)\s+Update\b"),
    re.compile(r"\bfrom\s+([A-Z][\w&.'’() -]{1,40}?)\s+on your\b"),
    re.compile(r"\bat\s+([A-Z][\w&.'’ -]{1,40}?)\s+(?:has|is|was)\b"),
    re.compile(r"^([A-Z][\w&.'’ -]{1,40}?)\s+(?:has\s+)?received your application\b"),
]
BODY_COMPANY = [
    re.compile(r"(?:position|role|opening|opportunit(?:y|ies)|job)\s+(?:of\s+[^\n.]{2,70}?\s+)?(?:at|with)\s+"
               r"([A-Z][\w&.,'’ -]{1,50}?)(?:\.\s|[.,!\n]|\s+and\s|\s+in\s|\s+we\s)"),
    re.compile(r"(?:interest in|applying (?:to|at|with)|considering|application (?:to|with))\s+(?:joining\s+)?(?:the\s+)?"
               r"([A-Z][\w&.,'’ -]{1,50}?)(?:\.\s|[.,!\n]|\s+and\s|\s+as\s|\s+for\s|\s+in\s)"),
    re.compile(r"\n([A-Z][\w&.'’ -]{1,40}?)\s+(?:Talent Acquisition|Recruiting|Recruitment|Careers|HR|Hiring) Team\b"),
]
SUBJ_ROLE = [
    re.compile(r"(?:for|to)\s+(?:the\s+)?(?:position of\s+|role of\s+)?(.{3,80}?)\s+(?:position|role|opening|opportunity|job)?\s*"
               r"(?:at|with|in)\s+[A-Z]", re.I),
    re.compile(r"^(?:Re:\s*)?(.{3,70}?)\s+-\s+Application", re.I),
    re.compile(r"(?:interest|applying|application|update)\s*[-–—:]\s*(.{3,80}?)$", re.I),
    re.compile(r"(?:interview|scheduled)\s*[-–—:]\s*(?:[A-Z][\w .]+\s[-–—]\s)?(.{3,80}?)(?:\s+at\s|$)", re.I),
    re.compile(r"\bfor\s+(?:the\s+)?(?:position of\s+|role of\s+|post of\s+)?(.{3,80}?)\s*(?:[!.]|$)", re.I),
    re.compile(r"\((.{3,60}?)\)\s*@"),
]
BODY_ROLE = [
    re.compile(r"(?:position|role|post) of\s+([^\n.]{3,80}?)(?:\s+(?:at|with|in|and|which|that)\s|[.\n,])", re.I),
    re.compile(r"for the\s+(?:R\d+\s+|JR\d+\s+)?([^\n.]{3,80}?)\s+(?:position|role|opening|job)\b", re.I),
    re.compile(r"(?:application|applying) for\s+(?:the\s+)?(?:R\d+\s+|JR\d+\s+)?([^\n.]{3,80}?)\s+(?:position|role|at|with)\b", re.I),
    re.compile(r"(?:applying|applied) for\s+([^\n.]{3,80}?)\s+position\b", re.I),
    re.compile(r"Ref:\s*\d+\s*-\s*([^\n]{3,60})"),
    re.compile(r"(?:Job Title|Position|Role|Designation)\s*[:\-]\s*([^\n]{3,80})", re.I),
]
JOB_ID = re.compile(r"\b(?:job\s*(?:id|number|no\.?|code|req(?:uisition)?)|req(?:uisition)?\s*(?:id|#|no\.?|number)?|"
                    r"ref(?:erence)?(?:\s*(?:id|no\.?|number))?|posting\s*(?:id|number))\s*[:#-]?\s*"
                    r"([A-Z]{0,5}[-_]?\d{3,}[\w-]{0,12})", re.I)
JOB_ID2 = re.compile(r"\b((?:JR|REQ|R|JOB)[-_]?\d{5,}(?:[-_]\d+)?)\b")


def first_match(patterns, text, check):
    for rx in patterns:
        for m in rx.finditer(text):
            v = check(m.group(1))
            if v:
                return v
    return ""


COMPANYISH = re.compile(r"\b(?:team|hr|careers?|recruit\w*|talent|group|inc|ltd|llp|llc|pvt|corp\w*|technolog\w*|solutions|"
                        r"global|labs?|services|systems|software|security|digital|consulting|capital|bank|financial|"
                        r"networks?|communications|healthcare|health|motors|industries|international|ai|tech|"
                        r"workday|notifications?|people|acquisition|resourcing|university|academy)\b", re.I)
GENERIC_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|noreply|contact|support|info|hello|hr|careers?|jobs|"
                           r"talent|recruit\w*|hiring|team|mail|admin|notifications?|wcm)\b", re.I)


def looks_like_person(name, local, domain, portal):
    """'Pragati <pragati.k@…>', 'Chamoli, Lavanya', 'Shreya Panchal <careers@betterworks…>' are people, not companies."""
    name = re.sub(r"\s*\(.*?\)|\s+from\s+.*$|\s*<.*$", "", name or "").strip().strip('"')
    words = [w for w in re.split(r"[\s,]+", name) if w]
    if not (1 <= len(words) <= 3) or not all(re.fullmatch(r"[A-Za-z][A-Za-z.'’-]*", w) for w in words):
        return False
    if COMPANYISH.search(name) or ROLE_WORDS.search(name):
        return False
    tokens = [w.lower().strip(".") for w in words if len(w.strip(".")) >= 3]
    bare_local, bare_domain = re.sub(r"[^a-z]", "", local), re.sub(r"[^a-z]", "", domain)
    if tokens and bare_local == "".join(tokens):
        return not portal  # 'gevernova@myworkday.com' is a Workday tenant, 'pragati@x.com' a person
    if any(t in bare_domain for t in tokens):
        return False  # 'Visa <careers@visa.com>'
    if portal:
        return not re.match(r"no-?reply|do-?not-?reply|donotreply", local) and (
            bool(GENERIC_LOCAL.match(local)) or any(t in bare_local for t in tokens))
    return True


def company_from_sender(name, addr):
    local, _, domain = addr.lower().partition("@")
    portal = next((label for d, label in PORTALS.items() if domain == d or domain.endswith("." + d)), "")
    tenant = ""
    if portal and domain.endswith(("myworkday.com", "icims.com")):
        t = re.sub(r"[+_.-].*", "", local)
        if t and not re.match(r"no-?reply|donotreply|workday", t):
            tenant = good_company(re.sub(r"\d+$", "", t))
    if re.search(r"\bfrom\s+([A-Za-z][\w&. -]{1,30})$", name or ""):  # "Zara from micro1"
        return good_company(re.search(r"\bfrom\s+(.+)$", name).group(1)), portal, tenant
    if looks_like_person(name, local, domain, portal):
        return "", portal, tenant
    raw = re.sub(r"\s*<.*", "", name or "")
    raw = re.sub(r"[_]+", " ", raw)
    raw = re.sub(r"(?<=[A-Za-z])(?=(?:Recruit\w*|Talent|Careers?|Workday|Notifications?|Hiring|Touch|Jobs)\b)", " ", raw)
    raw = re.sub(r"\b(?:TA|in Touch|ITM|attraction and acquisition|auto ?notification|system)\b", " ", raw, flags=re.I)
    words = []
    for w in SENDER_WORDS.sub(" ", raw).split():
        if not words or w.lower() != words[-1].lower():
            words.append(w)
    display = good_company(" ".join(words))
    if display and display.lower() not in {p.lower() for p in PORTALS.values()}:
        return display, portal, tenant
    return "", portal, tenant


def company_from_domain(addr):
    domain = addr.lower().partition("@")[2]
    if not domain or domain in C().PERSONAL_DOMAINS or any(domain == d or domain.endswith("." + d) for d in PORTALS):
        return ""
    return good_company(C().company_from_domain(re.sub(r"^(?:(?:mail|email|hr|careers?|jobs|talent|people|notifications?|"
                                                         r"recruit\w*|qazmail|messages?|e|m)\.)+", "", domain)))


def extract(subject, sender_name, sender_addr, body, me=()):
    subj = tidy(re.sub(r"^(?:(?:re|fw|fwd|reg)\s*:\s*)+", "", subject, flags=re.I), 200)
    from_company, portal, tenant = company_from_sender(sender_name, sender_addr)
    company = (from_company or first_match(SUBJ_COMPANY, subj, good_company)
               or first_match(BODY_COMPANY, body[:2500], good_company) or tenant or company_from_domain(sender_addr))
    check = lambda r: good_role(r, me)  # noqa: E731
    role = first_match(SUBJ_ROLE, subj, check) or first_match(BODY_ROLE, body[:3000], check)
    if role and company and norm_company(role) == norm_company(company):
        role = ""
    jid = ""
    for text in (subj, body[:4000]):
        m = JOB_ID.search(text) or JOB_ID2.search(text)
        if m:
            jid = m.group(1).strip("-_")
            break
    return {"company": company, "role": role, "job_id": jid, "portal": portal}


def snippet(body, phrase):
    if not phrase:
        return tidy(body[:220], 220)
    i = body.lower().find(phrase.lower()[:40])
    if i < 0:
        return tidy(body[:220], 220)
    start = max(body.rfind("\n", 0, i), body.rfind(". ", 0, i) + 1, i - 160)
    return tidy(body[start:i + 220], 240)


# ---- Naukri emails list several jobs in one message

def naukri_events(subject, body, when):
    events = []
    if re.search(r"you applied for \d+ jobs?", subject, re.I):
        block = re.search(r"Applied on [^\n]+\n(.*?)(?:\nTrack applications|\nSimilar jobs)", body, re.S)
        lines = [ln.strip() for ln in (block.group(1).split("\n") if block else []) if ln.strip()]
        piped = [ln for ln in lines if ln.count("|") >= 1]
        pairs = ([(ln.split("|")[1], ln.split("|")[0]) for ln in piped] if piped
                 else [(lines[i], lines[i + 1]) for i in range(0, len(lines) - 1, 2)])
        for role, comp in pairs:
            role, comp = good_role(role), good_company(re.sub(r"\s*\(.*$", "", comp))
            if role and comp:
                events.append({"company": comp, "role": role, "status": "applied", "phrase": "Applied on Naukri"})
    elif re.search(r"status of your job application", subject, re.I):
        seen = set()
        for m in re.finditer(r"\n([^\n|]{2,60})\|\s*([^\n|]{3,90})(?:\|[^\n]*)?\n", body):
            comp, role = good_company(m.group(1)), good_role(m.group(2))
            if comp and role and (comp, role) not in seen:
                seen.add((comp, role))
                events.append({"company": comp, "role": role, "status": "in_review",
                               "phrase": "Recruiter activity on your Naukri application"})
    return events


# ================================================================= mailbox scan

SEARCH_TERMS = ['"your application"', '"application status"', 'applied', 'applying', 'interview', 'assessment',
                '"offer letter"', 'candidature', 'candidacy', 'shortlisted', '"thank you for your interest"', 'regret',
                '"next steps"', '"not selected"', '"other candidates"', '"application received"', '"application for"']
ATS_FROM = ("myworkday.com", "lever.co", "greenhouse-mail.io", "greenhouse.io", "icims.com", "smartrecruiters.com",
            "successfactors.com", "brassring.com", "avature.net", "ashbyhq.com", "rippling.com", "jobvite.com",
            "taleo.net", "oraclecloud.com", "workablemail.com", "naukri.com", "jobs-noreply@linkedin.com")


def gmail_query(since):
    q = "(" + " OR ".join(SEARCH_TERMS) + " OR " + " OR ".join(f"from:{d}" for d in ATS_FROM) + ") -from:me -in:chats"
    if since:
        q += f" after:{since.strftime('%Y/%m/%d')}"
    return q


def search_ids(imap, since, is_gmail):
    if is_gmail:
        typ, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
        if typ == "OK":
            q = gmail_query(since).replace("\\", "\\\\").replace('"', '\\"')
            typ, data = imap.search(None, "X-GM-RAW", f'"{q}"')
            if typ == "OK":
                return data[0].split() if data and data[0] else []
    imap.select("INBOX", readonly=True)
    crit = [f"SINCE {since.strftime('%d-%b-%Y')}"] if since else []
    ids = set()
    for term in ("application", "applying", "applied", "interview", "assessment", "offer", "shortlisted", "candidature"):
        typ, data = imap.search(None, *crit, "SUBJECT", f'"{term}"')
        if typ == "OK" and data and data[0]:
            ids.update(data[0].split())
    for dom in ATS_FROM:
        typ, data = imap.search(None, *crit, "FROM", f'"{dom}"')
        if typ == "OK" and data and data[0]:
            ids.update(data[0].split())
    return sorted(ids, key=int)


def fetch_batch(imap, nums, what):
    """{num: bytes} for a batch of message numbers."""
    out = {}
    typ, data = imap.fetch(b",".join(nums).decode(), what)
    if typ != "OK":
        return out
    for item in data:
        if isinstance(item, tuple) and item[0]:
            m = re.match(rb"(\d+) ", item[0])
            if m:
                out[m.group(1)] = item[1]
    return out


SYNC_LOCK = {}  # uid -> Lock; one sync per account at a time
PROGRESS = {}   # uid -> {"stage", "done", "total", "started"}


def scan(ws, full=False, notify=True):
    """Read application emails and update ws['applications']. Returns a summary dict."""
    core = C()
    lock = SYNC_LOCK.setdefault(ws.uid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise core.Invalid("A sync is already running.", status=409)
    state = ws.load("apps_sync", {})
    PROGRESS[ws.uid] = {"stage": "Connecting to your mailbox…", "done": 0, "total": 0, "started": time.time()}
    try:
        profile = ws.profile()
        imap = core.imap_connect(profile)
        is_gmail = core.imap_host_for(profile) == "imap.gmail.com"
        since = None
        if not full and state.get("full_done") and state.get("last_ok"):
            since = datetime.fromisoformat(state["last_ok"]).date() - timedelta(days=3)
        seen = set(ws.load("apps_seen", []))
        apps = ws.load("applications", {})
        me = (profile.get("email") or "").lower()
        me_names = my_name_parts(profile)
        ME_NAMES.set(me_names)
        before = {k: v.get("status") for k, v in apps.items()}
        added = updated = mails = 0
        changes = []
        try:
            PROGRESS[ws.uid]["stage"] = "Searching your mailbox…"
            ids = search_ids(imap, since, is_gmail)
            PROGRESS[ws.uid].update(stage="Reading emails…", total=len(ids))
            wanted = []
            for i in range(0, len(ids), 200):
                chunk = ids[i:i + 200]
                heads = fetch_batch(imap, chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)])")
                for num in chunk:
                    h = core.email_lib.message_from_bytes(heads.get(num, b""), policy=core.email_policy)
                    mid = str(h.get("Message-ID") or f"{num}-{h.get('Date')}").strip()
                    key = hashlib.sha256(("appmail:" + mid).encode()).hexdigest()[:24]
                    if key in seen:
                        continue
                    subject, sender = str(h.get("Subject") or ""), str(h.get("From") or "")
                    if me and me in sender.lower():
                        seen.add(key)
                        continue
                    if NOISE_SUBJECT.search(subject) or NOISE_FROM.search(sender):
                        seen.add(key)
                        continue
                    wanted.append((num, key, mid, subject, sender, str(h.get("Date") or "")))
            PROGRESS[ws.uid].update(total=len(wanted), done=0)
            for i in range(0, len(wanted), 25):
                chunk = wanted[i:i + 25]
                bodies = fetch_batch(imap, [c[0] for c in chunk], "(BODY.PEEK[]<0.400000>)")
                for num, key, mid, subject, sender, date_h in chunk:
                    seen.add(key)
                    raw = bodies.get(num)
                    if not raw:
                        continue
                    mails += 1
                    try:
                        msg = core.email_lib.message_from_bytes(raw, policy=core.email_policy)
                        a, u, ch = ingest(apps, msg, mid, subject, sender, date_h, me_names)
                    except Exception as e:  # one odd email mustn't stop the scan
                        print(f"[Reachout] apps: skipped a message ({e})", flush=True)
                        continue
                    added += a
                    updated += u
                    changes += ch
                PROGRESS[ws.uid]["done"] = min(len(wanted), i + 25)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        with ws.lock:  # merge into what's saved now: edits you made while the scan ran are kept
            apps = merge_scanned(ws.load("applications", {}), apps, set(before))
            ws.save("applications", apps)
            ws.save("apps_seen", sorted(seen)[-40000:])
        now = datetime.now().isoformat(timespec="seconds")
        state.update(last_run=now, last_ok=now, last_error="", full_done=state.get("full_done") or since is None,
                     emails=mails, added=added, updated=updated)
        ws.save("apps_sync", state)
        moved = [(k, before.get(k), apps[k]["status"]) for k in apps if k in before and before[k] != apps[k]["status"]]
        if notify:
            announce(ws, apps, moved, added, full=since is None)
        return {"emails": mails, "added": added, "updated": updated, "changed": len(moved), "total": len(apps)}
    except Exception as e:
        state.update(last_run=datetime.now().isoformat(timespec="seconds"),
                     last_error=str(e) if isinstance(e, core.Invalid) else f"Sync failed: {e}")
        ws.save("apps_sync", state)
        raise
    finally:
        PROGRESS.pop(ws.uid, None)
        lock.release()


def ingest(apps, msg, mid, subject, sender, date_h, me_names=()):
    """Turn one email into events on the applications dict. Returns (added, updated, changes)."""
    core = C()
    name, addr = parseaddr(sender)
    body = body_text(msg)
    try:
        when = core.parsedate_to_datetime(date_h).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        when = datetime.now()
    base = {"mid": mid, "subject": tidy(subject, 200), "from": tidy(name or addr, 80), "from_addr": addr.lower(),
            "at": when.isoformat(timespec="seconds")}
    if addr.lower().endswith("naukri.com") and (evs := naukri_events(subject, body, when)):
        events = [{**base, **e, "portal": "Naukri", "job_id": "", "snippet": e["phrase"]} for e in evs]
    else:
        status, phrase = classify(subject, body[:6000])
        if not status:
            return 0, 0, []
        info = extract(subject, name, addr, body, me_names)
        if not info["company"]:
            return 0, 0, []
        ev = {**base, **info, "status": status, "snippet": snippet(body, phrase)}
        if status == "interview":
            start = calendar_start(msg)
            if start:
                ev["interview_at"] = start.isoformat(timespec="minutes")
            if re.search(r"\bmissed interview\b|\bno[- ]show\b", subject, re.I):
                ev["note"] = "Missed interview"
        if addr.lower().endswith("linkedin.com"):
            ev["portal"] = "LinkedIn"
        events = [ev]
    added = updated = 0
    for ev in events:
        app, new = match(apps, ev)
        if any(h.get("mid") == ev["mid"] and h["status"] == ev["status"] for h in app["history"]):
            continue
        app["history"].append({k: ev.get(k) for k in ("status", "at", "subject", "from", "snippet", "mid", "interview_at", "note")
                               if ev.get(k)})
        for k in ("role", "job_id", "portal"):
            if ev.get(k) and not app.get(k):
                app[k] = ev[k]
        added += new
        updated += not new
    return added, updated, []


def app_id(company, role, jid=""):
    return hashlib.sha256(f"{norm_company(company)}|{norm_role(role)}|{jid}".encode()).hexdigest()[:16]


def match(apps, ev):
    """Find the application an event belongs to, or create it. Returns (app, created)."""
    nc, nr, jid = norm_company(ev["company"]), norm_role(ev.get("role")), (ev.get("job_id") or "").upper()
    same = [a for a in apps.values() if norm_company(a["company"]) == nc and not a.get("merged_into")]
    pick = None
    if jid:
        pick = next((a for a in same if (a.get("job_id") or "").upper() == jid), None)
    if not pick and nr:
        pick = next((a for a in same if norm_role(a.get("role")) == nr), None) or \
            next((a for a in same if a.get("role") and (nr in norm_role(a["role"]) or norm_role(a["role"]) in nr)), None)
        if not pick and ev["status"] != "applied":
            pick = next((a for a in sorted(same, key=lambda a: a.get("updated_at", ""), reverse=True) if not a.get("role")), None)
    if not pick and not nr and not jid and same:
        ordered = sorted(same, key=lambda a: max((h["at"] for h in a["history"]), default=""), reverse=True)
        if ev["status"] == "applied":
            # A confirmation without a role is its own application, unless it repeats one from the same day.
            day = ev["at"][:10]
            pick = next((a for a in ordered if any(h["status"] == "applied" and h["at"][:10] == day for h in a["history"])), None)
        else:
            # An update that doesn't say which role: the most recent one still open, else the most recent.
            open_ = [a for a in ordered if (a["history"] and a["history"][-1]["status"]) in ACTIVE]
            pick = open_[0] if open_ else None
    if pick:
        return pick, False
    aid = app_id(ev["company"], ev.get("role", ""), jid if not nr else "")
    while aid in apps:
        aid = hashlib.sha256((aid + ev["mid"]).encode()).hexdigest()[:16]
    apps[aid] = {"id": aid, "company": ev["company"], "role": ev.get("role", ""), "job_id": ev.get("job_id", ""),
                 "portal": ev.get("portal", ""), "history": [], "created": datetime.now().isoformat(timespec="seconds")}
    return apps[aid], True


HISTORY_MAX = 200  # events kept per application


def merge_scanned(current, scanned, before):
    """Fold a finished mailbox scan into the latest saved applications.
    The scan only adds applications and history events (and fills blanks). What you did meanwhile wins:
    edits, hides, manual statuses; an application you deleted or merged during the scan stays gone."""
    for aid, a in scanned.items():
        cur = current.get(aid)
        if cur is None:
            if aid not in before:  # genuinely new from this scan
                current[aid] = a
            continue
        have = {(h.get("at"), h.get("status"), h.get("mid")) for h in cur.get("history", [])}
        cur.setdefault("history", []).extend(h for h in a.get("history", []) if (h.get("at"), h.get("status"), h.get("mid")) not in have)
        for k, v in a.items():
            if k not in ("history", "status", "prev_status", "hidden", "manual_status", "notes") and v and not cur.get(k):
                cur[k] = v
    for a in current.values():
        if len(a.get("history", [])) > HISTORY_MAX:
            a["history"] = sorted(a["history"], key=lambda h: h.get("at", ""))[-HISTORY_MAX:]
        recompute(a)
    return current


def recompute(a):
    """Derive applied_at / status / previous status / interview time from the history."""
    hist = sorted(a["history"], key=lambda h: (h["at"], STATUSES.get(h["status"], ("", 0))[1]))
    a["history"] = hist
    if not hist:
        return a
    applied = [h for h in hist if h["status"] == "applied"]
    a["applied_at"] = (applied[0] if applied else hist[0])["at"]
    a["updated_at"] = hist[-1]["at"]
    manual = a.get("manual_status")
    # Current status: the latest event; a later plain "applied" confirmation doesn't undo real progress.
    statuses = []
    for h in hist:
        s = h["status"]
        if statuses and s == "applied" and statuses[-1] != "applied" and STATUSES[statuses[-1]][1] > 1:
            continue
        if not statuses or statuses[-1] != s:
            statuses.append(s)
    a["status"] = manual or statuses[-1]
    a["prev_status"] = (statuses[-1] if manual and manual != statuses[-1] else statuses[-2] if len(statuses) > 1 else "")
    iv = [h for h in hist if h["status"] == "interview"]
    a["interview_at"] = next((h["interview_at"] for h in reversed(iv) if h.get("interview_at")), "")
    a["events"] = len(hist)
    a["contacted"] = any(h["status"] in ("shortlisted", "interview", "assessment", "offer") for h in hist)
    return a


def announce(ws, apps, moved, added, full):
    notify = getattr(bridge, "notify", None)
    if not notify:
        return
    if full and added:
        notify(ws.uid, f"Found {added} job application{'s' if added != 1 else ''}",
               "Your inbox scan is done. See them all under Applications.", "#applications", "application")
        return
    for aid, old, new in moved[:8]:
        a = apps[aid]
        label = STATUSES.get(new, (new,))[0]
        title = f"{a['company']}: {label}"
        body = (a.get("role") or "Your application") + (f" · was {STATUSES.get(old, (old,))[0]}" if old else "")
        notify(ws.uid, title, body, f"#applications/{aid}", "offer" if new == "offer" else "application")
    if len(moved) > 8:
        notify(ws.uid, f"{len(moved) - 8} more application updates", "Open Applications to see them.", "#applications", "application")
    if added and not full:
        notify(ws.uid, f"{added} new application{'s' if added != 1 else ''} found", "From today's mail sync.", "#applications", "application")


# ================================================================= daily sync (the "cron")

DEFAULT_SYNC = {"auto": True, "time": "06:00", "tz": "Asia/Kolkata"}


def sync_prefs(ws):
    return {**DEFAULT_SYNC, **ws.load("apps_sync_prefs", {})}


def next_run(p, last_auto=""):
    try:
        tz = ZoneInfo(p["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    hh, mm = (int(x) for x in p["time"].split(":"))
    run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now >= run and last_auto == now.date().isoformat():
        run += timedelta(days=1)
    elif now >= run:
        return now  # due (catch-up)
    return run


def daily_tick():
    """Every minute: run each account's morning sync once per local day, catching up if the app was off at 6."""
    core = C()
    for row in core.M.users.find({}, {"_id": 1}):
        ws = core.Workspace(row["_id"])
        p = sync_prefs(ws)
        if not p["auto"] or not ws.profile().get("smtp_password"):
            continue
        state = ws.load("apps_sync", {})
        try:
            tz = ZoneInfo(p["tz"])
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        hh, mm = (int(x) for x in p["time"].split(":"))
        if (now.hour, now.minute) < (hh, mm) or state.get("last_auto") == now.date().isoformat():
            continue
        state["last_auto"] = now.date().isoformat()
        ws.save("apps_sync", state)
        threading.Thread(target=morning_sync, args=(ws.uid,), daemon=True, name=f"morning-sync-{ws.uid[:6]}").start()


MAIL_SYNC = {}  # uid -> {"stage", "step", "steps", "started", "full"} while a mail sync runs


def sync_all(uid, full=False, reason="manual"):
    """One mail sync for everything that reads the mailbox: replies, applications, the inbox by company,
    job alerts and bounces. Used by the daily schedule and by the single "Sync now" in mail settings."""
    core = C()
    ws = core.Workspace(uid)
    if uid in MAIL_SYNC:
        return
    steps = [("Checking replies", "replies"), ("Updating applications", "apps"), ("Sorting your inbox by company", "inbox"),
             ("Reading job alerts", "jobs"), ("Checking bounced emails", "bounces")]
    MAIL_SYNC[uid] = {"stage": steps[0][0], "step": 0, "steps": len(steps), "started": time.time(), "full": full}
    parts, errors = [], []
    try:
        for i, (label, key) in enumerate(steps):
            MAIL_SYNC[uid].update(stage=label, step=i)
            try:
                if key == "replies":
                    from features import replies as feature_replies
                    n = feature_replies.scan(ws, first_days=60 if full else 21).get("found", 0)
                    if n:
                        parts.append(f"{n} new repl{'y' if n == 1 else 'ies'}")
                elif key == "apps":
                    r = scan(ws, full=full or not ws.load("apps_sync", {}).get("full_done"), notify=reason != "first")
                    if r["changed"] or r["added"]:
                        parts.append(f"{r['changed']} status change{'s' if r['changed'] != 1 else ''}, {r['added']} new application{'s' if r['added'] != 1 else ''}")
                elif key == "inbox":
                    from features import inbox as feature_inbox
                    n = feature_inbox.scan(ws, full=full)["added"]
                    if n:
                        parts.append(f"{n} new email{'s' if n != 1 else ''} sorted")
                elif key == "jobs":
                    from features import jobs as feature_jobs
                    j = feature_jobs.scan_alerts(ws, days=30 if full else 3)
                    if j["added"]:
                        parts.append(f"{j['added']} new job{'s' if j['added'] != 1 else ''} from alerts")
                elif key == "bounces":
                    core.check_bounces(ws, days=14 if full else 4)
            except Exception as e:
                msg = str(getattr(e, "message", e)).splitlines()[0][:160]
                if "already running" not in msg:
                    errors.append(f"{label.split()[-1]}: {msg}")
        state = ws.load("mail_sync", {})
        state.update(last_ok=datetime.now().isoformat(timespec="seconds"), last_error="; ".join(errors)[:400],
                     summary="; ".join(parts) or "Everything was already up to date", last_reason=reason)
        ws.save("mail_sync", state)
    finally:
        MAIL_SYNC.pop(uid, None)
    notify = getattr(bridge, "notify", None)
    if notify and reason != "silent":
        title = {"daily": "Morning mail sync done"}.get(reason, "Mail sync done")
        body = ("; ".join(parts) or "Everything was already up to date") + "." + (f" Some parts failed: {'; '.join(errors)}" if errors else "")
        notify(uid, title, body[:1].upper() + body[1:], "#profile", "sync")


def morning_sync(uid):
    sync_all(uid, reason="daily")


def start_workers():
    bridge.every(60, daily_tick, "daily-sync")


# ================================================================= API

def public(a):
    out = {k: a.get(k, "") for k in ("id", "company", "role", "job_id", "portal", "status", "prev_status", "applied_at",
                                     "updated_at", "interview_at", "events", "contacted", "hidden", "notes", "url",
                                     "location", "manual_status")}
    return out


def run_in_background(ws, full):
    def go():
        try:
            scan(ws, full=full)
        except Exception as e:
            print(f"[Reachout] apps sync: {e}", flush=True)
    threading.Thread(target=go, daemon=True, name=f"apps-sync-{ws.uid[:6]}").start()


@bp.get("/api/apps")
@login_required
def list_apps(ws):
    apps = ws.load("applications", {})
    for a in apps.values():
        if "status" not in a:
            recompute(a)
    state, p = ws.load("apps_sync", {}), sync_prefs(ws)
    nxt = next_run(p, state.get("last_auto", ""))
    return jsonify(apps=[public(a) for a in apps.values() if not a.get("merged_into")],
                   statuses={k: {"label": v[0], "rank": v[1], "terminal": v[2]} for k, v in STATUSES.items()},
                   sync={**{k: state.get(k) for k in ("last_run", "last_ok", "last_error", "full_done", "emails", "added", "updated")},
                         "running": ws.uid in PROGRESS, "progress": PROGRESS.get(ws.uid), "prefs": p,
                         "next_run": nxt.isoformat(timespec="minutes") if p["auto"] else None},
                   email_ready=bool(ws.profile().get("smtp_password")))


@bp.get("/api/apps/<aid>")
@login_required
def get_app(ws, aid):
    a = ws.load("applications", {}).get(aid)
    if not a:
        raise C().Invalid("That application no longer exists.", status=404)
    recompute(a)
    return jsonify(app=public(a), history=[{k: h.get(k, "") for k in ("status", "at", "subject", "from", "snippet", "mid",
                                                                        "interview_at", "note", "source")} for h in a["history"]])


@bp.post("/api/apps/sync")
@login_required
def sync_now(ws):
    if not ws.profile().get("smtp_password"):
        raise C().Invalid("Set up email on the Profile page first, so Reachout can read your inbox.")
    if ws.uid in PROGRESS:
        return jsonify(ok=True, running=True)
    full = bool(C().body().get("full")) or not ws.load("apps_sync", {}).get("full_done")
    run_in_background(ws, full)
    time.sleep(0.3)
    return jsonify(ok=True, running=True, full=full)


@bp.get("/api/apps/progress")
@login_required
def progress(ws):
    return jsonify(running=ws.uid in PROGRESS, progress=PROGRESS.get(ws.uid),
                   last_error=ws.load("apps_sync", {}).get("last_error", ""))


@bp.put("/api/apps/sync-prefs")
@login_required
def save_sync_prefs(ws):
    core, p = C(), C().body()
    cur = sync_prefs(ws)
    if "auto" in p:
        cur["auto"] = bool(p["auto"])
    if "time" in p:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(p["time"])):
            raise core.Invalid("Pick a time like 06:00.", "time")
        cur["time"] = p["time"]
    if "tz" in p:
        try:
            ZoneInfo(str(p["tz"]))
            cur["tz"] = str(p["tz"])
        except Exception:
            raise core.Invalid("Unknown time zone.", "tz")
    ws.save("apps_sync_prefs", cur)
    return jsonify(prefs=cur, next_run=next_run(cur, ws.load("apps_sync", {}).get("last_auto", "")).isoformat(timespec="minutes"))


@bp.put("/api/apps/<aid>")
@login_required
def edit_app(ws, aid):
    core, p = C(), C().body()
    with ws.lock:
        apps = ws.load("applications", {})
        a = apps.get(aid)
        if not a:
            raise core.Invalid("That application no longer exists.", status=404)
        for k, n in (("company", 80), ("role", 120), ("job_id", 40), ("notes", 2000), ("url", 500), ("location", 80)):
            if k in p:
                v = core.v_text(p.get(k), k, k.replace("_", " ").title(), n, required=k == "company")
                if k == "url" and v and not re.match(r"https?://", v):
                    raise core.Invalid("Links must start with http:// or https://", "url")
                a[k] = v
        if "hidden" in p:
            a["hidden"] = bool(p["hidden"])
        if "status" in p:
            st = p["status"]
            if st not in STATUSES:
                raise core.Invalid("Unknown status.", "status")
            a["history"].append({"status": st, "at": datetime.now().isoformat(timespec="seconds"), "source": "you",
                                 "subject": "Updated by you", "snippet": core.v_text(p.get("note"), "note", "Note", 300) or ""})
            a.pop("manual_status", None)
        recompute(a)
        ws.save("applications", apps)
    return jsonify(app=public(a))


@bp.delete("/api/apps/<aid>")
@login_required
def delete_app(ws, aid):
    with ws.lock:
        apps = ws.load("applications", {})
        apps.pop(aid, None)
        ws.save("applications", apps)
    return jsonify(ok=True)


@bp.post("/api/apps")
@login_required
def add_app(ws):
    core, p = C(), C().body()
    company = core.v_text(p.get("company"), "company", "Company", 80, required=True)
    role = core.v_text(p.get("role"), "role", "Position", 120)
    st = p.get("status") or "applied"
    if st not in STATUSES:
        raise core.Invalid("Unknown status.", "status")
    when = str(p.get("applied_at") or "")
    try:
        at = datetime.fromisoformat(when) if when else datetime.now()
    except ValueError:
        raise core.Invalid("Pick a valid date.", "applied_at")
    if at > datetime.now() + timedelta(days=1):
        raise core.Invalid("The application date can't be in the future.", "applied_at")
    with ws.lock:
        apps = ws.load("applications", {})
        aid = app_id(company, role, str(time.time()))
        hist = [{"status": "applied", "at": at.isoformat(timespec="seconds"), "source": "you", "subject": "Added by you"}]
        if st != "applied":
            hist.append({"status": st, "at": datetime.now().isoformat(timespec="seconds"), "source": "you", "subject": "Updated by you"})
        apps[aid] = {"id": aid, "company": company, "role": role, "job_id": core.v_text(p.get("job_id"), "job_id", "Job ID", 40),
                     "portal": core.v_text(p.get("portal"), "portal", "Applied through", 40) or "Manual", "history": hist,
                     "url": str(p.get("url") or "")[:500] if re.match(r"https?://", str(p.get("url") or "")) else "",
                     "created": datetime.now().isoformat(timespec="seconds")}
        recompute(apps[aid])
        ws.save("applications", apps)
    return jsonify(app=public(apps[aid]))


# ================================================================= Naukri (email-based connection)

NAUKRI_LINKS = {
    "profile": "https://www.naukri.com/mnjuser/profile", "applies": "https://www.naukri.com/myapply/historypage",
    "recommended": "https://www.naukri.com/mnjuser/recommendedjobs", "inbox": "https://www.naukri.com/mnjuser/inbox",
    "search": "https://www.naukri.com/software-developer-jobs?jobAge=1",
}


def naukri_stats(ws):
    apps = [a for a in ws.load("applications", {}).values() if a.get("portal") == "Naukri" and not a.get("hidden")]
    for a in apps:
        if "status" not in a:
            recompute(a)
    jobs = [j for j in ws.load("jobs", {}).values() if j.get("source") == "Naukri"]
    return {"applications": len(apps), "recruiter_activity": sum(1 for a in apps if a["status"] != "applied"),
            "job_alerts": len(jobs), "recent": sorted((public(a) for a in apps), key=lambda a: a["updated_at"], reverse=True)[:8]}


@bp.get("/api/naukri")
@login_required
def naukri_status(ws):
    n = ws.load("naukri", {})
    return jsonify(connected=bool(n.get("connected")), profile_url=n.get("profile_url", ""), checked_at=n.get("checked_at"),
                   emails_found=n.get("emails_found", 0), links=NAUKRI_LINKS, stats=naukri_stats(ws),
                   email_ready=bool(ws.profile().get("smtp_password")))


@bp.post("/api/naukri/connect")
@login_required
def naukri_connect(ws):
    core, p = C(), C().body()
    url = str(p.get("profile_url") or "").strip()
    if url and not re.match(r"https://(?:www\.)?naukri\.com/", url):
        raise core.Invalid("Paste a link that starts with https://www.naukri.com/", "profile_url")
    profile = ws.profile()
    imap = core.imap_connect(profile)
    try:
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "FROM", '"naukri.com"')
        found = len(data[0].split()) if typ == "OK" and data and data[0] else 0
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    if not found:
        raise core.Invalid("No Naukri emails in this mailbox yet. Use the email address your Naukri account is registered "
                           "with on the Profile page, and turn on Naukri's job-alert and application emails.")
    ws.save("naukri", {"connected": True, "profile_url": url, "emails_found": found,
                       "checked_at": datetime.now().isoformat(timespec="seconds")})
    if ws.uid not in PROGRESS:
        run_in_background(ws, not ws.load("apps_sync", {}).get("full_done"))
    return jsonify(ok=True, emails_found=found)


@bp.post("/api/naukri/disconnect")
@login_required
def naukri_disconnect(ws):
    ws.save("naukri", {})
    return jsonify(ok=True)


# ================================================================= dashboard overview (all platforms)

GH_CACHE = {}  # uid -> (time, summary)


def github_summary(ws):
    g = ws.load("github", {})
    if not g.get("token"):
        return {"connected": False}
    hit = GH_CACHE.get(ws.uid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = {"connected": True, "login": g.get("login"), "avatar": g.get("avatar")}
    try:
        from features import github as feature_github
        repos = feature_github.gh(ws, "GET", "/user/repos?per_page=100&sort=pushed&affiliation=owner")
        out.update(repos=len(repos), private=sum(1 for r in repos if r.get("private")),
                   stars=sum(r.get("stargazers_count", 0) for r in repos),
                   recent=[{"name": r["name"], "owner": r["owner"]["login"], "pushed_at": r.get("pushed_at"),
                            "language": r.get("language")} for r in repos[:5]])
        langs = {}
        for r in repos:
            if r.get("language"):
                langs[r["language"]] = langs.get(r["language"], 0) + 1
        out["languages"] = sorted(langs.items(), key=lambda kv: -kv[1])[:6]
    except Exception as e:
        out["error"] = str(getattr(e, "message", e))[:200]
    GH_CACHE[ws.uid] = (time.time(), out)
    return out


STATUS_GROUPS = {"active": lambda a: a["status"] in ACTIVE and a["status"] != "incomplete",
                 "heard": lambda a: a["status"] != "applied",
                 "interview": lambda a: a["status"] in ("interview", "shortlisted", "assessment"),
                 "offer": lambda a: a["status"] == "offer",
                 "rejected": lambda a: a["status"] in ("rejected", "closed", "withdrawn")}


def dash_filters(core):
    """Period (days back, or from/to dates), 'applied through' and status group from the query string."""
    args = request.args
    today = datetime.now().date()
    try:
        d_from = date.fromisoformat(args["from"]) if args.get("from") else None
        d_to = date.fromisoformat(args["to"]) if args.get("to") else None
    except ValueError:
        raise core.Invalid("Pick valid dates.", "from")
    days = core.v_int(args.get("days", 365), "days", "Period", 0, 3650)
    if d_from or d_to:
        d_to = min(d_to or today, today)
        d_from = d_from or d_to - timedelta(days=364)
        if d_from > d_to:
            raise core.Invalid("The start date is after the end date.", "from")
    elif days:
        d_to, d_from = today, today - timedelta(days=days - 1)
    else:
        d_to, d_from = today, None
    portal = str(args.get("portal") or "")[:60]
    group = args.get("status") if args.get("status") in STATUS_GROUPS else ""
    return d_from, d_to, portal, group


def buckets(d_from, d_to):
    """Chart buckets for a period: days (≤ 45 days), weeks (≤ 200 days) or months."""
    span = (d_to - d_from).days + 1
    if span <= 45:
        keys = [(d_from + timedelta(days=i)).isoformat() for i in range(span)]
        return "day", keys, lambda iso: iso[:10]
    if span <= 200:
        start = d_from - timedelta(days=d_from.weekday())
        keys, d = [], start
        while d <= d_to:
            keys.append(d.isoformat())
            d += timedelta(days=7)
        return "week", keys, lambda iso: (date.fromisoformat(iso[:10]) - timedelta(days=date.fromisoformat(iso[:10]).weekday())).isoformat()
    keys, y, m = [], d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        keys.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return "month", keys, lambda iso: iso[:7]


def app_stats(apps, lo, hi):
    """Counts for applications made in [lo, hi] (ISO date strings; lo may be '')."""
    sel = [a for a in apps if lo <= (a.get("applied_at") or "")[:10] <= hi]
    heard = sum(1 for a in sel if a["status"] != "applied")
    return {"total": len(sel), "responded": heard, "response_rate": round(100 * heard / len(sel)) if sel else 0,
            "interviews": sum(1 for a in sel if any(h["status"] == "interview" for h in a["history"])),
            "offers": sum(1 for a in sel if any(h["status"] == "offer" for h in a["history"])),
            "rejected": sum(1 for a in sel if a["status"] in ("rejected", "closed"))}


@bp.get("/api/overview")
@login_required
def overview(ws):
    core = C()
    today = datetime.now().date()
    d_from, d_to, portal, group = dash_filters(core)
    all_apps = [a for a in ws.load("applications", {}).values() if not a.get("hidden") and not a.get("merged_into")]
    for a in all_apps:
        if "status" not in a:
            recompute(a)
    portal_counts = {}
    for a in all_apps:
        pk = a.get("portal") or "Company site"
        portal_counts[pk] = portal_counts.get(pk, 0) + 1
    if not d_from:  # "all time": start at the first application / send
        first = min([(a.get("applied_at") or today.isoformat())[:10] for a in all_apps] or [today.isoformat()])
        d_from = min(date.fromisoformat(first), today - timedelta(days=29))
    lo, hi = d_from.isoformat(), d_to.isoformat()
    base = [a for a in all_apps if not portal or (a.get("portal") or "Company site") == portal]
    apps = [a for a in base if lo <= (a.get("applied_at") or "")[:10] <= hi and (not group or STATUS_GROUPS[group](a))]
    # previous period of the same length, for "vs previous" deltas
    span = (d_to - d_from).days + 1
    p_hi = (d_from - timedelta(days=1)).isoformat()
    p_lo = (d_from - timedelta(days=span)).isoformat()
    prev = app_stats([a for a in base if not group or STATUS_GROUPS[group](a)], p_lo, p_hi)
    cur = app_stats(apps, lo, hi)
    by_status = {k: 0 for k in STATUSES}
    for a in apps:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    unit, keys, key_of = buckets(d_from, d_to)
    series = {k: {"applied": 0, "rejected": 0, "progress": 0} for k in keys}
    for a in apps:
        k = key_of(a.get("applied_at") or "0000-00-00")
        if k in series:
            series[k]["applied"] += 1
        for h in a["history"]:
            if not (lo <= h["at"][:10] <= hi):
                continue
            hk = key_of(h["at"])
            if hk in series and h["status"] in ("rejected", "closed"):
                series[hk]["rejected"] += 1
            elif hk in series and h["status"] in ("shortlisted", "interview", "assessment", "offer"):
                series[hk]["progress"] += 1
    funnel = [("Applied", len(apps)),
              ("Heard back", sum(1 for a in apps if a["status"] != "applied" or a.get("events", 1) > 1)),
              ("Assessment / shortlisted", sum(1 for a in apps if any(h["status"] in ("assessment", "shortlisted", "interview", "offer") for h in a["history"]))),
              ("Interview", sum(1 for a in apps if any(h["status"] in ("interview", "offer") for h in a["history"]))),
              ("Offer", sum(1 for a in apps if any(h["status"] == "offer" for h in a["history"])))]
    portals = {}
    for a in apps:
        pk = a.get("portal") or "Company site"
        portals[pk] = portals.get(pk, 0) + 1
    now_iso = datetime.now().isoformat(timespec="minutes")
    upcoming = sorted((public(a) for a in base if a.get("interview_at") and a["interview_at"] >= now_iso[:10]),
                      key=lambda a: a["interview_at"])[:6]
    recent = sorted((public(a) for a in apps), key=lambda a: a["updated_at"] or "", reverse=True)[:8]
    # ---- outreach (campaign emails + WhatsApp) in the period, bucketed like the applications chart
    o_series = {k: {"email": 0, "whatsapp": 0, "failed": 0} for k in keys}
    o_tot = {"emailed": 0, "whatsapp": 0, "failed": 0, "opened": 0, "replied": 0}
    o_prev = 0
    since = datetime.combine(d_from - timedelta(days=span), datetime.min.time()).timestamp()
    until = datetime.combine(d_to + timedelta(days=1), datetime.min.time()).timestamp()
    for row in core.M.send_log.find({"uid": ws.uid, "ts": {"$gte": since, "$lt": until}},
                                    {"day": 1, "data": 1, "ts": 1, "opens": 1, "replied_at": 1}).limit(20000):
        e = core.unseal(row["data"], {}) or {}
        day = row.get("day") or datetime.fromtimestamp(row["ts"]).date().isoformat()
        sent = e.get("status") == "sent"
        if day < lo:
            o_prev += sent
            continue
        b = o_series.get(key_of(day))
        if sent:
            ch = "email" if e.get("channel") == "email" else "whatsapp"
            o_tot["emailed" if ch == "email" else "whatsapp"] += 1
            o_tot["opened"] += bool(row.get("opens"))
            o_tot["replied"] += bool(row.get("replied_at"))
            if b:
                b[ch] += 1
        elif e.get("status") in ("failed", "bounced", "invalid", "not_on_whatsapp"):
            o_tot["failed"] += 1
            if b:
                b["failed"] += 1
    recips = ws.load("recipients", [])
    outreach = {"contacts": len(recips), **o_tot, "bounced": o_tot["failed"], "prev_sent": o_prev,
                "by_day": [{"day": k, **v} for k, v in o_series.items()]}
    # ---- jobs, queue, platforms
    from features import jobs as feature_jobs
    jobs = ws.load("jobs", {})
    jp = feature_jobs.prefs(ws)
    job_stats = {"total": len(jobs), "matches": sum(1 for j in jobs.values() if j.get("state") == "new" and feature_jobs.score_job(j, jp)[3]),
                 "saved": sum(1 for j in jobs.values() if j.get("state") == "saved"),
                 "applied": sum(1 for j in jobs.values() if j.get("state") == "applied")}
    queued = core.M.queue.count_documents({"uid": ws.uid, "status": "queued"})
    li = ws.load("linkedin", {})
    nk = ws.load("naukri", {})
    state = ws.load("apps_sync", {})
    return jsonify(
        filters={"from": lo, "to": hi, "portal": portal, "status": group, "unit": unit, "days": span,
                 "portals": sorted(portal_counts.items(), key=lambda kv: -kv[1])},
        apps={**cur, "active": sum(1 for a in apps if a["status"] in ACTIVE and a["status"] != "incomplete"),
              "prev": prev, "this_week": sum(1 for a in base if (a.get("applied_at") or "") >= (today - timedelta(days=7)).isoformat()),
              "by_status": by_status, "months": [{"month": k, **v} for k, v in series.items()], "funnel": funnel,
              "portals": sorted(portals.items(), key=lambda kv: -kv[1])[:8], "upcoming": upcoming, "recent": recent},
        statuses={k: {"label": v[0], "rank": v[1], "terminal": v[2]} for k, v in STATUSES.items()},
        outreach=outreach, jobs=job_stats, queued=queued,
        platforms={"email": bool(ws.profile().get("smtp_password")), "github": github_summary(ws),
                   "linkedin": {"connected": bool(li.get("token")) and li.get("expires_at", 0) > time.time(),
                                "name": li.get("name"), "posts": len(ws.load("li_posts", []))},
                   "naukri": {"connected": bool(nk.get("connected")), **naukri_stats(ws)}},
        sync={"last_ok": state.get("last_ok"), "last_error": state.get("last_error"), "running": ws.uid in PROGRESS,
              "next_run": next_run(sync_prefs(ws), state.get("last_auto", "")).isoformat(timespec="minutes") if sync_prefs(ws)["auto"] else None})



@bp.get("/api/mail-sync")
@login_required
def mail_sync_status(ws):
    state, p = ws.load("mail_sync", {}), sync_prefs(ws)
    apps_state = ws.load("apps_sync", {})
    last = state.get("last_ok") or apps_state.get("last_ok")
    return jsonify(running=ws.uid in MAIL_SYNC or ws.uid in PROGRESS, progress=MAIL_SYNC.get(ws.uid), last_ok=last,
                   last_error=state.get("last_error", ""), summary=state.get("summary", ""), prefs=p,
                   next_run=next_run(p, apps_state.get("last_auto", "")).isoformat(timespec="minutes")
                   if p["auto"] and ws.profile().get("smtp_password") else None,
                   email_ready=bool(ws.profile().get("smtp_password")), first_done=bool(apps_state.get("full_done")))


@bp.post("/api/mail-sync")
@login_required
def mail_sync_now(ws):
    if not ws.profile().get("smtp_password"):
        raise C().Invalid("Add your Gmail app password below first.")
    if ws.uid in MAIL_SYNC:
        return jsonify(ok=True, running=True)
    full = bool(C().body().get("full"))
    threading.Thread(target=sync_all, args=(ws.uid, full), daemon=True, name=f"mail-sync-{ws.uid[:6]}").start()
    time.sleep(0.3)
    return jsonify(ok=True, running=True)
