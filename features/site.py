"""Website builder: a one-page personal site, designed and managed in Reachout and hosted by Reachout.

- The owner edits a draft (theme + ordered sections); visitors only ever see the published copy.
- Sites live at /p/<address> (the same address as the Portfolio). The Projects section is filled from
  the Portfolio, so projects added there appear on the site.
- Visitors can write through the contact form; each message becomes a lead on the owner's Leads page
  (stored encrypted) and the owner is notified.
- Every site carries a small “Powered by Reachout” tag in the footer.
- Images the owner uploads are stored encrypted and served at /p/i/<id>.
"""

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone
from html import escape

from flask import Blueprint, Response, jsonify, request

import bridge
from bridge import login_required

bp = Blueprint("site", __name__)


def C():
    return bridge.C


# ---------------------------------------------------------------- design options

FONTS = {
    "Inter": "Inter:wght@400;500;600;700;800", "Manrope": "Manrope:wght@400;500;600;700;800",
    "DM Sans": "DM+Sans:wght@400;500;600;700", "Poppins": "Poppins:wght@400;500;600;700;800",
    "Outfit": "Outfit:wght@400;500;600;700;800", "Sora": "Sora:wght@400;500;600;700;800",
    "Space Grotesk": "Space+Grotesk:wght@400;500;600;700", "Plus Jakarta Sans": "Plus+Jakarta+Sans:wght@400;500;600;700;800",
    "IBM Plex Sans": "IBM+Plex+Sans:wght@400;500;600;700", "Lora": "Lora:wght@400;500;600;700",
    "Playfair Display": "Playfair+Display:wght@500;600;700;800", "Fraunces": "Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700",
    "DM Serif Display": "DM+Serif+Display", "JetBrains Mono": "JetBrains+Mono:wght@400;500;700",
}

PRESETS = {
    "midnight": {"label": "Midnight", "bg": "#0b0d18", "surface": "#141830", "text": "#eceefa", "muted": "#a3a9c2", "accent": "#6366f1", "accent2": "#a855f7", "font_head": "Space Grotesk", "font_body": "Inter", "background": "glow", "card": "border"},
    "paper": {"label": "Paper", "bg": "#faf7f2", "surface": "#ffffff", "text": "#1c1917", "muted": "#6b645c", "accent": "#c2410c", "accent2": "#ea580c", "font_head": "Fraunces", "font_body": "Inter", "background": "plain", "card": "raised"},
    "ocean": {"label": "Ocean", "bg": "#f2f7fb", "surface": "#ffffff", "text": "#0f2233", "muted": "#5a6b7b", "accent": "#0284c7", "accent2": "#06b6d4", "font_head": "Sora", "font_body": "DM Sans", "background": "dots", "card": "raised"},
    "forest": {"label": "Forest", "bg": "#0e1812", "surface": "#15241b", "text": "#e6f0e9", "muted": "#9db3a5", "accent": "#22c55e", "accent2": "#a3e635", "font_head": "Outfit", "font_body": "Manrope", "background": "grid", "card": "border"},
    "sunset": {"label": "Sunset", "bg": "#1a0f14", "surface": "#27161e", "text": "#fbeff2", "muted": "#c9a7b2", "accent": "#f43f5e", "accent2": "#f59e0b", "font_head": "Poppins", "font_body": "Poppins", "background": "glow", "card": "glass"},
    "mono": {"label": "Mono", "bg": "#ffffff", "surface": "#f5f5f4", "text": "#0a0a0a", "muted": "#616161", "accent": "#0a0a0a", "accent2": "#525252", "font_head": "JetBrains Mono", "font_body": "IBM Plex Sans", "background": "plain", "card": "flat"},
    "lavender": {"label": "Lavender", "bg": "#f7f5ff", "surface": "#ffffff", "text": "#1e1b3a", "muted": "#6b6890", "accent": "#7c3aed", "accent2": "#db2777", "font_head": "Plus Jakarta Sans", "font_body": "Plus Jakarta Sans", "background": "glow", "card": "raised"},
    "editorial": {"label": "Editorial", "bg": "#111111", "surface": "#1a1a1a", "text": "#f5f1e8", "muted": "#a39e93", "accent": "#e8c07d", "accent2": "#e8c07d", "font_head": "DM Serif Display", "font_body": "Lora", "background": "plain", "card": "flat"},
}

CHOICES = {
    "card": ["flat", "border", "raised", "glass"],
    "button": ["gradient", "solid", "outline", "pill"],
    "background": ["plain", "glow", "grid", "dots"],
    "spacing": ["compact", "normal", "airy"],
    "width": ["narrow", "normal", "wide"],
    "nav": ["sticky", "static", "hidden"],
    "align": ["left", "center"],
}

SOCIALS = ["email", "github", "linkedin", "x", "instagram", "youtube", "dribbble", "website", "resume"]

# Each section type: the editor builds its form from these fields; the renderer draws it.
TYPES = {
    "hero": {"label": "Intro / hero", "icon": "star", "variants": ["split", "center", "minimal"], "nav": "", "fields": [
        {"k": "eyebrow", "label": "Small line above", "type": "text", "ph": "Hi, I'm"},
        {"k": "title", "label": "Headline", "type": "text", "ph": "Your name or a bold one-liner"},
        {"k": "subtitle", "label": "Intro text", "type": "textarea", "ph": "What you do, in one or two sentences."},
        {"k": "image", "label": "Photo", "type": "image"},
        {"k": "primary_label", "label": "Main button", "type": "text", "ph": "View my work"},
        {"k": "primary_link", "label": "Main button link", "type": "link", "ph": "#projects"},
        {"k": "secondary_label", "label": "Second button", "type": "text", "ph": "Download CV"},
        {"k": "secondary_link", "label": "Second button link", "type": "link"},
        {"k": "socials", "label": "Show social links", "type": "switch"},
        {"k": "badge", "label": "Status badge", "type": "text", "ph": "Open to work"}]},
    "about": {"label": "About", "icon": "user", "variants": ["text", "image"], "nav": "About", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Text", "type": "textarea", "rows": 6, "hint": "Leave a blank line between paragraphs. **bold** and [links](https://…) work."},
        {"k": "image", "label": "Image", "type": "image"}]},
    "skills": {"label": "Skills", "icon": "zap", "variants": ["chips", "columns", "bars"], "nav": "Skills", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Intro", "type": "textarea", "rows": 2},
        {"k": "items", "label": "Skill groups", "type": "list", "add": "Add group", "fields": [
            {"k": "name", "label": "Group", "type": "text", "ph": "Frontend"},
            {"k": "items", "label": "Skills (comma separated)", "type": "text", "ph": "React, TypeScript, Tailwind",
             "hint": "For the bars style add a level: React:90, Node.js:80"}]}]},
    "projects": {"label": "Projects", "icon": "github", "variants": ["grid", "list"], "nav": "Projects", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Intro", "type": "textarea", "rows": 2},
        {"k": "limit", "label": "How many to show", "type": "select", "options": ["All", "3", "4", "6", "8", "9", "12"]},
        {"k": "note", "type": "note", "text": "Projects come from the Projects tab: add, order and edit them there."}]},
    "experience": {"label": "Experience", "icon": "briefcase", "variants": ["timeline", "cards"], "nav": "Experience", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "items", "label": "Roles", "type": "list", "add": "Add role", "fields": [
            {"k": "role", "label": "Role", "type": "text", "ph": "Software Engineer"},
            {"k": "org", "label": "Company", "type": "text"},
            {"k": "period", "label": "When", "type": "text", "ph": "2023 – Present"},
            {"k": "text", "label": "What you did", "type": "textarea", "rows": 3}]}]},
    "education": {"label": "Education", "icon": "file", "variants": ["timeline", "cards"], "nav": "Education", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "items", "label": "Entries", "type": "list", "add": "Add entry", "fields": [
            {"k": "role", "label": "Degree / course", "type": "text"},
            {"k": "org", "label": "School", "type": "text"},
            {"k": "period", "label": "When", "type": "text"},
            {"k": "text", "label": "Details", "type": "textarea", "rows": 2}]}]},
    "services": {"label": "Services / what I do", "icon": "grid", "variants": ["cards", "list"], "nav": "Services", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Intro", "type": "textarea", "rows": 2},
        {"k": "items", "label": "Items", "type": "list", "add": "Add item", "fields": [
            {"k": "icon", "label": "Emoji", "type": "text", "ph": "⚡"},
            {"k": "title", "label": "Title", "type": "text"},
            {"k": "text", "label": "Text", "type": "textarea", "rows": 2}]}]},
    "stats": {"label": "Numbers", "icon": "activity", "variants": ["row", "cards"], "nav": "", "fields": [
        {"k": "items", "label": "Numbers", "type": "list", "add": "Add number", "fields": [
            {"k": "value", "label": "Value", "type": "text", "ph": "20+"},
            {"k": "label", "label": "Label", "type": "text", "ph": "Projects shipped"}]}]},
    "testimonials": {"label": "Testimonials", "icon": "message", "variants": ["cards", "quote"], "nav": "Testimonials", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "items", "label": "Quotes", "type": "list", "add": "Add quote", "fields": [
            {"k": "quote", "label": "Quote", "type": "textarea", "rows": 3},
            {"k": "name", "label": "Name", "type": "text"},
            {"k": "role", "label": "Role / company", "type": "text"}]}]},
    "cta": {"label": "Call to action", "icon": "send", "variants": ["banner", "plain"], "nav": "", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Text", "type": "textarea", "rows": 2},
        {"k": "button_label", "label": "Button", "type": "text"},
        {"k": "button_link", "label": "Button link", "type": "link"}]},
    "text": {"label": "Text block", "icon": "note", "variants": ["plain", "card"], "nav": "", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Text", "type": "textarea", "rows": 6, "hint": "Blank line = new paragraph. **bold** and [links](https://…) work."},
        {"k": "button_label", "label": "Button", "type": "text"},
        {"k": "button_link", "label": "Button link", "type": "link"}]},
    "contact": {"label": "Contact", "icon": "mail", "variants": ["split", "center"], "nav": "Contact", "fields": [
        {"k": "heading", "label": "Heading", "type": "text"},
        {"k": "text", "label": "Text", "type": "textarea", "rows": 3},
        {"k": "form", "label": "Show a contact form (messages arrive in your Leads page)", "type": "switch"},
        {"k": "show_email", "label": "Show my email address", "type": "switch"},
        {"k": "phone", "label": "Phone", "type": "text"},
        {"k": "location", "label": "Location", "type": "text", "ph": "Noida, India"}]},
}

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LANGS = {"en": "English", "hi": "हिन्दी", "es": "Español", "fr": "Français", "de": "Deutsch", "pt": "Português", "it": "Italiano",
         "nl": "Nederlands", "ja": "日本語", "zh": "中文", "ar": "العربية", "bn": "বাংলা", "ta": "தமிழ்", "te": "తెలుగు", "mr": "मराठी"}
