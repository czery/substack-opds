"""Rauchtest gegen einen gefälschten Substack-Server (lokal, ohne Netzwerk zu Substack).

Start:  DATA_DIR=$(mktemp -d) HTTPS_ONLY=0 python tests/smoke.py
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


ITEM = {"post": {"id": 42, "title": "Über Ärger & Öl", "subtitle": "Ein Test",
                 "post_date": "2026-10-16T10:00:00.000Z", "audience": "only_paid",
                 "canonical_url": "https://x.substack.com/p/test"},
        "publication": {"name": "Testletter"}}


@fake.get("/api/v1/reader/feed")
async def feed(request: Request):
    seen["cookies"].append(request.headers.get("cookie", ""))
    return {"items": [ITEM]}


@fake.get("/api/v1/reader/posts/saved")
async def saved(request: Request):
    seen["cookies"].append(request.headers.get("cookie", ""))
    return {"posts": [ITEM["post"]]}


@fake.get("/api/v1/posts/by-id/{pid}")
async def post(pid: int):
    body = ('<p>Hallo <strong>Welt</strong></p><script>alert(1)</script>'
            f'<div class="subscription-widget">KAUFEN</div>'
            f'<figure><a href="x"><img src="http://127.0.0.1:{PORT}/img.png"></a></figure>'
            '<p><a href="javascript:evil()">böse</a> <a href="https://ok.de">gut</a></p>')
    return {**ITEM["post"], "body_html": body, "publishedBylines": [{"name": "Erika"}]}


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
    epub._ok = lambda u: True  # Test-Host ist nicht in der Allowlist

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app, follow_redirects=True)
    assert c.post("/register", data={"email": "a@b.de", "password": "kurz"}).status_code == 200
    r = c.post("/register", data={"email": "a@b.de", "password": "langespasswort1"})
    opds = re.search(r"/o/([\w-]+)/", r.text).group(1)
    sync = re.search(r"Sync-Token <code>([\w-]+)</code>", r.text).group(1)
    print("registriert, Tokens ok")

    assert c.post("/api/cookie", json={"cookie": "x" * 40}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    cookie = "s%3A" + "abc123" * 10
    r = c.post("/api/cookie", json={"cookie": cookie}, headers={"Authorization": f"Bearer {sync}"})
    assert r.json() == {"ok": True}
    print("Extension-API ok")

    root = c.get(f"/o/{opds}/").text
    assert root.count("<entry>") == 4, root
    for key in ("latest", "saved", "paid"):
        f = c.get(f"/o/{opds}/feed/{key}").text
        assert "42-ueber-aerger-oel.epub" in f, (key, f)
    assert "🔒" in c.get(f"/o/{opds}/feed/paid").text
    assert cookie in seen["cookies"][-1], seen["cookies"]
    print("OPDS-Feeds ok, Cookie wurde an Substack gesendet")

    r = c.get(f"/o/{opds}/post/42-ueber-aerger-oel.epub")
    assert r.status_code == 200 and r.headers["content-type"] == "application/epub+zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    art = z.read("EPUB/article.xhtml").decode()
    assert "Hallo" in art and "alert" not in art and "KAUFEN" not in art and "javascript:" not in art
    assert "https://ok.de" in art and "images/img1.jpg" in art, art
    assert any(n.endswith("img1.jpg") for n in names)
    im = Image.open(io.BytesIO(z.read("EPUB/images/img1.jpg")))
    assert im.mode == "L" and im.width <= 700
    print("EPUB ok:", len(r.content), "Bytes,", im.size, im.mode)

    assert c.get("/o/falscher-token/").status_code == 404
    c.post("/account/delete")
    assert c.get(f"/o/{opds}/").status_code == 404
    print("Konto löschen ok\nALLES GRÜN")


main()
