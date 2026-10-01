"""Portfolio: choose which GitHub projects appear on your website, with their live URL and a preview.

- The selection (order, title, description, tags, live URL, image, featured) is stored per account.
- Live URLs are detected from GitHub: the repo's "Website" field, GitHub Pages, or the latest
  deployment's environment URL (Vercel, Netlify, Render… report these to GitHub).
- Publishing writes a projects.json file to the repo your website is built from (a normal commit),
  so the site redeploys itself. A public JSON feed and a public showcase page are also available at
  /p/<slug>.json and /p/<slug> for sites that prefer to fetch the list at runtime.
"""

import json
import re
from datetime import datetime
from html import escape

from flask import Blueprint, Response, jsonify, redirect

import bridge
from features import github as ghub
from bridge import login_required

bp = Blueprint("portfolio", __name__)


def C():
    return bridge.C


def cfg(ws):
    return {"items": [], "slug": "", "public": False, "headline": "", "about": "",
            "target": {"owner": "", "repo": "", "branch": "", "path": "public/projects.json"}, "published": None,
            **ws.load("portfolio", {})}


RESERVED_SLUGS = {"i", "api", "app", "admin", "login", "signup", "static", "assets", "dist", "p"}


def claim(uid, slug):
    """Atomically take an address. Two people claiming the same one at the same moment: exactly one wins."""
    from pymongo.errors import DuplicateKeyError
    key = "portfolio-slug:" + slug
    try:
        C().M.site.insert_one({"_id": key, "uid": uid})
        return True
    except DuplicateKeyError:
        return bool(C().M.site.find_one({"_id": key, "uid": uid}))


def og_image(owner, repo):
    return f"https://opengraph.githubassets.com/1/{owner}/{repo}"


def clean_url(v, field):
    v = str(v or "").strip()
    if not v:
        return ""
    if not re.match(r"^https?://", v):
        v = "https://" + v
    if not re.fullmatch(r"https?://[^\s<>\"']{3,500}", v):
        raise C().Invalid("Enter a full link like https://my-app.vercel.app", field)
    return v


def detect_live(ws, owner, repo, info=None):
    """Best guess at a project's live URL, with where it came from."""
    info = info or ghub.gh(ws, "GET", f"/repos/{owner}/{repo}")
    if info.get("homepage"):
        url = info["homepage"] if info["homepage"].startswith("http") else "https://" + info["homepage"]
        if re.fullmatch(r"https?://[^\s<>\"']{3,500}", url):
            return url, "Website field on GitHub"
    try:
        deps = ghub.gh(ws, "GET", f"/repos/{owner}/{repo}/deployments?per_page=10")
        for d in deps:
            for st in ghub.gh(ws, "GET", f"/repos/{owner}/{repo}/deployments/{d['id']}/statuses?per_page=5"):
                if st.get("state") == "success" and (st.get("environment_url") or st.get("target_url")):
                    url = st.get("environment_url") or st.get("target_url") or ""
                    if not re.fullmatch(r"https?://[^\s<>\"']{3,500}", url):
                        continue  # only real web links are ever used as a live URL
                    if "vercel.com/" in url and "vercel.app" not in url:
                        continue  # a dashboard link, not the site
                    host = re.sub(r"^https?://([^/]+).*", r"\1", url)
                    if re.search(r"-[a-z0-9]{9}-[\w-]+-projects\.vercel\.app$", host):
                        return url, "Latest Vercel deployment (a one-off link: set your main domain if you have one)"
                    return url, f"Latest {d.get('environment') or 'deployment'} on {host}"
    except C().Invalid:
        pass
    if info.get("has_pages"):
        name = info["name"]
        url = f"https://{owner}.github.io/" if name.lower() == f"{owner}.github.io".lower() else f"https://{owner}.github.io/{name}/"
        return url, "GitHub Pages"
    return "", ""


