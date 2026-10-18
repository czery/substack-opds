"""SQLite storage, password hashing and cookie encryption."""
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from . import substack

DATA = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA, mode=0o700, exist_ok=True)
DB = os.path.join(DATA, "app.db")


def _load_keys():
    f, s = os.environ.get("FERNET_KEY"), os.environ.get("SECRET_KEY")
    if f and s:
        return f, s
    p = os.path.join(DATA, "keys.json")
    if os.path.exists(p):
        with open(p) as fh:
            d = json.load(fh)
    else:
        d = {
            "fernet": Fernet.generate_key().decode(),
            "secret": secrets.token_urlsafe(48),
        }
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh)
    return f or d["fernet"], s or d["secret"]


_F, SECRET = _load_keys()
fernet = Fernet(_F)


def _run(sql, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows, cur.lastrowid
    finally:
        c.close()


def init():
    _run(
        """CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            pw TEXT NOT NULL,
            opds_token TEXT UNIQUE NOT NULL,
            sync_token TEXT UNIQUE NOT NULL,
            cookie_enc BLOB,
            cookie_at INTEGER,
            created INTEGER NOT NULL)"""
    )
    cols = [r["name"] for r in _run("PRAGMA table_info(users)")[0]]
    if "feeds" not in cols:  # migration for existing databases
        _run("ALTER TABLE users ADD COLUMN feeds TEXT")
    if "author_prefix" not in cols:
        _run("ALTER TABLE users ADD COLUMN author_prefix TEXT")
    os.makedirs(os.path.join(DATA, "cache"), exist_ok=True)


def _default_feeds() -> list[dict]:
    return [{**f, "limit": substack.DEFAULT_LIMIT, "enabled": True} for f in substack.DEFAULT_FEEDS]


def get_feeds(uid: int) -> list[dict]:
    """Catalogs of a user. Without custom settings the default catalogs apply."""
    u = get_user(uid)
    raw = u["feeds"] if u else None
    if raw is None:
        return _default_feeds()
    try:
        out = []
        for f in json.loads(raw):
            out.append({
                "key": str(f["key"]), "name": str(f["name"]), "path": str(f["path"]),
                "limit": max(1, min(substack.MAX_LIMIT, int(f.get("limit", substack.DEFAULT_LIMIT)))),
                "enabled": bool(f.get("enabled", True)),
            })
        return out
    except (ValueError, KeyError, TypeError):
        return _default_feeds()


def set_feeds(uid: int, feeds: list[dict] | None):
    """None resets to the default catalogs."""
    _run("UPDATE users SET feeds=? WHERE id=?", (None if feeds is None else json.dumps(feeds), uid))


def set_author_prefix(uid: int, prefix: str):
    _run("UPDATE users SET author_prefix=? WHERE id=?", (prefix or None, uid))


def _hash(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def _check(pw: str, stored: str) -> bool:
    salt, h = stored.split("$")
    return hmac.compare_digest(_hash(pw, bytes.fromhex(salt)), stored)


def create_user(email: str, pw: str):
    try:
        _, uid = _run(
            "INSERT INTO users(email,pw,opds_token,sync_token,created) VALUES(?,?,?,?,?)",
            (email, _hash(pw), secrets.token_urlsafe(24), secrets.token_urlsafe(24), int(time.time())),
        )
        return uid
    except sqlite3.IntegrityError:
        return None


_DUMMY = _hash("dummy")


def verify_user(email: str, pw: str):
    rows, _ = _run("SELECT * FROM users WHERE email=?", (email,))
    if not rows:
        _check(pw, _DUMMY)  # same runtime, no user-enumeration timing
        return None
    return rows[0] if _check(pw, rows[0]["pw"]) else None


def get_user(uid: int):
    rows, _ = _run("SELECT * FROM users WHERE id=?", (uid,))
    return rows[0] if rows else None


def user_by_token(col: str, token: str):
    assert col in ("opds_token", "sync_token")
    rows, _ = _run(f"SELECT * FROM users WHERE {col}=?", (token,))
    return rows[0] if rows else None


def rotate_tokens(uid: int):
    _run(
        "UPDATE users SET opds_token=?, sync_token=? WHERE id=?",
        (secrets.token_urlsafe(24), secrets.token_urlsafe(24), uid),
    )


def set_cookie(uid: int, cookie: str | None):
    enc = fernet.encrypt(cookie.encode()) if cookie else None
    _run(
        "UPDATE users SET cookie_enc=?, cookie_at=? WHERE id=?",
        (enc, int(time.time()) if cookie else None, uid),
    )


def get_cookie(uid: int) -> str | None:
    u = get_user(uid)
    if not u or not u["cookie_enc"]:
        return None
    try:
        return fernet.decrypt(bytes(u["cookie_enc"])).decode()
    except InvalidToken:
        return None


def cache_dir(uid: int) -> str:
    d = os.path.join(DATA, "cache", str(uid))
    os.makedirs(d, exist_ok=True)
    return d


def delete_user(uid: int):
    _run("DELETE FROM users WHERE id=?", (uid,))
    shutil.rmtree(os.path.join(DATA, "cache", str(uid)), ignore_errors=True)


def purge_cache(days: int = 30):
    root = os.path.join(DATA, "cache")
    limit = time.time() - days * 86400
    for dp, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dp, f)
            try:
                if os.path.getmtime(p) < limit:
                    os.remove(p)
            except OSError:
                pass
