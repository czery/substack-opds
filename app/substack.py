"""Dünner Client für die inoffiziellen Substack-Web-Endpunkte.

WICHTIG: Diese Endpunkte sind nicht dokumentiert und können sich ändern.
Alle Pfade stehen in FEEDS, LOGIN_URL und POST_URL, damit sie an einer Stelle
korrigiert werden können (im Browser: DevTools > Network auf substack.com).
Die Pfade sind noch nicht gegen einen echten Account getestet.
"""
import httpx

BASE = "https://substack.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

LOGIN_URL = "/api/v1/login"
FEEDS = {
    "latest": ("Neueste", "/api/v1/reader/feed?tab=all", None),
    "for-you": ("Für dich", "/api/v1/reader/feed?tab=for-you", None),
    "saved": ("Lesezeichen", "/api/v1/reader/posts/saved", None),
    "paid": (
        "Bezahlt",
        "/api/v1/reader/feed?tab=all",
        lambda p: p.get("audience") == "only_paid",
    ),
}
POST_URL = "/api/v1/posts/by-id/{id}"


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


def normalize(item: dict) -> dict | None:
    p = item.get("post") or item
    if not p.get("id"):
        return None
    pub = item.get("publication") or p.get("publication") or {}
    bylines = p.get("publishedBylines") or []
    author = bylines[0].get("name") if bylines else pub.get("name")
    return {
        "id": p["id"],
        "title": p.get("title") or "(ohne Titel)",
        "subtitle": p.get("subtitle") or p.get("description") or "",
        "date": p.get("post_date") or "",
        "url": p.get("canonical_url") or "",
        "audience": p.get("audience") or "everyone",
        "pub": pub.get("name") or "",
        "author": author or pub.get("name") or "Substack",
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

    async def feed(self, key: str, limit: int = 25) -> list[dict]:
        _, path, flt = FEEDS[key]
        r = await self.http.get(BASE + path)
        r.raise_for_status()
        data = r.json()
        raw = data.get("items") or data.get("posts") or []
        posts = [n for n in (normalize(i) for i in raw) if n]
        if flt:
            posts = [p for p in posts if flt(p)]
        return posts[:limit]

    async def post(self, post_id: int | str) -> dict:
        r = await self.http.get(BASE + POST_URL.format(id=post_id))
        r.raise_for_status()
        return r.json()