def web_url(u):
    """Only plain web links (or our own uploaded images) leave this module: never javascript:/data: links."""
    u = str(u or "").strip()
    return u if re.fullmatch(r"(https?://[^\s<>\"']{3,1500}|/p/i/[0-9a-f]{32})", u) else ""


def public_items(c):
    out = []
    for it in c["items"]:
        if it.get("hidden"):
            continue
        out.append({k: it.get(k) for k in ("title", "description", "tags", "featured", "language", "stars", "updated_at")}
                   | {k: web_url(it.get(k)) for k in ("live_url", "repo_url", "image")}
                   | {"slug": re.sub(r"[^a-z0-9]+", "-", (it.get("title") or it["repo"]).lower()).strip("-")})
    return out


def feed(c, name):
    return {"name": name, "headline": c.get("headline", ""), "about": c.get("about", ""),
            "updated": datetime.now().isoformat(timespec="seconds"), "projects": public_items(c)}


# ---------------------------------------------------------------- API

@bp.get("/api/portfolio")
@login_required
def get_portfolio(ws):
    c = cfg(ws)
    gh_status = ws.load("github", {})
    if not c["slug"] and gh_status.get("login"):
        c["slug"] = gh_status["login"].lower()
    return jsonify(**c, github=bool(gh_status.get("token")), login=gh_status.get("login", ""), public_url=f"{C().site_url()}/p/{c['slug']}" if c["slug"] else "")


@bp.post("/api/portfolio/items")
@login_required
def add_items(ws):
    """Add repos (owner/name) to the portfolio, filling details and the live URL from GitHub."""
    p = C().body()
    names = [str(x) for x in (p.get("repos") or []) if re.fullmatch(r"[\w.-]+/[\w.-]+", str(x))][:30]
    if not names:
        raise C().Invalid("Pick at least one repository.")
    with ws.lock:
        c = cfg(ws)
        have = {i["repo"] for i in c["items"]}
        added = []
        for full in names:
            if full in have:
                continue
            owner, repo = full.split("/")
            info = ghub.gh(ws, "GET", f"/repos/{owner}/{repo}")
            live, source = detect_live(ws, owner, repo, info)
            title = re.sub(r"[-_]+", " ", info["name"]).strip()
            item = {"repo": full, "title": title[:1].upper() + title[1:], "description": info.get("description") or "",
                    "tags": [t for t in (info.get("topics") or [])][:8] or ([info["language"]] if info.get("language") else []),
                    "live_url": live, "live_source": source, "repo_url": info["html_url"] if not info.get("private") else "",
                    "private": info.get("private", False), "image": og_image(owner, repo), "featured": False, "hidden": False,
                    "language": info.get("language") or "", "stars": info.get("stargazers_count", 0), "updated_at": info.get("pushed_at")}
            c["items"].append(item)
            added.append(item)
        ws.save("portfolio", c)
    return jsonify(items=c["items"], added=len(added))


@bp.put("/api/portfolio/items/<path:full>")
@login_required
def edit_item(ws, full):
    core, p = C(), C().body()
    with ws.lock:
        c = cfg(ws)
        it = next((i for i in c["items"] if i["repo"] == full), None)
        if not it:
            raise core.Invalid("That project isn't in your portfolio.", status=404)
        if "title" in p:
            it["title"] = core.v_text(p["title"], "title", "Title", 80, required=True)
        if "description" in p:
            it["description"] = core.v_text(p["description"], "description", "Description", 500)
        if "tags" in p:
            it["tags"] = [core.v_text(t, "tags", "Tag", 30) for t in (p["tags"] or []) if str(t).strip()][:10]
        for k in ("live_url", "image", "repo_url"):
            if k in p:
                it[k] = clean_url(p[k], k)
                if k == "live_url":
                    it["live_source"] = "Set by you" if it[k] else ""
        for k in ("featured", "hidden"):
            if k in p:
                it[k] = bool(p[k])
        ws.save("portfolio", c)
    return jsonify(item=it)


