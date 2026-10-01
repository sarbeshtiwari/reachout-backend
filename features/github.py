"""GitHub workspace: connect with a personal access token, browse repos, edit files, commit & push, PRs.

The token is stored encrypted in the user's workspace and never sent to the browser. All GitHub calls go
through this server.
"""

import base64
import re
from urllib.parse import quote

from flask import Blueprint, jsonify, request

import bridge
from bridge import login_required

bp = Blueprint("github", __name__)
API = "https://api.github.com"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)$")
MAX_EDIT_BYTES = 1_000_000
IMAGE_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
             "svg": "image/svg+xml", "ico": "image/x-icon"}


def C():
    return bridge.C


def token_of(ws):
    gh = ws.load("github", {})
    if not gh.get("token"):
        raise C().Invalid("Connect your GitHub account first.", status=400)
    return gh["token"]


def gh(ws, method, path, ok=(200, 201, 204), **kw):
    """Call the GitHub REST API as this user; turn GitHub errors into readable messages."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    r = bridge.http(method, API + path, token=token_of(ws), headers=headers, **kw)
    if r.status_code in ok:
        return r.json() if r.content and "json" in r.headers.get("Content-Type", "") else {}
    try:
        msg = r.json().get("message", "")
        errs = r.json().get("errors") or []
        if errs and isinstance(errs, list):
            msg += ": " + "; ".join(str(e.get("message") or e.get("code") or e) if isinstance(e, dict) else str(e) for e in errs)
    except ValueError:
        msg = r.text[:200]
    if r.status_code == 401:
        raise C().Invalid("GitHub rejected the token (expired or revoked). Reconnect on the GitHub page.", status=401)
    if r.status_code == 403 and "rate limit" in msg.lower():
        raise C().Invalid("GitHub's rate limit was reached. Try again in a few minutes.", status=429)
    if r.status_code == 403:
        raise C().Invalid(f"GitHub says your token doesn't have permission for this: {msg}", status=403)
    if r.status_code == 404:
        raise C().Invalid("Not found on GitHub (or your token can't see it).", status=404)
    if r.status_code == 409 and "empty" in msg.lower():
        raise C().Invalid("This repository is empty.", status=409)
    raise C().Invalid(f"GitHub: {msg or r.status_code}", status=400 if r.status_code < 500 else 502)


def repo_path(owner, repo):
    if (not NAME_RE.match(owner or "") or not NAME_RE.match(repo or "")
            or set(owner) <= {"."} or set(repo) <= {"."}):  # "." / ".." would walk to other API paths
        raise C().Invalid("Invalid repository name.")
    return f"/repos/{owner}/{repo}"


def clean_path(path):
    path = str(path or "").strip().strip("/")
    if not path or len(path) > 400 or any(seg in ("", ".", "..") for seg in path.split("/")) or "\\" in path:
        raise C().Invalid("Invalid file path.", "path")
    return path


def clean_branch(name, field="branch"):
    name = str(name or "").strip()
    if not BRANCH_RE.match(name):
        raise C().Invalid("Invalid branch name. Use letters, numbers, - _ . and /.", field)
    return name


def body():
    return C().body()


# ---------------------------------------------------------------- account

@bp.get("/api/gh/status")
@login_required
def status(ws):
    g = ws.load("github", {})
    return jsonify(connected=bool(g.get("token")), **{k: g.get(k) for k in ("login", "name", "avatar", "scopes")})


@bp.post("/api/gh/connect")
@login_required
def connect(ws):
    token = str(body().get("token") or "").strip()
    if not re.fullmatch(r"(gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})", token):
        raise C().Invalid("That doesn't look like a GitHub token. It should start with ghp_ or github_pat_.", "token")
    r = bridge.http("GET", API + "/user", token=token, headers={"Accept": "application/vnd.github+json"})
    if r.status_code == 401:
        raise C().Invalid("GitHub rejected this token. Check you copied all of it and that it hasn't expired.", "token")
    if r.status_code != 200:
        raise C().Invalid(f"GitHub returned an error ({r.status_code}). Try again.", "token")
    u = r.json()
    ws.save("github", {"token": token, "login": u.get("login"), "name": u.get("name") or u.get("login"),
                       "avatar": u.get("avatar_url"), "scopes": r.headers.get("X-OAuth-Scopes", "")})
    return jsonify(ok=True, login=u.get("login"))


@bp.post("/api/gh/disconnect")
@login_required
def disconnect(ws):
    ws.save("github", {})
    return jsonify(ok=True)


# ---------------------------------------------------------------- repositories

def slim_repo(r):
    return {k: r.get(k) for k in ("name", "full_name", "description", "private", "default_branch", "language",
                                  "stargazers_count", "forks_count", "open_issues_count", "updated_at", "html_url",
                                  "fork", "archived", "size", "homepage", "has_pages", "topics", "created_at", "pushed_at")} | {"owner": r["owner"]["login"], "owner_type": r["owner"].get("type", "User"),
                                                                "permissions": r.get("permissions", {})}


@bp.get("/api/gh/repos")
@login_required
def repos(ws):
    out = []
    for page in range(1, 6):
        batch = gh(ws, "GET", f"/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner,collaborator,organization_member")
        out += [slim_repo(r) for r in batch]
        if len(batch) < 100:
            break
    return jsonify(repos=out)


@bp.post("/api/gh/repos")
@login_required
def create_repo(ws):
    p = body()
    name = str(p.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise C().Invalid("Repository names can use letters, numbers, - _ and . (up to 100 characters).", "name")
    r = gh(ws, "POST", "/user/repos", json={"name": name, "description": str(p.get("description") or "")[:350],
                                            "private": bool(p.get("private")), "auto_init": bool(p.get("readme", True))})
    return jsonify(slim_repo(r))


@bp.get("/api/gh/repo/<owner>/<repo>")
@login_required
def repo_info(ws, owner, repo):
    base = repo_path(owner, repo)
    info = slim_repo(gh(ws, "GET", base))
    branches = gh(ws, "GET", base + "/branches?per_page=100")
    info["branches"] = [b["name"] for b in branches]
    return jsonify(info)


@bp.get("/api/gh/tree/<owner>/<repo>")
@login_required
def tree(ws, owner, repo):
    base, ref = repo_path(owner, repo), clean_branch(request.args.get("ref"), "ref")
    try:
        head = gh(ws, "GET", f"{base}/commits/{quote(ref, safe='')}")
    except C().Invalid as e:
        if e.status == 409:
            return jsonify(empty=True, items=[], head=None)
        raise
    t = gh(ws, "GET", f"{base}/git/trees/{head['commit']['tree']['sha']}?recursive=1")
    items = [{"path": i["path"], "type": "dir" if i["type"] == "tree" else "file", "sha": i["sha"],
              "size": i.get("size", 0), "mode": i["mode"]} for i in t.get("tree", []) if i["type"] in ("tree", "blob")]
    return jsonify(items=items, head=head["sha"], truncated=t.get("truncated", False),
                   message=head["commit"]["message"].split("\n")[0], author=head["commit"]["author"]["name"],
                   date=head["commit"]["author"]["date"])


@bp.get("/api/gh/file/<owner>/<repo>")
@login_required
def read_file(ws, owner, repo):
    base, path = repo_path(owner, repo), clean_path(request.args.get("path"))
    ref = clean_branch(request.args.get("ref"), "ref")
    meta = gh(ws, "GET", f"{base}/contents/{quote(path)}?ref={quote(ref, safe='')}")
    if isinstance(meta, list) or meta.get("type") != "file":
        raise C().Invalid("That's a folder, not a file.")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if meta["size"] > MAX_EDIT_BYTES:
        return jsonify(path=path, sha=meta["sha"], size=meta["size"], kind="large", html_url=meta.get("html_url"))
    raw = base64.b64decode(meta.get("content") or "") if meta.get("encoding") == "base64" else \
        base64.b64decode(gh(ws, "GET", f"{base}/git/blobs/{meta['sha']}")["content"])
    if ext in IMAGE_EXT:
        return jsonify(path=path, sha=meta["sha"], size=meta["size"], kind="image",
                       data=f"data:{IMAGE_EXT[ext]};base64,{base64.b64encode(raw).decode()}")
    try:
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise UnicodeDecodeError("utf-8", raw, 0, 1, "nul")
    except UnicodeDecodeError:
        return jsonify(path=path, sha=meta["sha"], size=meta["size"], kind="binary", html_url=meta.get("html_url"))
    return jsonify(path=path, sha=meta["sha"], size=meta["size"], kind="text", content=text)


@bp.post("/api/gh/commit/<owner>/<repo>")
@login_required
def commit(ws, owner, repo):
    """Commit several file changes at once and push them to a branch (create, edit, delete).

    base_head is the commit the editor loaded. If the branch moved since then and the new commits touch the
    same files, the push is refused (conflict) so nobody's work is overwritten."""
    base, p = repo_path(owner, repo), body()
    branch = clean_branch(p.get("branch"))
    message = str(p.get("message") or "").strip()
    if not message:
        raise C().Invalid("Write a commit message describing the change.", "message")
    changes = p.get("changes")
    if not isinstance(changes, list) or not changes:
        raise C().Invalid("There are no changes to commit.")
    if len(changes) > 100:
        raise C().Invalid("Commit at most 100 files at a time.")
    tree_items, paths = [], set()
    for ch in changes:
        path = clean_path(ch.get("path"))
        if path in paths:
            raise C().Invalid(f"{path} appears twice in this commit.")
        paths.add(path)
        mode = ch.get("mode") if ch.get("mode") in ("100644", "100755") else "100644"
        if ch.get("delete"):
            tree_items.append({"path": path, "mode": mode, "type": "blob", "sha": None})
        else:
            content = ch.get("content")
            if not isinstance(content, str):
                raise C().Invalid(f"Missing content for {path}.")
            if len(content.encode()) > MAX_EDIT_BYTES:
                raise C().Invalid(f"{path} is larger than 1 MB.")
            tree_items.append({"path": path, "mode": mode, "type": "blob", "content": content})
    try:
        ref = gh(ws, "GET", f"{base}/git/ref/heads/{quote(branch, safe='/')}")
    except C().Invalid as e:
        if e.status in (404, 409):
            return commit_to_empty_repo(ws, base, branch, message, tree_items)
        raise
    head = ref["object"]["sha"]
    known = p.get("base_head")
    if known and known != head:
        cmp = gh(ws, "GET", f"{base}/compare/{known}...{head}")
        clash = sorted(paths & {f["filename"] for f in cmp.get("files", [])})
        if clash:
            raise C().Invalid("Someone pushed changes to " + ", ".join(clash[:5]) + " since you opened them. "
                              "Pull the latest version first (your edits stay in the editor).", "conflict", 409)
    new_commit = push_tree(ws, base, head, branch, message, tree_items)
    return jsonify(ok=True, sha=new_commit["sha"], url=new_commit.get("html_url"), files=len(tree_items))


