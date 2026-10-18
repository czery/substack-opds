"""Substack -> OPDS: multi-user server with web interface, OPDS catalog and EPUB export."""
import html
import io
import json
import os
import re
import secrets
import time
import unicodedata
import zipfile
from pathlib import Path
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import store, substack
from .epub import build_epub

store.init()
store.purge_cache()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=store.SECRET,
    https_only=os.environ.get("HTTPS_ONLY", "1") == "1",
    same_site="lax",
    max_age=30 * 86400,
)

REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "1") == "1"
INVITE_CODE = os.environ.get("INVITE_CODE", "")
MAX_FEEDS = 12
OPDS_NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
OPDS_ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"

_feed_cache: dict = {}
_attempts: dict = {}


# ---------- Helpers ----------

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def base(request: Request) -> str:
    return (os.environ.get("PUBLIC_URL") or str(request.base_url)).rstrip("/")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def throttle(request: Request, limit: int = 10, window: int = 600):
    ip = request.client.host if request.client else "?"
    t = time.time()
    hits = [x for x in _attempts.get(ip, []) if t - x < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many attempts, please try again later.")
    hits.append(t)
    _attempts[ip] = hits


def slugify(s: str) -> str:
    s = s.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "artikel"


def clean_cookie(raw: str) -> str:
    raw = (raw or "").strip().strip('"')
    raw = re.sub(r"^substack\.sid=", "", raw)
    raw = raw.split(";")[0].strip()
    return raw if 20 <= len(raw) <= 4096 and not re.search(r"\s", raw) else ""


async def check_cookie(cookie: str) -> str:
    try:
        async with substack.SubstackClient(cookie) as c:
            await c.feed(substack.CHECK_PATH, 1)
        return "ok"
    except httpx.HTTPStatusError as e:
        return "invalid" if e.response.status_code in (401, 403) else "unknown"
    except Exception:
        return "unknown"


async def get_posts(uid: int, cookie: str, f: dict):
    ck = (uid, f["key"], f["path"], f["limit"])  # settings are part of the key: changes take effect immediately
    hit = _feed_cache.get(ck)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    async with substack.SubstackClient(cookie, lambda c: store.set_cookie(uid, c)) as c:
        posts = await c.feed(f["path"], f["limit"])
    _feed_cache[ck] = (time.time(), posts)
    return posts


def find_feed(uid: int, key: str, only_enabled: bool = True):
    for f in store.get_feeds(uid):
        if f["key"] == key and (f["enabled"] or not only_enabled):
            return f
    return None


def clamp_limit(raw) -> int:
    try:
        return max(1, min(substack.MAX_LIMIT, int(raw)))
    except (TypeError, ValueError):
        return substack.DEFAULT_LIMIT


def drop_cache(uid: int):
    for k in [k for k in _feed_cache if k[0] == uid]:
        del _feed_cache[k]


# ---------- Web interface ----------

CSS = """
:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e3e3dc;--acc:#ff6719}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecece6;--mut:#9a9a92;--card:#1f1f1c;--line:#33332f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.1rem;margin:0 0 .5em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}
input,textarea{width:100%;padding:10px;margin:4px 0 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:10px 16px;border:0;border-radius:6px;background:var(--acc);color:#fff;font:inherit;cursor:pointer}
.btn{display:inline-block;padding:10px 16px;border-radius:6px;background:var(--acc);color:#fff;text-decoration:none;margin:0 6px 6px 0}
button.sec{background:transparent;color:var(--fg);border:1px solid var(--line)}button.danger{background:#b3261e}
code{background:var(--bg);padding:2px 6px;border-radius:4px;word-break:break-all}.mut{color:var(--mut);font-size:.9rem}
.flash{padding:10px 14px;border-radius:6px;background:var(--card);border-left:4px solid var(--acc);margin:12px 0}
.feedrow{border-top:1px solid var(--line);padding-top:10px;margin-top:6px}
.chk{display:inline-block;margin-right:16px}.chk input{width:auto;margin:0 6px 0 0}
"""


def page(title: str, body: str, flash: str = "") -> HTMLResponse:
    f = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{f}{body}</main></body></html>"
    )