@bp.post("/api/portfolio/items/<path:full>/detect")
@login_required
def redetect(ws, full):
    owner, repo = full.split("/", 1)
    live, source = detect_live(ws, owner, repo)
    with ws.lock:
        c = cfg(ws)
        it = next((i for i in c["items"] if i["repo"] == full), None)
        if not it:
            raise C().Invalid("That project isn't in your portfolio.", status=404)
        if live:
            it["live_url"], it["live_source"] = live, source
        ws.save("portfolio", c)
    return jsonify(item=it, found=bool(live))


@bp.delete("/api/portfolio/items/<path:full>")
@login_required
def remove_item(ws, full):
    with ws.lock:
        c = cfg(ws)
        c["items"] = [i for i in c["items"] if i["repo"] != full]
        ws.save("portfolio", c)
    return jsonify(items=c["items"])


@bp.put("/api/portfolio/order")
@login_required
def reorder(ws):
    order = [str(x) for x in (C().body().get("order") or [])]
    with ws.lock:
        c = cfg(ws)
        pos = {r: i for i, r in enumerate(order)}
        c["items"].sort(key=lambda i: pos.get(i["repo"], 10 ** 6))
        ws.save("portfolio", c)
    return jsonify(items=c["items"])


@bp.put("/api/portfolio/settings")
@login_required
def settings(ws):
    core, p = C(), C().body()
    with ws.lock:
        c = cfg(ws)
        if "headline" in p:
            c["headline"] = core.v_text(p["headline"], "headline", "Headline", 120)
        if "about" in p:
            c["about"] = core.v_text(p["about"], "about", "About", 1000)
        if "public" in p:
            c["public"] = bool(p["public"])
        if "slug" in p:
            slug = str(p["slug"] or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}", slug):
                raise core.Invalid("Use 2–39 letters, numbers or dashes.", "slug")
            if slug in RESERVED_SLUGS or not claim(ws.uid, slug):
                raise core.Invalid("That address is taken. Try another.", "slug")
            if c.get("slug") and c["slug"] != slug:
                core.M.site.delete_one({"_id": "portfolio-slug:" + c["slug"], "uid": ws.uid})
            c["slug"] = slug
        if "target" in p:
            t = p["target"] or {}
            c["target"] = {"owner": str(t.get("owner") or "")[:100], "repo": str(t.get("repo") or "")[:100],
                           "branch": str(t.get("branch") or "")[:100], "path": ghub.clean_path(t.get("path") or "public/projects.json")}
        ws.save("portfolio", c)
    return jsonify(ok=True, **{k: c[k] for k in ("headline", "about", "public", "slug", "target")})


@bp.post("/api/portfolio/publish")
@login_required
def publish(ws):
    """Write projects.json to the website repo. The host (Vercel/Netlify/Pages) redeploys on the commit."""
    core = C()
    c = cfg(ws)
    t = c["target"]
    if not (t.get("owner") and t.get("repo")):
        raise core.Invalid("Choose the repository your website is built from.", "repo")
    info = ghub.gh(ws, "GET", f"/repos/{t['owner']}/{t['repo']}")
    branch = t.get("branch") or info.get("default_branch") or "main"
    user = core.find_user(uid=ws.uid) or {}
    content = json.dumps(feed(c, user.get("name", "")), indent=2, ensure_ascii=False) + "\n"
    n = len(public_items(c))
    commit = ghub.write_file(ws, t["owner"], t["repo"], branch, t["path"], content, f"Update portfolio: {n} project{'s' if n != 1 else ''} (via Reachout)")
    with ws.lock:
        c = cfg(ws)
        c["published"] = {"at": datetime.now().isoformat(timespec="seconds"), "sha": commit.get("sha"), "url": commit.get("html_url"),
                          "branch": branch, "count": n}
        ws.save("portfolio", c)
    return jsonify(ok=True, **c["published"])


# ---------------------------------------------------------------- public feed + page

