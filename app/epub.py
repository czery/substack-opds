"""Substack-Artikel (body_html) -> schlankes EPUB für E-Ink-Reader."""
import asyncio
import html
import io
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image

# Nur diese Hosts werden serverseitig für Bilder abgerufen (Schutz vor SSRF).
ALLOWED_HOSTS = ("substackcdn.com", "substack.com", "substack-post-media.s3.amazonaws.com")
MAX_IMAGES = 25
MAX_IMG_BYTES = 8 * 1024 * 1024
IMG_WIDTH = 700

JUNK = ["script", "style", "button", "svg", "iframe", "form", "input", "source",
        "noscript", "video", "audio", "canvas", "object", "embed"]
JUNK_CLASS = re.compile(r"subscri|button-wrapper|captioned-button|share-|comment-|paywall-cta")
ALLOWED_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "a", "img", "strong", "b",
                "em", "i", "u", "blockquote", "ul", "ol", "li", "br", "hr", "figure",
                "figcaption", "pre", "code", "sub", "sup", "table", "thead", "tbody",
                "tr", "td", "th"}


def _ok(url: str) -> bool:
    u = urlparse(url or "")
    h = (u.hostname or "").lower()
    return u.scheme == "https" and any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS)


async def _fetch_img(http: httpx.AsyncClient, url: str) -> bytes | None:
    for _ in range(4):  # Redirects manuell folgen und jeden Hop prüfen
        if not _ok(url):
            return None
        r = await http.get(url)
        if r.is_redirect and r.headers.get("location"):
            url = str(httpx.URL(url).join(r.headers["location"]))
            continue
        if r.status_code != 200 or len(r.content) > MAX_IMG_BYTES:
            return None
        return r.content
    return None


def _gray(data: bytes, width: int) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.seek(0)
    im = im.convert("L")
    im.thumbnail((width, 1400))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=70, optimize=True)
    return out.getvalue()


def _clean(soup: BeautifulSoup):
    for t in soup.find_all(JUNK):
        t.decompose()
    for t in soup.find_all(class_=JUNK_CLASS):
        t.decompose()
    for pic in soup.find_all("picture"):
        pic.unwrap()
    for a in soup.find_all("a"):
        if a.find("img"):
            a.unwrap()  # Bild-Links (Vergrößern) wegwerfen, Bild behalten


def _whitelist(soup: BeautifulSoup):
    for t in soup.find_all(True):
        if t.name not in ALLOWED_TAGS:
            t.unwrap()
            continue
        keep = {}
        if t.name == "a" and re.match(r"https?://", t.get("href", "")):
            keep["href"] = t["href"]
        if t.name == "img":
            keep["src"] = t.get("src", "")
            keep["alt"] = t.get("alt", "")
        t.attrs = keep


async def build_epub(post: dict) -> bytes:
    p = post.get("post") or post
    title = p.get("title") or "Substack"
    lang = p.get("language") or "en"
    soup = BeautifulSoup(p.get("body_html") or "", "html.parser")
    _clean(soup)

    imgs = soup.find_all("img")
    for extra in imgs[MAX_IMAGES:]:
        extra.decompose()
    imgs = imgs[:MAX_IMAGES]

    book = epub.EpubBook()
    book.set_identifier(f"substack-{p.get('id')}")
    book.set_title(title)
    book.set_language(lang)
    author = ((p.get("publishedBylines") or [{}])[0]).get("name") or "Substack"
    book.add_author(author)

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
    ) as http:  # bewusst ohne Substack-Cookie
        sem = asyncio.Semaphore(5)

        async def one(url, width):
            async with sem:
                raw = await _fetch_img(http, url)
            return await asyncio.to_thread(_gray, raw, width) if raw else None

        results = await asyncio.gather(
            *(one(i.get("src") or i.get("data-src") or "", IMG_WIDTH) for i in imgs),
            return_exceptions=True,
        )
        cover = p.get("cover_image")
        if cover:
            try:
                cdata = await one(cover, 400)
                if cdata:
                    book.set_cover("cover.jpg", cdata, create_page=False)
            except Exception:
                pass

    n = 0
    for img, res in zip(imgs, results):
        if isinstance(res, bytes):
            n += 1
            name = f"images/img{n}.jpg"
            book.add_item(epub.EpubItem(uid=f"img{n}", file_name=name,
                                        media_type="image/jpeg", content=res))
            img["src"] = name
        else:
            img.decompose()

    _whitelist(soup)

    date = (p.get("post_date") or "")[:10]
    head = f"<h1>{html.escape(title)}</h1>"
    if p.get("subtitle"):
        head += f"<p><em>{html.escape(p['subtitle'])}</em></p>"
    head += f"<p>{html.escape(author)}{' · ' + date if date else ''}</p><hr/>"
    foot = ""
    if re.match(r"https?://", p.get("canonical_url") or ""):
        foot = f'<hr/><p><a href="{html.escape(p["canonical_url"])}">Original article</a></p>'

    ch = epub.EpubHtml(title=title, file_name="article.xhtml", lang=lang)
    ch.content = head + str(soup) + foot
    book.add_item(ch)
    book.toc = (ch,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [ch]

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