def current_user(request: Request):
    uid = request.session.get("uid")
    return store.get_user(uid) if uid else None


def flash(request: Request, msg: str):
    request.session["flash"] = msg


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    msg = request.session.pop("flash", "")
    u = current_user(request)
    if not u:
        reg = ""
        if REGISTRATION_OPEN:
            inv = "<label>Invite code<input name='invite' required></label>" if INVITE_CODE else ""
            reg = f"""<div class=card><h2>Sign up</h2>
<form method=post action=/register><label>Email<input name=email type=email required></label>
<label>Password (min. 10 characters)<input name=password type=password minlength=10 required></label>{inv}
<button>Create account</button></form></div>"""
        return page("Substack → E-reader", f"""<h1>Substack → E-reader</h1>
<p class=mut>Your Substack articles as an OPDS catalog for CrossPoint and other e-ink readers.</p>
<div class=card><h2>Log in</h2><form method=post action=/login>
<label>Email<input name=email type=email required></label>
<label>Password<input name=password type=password required></label><button>Log in</button></form></div>{reg}""", msg)

    b = base(request)
    connected = (
        f"connected (last updated {datetime.fromtimestamp(u['cookie_at']).strftime('%Y-%m-%d %H:%M')})"
        if u["cookie_enc"] else "not connected yet"
    )
    return page("Dashboard", f"""<h1>Your account</h1><p class=mut>{esc(u['email'])}</p>
<div class=card><h2>OPDS address for your reader</h2>
<p><code>{esc(b)}/o/{esc(u['opds_token'])}/</code></p>
<p class=mut>This address is secret and grants access to your account. Add it to your reader as an OPDS server.</p></div>
<div class=card><h2>Substack connection: {esc(connected)}</h2>
<p class=mut>The easiest way is the browser extension. It reads your <code>substack.sid</code> cookie and sends it to this server
whenever it changes and every 6 hours, so your reader keeps working without any manual steps. You have to stay logged in to Substack in that browser.
The cookie is only sent to this server.</p>
<p><a class=btn href=/extension/chrome.zip>Download for Chrome / Edge / Brave</a>
<a class=btn href=/extension/firefox.zip>Download for Firefox</a></p>
<p class=mut>The download is personalized: server address and sync token are already filled in.
Browsers do not allow one-click installs from outside their stores, so it takes a few steps:</p>
<ol class=mut>
<li><strong>Chrome / Edge / Brave:</strong> unzip the file, open <code>chrome://extensions</code>, enable developer mode, click “Load unpacked” and select the unzipped folder.</li>
<li><strong>Firefox:</strong> open <code>about:debugging#/runtime/this-firefox</code>, click “Load Temporary Add-on…” and select the zip file (or its <code>manifest.json</code>).
Temporary add-ons are removed when Firefox closes; for a permanent install use Firefox Developer Edition/Nightly with <code>xpinstall.signatures.required</code> set to false, or sign the add-on yourself via addons.mozilla.org (unlisted).</li>
<li>Click the extension icon, press “Save &amp; sync” and allow access to this server and substack.com when asked.</li>
</ol>
<p class=mut>Without the download: set up the extension manually with server <code>{esc(b)}</code> and sync token <code>{esc(u['sync_token'])}</code>.
Or paste the value of <code>substack.sid</code> here (DevTools → Application → Cookies → substack.com).</p>
<form method=post action=/cookie><textarea name=cookie rows=3 placeholder="substack.sid value" required></textarea>
<button>Save cookie</button></form></div>
{feeds_card(u)}
<div class=card><h2>Account</h2>
<form method=post action=/cookie/delete style=display:inline><button class=sec>Disconnect Substack</button></form>
<form method=post action=/token/rotate style=display:inline><button class=sec>Regenerate addresses</button></form>
<form method=post action=/logout style=display:inline><button class=sec>Log out</button></form>
<form method=post action=/account/delete onsubmit="return confirm('Permanently delete your account and stored data?')" style="margin-top:12px">
<button class=danger>Delete account</button></form></div>""", msg)