LOCALES = {"en": "en_US", "hi": "hi_IN", "es": "es_ES", "fr": "fr_FR", "de": "de_DE", "pt": "pt_BR", "it": "it_IT", "nl": "nl_NL",
           "ja": "ja_JP", "zh": "zh_CN", "ar": "ar_AR", "bn": "bn_IN", "ta": "ta_IN", "te": "te_IN", "mr": "mr_IN"}


def meta():
    return {"presets": PRESETS, "fonts": list(FONTS), "choices": CHOICES, "types": TYPES, "socials": SOCIALS, "langs": LANGS}


# ---------------------------------------------------------------- draft / validation

def new_id():
    return uuid.uuid4().hex[:8]


def section(kind, variant=None, **data):
    t = TYPES[kind]
    return {"id": new_id(), "type": kind, "on": True, "nav": t["nav"], "variant": variant or t["variants"][0], "data": data}


def seed(ws):
    """A sensible first site from what Reachout already knows (name, GitHub, resume links, job preferences)."""
    core = C()
    user = core.find_user(uid=ws.uid) or {}
    prof = ws.profile() or {}
    name = user.get("name") or prof.get("name") or "Your Name"
    gh = ws.load("github", {})
    jp = ws.load("job_prefs", {}) or {}
    try:
        from features import replies as feature_replies
        links = feature_replies.resume_links(ws)
    except Exception:
        links = {}
    role = (jp.get("roles") or ["Software Engineer"])[0]
    skills = jp.get("skills") or []
    socials = {"email": prof.get("email") or user.get("email", ""), "github": f"https://github.com/{gh['login']}" if gh.get("login") else "",
               "linkedin": links.get("linkedin", ""), "website": ""}
    groups = [{"name": "Core skills", "items": ", ".join(skills[:14])}] if skills else [
        {"name": "Frontend", "items": "React, TypeScript, HTML, CSS"}, {"name": "Backend", "items": "Node.js, Python, REST APIs, MongoDB"}]
    first = name.split()[0]
    return {
        "theme": {"preset": "midnight", **{k: v for k, v in PRESETS["midnight"].items() if k != "label"},
                  "radius": 16, "button": "gradient", "spacing": "normal", "width": "normal", "nav": "sticky", "align": "left", "animate": True},
        "profile": {"name": name, "socials": socials},
        "seo": {"title": f"{name} · {role}", "description": f"{name} is a {role}. Projects, experience and how to get in touch.", "favicon": "✦", "image": "", "lang": "en", "index": True, "google": ""},
        "sections": [
            section("hero", "split", eyebrow=f"Hi, I'm {first} 👋", title=role, subtitle=f"I build fast, reliable products people enjoy using. {f'{jp['years']:g}+ years of experience. ' if jp.get('years') else ''}Currently open to new opportunities.",
                    image=f"https://github.com/{gh['login']}.png?size=460" if gh.get("login") else "", primary_label="View my work", primary_link="#projects",
                    secondary_label="Get in touch", secondary_link="#contact", socials=True, badge="Open to work"),
            section("about", "text", heading="About me", text=f"I'm {name}, a {role.lower()} who enjoys turning ideas into polished, working software.\n\nTell visitors what you care about, what you're great at and what you're looking for next."),
            section("skills", "chips", heading="Skills", text="", items=groups),
            section("projects", "grid", heading="Selected projects", text="A few things I've built recently.", limit="All"),
            section("experience", "timeline", heading="Experience", items=[{"role": role, "org": "Company name", "period": "2023 – Present", "text": "What you built, owned or improved. Numbers help."}]),
            section("contact", "split", heading="Let's work together", text="Have a role, a project or a question? Send a message and I'll reply soon.", form=True, show_email=True, phone="", location=""),
        ],
    }


def text(v, n):
    return str(v if v is not None else "")[:n].replace("\x00", "")


def clean(cfg):
    """Validate a draft from the editor: known keys only, sizes capped, colours and fonts checked."""
    core = C()
    if not isinstance(cfg, dict):
        raise core.Invalid("Bad request.")
    if len(json.dumps(cfg)) > 300_000:
        raise core.Invalid("Your site is too large. Shorten some sections.")
    t_in = cfg.get("theme") or {}
    theme = {"preset": text(t_in.get("preset"), 30)}
    for k in ("bg", "surface", "text", "muted", "accent", "accent2"):
        v = str(t_in.get(k) or "")
        if not HEX.match(v):
            raise core.Invalid(f"“{v or 'empty'}” isn't a colour. Use a hex code like #6366f1.", k)
        theme[k] = v.lower()
    for k in ("font_head", "font_body"):
        theme[k] = t_in.get(k) if t_in.get(k) in FONTS else "Inter"
    for k, opts in CHOICES.items():
        theme[k] = t_in.get(k) if t_in.get(k) in opts else opts[1 if k in ("card", "spacing", "width") else 0]
    theme["radius"] = max(0, min(32, core.safe_int(t_in.get("radius"), 16)))
    theme["animate"] = bool(t_in.get("animate", True))

    p_in = cfg.get("profile") or {}
    socials = {k: text((p_in.get("socials") or {}).get(k), 300).strip() for k in SOCIALS}
    if socials["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", socials["email"]):
        raise core.Invalid("Enter a valid email address.", "email")
    for k, v in socials.items():
        if k != "email" and v and not re.match(r"^(https?://|/p/i/)", v):
            socials[k] = "https://" + v
    profile = {"name": text(p_in.get("name"), 80).strip() or "Your Name", "socials": socials}

    s_in = cfg.get("seo") or {}
    google = str(s_in.get("google") or "").strip()
    m = re.search(r'content=["\']([^"\']+)', google)  # accept the whole <meta> tag Google gives you
    google = m.group(1) if m else google
    if google and not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", google):
        raise core.Invalid("Paste the verification code (or the whole <meta> tag) from Google Search Console.", "google")
    seo = {"title": text(s_in.get("title"), 90), "description": text(s_in.get("description"), 300), "favicon": text(s_in.get("favicon"), 4),
           "image": safe_url(s_in.get("image")), "lang": s_in.get("lang") if s_in.get("lang") in LANGS else "en",
           "index": bool(s_in.get("index", True)), "google": google}

    sections = []
    for s in (cfg.get("sections") or [])[:24]:
        if not isinstance(s, dict) or s.get("type") not in TYPES:
            continue
        t = TYPES[s["type"]]
        data = {}
        for f in t["fields"]:
            v = (s.get("data") or {}).get(f["k"])
            if f["type"] == "switch":
                data[f["k"]] = bool(v)
            elif f["type"] == "list":
                rows = []
                for row in (v if isinstance(v, list) else [])[:30]:
                    if isinstance(row, dict):
                        rows.append({sf["k"]: text(row.get(sf["k"]), 1500) for sf in f["fields"]})
                data[f["k"]] = rows
            elif f["type"] != "note":
                data[f["k"]] = text(v, 4000 if f["type"] == "textarea" else 500)
        sections.append({"id": s.get("id") if re.fullmatch(r"[a-z0-9]{4,16}", str(s.get("id") or "")) else new_id(), "type": s["type"],
                         "on": bool(s.get("on", True)), "nav": text(s.get("nav"), 24),
                         "variant": s.get("variant") if s.get("variant") in t["variants"] else t["variants"][0], "data": data})
    return {"theme": theme, "profile": profile, "seo": seo, "sections": sections}