def by_slug(slug):
    core = C()
    row = core.M.site.find_one({"_id": "portfolio-slug:" + slug.lower()})
    if not row:
        return None, None
    ws = core.Workspace(row["uid"])
    c = cfg(ws)
    return (c, core.find_user(uid=row["uid"]) or {}) if c.get("public") else (None, None)


@bp.get("/p/<slug>.json")
def public_json(slug):
    c, user = by_slug(slug)
    if not c:
        return jsonify(error="Not found"), 404
    resp = jsonify(feed(c, user.get("name", "")))
    resp.headers["Access-Control-Allow-Origin"] = "*"  # your site can fetch it from the browser
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.get("/p/<slug>/")
def public_page_slash(slug):
    return redirect(f"/p/{slug.lower()}", 301)


@bp.get("/p/<slug>")
def public_page(slug):
    from features import site as feature_site
    if slug != slug.lower():  # one canonical address per site
        return redirect(f"/p/{slug.lower()}", 301)
    resp = feature_site.public_html(slug)
    if resp:
        return resp
    c, user = by_slug(slug)
    if not c:
        return C().page("404.html", 404)
    items = public_items(c)
    name = escape(user.get("name", ""))
    cards = "".join(f"""<article class="{'feat' if i['featured'] else ''}">
  <a class="img" href="{escape(i['live_url'] or i['repo_url'] or '#')}" target="_blank" rel="noopener"><img src="{escape(i['image'] or '')}" alt="" loading="lazy"></a>
  <div class="b"><h2>{escape(i['title'] or '')}</h2><p>{escape(i['description'] or '')}</p>
  <div class="tags">{''.join(f'<span>{escape(t)}</span>' for t in i['tags'] or [])}</div>
  <div class="links">{f'<a class="live" href="{escape(i["live_url"])}" target="_blank" rel="noopener">Live site ↗</a>' if i['live_url'] else ''}{f'<a href="{escape(i["repo_url"])}" target="_blank" rel="noopener">Code</a>' if i['repo_url'] else ''}</div></div>
</article>""" for i in items)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} · Projects</title><meta name="description" content="{escape(c.get('headline') or 'Projects by ' + user.get('name', ''))}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.6 Inter,system-ui,sans-serif;background:#0b0d18;color:#e9ebf5}}
header{{max-width:1120px;margin:0 auto;padding:64px 24px 24px}}h1{{font-size:clamp(30px,5vw,48px);margin:0;letter-spacing:-.03em;background:linear-gradient(135deg,#a5b4fc,#c4b5fd);-webkit-background-clip:text;color:transparent}}
header p{{color:#a8aec5;max-width:680px}}main{{max-width:1120px;margin:0 auto;padding:0 24px 64px;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px}}
article{{background:#131729;border:1px solid #232a44;border-radius:18px;overflow:hidden;transition:transform .2s,box-shadow .2s}}article:hover{{transform:translateY(-4px);box-shadow:0 20px 40px -20px #6366f1}}
article.feat{{grid-column:span 2}}.img img{{width:100%;aspect-ratio:1200/630;object-fit:cover;display:block;background:#1b2036}}.b{{padding:18px 20px}}
h2{{margin:0 0 6px;font-size:19px}}.b p{{color:#a8aec5;margin:0 0 12px}}.tags{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}}.tags span{{font-size:12px;padding:3px 10px;border-radius:99px;background:#1f2542;color:#c7d2fe}}
.links{{display:flex;gap:14px}}.links a{{color:#a5b4fc;text-decoration:none;font-weight:600}}.links a.live{{color:#fff;background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:6px 14px;border-radius:10px}}
@media(max-width:720px){{article.feat{{grid-column:auto}}}}footer{{text-align:center;color:#6b7290;font-size:12px;padding-bottom:32px}}
</style></head><body><header><h1>{name}</h1><p>{escape(c.get('headline') or '')}</p>{f'<p>{escape(c.get("about"))}</p>' if c.get('about') else ''}</header>
<main>{cards or '<p>No projects yet.</p>'}</main><footer>Made with Reachout</footer></body></html>"""
    return Response(html, mimetype="text/html")
