"""Shared plumbing for the feature modules (GitHub, jobs, LinkedIn).

app.py calls setup() with its own module once everything is defined, so feature modules can use the
core helpers (Workspace, Mongo handles, encryption, validation) without importing app.py a second time
(which would happen under `python app.py`, where the core module is __main__, not app).
"""

import functools
import threading
import time

import requests

C = None                 # the core app module, set by setup()
QUEUE_HANDLERS = {}      # kind -> fn(ws, payload) -> (ok: bool, detail: str); used by the send queue


def setup(core):
    global C
    C = core


def login_required(fn):
    """Same as the core decorator, resolved at request time."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return C.login_required(fn)(*args, **kwargs)
    return wrapper


def http(method, url, *, token=None, timeout=30, **kw):
    """requests wrapper with a sensible timeout and a clear error for network failures."""
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("User-Agent", "Reachout/1.0")
    try:
        return requests.request(method, url, headers=headers, timeout=timeout, **kw)
    except requests.RequestException as e:
        raise C.Invalid(f"Couldn't reach {url.split('/')[2]}: {e.__class__.__name__}. Check your internet connection.",
                        status=502)


def every(seconds, fn, name):
    """Run fn() forever in a daemon thread, every `seconds`, logging (not raising) errors."""
    def loop():
        while True:
            time.sleep(seconds)
            try:
                fn()
            except Exception as e:  # keep the loop alive
                print(f"[Reachout] {name}: {e}", flush=True)
    threading.Thread(target=loop, daemon=True, name=name).start()


class Timeout(Exception):
    """A per-account job ran past its deadline (it keeps running in the background, but the loop moves on)."""


def with_deadline(seconds, fn, *args, **kwargs):
    """Run fn in its own daemon thread and wait at most `seconds`. One slow or stalling mail server then
    can't hold up the background work of every other account (loops call this per account)."""
    box = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:  # handed back to the caller below
            box["error"] = e
    t = threading.Thread(target=run, daemon=True, name=f"deadline:{getattr(fn, '__name__', 'job')}")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise Timeout(f"{getattr(fn, '__name__', 'job')} took longer than {seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")