def state(ws):
    st = ws.load("website", {})
    if not st.get("draft"):
        draft = seed(ws)
        with ws.lock:
            st = ws.load("website", {})
            if not st.get("draft"):
                st = {"draft": draft, "published": None, "published_at": None, "online": False, "created": time.time()}
                ws.save("website", st)
    return st


# ---------------------------------------------------------------- rendering

def safe_url(u):
    u = str(u or "").strip()
    if re.match(r"^(https?://|mailto:|tel:|#|/p/i/)", u, re.I) and not re.search(r"[\s<>\"']", u):
        return u
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", u, re.I):
        return "https://" + u
    return ""


def md(s):
    """Tiny, safe formatting: paragraphs, line breaks, **bold**, *italic*, [label](url)."""
    out = []
    for para in re.split(r"\n\s*\n", str(s or "").strip()):
        h = escape(para)
        h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
        h = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", h)

        def link(m):
            u = safe_url(m.group(2).replace("&amp;", "&"))
            return f'<a href="{escape(u)}" target="_blank" rel="noopener">{m.group(1)}</a>' if u else m.group(1)
        h = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, h)
        out.append("<p>" + h.replace("\n", "<br>") + "</p>")
    return "".join(out) if out != ["<p></p>"] else ""


ICONS = {
    "github": '<path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.4 5.4 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4"/><path d="M9 18c-4.51 2-5-2-7-2"/>',
    "linkedin": '<path d="M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-4 0v7h-4v-7a6 6 0 0 1 6-6z"/><rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/>',
    "x": '<path d="M4 4l11.7 16H20L8.3 4z"/><path d="M4 20l6.8-7.4M20 4l-6.6 7.2"/>',
    "instagram": '<rect x="2" y="2" width="20" height="20" rx="5"/><circle cx="12" cy="12" r="4"/><path d="M17.5 6.5h.01"/>',
    "youtube": '<path d="M2.5 17a24 24 0 0 1 0-10 2 2 0 0 1 1.4-1.4 49.6 49.6 0 0 1 16.2 0A2 2 0 0 1 21.5 7a24 24 0 0 1 0 10 2 2 0 0 1-1.4 1.4 49.6 49.6 0 0 1-16.2 0A2 2 0 0 1 2.5 17"/><path d="m10 15 5-3-5-3z"/>',
    "dribbble": '<circle cx="12" cy="12" r="10"/><path d="M19.13 5.09C15.22 9.14 10 10.44 2.25 10.94M21.75 12.84c-6.62-1.41-12.14 1-16.38 6.32M8.56 2.75c4.37 6 6 9.42 8 17.72"/>',
    "website": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "email": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "resume": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/>',
    "arrow": '<path d="M7 17 17 7M7 7h10v10"/>',
}
LABELS = {"email": "Email", "github": "GitHub", "linkedin": "LinkedIn", "x": "X", "instagram": "Instagram", "youtube": "YouTube",
          "dribbble": "Dribbble", "website": "Website", "resume": "Résumé"}


def ic(name, cls="ic"):
    return f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>'


def social_links(socials, cls="soc"):
    out = []
    for k in SOCIALS:
        v = socials.get(k)
        if not v:
            continue
        href = "mailto:" + v if k == "email" else safe_url(v)
        if href:
            out.append(f'<a class="{cls}" href="{escape(href)}" target="_blank" rel="{'noopener' if k == 'email' else 'me noopener'}" aria-label="{LABELS[k]}" title="{LABELS[k]}">{ic(k)}</a>')
    return f'<div class="socials">{"".join(out)}</div>' if out else ""


def btn(label, link, kind="primary"):
    href = safe_url(link)
    if not (label and href):
        return ""
    ext = "" if href.startswith("#") else ' target="_blank" rel="noopener"'
    return f'<a class="btn btn-{kind}" href="{escape(href)}"{ext}>{escape(label)}</a>'


def intro(d):
    return f'<div class="intro rv">{md(d.get("text"))}</div>' if d.get("text") else ""


def head(d, fallback=""):
    h = d.get("heading") or fallback
    return f'<h2 class="sh rv">{escape(h)}</h2>' if h else ""


NOIMG = ""  # broken cover images are handled by the page script (no inline handlers, so a strict CSP works)