def push_tree(ws, base, head, branch, message, tree_items):
    parent = gh(ws, "GET", f"{base}/git/commits/{head}")
    new_tree = gh(ws, "POST", f"{base}/git/trees", json={"base_tree": parent["tree"]["sha"], "tree": tree_items})
    new_commit = gh(ws, "POST", f"{base}/git/commits", json={"message": message, "tree": new_tree["sha"], "parents": [head]})
    gh(ws, "PATCH", f"{base}/git/refs/heads/{quote(branch, safe='/')}", json={"sha": new_commit["sha"]})
    return new_commit


def write_file(ws, owner, repo, branch, path, content, message):
    """Create or update one file on a branch (used by Portfolio publishing). Returns the commit."""
    base, path, branch = repo_path(owner, repo), clean_path(path), clean_branch(branch)
    item = {"path": path, "mode": "100644", "type": "blob", "content": content}
    try:
        ref = gh(ws, "GET", f"{base}/git/ref/heads/{quote(branch, safe='/')}")
    except C().Invalid as e:
        if e.status in (404, 409):
            r = gh(ws, "PUT", f"{base}/contents/{quote(path)}", json={"message": message, "branch": branch,
                                                                    "content": base64.b64encode(content.encode()).decode()})
            return r["commit"]
        raise
    return push_tree(ws, base, ref["object"]["sha"], branch, message, [item])