def feeds_card(u) -> str:
    rows = ""
    for f in store.get_feeds(u["id"]):
        k = esc(f["key"])
        rows += (
            f'<div class=feedrow>'
            f'<label>Name<input name="name_{k}" value="{esc(f["name"])}" maxlength=40 required></label>'
            f'<label>Substack path or URL<input name="path_{k}" value="{esc(f["path"])}" required></label>'
            f'<label>Number of articles (1–{substack.MAX_LIMIT})'
            f'<input name="limit_{k}" type=number min=1 max={substack.MAX_LIMIT} value="{f["limit"]}"></label>'
            f'<label class=chk><input type=checkbox name=enabled value="{k}"{" checked" if f["enabled"] else ""}>enabled</label>'
            f'<label class=chk><input type=checkbox name=delete value="{k}">delete</label></div>'
        )
    return f"""<div class=card><h2>Catalogs in your reader</h2>
<p class=mut>Each catalog fetches one Substack path. To find new paths, open substack.com in Chrome:
DevTools → Network → filter for <code>api/v1</code>, then open the tab you want in Substack and copy the request.
Only <code>/api/…</code> paths or <code>https://substack.com/api/…</code> URLs are allowed. The response format is detected automatically.</p>
<p class=mut>Substack returns at most 20 articles per request. The server keeps paging until the number is reached
(at most {substack.MAX_PAGES} requests or {int(substack.TIME_BUDGET)} seconds). Mixed feeds like “For you” therefore take longer.</p>
<form method=post action=/feeds>{rows}
<div class=feedrow><strong>New catalog</strong>
<label>Name<input name=new_name maxlength=40></label>
<label>Substack path or URL<input name=new_path placeholder="/api/v1/…"></label>
<label>Number of articles<input name=new_limit type=number min=1 max={substack.MAX_LIMIT} value="{substack.DEFAULT_LIMIT}"></label></div>
<button>Save catalogs</button></form>
<form method=post action=/feeds/reset style=margin-top:10px><button class=sec>Reset to defaults</button></form>
<h3>Author prefix</h3>
<p class=mut>Optional text put in front of every author name in your reader, for example <code>Substack - </code>.
Inside the EPUB the author becomes “Prefix Lastname, Firstname” (e.g. “Substack - Hielscher, Matze”), so readers that sort their library by author list all articles together under that prefix.
It is written into the EPUB, so it applies to articles you download afterwards.</p>
<form method=post action=/prefix><label>Prefix<input name=prefix value="{esc(u['author_prefix'] or '')}" maxlength=30 placeholder="Substack-"></label>
<button>Save prefix</button></form></div>"""