def render_section(s, cfg, projects):
    d, v, kind = s["data"], s["variant"], s["type"]
    socials = cfg["profile"]["socials"]
    if kind == "hero":
        img = safe_url(d.get("image"))
        badge = f'<span class="badge rv"><i></i>{escape(d["badge"])}</span>' if d.get("badge") else ""
        body = (f'{badge}<h1 class="rv">{f'<span class="eyebrow">{escape(d["eyebrow"])}</span>' if d.get("eyebrow") else ""}<span class="h1-main">{escape(d.get("title") or cfg["profile"]["name"])}</span></h1>'
                f'<div class="lead rv">{md(d.get("subtitle"))}</div><div class="actions rv">{btn(d.get("primary_label"), d.get("primary_link"))}{btn(d.get("secondary_label"), d.get("secondary_link"), "ghost")}</div>'
                + (f'<div class="rv">{social_links(socials)}</div>' if d.get("socials") else ""))
        pic = f'<div class="hero-img rv"><img src="{escape(img)}" alt="Photo of {escape(cfg["profile"]["name"])}" width="460" height="460" fetchpriority="high" decoding="async"></div>' if img and v == "split" else ""
        return f'<div class="hero hero-{v}"><div class="hero-txt">{body}</div>{pic}</div>'
    if kind == "about":
        img = safe_url(d.get("image"))
        pic = f'<div class="about-img rv"><img src="{escape(img)}" alt="{escape(cfg["profile"]["name"])}" loading="lazy" decoding="async"></div>' if img and v == "image" else ""
        return f'{head(d, "About")}<div class="about {"about-img-on" if pic else ""}"><div class="prose rv">{md(d.get("text"))}</div>{pic}</div>'
    if kind == "skills":
        groups = []
        for g in d.get("items") or []:
            items = [x.strip() for x in (g.get("items") or "").split(",") if x.strip()]
            if v == "bars":
                rows = []
                for it in items:
                    name, _, lvl = it.partition(":")
                    pct = max(5, min(100, int(lvl))) if lvl.strip().isdigit() else 80
                    rows.append(f'<div class="bar"><div class="bar-top"><span>{escape(name.strip())}</span><span>{pct}%</span></div><div class="bar-track"><i style="--w:{pct}%"></i></div></div>')
                inner = "".join(rows)
            else:
                inner = "".join(f'<span class="chip">{escape(x.partition(":")[0].strip())}</span>' for x in items)
            groups.append(f'<div class="sk-group card rv"><h3>{escape(g.get("name") or "")}</h3><div class="sk-{v}">{inner}</div></div>')
        return f'{head(d, "Skills")}{intro(d)}<div class="sk sk-wrap-{v}">{"".join(groups)}</div>'
    if kind == "projects":
        items = projects
        if str(d.get("limit") or "All").isdigit():
            items = items[:int(d["limit"])]
        cards = []
        for p in items:
            live, code, img = safe_url(p.get("live_url")), safe_url(p.get("repo_url")), safe_url(p.get("image"))
            tags = "".join(f"<span>{escape(t)}</span>" for t in (p.get("tags") or [])[:6])
            links = (f'<a class="plink live" href="{escape(live)}" target="_blank" rel="noopener">Live site {ic("arrow")}</a>' if live else "") + \
                    (f'<a class="plink" href="{escape(code)}" target="_blank" rel="noopener">{ic("github")} Code</a>' if code else "")
            cover = f'<a class="p-img" href="{escape(live or code or "#")}" target="_blank" rel="noopener"><img src="{escape(img)}" alt="{escape(p.get("title") or "Project")} preview" width="1200" height="630" loading="lazy" decoding="async" data-fallback="1"><span>{escape((p.get("title") or "?")[:1])}</span></a>' if img else ""
            cards.append(f'<article class="proj card rv {"feat" if p.get("featured") and v == "grid" else ""}">{cover}<div class="p-b"><h3>{escape(p.get("title") or "")}</h3>'
                         f'<p>{escape(p.get("description") or "")}</p><div class="tags">{tags}</div><div class="plinks">{links}</div></div></article>')
        empty = '<p class="muted rv">Add projects in Reachout → Portfolio → Projects and they appear here.</p>' if not cards else ""
        return f'{head(d, "Projects")}{intro(d)}<div class="projs projs-{v}">{"".join(cards)}</div>{empty}'
    if kind in ("experience", "education"):
        rows = "".join(f'<div class="tl-item card rv"><div class="tl-when">{escape(i.get("period") or "")}</div><div><h3>{escape(i.get("role") or "")}</h3>'
                       f'<div class="tl-org">{escape(i.get("org") or "")}</div><div class="prose">{md(i.get("text"))}</div></div></div>' for i in d.get("items") or [])
        return f'{head(d)}<div class="tl tl-{v}">{rows}</div>'
    if kind == "services":
        rows = "".join(f'<div class="svc card rv"><div class="svc-ic">{escape(i.get("icon") or "✦")}</div><h3>{escape(i.get("title") or "")}</h3><div class="prose">{md(i.get("text"))}</div></div>' for i in d.get("items") or [])
        return f'{head(d)}{intro(d)}<div class="svcs svcs-{v}">{rows}</div>'
    if kind == "stats":
        rows = "".join(f'<div class="stat {"card" if v == "cards" else ""} rv"><b>{escape(i.get("value") or "")}</b><span>{escape(i.get("label") or "")}</span></div>' for i in d.get("items") or [])
        return f'<div class="stats stats-{v}">{rows}</div>'
    if kind == "testimonials":
        rows = "".join(f'<figure class="quote card rv"><blockquote>“{escape(i.get("quote") or "")}”</blockquote><figcaption><b>{escape(i.get("name") or "")}</b><span>{escape(i.get("role") or "")}</span></figcaption></figure>' for i in d.get("items") or [])
        return f'{head(d)}<div class="quotes quotes-{v}">{rows}</div>'
    if kind == "cta":
        return f'<div class="cta cta-{v} rv"><h2>{escape(d.get("heading") or "")}</h2><div class="prose">{md(d.get("text"))}</div><div class="actions">{btn(d.get("button_label"), d.get("button_link"))}</div></div>'
    if kind == "text":
        return f'<div class="txt {"card" if v == "card" else ""} rv">{head(d)}<div class="prose">{md(d.get("text"))}</div><div class="actions">{btn(d.get("button_label"), d.get("button_link"))}</div></div>'
    if kind == "contact":
        email = socials.get("email") if d.get("show_email") else ""
        facts = (f'<a class="fact" href="mailto:{escape(email)}">{ic("email")}{escape(email)}</a>' if email else "") + \
                (f'<a class="fact" href="tel:{escape(re.sub(r"[^0-9+]", "", d["phone"]))}">{ic("phone")}{escape(d["phone"])}</a>' if d.get("phone") else "") + \
                (f'<span class="fact">{ic("pin")}{escape(d["location"])}</span>' if d.get("location") else "")
        form = ('<form class="cform card" id="cform" novalidate><input type="text" name="website" tabindex="-1" autocomplete="off" class="hp" aria-hidden="true">'
                '<label>Your name<input name="name" required maxlength="80" autocomplete="name"></label>'
                '<label>Email<input name="email" type="email" required maxlength="200" autocomplete="email"></label>'
                '<label><span>Subject <small>(optional)</small></span><input name="subject" maxlength="120"></label>'
                '<label>Message<textarea name="message" rows="5" required maxlength="3000"></textarea></label>'
                '<button class="btn btn-primary" type="submit">Send message</button><p class="cf-status" role="status"></p></form>') if d.get("form") else ""
        return (f'<div class="contact contact-{v} {"has-form" if form else ""}"><div class="rv">{head(d, "Contact")}<div class="prose">{md(d.get("text"))}</div>'
                f'<div class="facts">{facts}</div>{social_links(socials)}</div>{f"<div class=rv>{form}</div>" if form else ""}</div>')
    return ""


