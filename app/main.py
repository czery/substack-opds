"""Substack -> OPDS: Mehrnutzer-Server mit Weboberfläche, OPDS-Katalog und EPUB-Export."""
import html
import os
import re
import time
import unicodedata
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
FEED_ORDER = ["latest", "for-you", "saved", "paid"]
OPDS_NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
OPDS_ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"

_feed_cache: dict = {}
_attempts: dict = {}


# ---------- Helfer ----------

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
        raise HTTPException(429, "Zu viele Versuche, bitte später erneut probieren.")
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
            await c.feed("saved", 1)
        return "ok"
    except httpx.HTTPStatusError as e:
        return "invalid" if e.response.status_code in (401, 403) else "unknown"
    except Exception:
        return "unknown"


async def get_posts(uid: int, cookie: str, key: str):
    hit = _feed_cache.get((uid, key))
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    async with substack.SubstackClient(cookie, lambda c: store.set_cookie(uid, c)) as c:
        posts = await c.feed(key, 30)
    _feed_cache[(uid, key)] = (time.time(), posts)
    return posts


def drop_cache(uid: int):
    for k in [k for k in _feed_cache if k[0] == uid]:
        del _feed_cache[k]


# ---------- Weboberfläche ----------

CSS = """
:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e3e3dc;--acc:#ff6719}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecece6;--mut:#9a9a92;--card:#1f1f1c;--line:#33332f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.1rem;margin:0 0 .5em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}
input,textarea{width:100%;padding:10px;margin:4px 0 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:10px 16px;border:0;border-radius:6px;background:var(--acc);color:#fff;font:inherit;cursor:pointer}
button.sec{background:transparent;color:var(--fg);border:1px solid var(--line)}button.danger{background:#b3261e}
code{background:var(--bg);padding:2px 6px;border-radius:4px;word-break:break-all}.mut{color:var(--mut);font-size:.9rem}
.flash{padding:10px 14px;border-radius:6px;background:var(--card);border-left:4px solid var(--acc);margin:12px 0}
"""