@app.post("/feeds")
async def save_feeds(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", 303)
    form = await request.form()
    enabled, delete = set(form.getlist("enabled")), set(form.getlist("delete"))
    out: list[dict] = []
    errors: list[str] = []
    for f in store.get_feeds(u["id"]):
        k = f["key"]
        if k in delete:
            continue
        name = (form.get(f"name_{k}") or "").strip()[:40]
        path = substack.parse_feed_path(form.get(f"path_{k}") or "")
        if not name or not path:
            errors.append(f"“{name or f['name']}” unchanged (name missing or path invalid)")
            out.append({**f, "enabled": k in enabled})
            continue
        out.append({"key": k, "name": name, "path": path,
                    "limit": clamp_limit(form.get(f"limit_{k}")), "enabled": k in enabled})
    new_name = (form.get("new_name") or "").strip()[:40]
    new_raw = (form.get("new_path") or "").strip()
    if new_name or new_raw:
        new_path = substack.parse_feed_path(new_raw)
        if not new_name or not new_path:
            errors.append("New catalog not created (name missing or path invalid)")
        elif len(out) >= MAX_FEEDS:
            errors.append(f"New catalog not created (maximum {MAX_FEEDS} catalogs)")
        else:
            out.append({"key": "f" + secrets.token_hex(3), "name": new_name, "path": new_path,
                        "limit": clamp_limit(form.get("new_limit")), "enabled": True})
    store.set_feeds(u["id"], out)
    drop_cache(u["id"])
    flash(request, "Catalogs saved." if not errors else "Saved, but: " + "; ".join(errors) + ".")
    return RedirectResponse("/", 303)


@app.post("/feeds/reset")
async def reset_feeds(request: Request):
    u = current_user(request)
    if u:
        store.set_feeds(u["id"], None)
        drop_cache(u["id"])
        flash(request, "Catalogs reset to defaults.")
    return RedirectResponse("/", 303)


@app.post("/register")
async def register(request: Request, email: str = Form(), password: str = Form(), invite: str = Form("")):
    throttle(request)
    if not REGISTRATION_OPEN or (INVITE_CODE and invite != INVITE_CODE):
        flash(request, "Registration is not possible.")
        return RedirectResponse("/", 303)
    email = email.strip().lower()
    if len(password) < 10 or "@" not in email:
        flash(request, "Please enter a valid email address and a password with at least 10 characters.")
        return RedirectResponse("/", 303)
    uid = store.create_user(email, password)
    if not uid:
        flash(request, "This email address is already registered.")
        return RedirectResponse("/", 303)
    request.session["uid"] = uid
    return RedirectResponse("/", 303)


@app.post("/login")
async def login(request: Request, email: str = Form(), password: str = Form()):
    throttle(request)
    u = store.verify_user(email.strip().lower(), password)
    if not u:
        flash(request, "Wrong email or password.")
    else:
        request.session["uid"] = u["id"]
    return RedirectResponse("/", 303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.post("/cookie")
async def save_cookie(request: Request, cookie: str = Form()):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", 303)
    c = clean_cookie(cookie)
    if not c:
        flash(request, "That does not look like a substack.sid cookie.")
        return RedirectResponse("/", 303)
    state = await check_cookie(c)
    if state == "invalid":
        flash(request, "Substack rejects this cookie. Are you logged in there?")
        return RedirectResponse("/", 303)
    store.set_cookie(u["id"], c)
    drop_cache(u["id"])
    flash(request, "Saved." if state == "ok" else "Saved, but Substack could not be checked right now.")
    return RedirectResponse("/", 303)


@app.post("/prefix")
async def save_prefix(request: Request, prefix: str = Form("")):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", 303)
    prefix = re.sub(r"[\x00-\x1f<>&]", "", prefix).lstrip()[:30]
    if prefix != (u["author_prefix"] or ""):
        store.set_author_prefix(u["id"], prefix)
        drop_cache(u["id"])
        d = store.cache_dir(u["id"])  # EPUBs carry the prefix, so rebuild them on the next download
        for f in os.listdir(d):
            if f.endswith(".epub"):
                os.remove(os.path.join(d, f))
    flash(request, "Author prefix saved." if prefix else "Author prefix removed.")
    return RedirectResponse("/", 303)


@app.post("/cookie/delete")
async def delete_cookie(request: Request):
    u = current_user(request)
    if u:
        store.set_cookie(u["id"], None)
        drop_cache(u["id"])
        flash(request, "Substack disconnected.")
    return RedirectResponse("/", 303)


@app.post("/token/rotate")
async def rotate(request: Request):
    u = current_user(request)
    if u:
        store.rotate_tokens(u["id"])
        flash(request, "New addresses generated. Update your reader and the extension.")
    return RedirectResponse("/", 303)


@app.post("/account/delete")
async def delete_account(request: Request):
    u = current_user(request)
    if u:
        store.delete_user(u["id"])
        drop_cache(u["id"])
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------- Extension API ----------

@app.post("/api/cookie")
async def api_cookie(request: Request, authorization: str = Header(default="")):
    throttle(request, limit=60)
    token = authorization.removeprefix("Bearer ").strip()
    u = store.user_by_token("sync_token", token) if token else None
    if not u:
        raise HTTPException(401, "Invalid token")
    try:
        c = clean_cookie((await request.json()).get("cookie", ""))
    except Exception:
        c = ""
    if not c:
        raise HTTPException(400, "Cookie missing or invalid")
    store.set_cookie(u["id"], c)
    drop_cache(u["id"])
    return JSONResponse({"ok": True})


EXT_DIR = Path(__file__).resolve().parent.parent / "extension"
EXT_FILES = ("background.js", "popup.html", "popup.js")


@app.get("/extension/{browser}.zip")
def extension_zip(request: Request, browser: str):
    """Personalized extension package: server URL and sync token are prefilled in config.json."""
    u = current_user(request)
    if not u:
        return RedirectResponse("/", 303)
    if browser not in ("chrome", "firefox"):
        raise HTTPException(404)
    manifest = "manifest.json" if browser == "chrome" else "manifest.firefox.json"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(EXT_DIR / manifest, "manifest.json")
        for name in EXT_FILES:
            z.write(EXT_DIR / name, name)
        z.writestr("config.json", json.dumps({"server": base(request), "token": u["sync_token"]}))
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="substack-opds-{browser}.zip"',
                 "Cache-Control": "no-store"},
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------- OPDS ----------

def atom(title: str, self_href: str, start: str, entries: list[str], nav: bool) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">'
        f"<id>urn:substack-opds:{esc(self_href)}</id><title>{esc(title)}</title><updated>{now_iso()}</updated>"
        f'<link rel="self" href="{esc(self_href)}" type="{OPDS_NAV if nav else OPDS_ACQ}"/>'
        f'<link rel="start" href="{esc(start)}" type="{OPDS_NAV}"/>'
        + "".join(entries)
        + "</feed>"
    )
    return Response(xml, media_type=OPDS_NAV if nav else OPDS_ACQ)