def css(t):
    radius = t["radius"]
    pad = {"compact": "64px", "normal": "96px", "airy": "136px"}[t["spacing"]]
    width = {"narrow": "880px", "normal": "1120px", "wide": "1320px"}[t["width"]]
    bg_layer = {
        "plain": "none",
        "glow": "radial-gradient(60rem 40rem at 85% -10%, color-mix(in srgb, var(--accent) 22%, transparent), transparent 60%), radial-gradient(50rem 36rem at -10% 30%, color-mix(in srgb, var(--accent2) 16%, transparent), transparent 60%)",
        "grid": "linear-gradient(color-mix(in srgb, var(--text) 6%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in srgb, var(--text) 6%, transparent) 1px, transparent 1px)",
        "dots": "radial-gradient(color-mix(in srgb, var(--text) 14%, transparent) 1px, transparent 1.4px)",
    }[t["background"]]
    bg_size = {"grid": "44px 44px", "dots": "22px 22px"}.get(t["background"], "auto")
    card = {
        "flat": "background:var(--surface);",
        "border": "background:var(--surface);border:1px solid var(--line);",
        "raised": "background:var(--surface);box-shadow:0 1px 2px rgba(0,0,0,.05),0 12px 32px -12px rgba(0,0,0,.18);",
        "glass": "background:color-mix(in srgb, var(--surface) 60%, transparent);border:1px solid var(--line);backdrop-filter:blur(14px);",
    }[t["card"]]
    button = {
        "gradient": "background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0;",
        "solid": "background:var(--accent);color:var(--on-accent);border:0;",
        "outline": "background:transparent;color:var(--accent);border:2px solid var(--accent);",
        "pill": "background:var(--accent);color:var(--on-accent);border:0;border-radius:999px !important;",
    }[t["button"]]
    center = t["align"] == "center"
    return f"""
:root{{--bg:{t['bg']};--surface:{t['surface']};--text:{t['text']};--muted:{t['muted']};--accent:{t['accent']};--accent2:{t['accent2']};
--on-accent:{on_color(t['accent'])};--line:color-mix(in srgb,var(--text) 12%,transparent);--r:{radius}px;--pad:{pad};--w:{width};
--fh:'{t['font_head']}',system-ui,sans-serif;--fb:'{t['font_body']}',system-ui,sans-serif}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth;scroll-padding-top:80px}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.7 var(--fb);-webkit-font-smoothing:antialiased;background-image:{bg_layer};background-size:{bg_size};background-attachment:fixed}}
img{{max-width:100%;display:block}}a{{color:var(--accent)}}
h1,h2,h3{{font-family:var(--fh);line-height:1.15;letter-spacing:-.02em;margin:0}}
.wrap{{max-width:var(--w);margin:0 auto;padding:0 24px}}
section{{padding:calc(var(--pad)/2) 0}}section:first-of-type{{padding-top:calc(var(--pad)*.9)}}
.sh{{font-size:clamp(28px,4vw,40px);margin-bottom:28px;{'text-align:center;' if center else ''}}}
.intro{{color:var(--muted);max-width:640px;margin:-14px {'auto' if center else '0'} 28px;{'text-align:center;' if center else ''}}}
.intro p{{margin:0}}.muted{{color:var(--muted)}}.prose p{{margin:0 0 12px}}.prose p:last-child{{margin:0}}.prose{{color:var(--muted)}}.prose strong{{color:var(--text)}}
.card{{{card}border-radius:var(--r)}}
.btn{{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;border-radius:calc(var(--r)*.7);font:600 15px var(--fb);text-decoration:none;cursor:pointer;transition:transform .2s,box-shadow .2s,opacity .2s}}
.btn:hover{{transform:translateY(-2px)}}.btn-primary{{{button}box-shadow:0 10px 30px -12px var(--accent)}}
.btn-ghost{{background:transparent;color:var(--text);border:1px solid var(--line)}}.btn-ghost:hover{{border-color:var(--text)}}
.actions{{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}}.actions:empty{{display:none}}
.ic{{width:18px;height:18px;flex:none}}
.socials{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}
.soc{{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;border:1px solid var(--line);color:var(--text);transition:all .2s}}
.soc:hover{{color:var(--accent);border-color:var(--accent);transform:translateY(-2px)}}
nav.top{{position:{'sticky' if t['nav'] == 'sticky' else 'relative'};top:0;z-index:50;{'display:none;' if t['nav'] == 'hidden' else ''}backdrop-filter:blur(14px);background:color-mix(in srgb,var(--bg) 78%,transparent);border-bottom:1px solid transparent;transition:border-color .2s}}
nav.top.scrolled{{border-color:var(--line)}}
.nav-in{{display:flex;align-items:center;gap:20px;height:68px}}.brand{{font:700 18px var(--fh);color:var(--text);text-decoration:none;margin-right:auto}}
.nav-links{{display:flex;gap:4px}}.nav-links a{{color:var(--muted);text-decoration:none;font-weight:500;font-size:14.5px;padding:8px 12px;border-radius:10px;transition:color .2s,background .2s}}
.nav-links a:hover,.nav-links a.on{{color:var(--text);background:color-mix(in srgb,var(--text) 6%,transparent)}}
.menu{{display:none;background:none;border:1px solid var(--line);color:var(--text);border-radius:10px;width:40px;height:40px;cursor:pointer}}
@media(max-width:760px){{.menu{{display:grid;place-items:center}}.nav-links{{display:none;position:absolute;left:0;right:0;top:68px;flex-direction:column;padding:12px 24px 20px;background:var(--bg);border-bottom:1px solid var(--line)}}.nav-links.open{{display:flex}}}}
.hero{{display:grid;gap:48px;align-items:center;min-height:min(78vh,760px)}}
.hero-split{{grid-template-columns:1.25fr .75fr}}.hero-center{{text-align:center;justify-items:center}}.hero-center .hero-txt{{max-width:760px}}
.hero-center .actions,.hero-center .socials{{justify-content:center}}.hero-minimal{{min-height:auto;padding:40px 0}}
.hero h1 .h1-main{{font-size:clamp(40px,7vw,76px);letter-spacing:-.035em;background:linear-gradient(135deg,var(--text) 30%,var(--accent));-webkit-background-clip:text;background-clip:text;color:transparent;padding-bottom:4px}}
.eyebrow{{display:block;color:var(--accent);font:600 17px/1.4 var(--fb);letter-spacing:0;margin:0 0 10px;-webkit-text-fill-color:var(--accent)}}.h1-main{{display:block}}.lead{{font-size:clamp(17px,2vw,20px);color:var(--muted);margin-top:18px;max-width:620px}}
.hero-center .lead{{margin-left:auto;margin-right:auto}}.lead p{{margin:0}}
.badge{{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;padding:6px 14px;border-radius:99px;border:1px solid var(--line);margin-bottom:18px;background:var(--surface)}}
.badge i{{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 4px color-mix(in srgb,#22c55e 25%,transparent);animation:pulse 2s infinite}}
@keyframes pulse{{50%{{box-shadow:0 0 0 8px transparent}}}}
.hero-img{{position:relative}}.hero-img img{{width:100%;height:auto;aspect-ratio:1;object-fit:cover;border-radius:calc(var(--r)*2);position:relative;z-index:1}}
.hero-img::before{{content:"";position:absolute;inset:-14px;border-radius:calc(var(--r)*2 + 10px);background:linear-gradient(135deg,var(--accent),var(--accent2));opacity:.35;filter:blur(24px)}}
@media(max-width:860px){{.hero-split{{grid-template-columns:1fr}}.hero-img{{max-width:280px;order:-1}}.hero{{min-height:auto}}}}
.about{{display:grid;gap:40px}}.about-img-on{{grid-template-columns:1.3fr .7fr;align-items:center}}.about .prose{{font-size:17px}}
.about-img img{{border-radius:var(--r);width:100%;object-fit:cover}}@media(max-width:760px){{.about-img-on{{grid-template-columns:1fr}}}}
.sk{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}.sk-group{{padding:22px}}.sk-group h3{{font-size:16px;margin-bottom:14px}}
.sk-chips{{display:flex;flex-wrap:wrap;gap:8px}}.chip{{font-size:13.5px;padding:6px 12px;border-radius:99px;background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--text);border:1px solid color-mix(in srgb,var(--accent) 22%,transparent)}}
.sk-columns{{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px}}.sk-columns .chip{{background:none;border:0;padding:0;color:var(--muted)}}.sk-columns .chip::before{{content:"▹ ";color:var(--accent)}}
.bar{{margin-bottom:12px}}.bar-top{{display:flex;justify-content:space-between;font-size:14px;margin-bottom:6px}}.bar-top span:last-child{{color:var(--muted)}}
.bar-track{{height:8px;border-radius:99px;background:color-mix(in srgb,var(--text) 8%,transparent);overflow:hidden}}
.bar-track i{{display:block;height:100%;width:var(--w);border-radius:99px;background:linear-gradient(90deg,var(--accent),var(--accent2));transform-origin:left;transition:transform 1.2s cubic-bezier(.2,.8,.2,1)}}
.anim .rv:not(.in) .bar-track i{{transform:scaleX(0)}}
.projs-grid{{display:grid;gap:20px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}}.proj{{overflow:hidden;display:flex;flex-direction:column;transition:transform .25s,box-shadow .25s}}
.proj:hover{{transform:translateY(-4px);box-shadow:0 24px 50px -24px color-mix(in srgb,var(--accent) 60%,transparent)}}
.projs-grid .feat{{grid-column:span 2}}@media(max-width:700px){{.projs-grid .feat{{grid-column:auto}}}}
.p-img{{display:block;overflow:hidden;background:color-mix(in srgb,var(--text) 5%,transparent)}}.p-img>span{{display:none}}.p-img.noimg{{aspect-ratio:1200/630;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2))}}.p-img.noimg>span{{display:block;font:800 64px var(--fh);color:#fff;opacity:.9}}.p-img img{{width:100%;height:auto;aspect-ratio:1200/630;object-fit:cover;transition:transform .5s}}.proj:hover .p-img img{{transform:scale(1.04)}}
.p-b{{padding:20px 22px;display:flex;flex-direction:column;flex:1}}.p-b h3{{font-size:19px;margin-bottom:6px}}.p-b p{{color:var(--muted);margin:0 0 14px;font-size:15px}}
.tags{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}}.tags span{{font-size:12px;padding:3px 10px;border-radius:99px;background:color-mix(in srgb,var(--text) 7%,transparent);color:var(--muted)}}
.plinks{{display:flex;gap:16px;margin-top:auto}}.plink{{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:14px;text-decoration:none;color:var(--text)}}.plink.live{{color:var(--accent)}}.plink .ic{{width:15px;height:15px}}
.projs-list{{display:grid;gap:16px}}.projs-list .proj{{flex-direction:row}}.projs-list .p-img{{width:38%;flex:none}}.projs-list .p-img img{{height:100%}}
@media(max-width:700px){{.projs-list .proj{{flex-direction:column}}.projs-list .p-img{{width:100%}}}}
.tl{{display:grid;gap:16px}}.tl-item{{display:grid;grid-template-columns:170px 1fr;gap:24px;padding:24px}}.tl-when{{color:var(--accent);font-weight:600;font-size:14px}}
.tl-item h3{{font-size:18px}}.tl-org{{color:var(--text);opacity:.8;font-weight:500;margin:2px 0 8px}}
.tl-timeline{{position:relative;padding-left:26px;gap:18px}}.tl-timeline::before{{content:"";position:absolute;left:5px;top:10px;bottom:10px;width:2px;background:linear-gradient(var(--accent),transparent)}}
.tl-timeline .tl-item{{position:relative}}.tl-timeline .tl-item::before{{content:"";position:absolute;left:-27px;top:30px;width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--bg)}}
.tl-cards{{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}.tl-cards .tl-item{{grid-template-columns:1fr;gap:8px}}
@media(max-width:640px){{.tl-item{{grid-template-columns:1fr;gap:6px}}}}
.svcs-cards{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}}.svc{{padding:26px}}.svc-ic{{font-size:28px;width:54px;height:54px;display:grid;place-items:center;border-radius:14px;background:color-mix(in srgb,var(--accent) 14%,transparent);margin-bottom:16px}}
.svc h3{{font-size:18px;margin-bottom:8px}}.svcs-list{{display:grid;gap:12px}}.svcs-list .svc{{display:grid;grid-template-columns:54px 1fr;column-gap:18px;padding:20px}}.svcs-list .svc-ic{{grid-row:span 2;margin:0}}
.stats{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));text-align:center}}.stat{{padding:22px}}
.stat b{{display:block;font:800 clamp(34px,5vw,48px)/1 var(--fh);background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:8px}}.stat span{{color:var(--muted)}}
.quotes-cards{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}.quote{{margin:0;padding:26px}}.quote blockquote{{margin:0 0 18px;font-size:17px}}
.quote figcaption b{{display:block}}.quote figcaption span{{color:var(--muted);font-size:14px}}.quotes-quote .quote{{text-align:center;max-width:780px;margin:0 auto 18px}}.quotes-quote blockquote{{font:500 clamp(20px,3vw,28px)/1.4 var(--fh)}}
.cta{{text-align:center;padding:56px 28px;border-radius:calc(var(--r)*1.5)}}.cta h2{{font-size:clamp(28px,4vw,42px);margin-bottom:12px}}.cta .actions{{justify-content:center}}.cta .prose{{max-width:620px;margin:0 auto}}
.cta-banner{{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}}.cta-banner .prose{{color:rgba(255,255,255,.85)}}.cta-banner .btn-primary{{background:#fff;color:#111;box-shadow:none}}
.txt.card{{padding:32px}}.txt .sh{{margin-bottom:16px}}
.contact{{display:grid;gap:40px}}.contact-split.has-form{{grid-template-columns:1fr 1fr;align-items:start}}.contact-center{{text-align:center;justify-items:center}}.contact-center .socials,.contact-center .facts{{justify-content:center}}
.contact-center .cform{{text-align:left;width:min(560px,100%)}}@media(max-width:800px){{.contact-split.has-form{{grid-template-columns:1fr}}}}
.facts{{display:flex;flex-direction:column;gap:12px;margin-top:22px}}.contact-center .facts{{flex-direction:row;flex-wrap:wrap}}
.fact{{display:inline-flex;align-items:center;gap:10px;color:var(--text);text-decoration:none;font-weight:500}}.fact .ic{{color:var(--accent)}}
.cform{{padding:26px;display:grid;gap:14px}}.cform label{{display:grid;gap:6px;font-size:14px;font-weight:600}}.cform label small{{font-weight:400;color:var(--muted)}}
.cform input,.cform textarea{{font:15px var(--fb);color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:calc(var(--r)*.6);padding:12px 14px;outline:none;transition:border-color .2s,box-shadow .2s;resize:vertical}}
.cform input:focus,.cform textarea:focus{{border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 18%,transparent)}}.cform .btn{{justify-content:center}}
.cform .bad{{border-color:#ef4444}}.cf-status{{margin:0;font-size:14px;min-height:1em}}.cf-status.ok{{color:#22c55e}}.cf-status.err{{color:#ef4444}}.hp{{position:absolute;left:-9999px}}
footer{{padding:40px 0 28px;color:var(--muted);font-size:14px;border-top:1px solid var(--line);margin-top:calc(var(--pad)/2)}}.powered{{margin-top:26px;display:flex;justify-content:center}}.powered a{{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);text-decoration:none;padding:6px 14px;border-radius:99px;border:1px solid var(--line);background:color-mix(in srgb,var(--surface) 70%,transparent);transition:color .2s,border-color .2s,transform .2s}}
.powered a:hover{{color:var(--text);border-color:var(--accent);transform:translateY(-1px)}}.powered b{{color:var(--text);font-weight:700}}.pw-mark{{color:var(--accent)}}
.foot{{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center}}footer .socials{{margin:0}}
.page-h1{{font-size:clamp(34px,5vw,52px)}}.anim .rv{{opacity:0;transform:translateY(22px);transition:opacity .7s cubic-bezier(.2,.8,.2,1),transform .7s cubic-bezier(.2,.8,.2,1)}}.anim .rv.in{{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){{.anim .rv{{opacity:1;transform:none;transition:none}}html{{scroll-behavior:auto}}}}
"""