def commit_to_empty_repo(ws, base, branch, message, items):
    """An empty repo has no commit to build on: create files one by one with the contents API."""
    sha = None
    for it in items:
        if it.get("sha", "") is None:
            continue
        r = gh(ws, "PUT", f"{base}/contents/{quote(it['path'])}",
               json={"message": message, "content": base64.b64encode(it["content"].encode()).decode(), "branch": branch})
        sha = r["commit"]["sha"]
    return jsonify(ok=True, sha=sha, files=len(items))


@bp.post("/api/gh/branch/<owner>/<repo>")
@login_required
def create_branch(ws, owner, repo):
    base, p = repo_path(owner, repo), body()
    name, src = clean_branch(p.get("name"), "name"), clean_branch(p.get("from"), "from")
    head = gh(ws, "GET", f"{base}/git/ref/heads/{quote(src, safe='/')}")["object"]["sha"]
    gh(ws, "POST", f"{base}/git/refs", json={"ref": f"refs/heads/{name}", "sha": head})
    return jsonify(ok=True, name=name)


@bp.get("/api/gh/commits/<owner>/<repo>")
@login_required
def commits(ws, owner, repo):
    base, ref = repo_path(owner, repo), clean_branch(request.args.get("ref"), "ref")
    rows = gh(ws, "GET", f"{base}/commits?sha={quote(ref, safe='')}&per_page=40")
    return jsonify(commits=[{"sha": c["sha"], "message": c["commit"]["message"], "author": c["commit"]["author"]["name"],
                             "date": c["commit"]["author"]["date"], "url": c["html_url"],
                             "avatar": (c.get("author") or {}).get("avatar_url")} for c in rows])