def info_entry(title: str, text: str) -> str:
    return (f"<entry><title>{esc(title)}</title><id>urn:info:{esc(slugify(title))}</id>"
            f"<updated>{now_iso()}</updated><content type='text'>{esc(text)}</content></entry>")


def _opds_user(token: str):
    u = store.user_by_token("opds_token", token)
    if not u:
        raise HTTPException(404)
    return u


@app.get("/o/{token}/")
async def opds_root(token: str, request: Request):
    u = _opds_user(token)
    b = f"{base(request)}/o/{token}"
    entries = [
        f"<entry><title>{esc(f['name'])}</title><id>urn:feed:{esc(f['key'])}</id><updated>{now_iso()}</updated>"
        f'<link rel="subsection" href="{esc(b)}/feed/{esc(f["key"])}" type="{OPDS_ACQ}"/></entry>'
        for f in store.get_feeds(u["id"]) if f["enabled"]
    ] or [info_entry("No catalogs enabled", "Please enable at least one catalog in the web interface.")]
    return atom("Substack", f"{b}/", f"{b}/", entries, nav=True)


@app.get("/o/{token}/feed/{key}")
async def opds_feed(token: str, key: str, request: Request):
    u = _opds_user(token)
    feed = find_feed(u["id"], key)
    if not feed:
        raise HTTPException(404)
    b = f"{base(request)}/o/{token}"
    cookie = store.get_cookie(u["id"])
    entries: list[str] = []
    if not cookie:
        entries.append(info_entry("No Substack connection", "Please connect Substack in the web interface."))
    else:
        try:
            for p in await get_posts(u["id"], cookie, feed):
                name = f"{p['id']}-{slugify(p['title'])}"
                paid = " 🔒" if p["audience"] == "only_paid" else ""
                summary = " · ".join(x for x in (p["pub"], p["subtitle"]) if x)
                entries.append(
                    f"<entry><title>{esc(p['title'] + paid)}</title><id>urn:substack:{p['id']}</id>"
                    f"<updated>{esc(p['date'] or now_iso())}</updated>"
                    f"<author><name>{esc((u['author_prefix'] or '') + p['author'])}</name></author>"
                    f"<summary>{esc(summary)}</summary>"
                    f'<link rel="http://opds-spec.org/acquisition" href="{esc(b)}/post/{name}.epub" '
                    f'type="application/epub+zip"/></entry>'
                )
            if not entries:
                entries.append(info_entry("No articles", "This catalog is empty."))
        except httpx.HTTPStatusError as ex:
            expired = ex.response.status_code in (401, 403)
            entries.append(info_entry(
                "Substack session expired" if expired else f"Substack error {ex.response.status_code}",
                "Please log in to Substack again (the extension then syncs automatically)." if expired
                else "Please try again later."))
        except httpx.HTTPError:
            entries.append(info_entry("Substack unreachable", "Please try again later."))
    return atom(feed["name"], f"{b}/feed/{key}", f"{b}/", entries, nav=False)