def on_color(hex_):
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#ffffff"


def csp(nonce):
    """Strict policy for published sites: only our own script (by nonce), no plugins, no framing, forms
    and fetches only to this server. If a bug ever let text through as HTML, it still couldn't run."""
    return ("default-src 'self'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
            "img-src 'self' https: data:; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


def render(cfg, projects, slug="", preview=False, nonce=""):
    t, prof, seo = cfg["theme"], cfg["profile"], cfg["seo"]
    shown = [s for s in cfg["sections"] if s["on"]]
    body, nav, seen = [], [], set()
    for s in shown:
        anchor = s["type"] if s["type"] not in seen else f"{s['type']}-{s['id']}"
        seen.add(s["type"])
        if s["type"] == "hero":
            anchor = "top"
        if s["nav"]:
            nav.append(f'<a href="#{anchor}">{escape(s["nav"])}</a>')
        body.append(f'<section id="{anchor}" data-sid="{s["id"]}"><div class="wrap">{render_section(s, cfg, projects)}</div></section>')
    fams = "&".join(f"family={FONTS[f]}" for f in dict.fromkeys([t["font_head"], t["font_body"]]))
    title = escape(seo.get("title") or prof["name"])
    desc = escape(seo.get("description") or "")
    head_html, ld = seo_head(cfg, shown, projects, slug, preview)
    lang = seo.get("lang") or "en"
    has_h1 = any(s["type"] == "hero" for s in shown)
    if not has_h1:  # every page needs one main heading: fall back to the person's name
        body.insert(0, f'<section id="top"><div class="wrap"><h1 class="page-h1 rv">{escape(prof["name"])}</h1></div></section>')
    script = """
(function(){var d=document,b=d.body;
d.querySelectorAll('img[data-fallback]').forEach(function(im){function f(){im.parentNode.classList.add('noimg');im.remove()}if(im.complete&&!im.naturalWidth&&im.getAttribute('src'))f();else im.addEventListener('error',f)});
var nav=d.querySelector('nav.top'),menu=d.querySelector('.menu'),links=d.querySelector('.nav-links');
if(menu)menu.onclick=function(){links.classList.toggle('open')};if(links)links.onclick=function(){links.classList.remove('open')};
addEventListener('scroll',function(){if(nav)nav.classList.toggle('scrolled',scrollY>10)},{passive:true});
if(b.classList.contains('anim')&&'IntersectionObserver'in window){var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}})},{threshold:.12,rootMargin:'0px 0px -40px 0px'});
d.querySelectorAll('.rv').forEach(function(el,i){el.style.transitionDelay=(i%4)*60+'ms';io.observe(el)})}else d.querySelectorAll('.rv').forEach(function(el){el.classList.add('in')});
var secs=[].slice.call(d.querySelectorAll('section[id]')),al=[].slice.call(d.querySelectorAll('.nav-links a'));
if('IntersectionObserver'in window){var so=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting)al.forEach(function(a){a.classList.toggle('on',a.getAttribute('href')==='#'+e.target.id)})})},{rootMargin:'-45% 0px -50% 0px'});secs.forEach(function(s){so.observe(s)})}
var f=d.getElementById('cform');if(f){var t0=Date.now();f.addEventListener('submit',function(ev){ev.preventDefault();var st=f.querySelector('.cf-status'),ok=true;
['name','email','message'].forEach(function(n){var el=f.elements[n],v=el.value.trim(),bad=!v||(n==='email'&&!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v))||(n==='message'&&v.length<10);el.classList.toggle('bad',bad);if(bad)ok=false});
if(!ok){st.className='cf-status err';st.textContent='Please fill in your name, a valid email and a message (10+ characters).';return}
if(PREVIEW){st.className='cf-status ok';st.textContent='Preview: once your site is published, messages arrive in your Leads page.';return}
var btn=f.querySelector('button');btn.disabled=true;btn.textContent='Sending…';
fetch('/api/site/public/'+SLUG+'/contact',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify({name:f.elements.name.value,email:f.elements.email.value,message:f.elements.message.value,subject:f.elements.subject.value,ref:document.referrer,website:f.elements.website.value,elapsed_ms:Date.now()-t0})})
.then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||'Something went wrong.');st.className='cf-status ok';st.textContent='Thanks! Your message was sent.';f.reset()})})
.catch(function(e){st.className='cf-status err';st.textContent=e.message||'Could not send. Please try again.'}).finally(function(){btn.disabled=false;btn.textContent='Send message'})})}
if(PREVIEW){addEventListener('message',function(e){var m=e.data||{};if(m.type==='scrollTo'){var el=d.querySelector('[data-sid=\"'+m.id+'\"]');if(el)scrollTo({top:el.getBoundingClientRect().top+scrollY-(nav&&getComputedStyle(nav).position==='sticky'?68:0),behavior:'smooth'})}else if(m.type==='restore'){d.documentElement.style.scrollBehavior='auto';scrollTo(0,m.y);d.documentElement.style.scrollBehavior=''}});
var tm;addEventListener('scroll',function(){clearTimeout(tm);tm=setTimeout(function(){parent.postMessage({type:'site-scroll',y:scrollY},'*')},80)},{passive:true});
d.addEventListener('click',function(e){var a=e.target.closest('a[href]');if(a&&!a.getAttribute('href').startsWith('#')){e.preventDefault();window.open(a.href,'_blank','noopener')}});parent.postMessage({type:'site-ready'},'*')}
})();"""
    return f"""<!doctype html><html lang="{lang}" dir="{'rtl' if lang == 'ar' else 'ltr'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><meta name="description" content="{desc}">
{head_html}<meta name="theme-color" content="{t['bg']}"><meta name="color-scheme" content="{'dark' if on_color(t['bg']) == '#ffffff' else 'light'}">
<link rel="icon" href="data:image/svg+xml,{quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">' + escape(seo.get("favicon") or "✦") + "</text></svg>")}">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?{fams}&display=swap" rel="stylesheet">
<script type="application/ld+json">{ld}</script><style>{css(t)}</style><noscript><style>.anim .rv{{opacity:1;transform:none}}</style></noscript></head>
<body class="{'anim' if t['animate'] else ''}"><nav class="top" aria-label="Main"><div class="wrap nav-in"><a class="brand" href="#top">{escape(prof['name'])}</a><div class="nav-links">{''.join(nav)}</div>
<button class="menu" aria-label="Menu"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button></div></nav>
<main>{''.join(body)}</main>
<footer><div class="wrap foot"><span>© {date.today().year} {escape(prof['name'])}</span>{social_links(prof['socials'])}</div>
<div class="wrap powered"><a href="{escape(C().LANDING_URL or C().site_url())}/?ref=site-{escape(slug)}" target="_blank" rel="noopener"><span class="pw-mark">✦</span>Powered by <b>Reachout</b></a></div></footer>
<script nonce="{nonce}">var PREVIEW={'true' if preview else 'false'},SLUG={json.dumps(slug)};{script}</script></body></html>"""


def abs_url(u):
    u = safe_url(u)
    return C().site_url() + u if u.startswith("/") else u if u.startswith("http") else ""


def seo_head(cfg, shown, projects, slug, preview):
    """Meta tags + schema.org data so search engines and link previews understand the page."""
    prof, seo = cfg["profile"], cfg["seo"]
    url = f"{C().site_url()}/p/{slug}" if slug else ""
    title, desc = seo.get("title") or prof["name"], seo.get("description") or ""
    hero = next((s["data"] for s in shown if s["type"] == "hero"), {})
    photo = abs_url(hero.get("image"))
    image = abs_url(seo.get("image")) or photo or next((abs_url(p.get("image")) for p in projects if p.get("image")), "")
    same_as = [v for k, v in prof["socials"].items() if v and k not in ("email", "resume") and v.startswith("http")]
    skills = []
    for s in shown:
        if s["type"] == "skills":
            for g in s["data"].get("items") or []:
                skills += [x.partition(":")[0].strip() for x in (g.get("items") or "").split(",") if x.strip()]
    job = next((i for s in shown if s["type"] == "experience" for i in (s["data"].get("items") or []) if i.get("org")), None)
    schools = [i["org"] for s in shown if s["type"] == "education" for i in (s["data"].get("items") or []) if i.get("org")]
    person = {"@type": "Person", "@id": f"{url}#person" if url else None, "name": prof["name"], "url": url or None,
              "image": photo or None, "jobTitle": hero.get("title") if hero.get("title") and hero.get("title") != prof["name"] else None,
              "description": desc or None, "sameAs": same_as or None, "knowsAbout": skills[:30] or None,
              "worksFor": {"@type": "Organization", "name": job["org"]} if job and re.search(r"present|now|current", job.get("period") or "", re.I) else None,
              "alumniOf": [{"@type": "EducationalOrganization", "name": n} for n in schools[:5]] or None}
    graph = [{"@type": "ProfilePage", "@id": url or None, "url": url or None, "name": title, "description": desc or None,
              "inLanguage": seo.get("lang") or "en", "dateModified": date.today().isoformat(), "mainEntity": {"@id": f"{url}#person"} if url else None},
             person]
    shown_projects = [p for p in projects if p.get("title")]
    if any(s["type"] == "projects" for s in shown) and shown_projects:
        graph.append({"@type": "ItemList", "name": "Projects", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "item": {"@type": "CreativeWork", "name": p["title"], "description": p.get("description") or None,
             "url": p.get("live_url") or p.get("repo_url") or None, "image": abs_url(p.get("image")) or None,
             "keywords": ", ".join(p.get("tags") or []) or None, "creator": {"@id": f"{url}#person"} if url else None}}
            for i, p in enumerate(shown_projects[:20])]})

    def prune(x):
        if isinstance(x, dict):
            return {k: prune(v) for k, v in x.items() if v not in (None, "", [], {})}
        return [prune(v) for v in x] if isinstance(x, list) else x
    # Inside <script>: escape < > & as \u sequences, so no text (</script>, <!--) can end or confuse the block.
    ld = json.dumps({"@context": "https://schema.org", "@graph": prune(graph)}, ensure_ascii=False)
    ld = ld.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    first, _, last = prof["name"].partition(" ")
    tags = [
        f'<meta name="robots" content="{"noindex, nofollow" if preview or not seo.get("index", True) else "index, follow, max-image-preview:large, max-snippet:-1"}">',
        f'<meta name="author" content="{escape(prof["name"])}">',
        f'<link rel="canonical" href="{escape(url)}">' if url else "",
        f'<meta name="google-site-verification" content="{escape(seo["google"])}">' if seo.get("google") else "",
        '<meta property="og:type" content="profile">', f'<meta property="og:site_name" content="{escape(prof["name"])}">',
        f'<meta property="og:locale" content="{LOCALES.get(seo.get("lang") or "en", "en_US")}">',
        f'<meta property="og:title" content="{escape(title)}">', f'<meta property="og:description" content="{escape(desc)}">',
        f'<meta property="og:url" content="{escape(url)}">' if url else "",
        f'<meta property="profile:first_name" content="{escape(first)}">', f'<meta property="profile:last_name" content="{escape(last)}">' if last else "",
        f'<meta property="og:image" content="{escape(image)}"><meta property="og:image:alt" content="{escape(title)}">' if image else "",
        f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">',
        f'<meta name="twitter:title" content="{escape(title)}">', f'<meta name="twitter:description" content="{escape(desc)}">',
        f'<meta name="twitter:image" content="{escape(image)}">' if image else "",
        f'<link rel="preload" as="image" href="{escape(photo)}" fetchpriority="high">' if photo and hero else "",
    ]
    return "\n".join(t for t in tags if t) + "\n", ld


