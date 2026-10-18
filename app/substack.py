"""Dünner Client für die inoffiziellen Substack-Web-Endpunkte.

WICHTIG: Diese Endpunkte sind nicht dokumentiert und können sich ändern.
Alle Pfade stehen in FEEDS, LOGIN_URL und POST_URL, damit sie an einer Stelle
korrigiert werden können (im Browser: DevTools > Network auf substack.com).
Die Pfade sind noch nicht gegen einen echten Account getestet.
"""
from urllib.parse import quote

import httpx

BASE = "https://substack.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

LOGIN_URL = "/api/v1/login"
POST_URL = "/api/v1/posts/by-id/{id}"
MAX_PAGES = 8

# key -> (Anzeigename, Pfad, Format)
# Format "posts": {posts, publications, more, cursor}  (Abonnements-Inbox, Bezahlt, Gespeichert)
# Format "feed":  {items, nextCursor}                   (Startseite "Für dich", gemischt mit Kommentaren)
FEEDS = {
    "latest": ("Neueste", "/api/v1/inbox/top?inboxType=inbox&surface=inbox_all&limit=20", "posts"),
    "for-you": ("Für dich", "/api/v1/reader/feed?tab=for-you&type=base", "feed"),
    "saved": ("Lesezeichen", "/api/v1/reader/posts?inboxType=saved&limit=20", "posts"),
    "paid": ("Bezahlt", "/api/v1/reader/posts?inboxType=paid&limit=20", "posts"),
}


COOKIE_DOMAIN = "substack.com"


class LoginError(Exception):
    pass


def _find_sid(jar, old=None):
    """Bevorzugt ein vom Server neu gesetztes substack.sid, sonst das alte."""
    for c in jar.jar:
        if c.name == "substack.sid" and c.value != old:
            return c.value
    return old


async def login_with_password(email: str, password: str) -> str:
    """Meldet sich an und gibt den Wert von substack.sid zurück.

    Das Passwort wird nur für diesen Aufruf benutzt und nicht gespeichert.
    Funktioniert nur, wenn der Account ein Passwort gesetzt hat und Substack
    kein Captcha verlangt. Sonst bleibt der Cookie-Weg (Extension/Einfügen).
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, follow_redirects=True, timeout=30
    ) as http:
        r = await http.post(
            BASE + LOGIN_URL,
            json={
                "email": email,
                "password": password,
                "for_pub": "",
                "redirect": "/",
                "captcha_response": None,
            },
        )
        sid = _find_sid(http.cookies)
        if r.status_code >= 400 or not sid:
            raise LoginError(
                f"Login fehlgeschlagen (HTTP {r.status_code}). "
                "Kein Passwort gesetzt oder Captcha nötig?"
            )
        return sid


def normalize(p: dict | None, pub: dict | None) -> dict | None:
    if not p or not p.get("id"):
        return None
    pub = pub or {}
    bylines = p.get("publishedBylines") or []
    author = (bylines[0].get("name") if bylines else None) or pub.get("name") or "Substack"
    return {
        "id": p["id"],
        "title": p.get("title") or "(ohne Titel)",
        "subtitle": p.get("subtitle") or p.get("description") or "",
        "date": p.get("post_date") or "",
        "url": p.get("canonical_url") or "",
        "audience": p.get("audience") or "everyone",
        "pub": pub.get("name") or "",
        "author": author,
    }


class SubstackClient:
    """Async-Context-Manager. Meldet ein erneuertes Cookie via on_cookie_update."""

    def __init__(self, cookie: str, on_cookie_update=None):
        self.cookie = cookie
        self.on_cookie_update = on_cookie_update
        self.http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self.http = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            timeout=30,
        )
        # Cookie nur für substack.com, nie an CDNs oder fremde Hosts senden.
        self.http.cookies.set("substack.sid", self.cookie, domain=COOKIE_DOMAIN)
        return self

    async def __aexit__(self, *exc):
        new = _find_sid(self.http.cookies, self.cookie)
        if new and new != self.cookie and self.on_cookie_update:
            self.on_cookie_update(new)
        await self.http.aclose()

    async def feed(self, key: str, limit: int = 30) -> list[dict]:
        _, path, fmt = FEEDS[key]
        posts: list[dict] = []
        seen: set = set()
        cursor = None
        for _ in range(MAX_PAGES):
            url = BASE + path + (f"&cursor={quote(cursor, safe='')}" if cursor else "")
            r = await self.http.get(url)
            r.raise_for_status()
            d = r.json()
            if fmt == "posts":
                pubs = {x.get("id"): x for x in d.get("publications") or []}
                batch = [normalize(p, pubs.get(p.get("publication_id"))) for p in d.get("posts") or []]
                c = d.get("cursor")
                cursor = c if d.get("more") and isinstance(c, str) else None
            else:
                batch = [
                    normalize(i.get("post"), i.get("publication"))
                    for i in d.get("items") or []
                    if i.get("type") == "post"
                ]
                nc = d.get("nextCursor")
                cursor = nc if isinstance(nc, str) else None
            for n in batch:
                if n and n["id"] not in seen:
                    seen.add(n["id"])
                    posts.append(n)
            if len(posts) >= limit or not cursor:
                break
        return posts[:limit]

    async def post(self, post_id: int | str) -> dict:
        r = await self.http.get(BASE + POST_URL.format(id=post_id))
        r.raise_for_status()
        return r.json()