@app.get("/o/{token}/post/{name}.epub")
async def opds_post(token: str, name: str):
    u = _opds_user(token)
    m = re.match(r"^(\d+)(?:-.*)?$", name)
    if not m:
        raise HTTPException(404)
    pid = int(m.group(1))
    path = os.path.join(store.cache_dir(u["id"]), f"{pid}.epub")  # per user, never shared
    if not os.path.exists(path):
        cookie = store.get_cookie(u["id"])
        if not cookie:
            raise HTTPException(409, "No Substack connection")
        try:
            async with substack.SubstackClient(cookie, lambda c: store.set_cookie(u["id"], c)) as c:
                post = await c.post(pid)
            data = await build_epub(post, u["author_prefix"] or "")
        except httpx.HTTPStatusError as ex:
            raise HTTPException(502, f"Substack responded with {ex.response.status_code}")
        except httpx.HTTPError:
            raise HTTPException(502, "Substack unreachable")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    return FileResponse(path, media_type="application/epub+zip", filename=f"{name}.epub")


@app.get("/o/{token}/debug/{key}")
async def opds_debug(token: str, key: str):
    """Diagnostics: shows only the structure of Substack's response (keys, types, titles), no content."""
    u = _opds_user(token)
    feed = find_feed(u["id"], key, only_enabled=False)
    if not feed:
        raise HTTPException(404)
    cookie = store.get_cookie(u["id"])
    if not cookie:
        raise HTTPException(409, "No Substack connection")
    async with substack.SubstackClient(cookie, lambda c: store.set_cookie(u["id"], c)) as c:
        r = await c.http.get(substack.BASE + feed["path"])
    out: dict = {"status": r.status_code, "final_url": str(r.url)}
    try:
        data = r.json()
    except Exception:
        out["body_start"] = r.text[:300]
        return JSONResponse(out)
    items = data if isinstance(data, list) else (data.get("items") or data.get("posts") or [])
    if isinstance(data, dict):
        out["top_keys"] = sorted(data)
    out["count"] = len(items)
    out["items"] = [
        {
            "keys": sorted(i)[:20],
            "type": i.get("type"),
            "post_keys": sorted(i["post"])[:12] if isinstance(i.get("post"), dict) else None,
            "id": (i.get("post") or i).get("id"),
            "title": (i.get("post") or i).get("title"),
        }
        for i in items[:40]
        if isinstance(i, dict)
    ]
    return JSONResponse(out)