def projects_for(ws):
    from features import portfolio as feature_portfolio
    return feature_portfolio.public_items(feature_portfolio.cfg(ws))


def slug_of(ws):
    from features import portfolio as feature_portfolio
    return feature_portfolio.cfg(ws).get("slug") or ""


def claim_slug(ws):
    """First publish without an address: take the GitHub username or the person's name, if free."""
    from features import portfolio as feature_portfolio
    core = C()
    user = core.find_user(uid=ws.uid) or {}
    base = [ws.load("github", {}).get("login", ""), re.sub(r"[^a-z0-9]+", "-", (user.get("name") or "").lower()).strip("-")]
    for cand in [b for b in base if b] + [f"{b}-{n}" for b in base if b for n in range(2, 6)]:
        cand = cand.lower()[:39]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}", cand):
            continue
        if cand in feature_portfolio.RESERVED_SLUGS or not feature_portfolio.claim(ws.uid, cand):
            continue
        with ws.lock:
            c = feature_portfolio.cfg(ws)
            c["slug"] = cand
            ws.save("portfolio", c)
        return cand
    return ""


# ---------------------------------------------------------------- owner API

def summary(ws, st):
    core = C()
    slug = slug_of(ws)
    since = (date.today() - timedelta(days=29)).isoformat()
    rows = list(core.M.site_stats.find({"uid": ws.uid}))
    days = {r["day"]: r["views"] for r in rows}
    series = [{"day": (date.today() - timedelta(days=i)).isoformat(), "views": days.get((date.today() - timedelta(days=i)).isoformat(), 0)} for i in range(29, -1, -1)]
    return {"published_at": st.get("published_at"), "online": st.get("online", False) and bool(st.get("published")),
            "dirty": st.get("published") != st.get("draft"), "slug": slug, "url": f"{core.site_url()}/p/{slug}" if slug else "",
            "views": {"total": sum(r["views"] for r in rows), "month": sum(v for d, v in days.items() if d >= since), "series": series},
            "unread": core.M.leads.count_documents({"uid": ws.uid, "status": "new"})}


@bp.get("/api/site")
@login_required
def get_site(ws):
    st = state(ws)
    return jsonify(draft=st["draft"], meta=meta(), **summary(ws, st))


@bp.put("/api/site")
@login_required
def save_site(ws):
    draft = clean(C().body().get("draft"))
    with ws.lock:
        st = state(ws)
        st["draft"] = draft
        ws.save("website", st)
    return jsonify(ok=True, saved_at=time.time(), dirty=st.get("published") != draft)