@bp.get("/api/gh/commit/<owner>/<repo>/<sha>")
@login_required
def commit_detail(ws, owner, repo, sha):
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise C().Invalid("Invalid commit.")
    c = gh(ws, "GET", f"{repo_path(owner, repo)}/commits/{sha}")
    return jsonify(sha=c["sha"], message=c["commit"]["message"], author=c["commit"]["author"]["name"],
                   date=c["commit"]["author"]["date"], url=c["html_url"], stats=c.get("stats"),
                   files=[{k: f.get(k) for k in ("filename", "status", "additions", "deletions", "patch")} for f in c["files"]])


# ---------------------------------------------------------------- pull requests

def slim_pr(pr):
    return {"number": pr["number"], "title": pr["title"], "state": "merged" if pr.get("merged_at") else pr["state"],
            "user": pr["user"]["login"], "head": pr["head"]["ref"], "base": pr["base"]["ref"], "draft": pr.get("draft"),
            "created_at": pr["created_at"], "updated_at": pr["updated_at"], "url": pr["html_url"], "body": pr.get("body") or ""}


@bp.get("/api/gh/pulls/<owner>/<repo>")
@login_required
def pulls(ws, owner, repo):
    state = request.args.get("state", "open")
    if state not in ("open", "closed", "all"):
        raise C().Invalid("Invalid state.")
    return jsonify(pulls=[slim_pr(p) for p in gh(ws, "GET", f"{repo_path(owner, repo)}/pulls?state={state}&per_page=50")])


@bp.get("/api/gh/pull/<owner>/<repo>/<int:number>")
@login_required
def pull_detail(ws, owner, repo, number):
    base = repo_path(owner, repo)
    pr = gh(ws, "GET", f"{base}/pulls/{number}")
    files = gh(ws, "GET", f"{base}/pulls/{number}/files?per_page=100")
    return jsonify(slim_pr(pr) | {"mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
                                  "additions": pr.get("additions"), "deletions": pr.get("deletions"),
                                  "files": [{k: f.get(k) for k in ("filename", "status", "additions", "deletions", "patch")}
                                            for f in files]})


@bp.post("/api/gh/pulls/<owner>/<repo>")
@login_required
def create_pull(ws, owner, repo):
    p = body()
    title = str(p.get("title") or "").strip()
    if not title:
        raise C().Invalid("Give the pull request a title.", "title")
    head, base_branch = clean_branch(p.get("head"), "head"), clean_branch(p.get("base"), "base")
    if head == base_branch:
        raise C().Invalid("Choose two different branches.", "head")
    pr = gh(ws, "POST", f"{repo_path(owner, repo)}/pulls",
            json={"title": title[:250], "head": head, "base": base_branch, "body": str(p.get("body") or "")[:20000]})
    return jsonify(slim_pr(pr))


@bp.post("/api/gh/pull/<owner>/<repo>/<int:number>/merge")
@login_required
def merge_pull(ws, owner, repo, number):
    method = body().get("method", "merge")
    if method not in ("merge", "squash", "rebase"):
        raise C().Invalid("Invalid merge method.")
    r = gh(ws, "PUT", f"{repo_path(owner, repo)}/pulls/{number}/merge", ok=(200,), json={"merge_method": method})
    return jsonify(ok=True, sha=r.get("sha"), message=r.get("message"))


@bp.post("/api/gh/pull/<owner>/<repo>/<int:number>/close")
@login_required
def close_pull(ws, owner, repo, number):
    gh(ws, "PATCH", f"{repo_path(owner, repo)}/pulls/{number}", json={"state": "closed"})
    return jsonify(ok=True)


# ---------------------------------------------------------------- markdown preview

@bp.post("/api/gh/markdown")
@login_required
def markdown(ws):
    p = body()
    text = str(p.get("text") or "")[:400_000]
    ctx = str(p.get("context") or "")
    payload = {"text": text, "mode": "gfm"}
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", ctx):
        payload["context"] = ctx
    r = bridge.http("POST", API + "/markdown", token=token_of(ws),
                    headers={"Accept": "application/vnd.github+json"}, json=payload)
    if r.status_code != 200:
        raise C().Invalid("GitHub couldn't render this Markdown.")
    return jsonify(html=r.text)