def page(title: str, body: str, flash: str = "") -> HTMLResponse:
    f = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return HTMLResponse(
        f"<!doctype html><html lang='de'><head><meta charset='utf-8'>"
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
            inv = "<label>Einladungscode<input name='invite' required></label>" if INVITE_CODE else ""
            reg = f"""<div class=card><h2>Registrieren</h2>
<form method=post action=/register><label>E-Mail<input name=email type=email required></label>
<label>Passwort (min. 10 Zeichen)<input name=password type=password minlength=10 required></label>{inv}
<button>Konto anlegen</button></form></div>"""
        return page("Substack → E-Reader", f"""<h1>Substack → E-Reader</h1>
<p class=mut>Deine Substack-Artikel als OPDS-Katalog für CrossPoint und andere E-Ink-Reader.</p>
<div class=card><h2>Anmelden</h2><form method=post action=/login>
<label>E-Mail<input name=email type=email required></label>
<label>Passwort<input name=password type=password required></label><button>Anmelden</button></form></div>{reg}""", msg)

    b = base(request)
    connected = (
        f"verbunden (zuletzt aktualisiert {datetime.fromtimestamp(u['cookie_at']).strftime('%d.%m.%Y %H:%M')})"
        if u["cookie_enc"] else "noch nicht verbunden"
    )
    return page("Dashboard", f"""<h1>Dein Konto</h1><p class=mut>{esc(u['email'])}</p>
<div class=card><h2>OPDS-Adresse für den Reader</h2>
<p><code>{esc(b)}/o/{esc(u['opds_token'])}/</code></p>
<p class=mut>Diese Adresse ist geheim und enthält deinen Zugang. Im Reader als OPDS-Server hinzufügen.</p></div>
<div class=card><h2>Substack-Zugang: {esc(connected)}</h2>
<p class=mut>Am einfachsten über die Browser-Extension (synct das Cookie automatisch).
Alternativ den Wert von <code>substack.sid</code> hier einfügen (DevTools → Application → Cookies → substack.com).</p>
<form method=post action=/cookie><textarea name=cookie rows=3 placeholder="substack.sid-Wert" required></textarea>
<button>Cookie speichern</button></form>
<p class=mut>Für die Extension: Server <code>{esc(b)}</code> und Sync-Token <code>{esc(u['sync_token'])}</code></p></div>
<div class=card><h2>Konto</h2>
<form method=post action=/cookie/delete style=display:inline><button class=sec>Substack trennen</button></form>
<form method=post action=/token/rotate style=display:inline><button class=sec>Adressen erneuern</button></form>
<form method=post action=/logout style=display:inline><button class=sec>Abmelden</button></form>
<form method=post action=/account/delete onsubmit="return confirm('Konto und gespeicherte Daten endgültig löschen?')" style="margin-top:12px">
<button class=danger>Konto löschen</button></form></div>""", msg)


@app.post("/register")
async def register(request: Request, email: str = Form(), password: str = Form(), invite: str = Form("")):
    throttle(request)
    if not REGISTRATION_OPEN or (INVITE_CODE and invite != INVITE_CODE):
        flash(request, "Registrierung nicht möglich.")
        return RedirectResponse("/", 303)
    email = email.strip().lower()
    if len(password) < 10 or "@" not in email:
        flash(request, "Bitte gültige E-Mail und ein Passwort mit mindestens 10 Zeichen angeben.")
        return RedirectResponse("/", 303)
    uid = store.create_user(email, password)
    if not uid:
        flash(request, "Diese E-Mail ist bereits registriert.")
        return RedirectResponse("/", 303)
    request.session["uid"] = uid
    return RedirectResponse("/", 303)


@app.post("/login")
async def login(request: Request, email: str = Form(), password: str = Form()):
    throttle(request)
    u = store.verify_user(email.strip().lower(), password)
    if not u:
        flash(request, "E-Mail oder Passwort falsch.")
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
        flash(request, "Das sieht nicht wie ein substack.sid-Cookie aus.")
        return RedirectResponse("/", 303)
    state = await check_cookie(c)
    if state == "invalid":
        flash(request, "Substack lehnt dieses Cookie ab. Bist du dort eingeloggt?")
        return RedirectResponse("/", 303)
    store.set_cookie(u["id"], c)
    drop_cache(u["id"])
    flash(request, "Gespeichert." if state == "ok" else "Gespeichert, aber Substack ließ sich gerade nicht prüfen.")
    return RedirectResponse("/", 303)


@app.post("/cookie/delete")
async def delete_cookie(request: Request):
    u = current_user(request)
    if u:
        store.set_cookie(u["id"], None)
        drop_cache(u["id"])
        flash(request, "Substack getrennt.")
    return RedirectResponse("/", 303)


@app.post("/token/rotate")
async def rotate(request: Request):
    u = current_user(request)
    if u:
        store.rotate_tokens(u["id"])
        flash(request, "Neue Adressen erzeugt. Reader und Extension müssen aktualisiert werden.")
    return RedirectResponse("/", 303)


@app.post("/account/delete")
async def delete_account(request: Request):
    u = current_user(request)
    if u:
        store.delete_user(u["id"])
        drop_cache(u["id"])
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------- Extension-API ----------

@app.post("/api/cookie")
async def api_cookie(request: Request, authorization: str = Header(default="")):
    throttle(request, limit=60)
    token = authorization.removeprefix("Bearer ").strip()
    u = store.user_by_token("sync_token", token) if token else None
    if not u:
        raise HTTPException(401, "Ungültiger Token")
    try:
        c = clean_cookie((await request.json()).get("cookie", ""))
    except Exception:
        c = ""
    if not c:
        raise HTTPException(400, "Cookie fehlt oder ist ungültig")
    store.set_cookie(u["id"], c)
    drop_cache(u["id"])
    return JSONResponse({"ok": True})


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
    _opds_user(token)
    b = f"{base(request)}/o/{token}"
    entries = [
        f"<entry><title>{esc(substack.FEEDS[k][0])}</title><id>urn:feed:{k}</id><updated>{now_iso()}</updated>"
        f'<link rel="subsection" href="{esc(b)}/feed/{k}" type="{OPDS_ACQ}"/></entry>'
        for k in FEED_ORDER
    ]
    return atom("Substack", f"{b}/", f"{b}/", entries, nav=True)


@app.get("/o/{token}/feed/{key}")
async def opds_feed(token: str, key: str, request: Request):
    u = _opds_user(token)
    if key not in substack.FEEDS:
        raise HTTPException(404)
    b = f"{base(request)}/o/{token}"
    cookie = store.get_cookie(u["id"])
    entries: list[str] = []
    if not cookie:
        entries.append(info_entry("Kein Substack-Zugang verbunden", "Bitte in der Weboberfläche verbinden."))
    else:
        try:
            for p in await get_posts(u["id"], cookie, key):
                name = f"{p['id']}-{slugify(p['title'])}"
                paid = " 🔒" if p["audience"] == "only_paid" else ""
                summary = " · ".join(x for x in (p["pub"], p["subtitle"]) if x)
                entries.append(
                    f"<entry><title>{esc(p['title'] + paid)}</title><id>urn:substack:{p['id']}</id>"
                    f"<updated>{esc(p['date'] or now_iso())}</updated>"
                    f"<author><name>{esc(p['author'])}</name></author>"
                    f"<summary>{esc(summary)}</summary>"
                    f'<link rel="http://opds-spec.org/acquisition" href="{esc(b)}/post/{name}.epub" '
                    f'type="application/epub+zip"/></entry>'
                )
            if not entries:
                entries.append(info_entry("Keine Artikel", "Dieser Feed ist leer."))
        except httpx.HTTPStatusError as ex:
            expired = ex.response.status_code in (401, 403)
            entries.append(info_entry(
                "Substack-Session abgelaufen" if expired else f"Substack-Fehler {ex.response.status_code}",
                "Bitte bei Substack neu einloggen (die Extension synct danach automatisch)." if expired
                else "Später erneut versuchen."))
        except httpx.HTTPError:
            entries.append(info_entry("Substack nicht erreichbar", "Später erneut versuchen."))
    return atom(substack.FEEDS[key][0], f"{b}/feed/{key}", f"{b}/", entries, nav=False)


@app.get("/o/{token}/post/{name}.epub")
async def opds_post(token: str, name: str):
    u = _opds_user(token)
    m = re.match(r"^(\d+)(?:-.*)?$", name)
    if not m:
        raise HTTPException(404)
    pid = int(m.group(1))
    path = os.path.join(store.cache_dir(u["id"]), f"{pid}.epub")  # pro Nutzer:in, nie geteilt
    if not os.path.exists(path):
        cookie = store.get_cookie(u["id"])
        if not cookie:
            raise HTTPException(409, "Kein Substack-Zugang verbunden")
        try:
            async with substack.SubstackClient(cookie, lambda c: store.set_cookie(u["id"], c)) as c:
                post = await c.post(pid)
            data = await build_epub(post)
        except httpx.HTTPStatusError as ex:
            raise HTTPException(502, f"Substack antwortet {ex.response.status_code}")
        except httpx.HTTPError:
            raise HTTPException(502, "Substack nicht erreichbar")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    return FileResponse(path, media_type="application/epub+zip", filename=f"{name}.epub")