@bp.post("/api/site/render")
@login_required
def render_preview(ws):
    p = C().body()
    cfg = clean(p.get("draft")) if p.get("draft") else state(ws)["draft"]
    # The editor shows the preview in a frame loaded from a short-lived link (not inline HTML), so the page
    # gets its own strict policy and the app's policy doesn't have to allow its script.
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with PREVIEW_LOCK:
        now = time.time()
        for k in [k for k, v in PREVIEWS.items() if v[0] < now]:
            PREVIEWS.pop(k, None)
        mine = [k for k, v in PREVIEWS.items() if v[1] == ws.uid]
        for k in mine[:-3]:  # keep only this account's latest few
            PREVIEWS.pop(k, None)
        PREVIEWS[token] = (now + 300, ws.uid, render(cfg, projects_for(ws), slug_of(ws), preview=True, nonce="__NONCE__"))
    return jsonify(url=f"/site-preview/{token}")


PREVIEWS, PREVIEW_LOCK = {}, __import__("threading").Lock()


@bp.get("/site-preview/<token>")
def preview_frame(token):
    item = PREVIEWS.get(token) if re.fullmatch(r"[0-9a-f]{64}", token) else None
    if not item or item[0] < time.time():
        return Response("Preview expired. Edit anything to refresh it.", status=404, mimetype="text/plain")
    nonce = uuid.uuid4().hex
    resp = Response(item[2].replace("__NONCE__", nonce), mimetype="text/html")
    resp.headers["Content-Security-Policy"] = csp(nonce).replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"  # the editor frames it; nothing else can
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp.post("/api/site/publish")
@login_required
def publish(ws):
    core = C()
    slug = slug_of(ws) or claim_slug(ws)
    if not slug:
        raise core.Invalid("Choose your website address first (Details tab).", "slug")
    with ws.lock:
        st = state(ws)
        st.update(published=copy.deepcopy(st["draft"]), published_at=time.time(), online=True)
        ws.save("website", st)
    SITEMAP_CACHE["at"] = 0  # the sitemap reflects this change right away
    return jsonify(ok=True, **summary(ws, st))


@bp.post("/api/site/unpublish")
@login_required
def unpublish(ws):
    """Take the site down: visitors get “not found”. The draft stays, so publishing again restores it."""
    with ws.lock:
        st = state(ws)
        st.update(published=None, published_at=None, online=False, unpublished_at=time.time())
        ws.save("website", st)
    SITEMAP_CACHE["at"] = 0  # the sitemap reflects this change right away
    return jsonify(ok=True, **summary(ws, st))


@bp.post("/api/site/reset")
@login_required
def reset(ws):
    """Start again from a fresh starter site (keeps the published copy until you publish again)."""
    with ws.lock:
        st = state(ws)
        st["draft"] = seed(ws)
        ws.save("website", st)
    return jsonify(draft=st["draft"], **summary(ws, st))


# images

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@bp.post("/api/site/images")
@login_required
def upload_image(ws):
    core = C()
    f = request.files.get("file")
    if not f or f.mimetype not in IMAGE_TYPES:
        raise core.Invalid("Upload a PNG, JPG, WebP or GIF image.", "file")
    data = f.read(3 * 1024 * 1024 + 1)
    if len(data) > 3 * 1024 * 1024:
        raise core.Invalid("Images can be up to 3 MB.", "file")
    if core.M.site_images.count_documents({"uid": ws.uid}) >= 60:
        raise core.Invalid("You've reached 60 images. Remove unused ones first.")
    iid = uuid.uuid4().hex
    core.M.site_images.insert_one({"_id": iid, "uid": ws.uid, "type": f.mimetype, "created": time.time(),
                                   "data": core.seal(base64.b64encode(data).decode())})
    return jsonify(url=f"/p/i/{iid}")


@bp.get("/p/i/<iid>")
def image(iid):
    core = C()
    r = core.M.site_images.find_one({"_id": iid}) if re.fullmatch(r"[0-9a-f]{32}", iid) else None
    if not r:
        return Response(status=404)
    resp = Response(base64.b64decode(core.unseal(r["data"], "")), mimetype=r["type"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------- visitors

def public_html(slug):
    """The published site for an address, or None when there isn't one online."""
    core = C()
    row = core.M.site.find_one({"_id": "portfolio-slug:" + slug.lower()})
    if not row:
        return None
    ws = core.Workspace(row["uid"])
    st = ws.load("website", {})
    if not (st.get("online") and st.get("published")):
        return None
    ua = (request.headers.get("User-Agent") or "").lower()
    # One visit per visitor per site per day: reloading or scripted requests don't inflate the count.
    fresh = not core.rate_limited(("site-view", slug.lower(), core.client_ip(), ua[:120]), 1, 86400)
    if fresh and not re.search(r"bot|crawl|spider|preview|headless|monitor|curl|wget|python|http", ua) and request.headers.get("Purpose") != "prefetch":
        core.M.site_stats.update_one({"_id": f"{row['uid']}:{date.today().isoformat()}"},
                                     {"$inc": {"views": 1}, "$setOnInsert": {"uid": row["uid"], "day": date.today().isoformat()}}, upsert=True)
    # Stable per published version (secret-keyed), so a cached copy + a 304 still match the policy's nonce.
    nonce = core.lookup_hash(f"site-nonce:{slug.lower()}:{st.get('published_at')}")[:32]
    html = render(st["published"], projects_for(ws), slug.lower(), nonce=nonce)
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = csp(nonce)
    resp.headers["Content-Language"] = st["published"]["seo"].get("lang") or "en"
    resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"  # always fresh after a publish, cheap 304s otherwise
    resp.last_modified = datetime.fromtimestamp(st.get("published_at") or time.time(), tz=timezone.utc)
    if not st["published"]["seo"].get("index", True):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["ETag"] = '"' + hashlib.sha256(html.encode()).hexdigest()[:32] + '"'
    return resp.make_conditional(request)


SITEMAP_CACHE = {"at": 0, "rows": []}


def sitemap_entries():
    """(url, lastmod) for every published, indexable site: added to /sitemap.xml. Cached for 30 minutes,
    so this public URL can't be used to make the server decrypt every site over and over."""
    if time.time() - SITEMAP_CACHE["at"] < 1800:
        return SITEMAP_CACHE["rows"]
    SITEMAP_CACHE["at"] = time.time()
    SITEMAP_CACHE["rows"] = _sitemap_entries()
    return SITEMAP_CACHE["rows"]


def _sitemap_entries():
    core = C()
    out = []
    for row in core.M.site.find({"_id": {"$regex": "^portfolio-slug:"}}):
        st = core.Workspace(row["uid"]).load("website", {})
        if st.get("online") and st.get("published") and st["published"]["seo"].get("index", True):
            out.append((f"{core.site_url()}/p/{row['_id'].split(':', 1)[1]}", date.fromtimestamp(st.get("published_at") or time.time()).isoformat()))
    return out


@bp.post("/api/site/public/<slug>/contact")
def visitor_contact(slug):
    core = C()
    p = core.body()
    if p.get("website") or core.safe_int(p.get("elapsed_ms")) < 2500:
        return jsonify(ok=True)  # a bot: accept quietly, store nothing
    row = core.M.site.find_one({"_id": "portfolio-slug:" + slug.lower()})
    st = core.Workspace(row["uid"]).load("website", {}) if row else {}
    if not (st.get("online") and st.get("published")):
        raise core.Invalid("This site isn't accepting messages.", status=404)
    if core.rate_limited(("site-msg", core.client_ip()), 5, 3600) or core.rate_limited(("site-msg-site", slug.lower()), 30, 3600):
        raise core.Invalid("You've sent several messages already. Please try again later.", status=429)
    lead = {"name": core.v_text(p.get("name"), "name", "Your name", 80, required=True, min_len=2),
            "email": core.v_email(p.get("email")), "phone": "", "company": "",
            "topic": core.v_text(p.get("subject"), "subject", "Subject", 120) or "Website enquiry",
            "message": core.v_text(p.get("message"), "message", "Message", 3000, required=True, min_len=10),
            "source": "website", "page": f"/p/{slug.lower()}", "referrer": text(p.get("ref"), 300), "landing": "", "utm": {}, "notes": []}
    if core.rate_limited(("site-msg-email", core.lookup_hash(lead["email"])), 3, 3600):
        raise core.Invalid("We've already received your messages. You'll hear back soon.", status=429)
    core.M.leads.insert_one({"_id": uuid.uuid4().hex[:12], "uid": row["uid"], "created": time.time(), "status": "new", "data": core.seal(lead)})
    notify = getattr(bridge, "notify", None)
    if notify:
        notify(row["uid"], f"New enquiry from {lead['name']}", f"{lead['topic']}: {lead['message'][:120]}", "/app/leads", "message")
    return jsonify(ok=True)
