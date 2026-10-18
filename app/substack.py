"""Thin client for Substack's unofficial web endpoints.

IMPORTANT: These endpoints are undocumented and can change at any time.
The default feed paths (DEFAULT_FEEDS), LOGIN_URL and POST_URL live at the top of
this file (find new ones in the browser: DevTools > Network on substack.com).
The feed and post endpoints were verified against a real account on 2026-10-18.
"""
import re
import time
from urllib.parse import quote, urlsplit

import httpx

BASE = "https://substack.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

LOGIN_URL = "/api/v1/login"
POST_URL = "/api/v1/posts/by-id/{id}"
MAX_PAGES = 15  # maximum number of requests per catalog
TIME_BUDGET = 12.0  # seconds; after that the server responds with what it has collected so far
MAX_LIMIT = 100
DEFAULT_LIMIT = 30

# Default catalogs. Each user can change them in the dashboard.
# Two response formats are detected automatically:
#   {posts, publications, more, cursor}  (subscriptions inbox, Paid, Saved)
#   {items, nextCursor}                   (home "For you", mixed with comments)
DEFAULT_FEEDS = [
    {"key": "latest", "name": "Latest",
     "path": "/api/v1/inbox/top?inboxType=inbox&surface=inbox_all&limit=20"},
    {"key": "for-you", "name": "For you", "path": "/api/v1/reader/feed?tab=for-you&type=base"},
    {"key": "saved", "name": "Saved", "path": "/api/v1/reader/posts?inboxType=saved&limit=20"},
    {"key": "paid", "name": "Paid", "path": "/api/v1/reader/posts?inboxType=paid&limit=20"},
]
CHECK_PATH = "/api/v1/reader/posts?inboxType=saved&limit=20"


def parse_feed_path(raw: str) -> str | None:
    """Accepts an /api/… path or a https://substack.com/api/… URL.

    Returns the path (with query) or None. Foreign hosts are not allowed,
    the server only ever calls substack.com.
    """
    raw = (raw or "").strip()
    if raw.startswith(("http://", "https://")):
        u = urlsplit(raw)
        if u.scheme != "https" or (u.hostname or "").lower() != "substack.com":
            return None
        raw = u.path + ("?" + u.query if u.query else "")
    raw = raw.split("#")[0]
    if (not raw.startswith("/api/") or len(raw) > 500 or ".." in raw
            or "//" in raw.split("?")[0] or re.search(r"[\s\x00-\x1f]", raw)):
        return None
    return raw


COOKIE_DOMAIN = "substack.com"


class LoginError(Exception):
    pass


def _find_sid(jar, old=None):
    """Prefers a substack.sid newly set by the server, otherwise the old one."""
    for c in jar.jar:
        if c.name == "substack.sid" and c.value != old:
            return c.value
    return old


async def login_with_password(email: str, password: str) -> str:
    """Logs in and returns the value of substack.sid.

    The password is only used for this call and is not stored.
    Only works if the account has a password set and Substack does not
    require a captcha. Otherwise use the cookie route (extension/paste).
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
                f"Login failed (HTTP {r.status_code}). "
                "No password set or captcha required?"
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
        "title": p.get("title") or "(untitled)",
        "subtitle": p.get("subtitle") or p.get("description") or "",
        "date": p.get("post_date") or "",
        "url": p.get("canonical_url") or "",
        "audience": p.get("audience") or "everyone",
        "pub": pub.get("name") or "",
        "author": author,
    }


class SubstackClient:
    """Async context manager. Reports a renewed cookie via on_cookie_update."""

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
        # Send the cookie to substack.com only, never to CDNs or foreign hosts.
        self.http.cookies.set("substack.sid", self.cookie, domain=COOKIE_DOMAIN)
        return self

    async def __aexit__(self, *exc):
        new = _find_sid(self.http.cookies, self.cookie)
        if new and new != self.cookie and self.on_cookie_update:
            self.on_cookie_update(new)
        await self.http.aclose()

    async def feed(self, path: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Fetches articles from a Substack path and pages via cursor until `limit` is reached."""
        posts: list[dict] = []
        seen: set = set()
        cursor = None
        sep = "&" if "?" in path else "?"
        deadline = time.monotonic() + TIME_BUDGET
        for _ in range(MAX_PAGES):
            url = BASE + path + (f"{sep}cursor={quote(cursor, safe='')}" if cursor else "")
            r = await self.http.get(url)
            r.raise_for_status()
            d = r.json()
            if not isinstance(d, dict):
                break
            if isinstance(d.get("posts"), list):
                pubs = {x.get("id"): x for x in d.get("publications") or [] if isinstance(x, dict)}
                batch = [
                    normalize(p, pubs.get(p.get("publication_id")))
                    for p in d["posts"] if isinstance(p, dict)
                ]
                c = d.get("cursor")
                cursor = c if d.get("more") and isinstance(c, str) else None
            else:
                batch = [
                    normalize(i.get("post"), i.get("publication"))
                    for i in d.get("items") or []
                    if isinstance(i, dict) and i.get("type") == "post"
                ]
                nc = d.get("nextCursor")
                cursor = nc if isinstance(nc, str) else None
            for n in batch:
                if n and n["id"] not in seen:
                    seen.add(n["id"])
                    posts.append(n)
            if len(posts) >= limit or not cursor or time.monotonic() > deadline:
                break
        return posts[:limit]

    async def post(self, post_id: int | str) -> dict:
        r = await self.http.get(BASE + POST_URL.format(id=post_id))
        r.raise_for_status()
        return r.json()
