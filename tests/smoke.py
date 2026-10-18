"""Smoke test against a fake Substack server (local, no network access to Substack).

Run:    DATA_DIR=$(mktemp -d) HTTPS_ONLY=0 python tests/smoke.py
"""
import io
import re
import sys
import threading
import time
import zipfile

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from PIL import Image

sys.path.insert(0, ".")
from app import epub, substack  # noqa: E402

PORT = 9001
seen = {"cookies": []}
fake = FastAPI()


def png():
    b = io.BytesIO()
    Image.new("RGB", (1200, 600), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


PUB = {"id": 7, "name": "Testletter", "subdomain": "x"}
POST = {"id": 42, "title": "Über Ärger & Öl", "subtitle": "A test", "publication_id": 7,
        "post_date": "2026-10-16T10:00:00.000Z", "audience": "only_paid",
        "canonical_url": "https://x.substack.com/p/test"}
POST2 = {**POST, "id": 43, "title": "Second page", "audience": "everyone"}


def note(request: Request):
    seen["cookies"].append(request.headers.get("cookie", ""))


@fake.get("/api/v1/inbox/top")
async def inbox(request: Request):
    note(request)
    return {"posts": [POST], "publications": [PUB], "more": False, "cursor": None}


@fake.get("/api/v1/reader/posts")
async def reader_posts(request: Request, inboxType: str = ""):
    note(request)
    return {"posts": [POST] if inboxType == "paid" else [], "publications": [PUB],
            "more": False, "cursor": None}


@fake.get("/api/v1/reader/feed")
async def reader_feed(request: Request, cursor: str = ""):
    note(request)
    mixed = [{"type": "comment", "post": None}, {"type": "userSuggestions"}]
    if not cursor:  # page 1: only comments + 1 article, with nextCursor
        return {"items": mixed + [{"type": "post", "post": POST, "publication": PUB}],
                "nextCursor": "c1"}
    return {"items": mixed + [{"type": "post", "post": POST2, "publication": PUB},
                              {"type": "post", "post": POST, "publication": PUB}],
            "nextCursor": None}


@fake.get("/api/v1/posts/by-id/{pid}")
async def post(pid: int):
    body = ('<p>Hello <strong>world</strong></p><script>alert(1)</script>'
            f'<div class="subscription-widget">KAUFEN</div>'
            f'<figure><a href="x"><img src="http://127.0.0.1:{PORT}/img.png"></a></figure>'
            '<p><a href="javascript:evil()">evil</a> <a href="https://ok.de">good</a></p>')
    return {"post": {**POST, "body_html": body, "publishedBylines": [{"name": "Erika"}]}}


@fake.get("/img.png")
async def img():
    return Response(png(), media_type="image/png")


def main():
    threading.Thread(
        target=uvicorn.Server(uvicorn.Config(fake, port=PORT, log_level="error")).run, daemon=True
    ).start()
    time.sleep(1)
    substack.BASE = f"http://127.0.0.1:{PORT}"
    substack.COOKIE_DOMAIN = "127.0.0.1"
    epub._ok = lambda u: True  # the test host is not in the allowlist

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app, follow_redirects=True)
    assert c.post("/register", data={"email": "a@b.de", "password": "kurz"}).status_code == 200
    r = c.post("/register", data={"email": "a@b.de", "password": "langespasswort1"})
    opds = re.search(r"/o/([\w-]+)/", r.text).group(1)
    sync = re.search(r"sync token <code>([\w-]+)</code>", r.text).group(1)
    print("registered, tokens ok")

    assert c.post("/api/cookie", json={"cookie": "x" * 40}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    cookie = "s%3A" + "abc123" * 10
    r = c.post("/api/cookie", json={"cookie": cookie}, headers={"Authorization": f"Bearer {sync}"})
    assert r.json() == {"ok": True}
    print("extension API ok")

    import io, json, zipfile
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/extension/chrome.zip").status_code == 303  # login required
    assert c.get("/extension/safari.zip").status_code == 404
    for br, bg in (("chrome", "service_worker"), ("firefox", "scripts")):
        z = zipfile.ZipFile(io.BytesIO(c.get(f"/extension/{br}.zip").content))
        assert sorted(z.namelist()) == ["background.js", "config.json", "manifest.json", "popup.html", "popup.js"], z.namelist()
        assert bg in json.loads(z.read("manifest.json"))["background"], br
        assert json.loads(z.read("config.json"))["token"] == sync
    print("extension download ok")

    root = c.get(f"/o/{opds}/").text
    assert root.count("<entry>") == 4, root
    latest = c.get(f"/o/{opds}/feed/latest").text
    assert "42-ueber-aerger-oel.epub" in latest and "Testletter" in latest, latest
    paid = c.get(f"/o/{opds}/feed/paid").text
    assert "42-ueber-aerger-oel.epub" in paid and "🔒" in paid, paid
    saved = c.get(f"/o/{opds}/feed/saved").text
    assert "<entry>" in saved and "No articles" in saved, saved  # empty catalog -> info entry
    foryou = c.get(f"/o/{opds}/feed/for-you").text
    assert foryou.count("urn:substack:") == 2, foryou  # comments dropped, paged, deduplicated
    assert "42-ueber" in foryou and "43-second-page" in foryou, foryou
    assert cookie in seen["cookies"][-1], seen["cookies"]
    print("OPDS catalogs ok (inbox, paid, empty, For you with paging), cookie was sent")

    # --- configure catalogs in the dashboard ---
    from app import store
    defaults = {"latest": "Latest", "for-you": "For you", "saved": "Saved", "paid": "Paid"}
    paths = {f["key"]: f["path"] for f in substack.DEFAULT_FEEDS}

    def form(limits=None, enabled=None, delete=(), new=None):
        d = {"enabled": list(enabled if enabled is not None else defaults), "delete": list(delete)}
        for k, name in defaults.items():
            d[f"name_{k}"], d[f"path_{k}"] = name, paths[k]
            d[f"limit_{k}"] = str((limits or {}).get(k, 30))
        if new:
            d["new_name"], d["new_path"], d["new_limit"] = new
        return d

    n_entries = lambda t: t.count("urn:substack:")
    r = c.post("/feeds", data=form(limits={"for-you": 1}, new=("Meine Liste", "https://substack.com/api/v1/reader/posts?inboxType=paid&limit=20", "5")))
    assert "Catalogs saved" in r.text, r.text[:800]
    assert n_entries(c.get(f"/o/{opds}/feed/for-you").text) == 1  # limit works (page 2 is not fetched)
    root = c.get(f"/o/{opds}/").text
    assert root.count("<entry>") == 5 and "Meine Liste" in root, root
    custom = re.search(r"feed/(f[0-9a-f]{6})", root).group(1)
    assert "42-ueber" in c.get(f"/o/{opds}/feed/{custom}").text
    print("catalogs configurable: limit, new catalog ok")

    r = c.post("/feeds", data=form(new=("Evil", "https://evil.example/api/v1/x", "5")))
    assert "New catalog not created" in r.text
    r = c.post("/feeds", data=form(new=("Evil", "/etc/passwd", "5")))
    assert "New catalog not created" in r.text
    print("foreign hosts and invalid paths rejected")

    r = c.post("/feeds", data=form(enabled=["latest", "for-you", "paid"]))
    assert c.get(f"/o/{opds}/").text.count("<entry>") == 3
    assert c.get(f"/o/{opds}/feed/saved").status_code == 404  # disabled
    c.post("/feeds/reset")
    assert c.get(f"/o/{opds}/").text.count("<entry>") == 4
    assert n_entries(c.get(f"/o/{opds}/feed/for-you").text) == 2  # defaults again (30 -> pages through)
    print("disable and reset ok")

    r = c.get(f"/o/{opds}/post/42-ueber-aerger-oel.epub")
    assert r.status_code == 200 and r.headers["content-type"] == "application/epub+zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    art = z.read("EPUB/article.xhtml").decode()
    assert "Hello" in art and "alert" not in art and "KAUFEN" not in art and "javascript:" not in art
    assert "https://ok.de" in art and "images/img1.jpg" in art, art
    assert any(n.endswith("img1.jpg") for n in names)
    im = Image.open(io.BytesIO(z.read("EPUB/images/img1.jpg")))
    assert im.mode == "L" and im.width <= 700
    print("EPUB ok:", len(r.content), "bytes,", im.size, im.mode)

    assert c.get("/o/falscher-token/").status_code == 404
    c.post("/account/delete")
    assert c.get(f"/o/{opds}/").status_code == 404
    print("account deletion ok\nALL GREEN")


main()
