#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import browser_cookie3
import requests
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

__version__ = "0.2.0"

PROG = "yt"
ORIGIN = "https://music.youtube.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"
DEFAULT_PLAYLIST = os.environ.get("YTM_PLAYLIST")
BATCH = 50
VERIFY_TIMEOUT = 25
RETRIES = 2
AUTH_HINT = "log into https://music.youtube.com in your browser, then retry (cookies rotate hourly)"
COOKIE_HINT = "launch the browser with --password-store=basic, or pass --cookie-file <path to Cookies database>"

WHITELIST = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "LOGIN_INFO", "VISITOR_INFO1_LIVE", "YSC", "PREF", "SOCS",
}
REQUIRED = {"__Secure-1PSID", "__Secure-3PSID", "__Secure-3PAPISID"}
CHROMIUM_DIRS = {
    "chrome": ("google-chrome", "google-chrome-beta", "google-chrome-unstable"),
    "chromium": ("chromium",),
    "brave": ("BraveSoftware/Brave-Browser",),
    "edge": ("microsoft-edge",),
    "vivaldi": ("vivaldi",),
    "opera": ("opera",),
    "opera_gx": ("opera-gx",),
}
FIREFOX_ROOTS = (
    ".mozilla/firefox",
    "snap/firefox/common/.mozilla/firefox",
    ".var/app/org.mozilla.firefox/.mozilla/firefox",
)
BROWSERS = ("auto", "firefox", *CHROMIUM_DIRS)
EXIT_CODES = {
    "internal": 1,
    "usage": 2,
    "auth": 3,
    "not_found": 4,
    "conflict": 5,
    "unverified": 6,
    "network": 7,
    "api": 8,
}


class Failure(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)


class Out:
    def __init__(self, json_mode: bool, verbose: bool = False):
        self.json_mode = json_mode
        self.verbose = verbose
        self.cmd: str | None = None

    def result(self, payload: dict, human=None) -> None:
        if self.json_mode:
            doc = {"ok": True, "cmd": self.cmd, **payload}
            print(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        elif human is not None:
            human(payload)

    def note(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)


class Session:
    def __init__(self, out: Out, browser: str | None = None, cookie_file: Path | None = None):
        self.out = out
        self.browser = browser if browser and browser != "auto" else None
        self.cookie_file = cookie_file
        self.source: str | None = None
        self.count = 0
        self._yt: YTMusic | None = None
        self._refreshed = False

    def cookies(self, force: bool = False) -> dict[str, str]:
        if not force and self.cookie_file is None:
            cached = read_cache()
            if (
                cached is not None
                and time.time() - cached["created"] <= auth_ttl()
                and (self.browser is None or cached["browser"] == self.browser)
            ):
                self.source = cached["browser"]
                self.count = len(cached["cookies"])
                self.out.note(f"auth: cached cookies from {cached['browser']}")
                return cached["cookies"]
        browser, cookies = discover(self.browser, self.cookie_file, self.out)
        if self.cookie_file is None:
            write_cache(browser, cookies)
        self.source = browser
        self.count = len(cookies)
        return cookies

    def client(self, force: bool = False) -> YTMusic:
        if self._yt is not None and not force:
            return self._yt
        self._yt = YTMusic(headers_for(self.cookies(force)))
        return self._yt

    def call(self, name: str, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return getattr(self.client(), name)(*args, **kwargs)
            except YTMusicServerError as e:
                message = str(e)
                if is_auth_error(message) and not self._refreshed:
                    self._refreshed = True
                    self.out.note("auth: rejected, re-reading cookies from browser")
                    self.client(force=True)
                    continue
                if is_transient(message) and attempt < RETRIES:
                    attempt += 1
                    time.sleep(2**attempt)
                    continue
                raise translate(message)
            except requests.RequestException as e:
                if attempt < RETRIES:
                    attempt += 1
                    time.sleep(2**attempt)
                    continue
                raise Failure("network", f"{name}: {e}")
            except YTMusicUserError as e:
                raise Failure("api", short(str(e)))
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise Failure("api", short(f"{name}: {type(e).__name__}: {e}"))


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "ytm"


def cache_path() -> Path:
    return state_dir() / "auth.json"


def auth_ttl() -> int:
    try:
        return max(0, int(os.environ.get("YTM_AUTH_TTL", "900")))
    except ValueError:
        return 900


def read_cache() -> dict | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), dict):
        return None
    if not isinstance(data.get("browser"), str) or not isinstance(data.get("created"), int):
        return None
    if not REQUIRED <= data["cookies"].keys():
        return None
    return data


def write_cache(browser: str, cookies: dict[str, str]) -> Path:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"browser": browser, "created": int(time.time()), "cookies": cookies}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    tmp.replace(path)
    return path


def headers_for(cookies: dict[str, str]) -> dict[str, str]:
    stamp = str(int(time.time()))
    sapisid = cookies["__Secure-3PAPISID"]
    digest = hashlib.sha1(f"{stamp} {sapisid} {ORIGIN}".encode()).hexdigest()
    return {
        "user-agent": USER_AGENT,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"SAPISIDHASH {stamp}_{digest}",
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "x-goog-authuser": "0",
        "origin": ORIGIN,
        "x-origin": ORIGIN,
    }


def firefox_dbs() -> list[Path]:
    root = Path.home()
    dbs = [p for rel in FIREFOX_ROOTS if (root / rel).is_dir() for p in (root / rel).glob("*/cookies.sqlite")]
    return sorted(dbs, key=lambda p: p.stat().st_mtime, reverse=True)


def chromium_dbs(browser: str) -> list[Path]:
    root = Path.home()
    bases = [root / ".config"]
    flatpak = root / ".var/app"
    if flatpak.is_dir():
        bases += sorted(app / "config" for app in flatpak.iterdir() if app.is_dir())
    if browser == "chromium" and (root / "snap/chromium/common").is_dir():
        bases.append(root / "snap/chromium/common")
    dbs: list[Path] = []
    for base in bases:
        for rel in CHROMIUM_DIRS[browser]:
            dbs += [p for p in (base / rel).glob("*/Network/Cookies") if p.is_file()]
    return sorted(set(dbs), key=lambda p: p.stat().st_mtime, reverse=True)


def clean(cookies: dict[str, str]) -> dict[str, str]:
    return {k: v.strip().strip('"') for k, v in cookies.items() if v.strip().strip('"')}


def firefox_cookies(db: Path) -> dict[str, str]:
    query = "SELECT name, value FROM moz_cookies WHERE host LIKE '%youtube.com%'"
    rows = read_rows(db, query)
    return clean({name: value for name, value in rows if name in WHITELIST and value})


def read_rows(db: Path, query: str) -> list[tuple]:
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute(query).fetchall()
        finally:
            con.close()
    except sqlite3.Error as direct_error:
        try:
            with tempfile.TemporaryDirectory(prefix="ytm-sqlite-") as tmp:
                staged = Path(tmp) / db.name
                stage_copy(db, staged)
                con = sqlite3.connect(str(staged), timeout=5)
                try:
                    return con.execute(query).fetchall()
                finally:
                    con.close()
        except sqlite3.Error as copy_error:
            raise Failure("auth", f"cannot read {db}: {copy_error}", hint=f"close the browser and retry ({direct_error})")
        except OSError as copy_error:
            raise Failure("auth", f"cannot read {db}: {copy_error}", hint=f"close the browser and retry ({direct_error})")


def stage_copy(db: Path, target: Path) -> None:
    shutil.copy2(db, target)
    for suffix in ("-wal", "-shm"):
        sidecar = db.with_name(db.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))


def stage_database(db: Path, target: Path) -> None:
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            con.execute("VACUUM INTO ?", (str(target),))
        finally:
            con.close()
        return
    except sqlite3.Error:
        pass
    try:
        stage_copy(db, target)
    except OSError as e:
        raise Failure("auth", f"cannot copy {db}: {e}")


def chromium_cookies(browser: str, db: Path) -> dict[str, str]:
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise Failure("usage", f"unsupported browser: {browser}")
    with tempfile.TemporaryDirectory(prefix="ytm-cookies-") as tmp:
        staged = Path(tmp) / "Cookies"
        stage_database(db, staged)
        try:
            jar = loader(cookie_file=str(staged), domain_name="youtube.com")
        except Exception as e:  # noqa: BLE001
            raise Failure("auth", f"cannot decrypt {browser} cookies: {e}", hint=COOKIE_HINT)
    return clean({c.name: c.value for c in jar if c.name in WHITELIST and c.value})


def infer_browser(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".sqlite"):
        return "firefox"
    text = str(path).lower()
    for browser in ("brave", "edge", "vivaldi", "opera-gx", "opera", "chromium"):
        if browser.replace("-", "_") in text or browser in text:
            return browser.replace("-", "_")
    return "chrome"


def cookies_from(browser: str, db: Path) -> dict[str, str]:
    return firefox_cookies(db) if browser == "firefox" else chromium_cookies(browser, db)


def discover(preferred: str | None, cookie_file: Path | None, out: Out) -> tuple[str, dict[str, str]]:
    if cookie_file is not None:
        browser = preferred or infer_browser(cookie_file)
        cookies = cookies_from(browser, cookie_file)
        missing = REQUIRED - cookies.keys()
        if missing:
            raise Failure("auth", f"{cookie_file}: missing cookies {sorted(missing)}", hint=COOKIE_HINT)
        return browser, cookies

    order = [preferred] if preferred else ["firefox", *CHROMIUM_DIRS]
    candidates = [(b, db) for b in order for db in (firefox_dbs() if b == "firefox" else chromium_dbs(b))]
    if not candidates:
        if preferred:
            raise Failure("auth", f"no {preferred} cookie database found", hint="log into music.youtube.com in that browser, or drop -b to auto-detect")
        raise Failure(
            "auth",
            "no supported browser cookie store found",
            hint=f"looked for {', '.join(order)}; log into music.youtube.com or pass --cookie-file",
        )

    missing_by_browser: dict[str, list[str]] = {}
    for browser, db in candidates:
        try:
            cookies = cookies_from(browser, db)
        except Failure as e:
            out.note(f"auth: {browser} {db}: {e.message}")
            continue
        if REQUIRED <= cookies.keys():
            out.note(f"auth: {browser} {db}")
            return browser, cookies
        missing_by_browser[browser] = sorted(REQUIRED - cookies.keys())
        out.note(f"auth: {browser} {db} missing {missing_by_browser[browser]}")

    detail = "; ".join(f"{b}: missing {m}" for b, m in missing_by_browser.items()) or "none readable"
    raise Failure("auth", f"no browser has valid YouTube Music cookies ({detail})", hint=AUTH_HINT)


def is_auth_error(message: str) -> bool:
    return "401" in message or "403" in message or "Unauthorized" in message or "Forbidden" in message


def short(message: str, limit: int = 240) -> str:
    flat = " ".join(message.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def is_transient(message: str) -> bool:
    return any(token in message for token in ("429", "500", "502", "503", "504", "timeout", "Timeout"))


def translate(message: str) -> Failure:
    if is_auth_error(message):
        return Failure("auth", short(message), hint=AUTH_HINT)
    if "404" in message or "not found" in message.lower():
        return Failure("not_found", short(message))
    if "409" in message or "conflict" in message.lower():
        return Failure("conflict", short(message))
    return Failure("api", short(message))


def playlist_of(args) -> str:
    value = getattr(args, "playlist", None) or DEFAULT_PLAYLIST
    if not value:
        raise Failure("usage", "no playlist specified", hint="pass -p/--playlist or set YTM_PLAYLIST")
    return playlist_id(value)


def playlist_id(value: str) -> str:
    value = value.strip()
    if "list=" in value:
        value = value.split("list=", 1)[1].split("&", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        raise Failure("usage", f"invalid playlist id: {value!r}", hint="pass a playlist id or URL via -p/--playlist")
    return value


def authenticated(sess: Session) -> bool:
    try:
        info = sess.call("get_account_info") or {}
    except Failure:
        return False
    return bool(info.get("accountName") or info.get("channelHandle"))


def fetch(sess: Session, pid: str) -> tuple[dict, list[dict]]:
    try:
        data = sess.call("get_playlist", pid, limit=None)
    except Failure as e:
        if e.code != "api":
            raise
        if not authenticated(sess):
            raise Failure("auth", "session is not authenticated for playlist " + pid, hint=AUTH_HINT) from None
        raise Failure("not_found", f"playlist not found or unavailable: {pid}", hint="check -p/--playlist") from None
    if not isinstance(data, dict) or not data.get("title"):
        if not authenticated(sess):
            raise Failure("auth", "session is not authenticated for playlist " + pid, hint=AUTH_HINT)
        raise Failure("not_found", f"playlist not found: {pid}")
    return data, data.get("tracks") or []


def track_row(position: int, track: dict) -> dict:
    artists = [a.get("name") for a in (track.get("artists") or []) if a.get("name")]
    return {
        "position": position,
        "videoId": track.get("videoId"),
        "setVideoId": track.get("setVideoId"),
        "title": track.get("title"),
        "artists": artists,
        "album": (track.get("album") or {}).get("name"),
        "duration": track.get("duration"),
        "duration_seconds": track.get("duration_seconds"),
    }


def editable(data: dict, tracks: list[dict]) -> bool:
    if tracks:
        return any(t.get("setVideoId") for t in tracks)
    return bool(data.get("owned"))


def require_editable(data: dict, tracks: list[dict]) -> None:
    if not editable(data, tracks):
        raise Failure(
            "conflict",
            f"playlist {data.get('title')} is read-only for this session",
            hint="your cookies are not authenticated as the owner; run `yt auth`",
        )


def label(row: dict) -> str:
    artists = ", ".join(row["artists"]) if row["artists"] else "Unknown"
    return f"{artists} - {row['title']}"


def chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def resolve_one(tracks: list[dict], spec: str) -> int:
    if spec.isdigit():
        index = int(spec) - 1
        if not 0 <= index < len(tracks):
            raise Failure("not_found", f"position out of range: {spec} (1..{len(tracks)})")
        return index
    for index, track in enumerate(tracks):
        if track.get("videoId") == spec:
            return index
    raise Failure("not_found", f"videoId not in playlist: {spec}")


def resolve_many(tracks: list[dict], specs: list[str]) -> list[int]:
    found: set[int] = set()
    for spec in specs:
        match = re.fullmatch(r"(\d+)-(\d+)", spec)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                raise Failure("usage", f"bad range: {spec}")
            for position in range(start, end + 1):
                found.add(resolve_one(tracks, str(position)))
            continue
        found.add(resolve_one(tracks, spec))
    return sorted(found)


def poll(sess: Session, pid: str, check, timeout: float = VERIFY_TIMEOUT) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        _, tracks = fetch(sess, pid)
        if check(tracks) or time.monotonic() >= deadline:
            return tracks
        time.sleep(2)


def confirm(out: Out, prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if out.json_mode or not sys.stdin.isatty():
        raise Failure("conflict", "refusing to modify the playlist without --yes", hint="add --yes to confirm, or --dry to preview")
    print(f"{prompt} [y/N]: ", end="", file=sys.stderr, flush=True)
    if input().strip().lower() not in {"y", "yes"}:
        raise Failure("conflict", "aborted")


def cmd_auth(args, out: Out, sess: Session) -> int:
    sess.client(force=True)
    account = None
    try:
        info = sess.call("get_account_info") or {}
        account = info.get("accountName") or info.get("channelHandle")
    except Failure as e:
        out.note(f"auth: account name unavailable ({e.message[:60]})")
    playlists = sess.call("get_library_playlists", limit=1) or []
    if not account and not playlists:
        raise Failure("auth", "browser cookies do not grant a YouTube Music session", hint=AUTH_HINT)
    payload = {
        "authenticated": True,
        "browser": sess.source,
        "account": account,
        "playlists_visible": len(playlists),
        "cookies": sess.count,
        "cache": str(cache_path()),
        "ttl": auth_ttl(),
    }
    out.result(payload, render_auth)
    return 0


def cmd_playlists(args, out: Out, sess: Session) -> int:
    playlists = sess.call("get_library_playlists", limit=None) or []
    if not playlists and not authenticated(sess):
        raise Failure("auth", "session is not authenticated", hint=AUTH_HINT)
    rows = [
        {"playlistId": p.get("playlistId"), "title": p.get("title"), "count": p.get("count")}
        for p in playlists
    ]
    out.result({"count": len(rows), "playlists": rows}, render_playlists)
    return 0


def cmd_show(args, out: Out, sess: Session) -> int:
    pid = playlist_of(args)
    data, tracks = fetch(sess, pid)
    if args.limit is not None:
        tracks = tracks[: args.limit]
    rows = [track_row(i, t) for i, t in enumerate(tracks, 1)]
    payload = {
        "playlist": {
            "id": pid,
            "title": data.get("title"),
            "count": len(data.get("tracks") or []),
            "duration": data.get("duration"),
            "description": data.get("description"),
            "editable": editable(data, data.get("tracks") or []),
        },
        "count": len(rows),
        "tracks": rows,
    }
    out.result(payload, render_show)
    return 0


def cmd_search(args, out: Out, sess: Session) -> int:
    results = sess.call("search", args.query, filter=args.filter, limit=args.limit) or []
    rows = [
        {
            "videoId": r.get("videoId"),
            "title": r.get("title"),
            "artists": [a.get("name") for a in (r.get("artists") or []) if a.get("name")],
            "album": (r.get("album") or {}).get("name"),
            "duration": r.get("duration"),
            "views": r.get("views"),
        }
        for r in results[: args.limit]
    ]
    payload = {"query": args.query, "filter": args.filter, "count": len(rows), "results": rows}
    out.result(payload, render_search)
    return 0


def cmd_add(args, out: Out, sess: Session) -> int:
    pid = playlist_of(args)
    data, tracks = fetch(sess, pid)
    if not args.dry:
        require_editable(data, tracks)
    existing = {t.get("videoId"): i for i, t in enumerate(tracks, 1) if t.get("videoId")}

    requested: list[dict] = []
    seen: set[str] = set()
    for query in args.queries:
        if args.id:
            video_id, title, artists = query, None, []
        else:
            results = sess.call("search", query, filter="songs") or []
            if not results:
                raise Failure("not_found", f"no search results for {query!r}")
            top = results[0]
            video_id = top.get("videoId")
            title = top.get("title")
            artists = [a.get("name") for a in (top.get("artists") or []) if a.get("name")]
        if not video_id:
            raise Failure("not_found", f"no playable result for {query!r}")
        if video_id in seen:
            continue
        seen.add(video_id)
        requested.append({"query": query, "videoId": video_id, "title": title, "artists": artists})

    plan = [r for r in requested if args.dupes or r["videoId"] not in existing]
    skipped = [
        {"videoId": r["videoId"], "reason": "already in playlist", "position": existing[r["videoId"]]}
        for r in requested
        if r["videoId"] in existing and not args.dupes
    ]
    payload = {
        "playlist": pid,
        "dry": args.dry,
        "requested": requested,
        "add": plan,
        "skipped": skipped,
        "verified": None,
    }
    if args.dry or not plan:
        payload["verified"] = True
        out.result(payload, render_add)
        return 0

    ids = [r["videoId"] for r in plan]
    for chunk in chunks(ids, BATCH):
        sess.call("add_playlist_items", pid, chunk, duplicates=True)

    final = poll(sess, pid, lambda fresh: all(v in {t.get("videoId") for t in fresh} for v in ids))
    present = {t.get("videoId") for t in final}
    missing = [v for v in ids if v not in present]
    payload["verified"] = not missing
    payload["missing"] = missing
    out.result(payload, render_add)
    return 0 if payload["verified"] else 6


def cmd_remove(args, out: Out, sess: Session) -> int:
    pid = playlist_of(args)
    data, tracks = fetch(sess, pid)
    if not args.dry:
        require_editable(data, tracks)
    indices = resolve_many(tracks, args.targets)
    victims = [track_row(i + 1, tracks[i]) for i in indices]
    payload = {"playlist": pid, "dry": args.dry, "count": len(victims), "removed": victims}
    if args.dry:
        out.result(payload, render_remove)
        return 0

    items = [{"videoId": tracks[i].get("videoId"), "setVideoId": tracks[i].get("setVideoId")} for i in indices]
    if any(not item["videoId"] or not item["setVideoId"] for item in items):
        raise Failure("conflict", "some tracks lack setVideoId (unavailable track)", hint="run `yt show` to inspect positions")
    confirm(out, f"remove {len(items)} track(s) from {data.get('title')}", args.yes)

    sess.call("remove_playlist_items", pid, items)
    stale = {item["setVideoId"] for item in items}
    final = poll(sess, pid, lambda fresh: not (stale & {t.get("setVideoId") for t in fresh}))
    payload["verified"] = not (stale & {t.get("setVideoId") for t in final})
    out.result(payload, render_remove)
    return 0 if payload["verified"] else 6


def cmd_move(args, out: Out, sess: Session) -> int:
    pid = playlist_of(args)
    anchors = [a for a in (args.before, args.after) if a]
    if len(anchors) > 1 or (anchors and args.dst is not None) or (not anchors and args.dst is None):
        raise Failure("usage", "give exactly one destination: DST, --before X or --after X")

    for attempt in range(2):
        data, tracks = fetch(sess, pid)
        if not args.dry:
            require_editable(data, tracks)
        total = len(tracks)
        src = resolve_one(tracks, args.src)
        others = [t for i, t in enumerate(tracks) if i != src]
        anchor = resolve_one(tracks, args.before or args.after) if (args.before or args.after) else None
        if args.before:
            target = anchor - 1 if anchor > src else anchor
        elif args.after:
            target = anchor + 1 if anchor < src else anchor
        else:
            target = args.dst - 1
        if anchor == src:
            target = src
        if not 0 <= target <= len(others):
            raise Failure("usage", f"destination out of range: {target + 1} (1..{total})")

        current = tracks[src]
        video_id = current.get("videoId")
        row = track_row(src + 1, current)
        payload = {
            "playlist": pid,
            "dry": args.dry,
            "videoId": video_id,
            "title": current.get("title"),
            "from": src + 1,
            "to": target + 1,
            "verified": None,
        }
        if src == target:
            payload["verified"] = True
            payload["note"] = "already in place"
            out.result(payload, render_move)
            return 0
        if args.dry:
            out.result(payload, render_move)
            return 0
        if not video_id or not current.get("setVideoId"):
            raise Failure("conflict", f"cannot move track at {src + 1}: missing identifiers", hint="run `yt auth`")
        if target < len(others) and not others[target].get("setVideoId"):
            raise Failure("conflict", f"cannot anchor on unavailable track at {target + 1}")

        confirm(out, f"move {label(row)} from {src + 1} to {target + 1}", args.yes)
        move = current["setVideoId"] if target == len(others) else (current["setVideoId"], others[target]["setVideoId"])
        try:
            sess.call("edit_playlist", pid, moveItem=move)
        except Failure as e:
            if e.code == "api" and attempt == 0:
                out.note(f"move: edit failed ({e.message[:80]}), retrying in 8s")
                time.sleep(8)
                continue
            raise

        final = poll(
            sess,
            pid,
            lambda fresh, video_id=video_id, target=target: {t.get("videoId"): i for i, t in enumerate(fresh)}.get(video_id) == target,
        )
        index = {t.get("videoId"): i for i, t in enumerate(final)}.get(video_id)
        payload["to"] = None if index is None else index + 1
        payload["verified"] = index == target
        out.result(payload, render_move)
        return 0 if payload["verified"] else 6

    raise Failure("api", "move failed after retry", hint="re-check with `yt show` before retrying")


def render_auth(payload: dict) -> None:
    who = payload["account"] or "unknown account"
    print(f"authenticated as {who} via {payload['browser']} ({payload['cookies']} cookies)")
    print(f"cache: {payload['cache']} (ttl {payload['ttl']}s)")


def render_playlists(payload: dict) -> None:
    for row in payload["playlists"]:
        count = "?" if row["count"] is None else str(row["count"])
        print(f"{row['playlistId']:<34} {count:>5}  {row['title']}")


def render_show(payload: dict) -> None:
    info = payload["playlist"]
    mode = "" if info["editable"] else "  (read-only)"
    print(f"{info['title']}  {info['id']}  {info['count']} tracks  {info['duration'] or ''}{mode}".rstrip())
    print()
    width = max((len(t["videoId"] or "-") for t in payload["tracks"]), default=11)
    for track in payload["tracks"]:
        duration = track["duration"] or ""
        print(f"{track['position']:>4}  {(track['videoId'] or '-'):<{width}}  {label(track)}  {duration}".rstrip())


def render_search(payload: dict) -> None:
    for row in payload["results"]:
        artists = ", ".join(row["artists"])
        album = row["album"] or ""
        print(f"{row['videoId']}  {artists} - {row['title']}  [{album}] {row['duration'] or ''} {row['views'] or ''}".rstrip())


def render_add(payload: dict) -> None:
    skipped = {row["videoId"] for row in payload["skipped"]}
    for row in payload["requested"]:
        mark = "~" if row["videoId"] in skipped else "+"
        print(f"{mark} {row['videoId']}  {row['query']}")
    if payload["dry"]:
        print(f"dry run: would add {len(payload['add'])}, skip {len(payload['skipped'])}")
    elif payload["verified"]:
        print(f"added {len(payload['add'])}, skipped {len(payload['skipped'])} (verified)")
    else:
        missing = payload.get("missing") or []
        print(f"added {len(payload['add'])} but {len(missing)} unconfirmed: {', '.join(missing)}")


def render_remove(payload: dict) -> None:
    for row in payload["removed"]:
        print(f"{row['position']:>4}  {row['videoId'] or '-'}  {label(row)}")
    if payload["dry"]:
        print(f"dry run: would remove {payload['count']} track(s)")
    else:
        state = "verified" if payload["verified"] else "UNCONFIRMED, re-check with `yt show`"
        print(f"removed {payload['count']} track(s) ({state})")


def render_move(payload: dict) -> None:
    if payload["dry"]:
        print(f"dry run: move {payload['from']} -> {payload['to']}: {payload['title']}")
        return
    if payload.get("note"):
        print(f"{payload['title']} already at {payload['to']}")
        return
    state = "verified" if payload["verified"] else "UNCONFIRMED, re-check with `yt show`"
    print(f"moved {payload['from']} -> {payload['to']} ({state}): {payload['title']}")


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Failure("usage", message, hint=f"{self.prog} --help")


def build_parser() -> argparse.ArgumentParser:
    common = Parser(add_help=False)
    common.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON on stdout, errors included")
    common.add_argument("-p", "--playlist", default=argparse.SUPPRESS, metavar="ID", help="playlist id or URL (env YTM_PLAYLIST)")
    common.add_argument("-b", "--browser", choices=BROWSERS, default=argparse.SUPPRESS, metavar="NAME", help="cookie source: " + ", ".join(BROWSERS))
    common.add_argument("--cookie-file", default=argparse.SUPPRESS, metavar="PATH", help="read this cookie database instead of auto-detecting")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="diagnostics on stderr")

    parser = Parser(
        prog=PROG,
        parents=[common],
        description="YouTube Music playlist manager driven by browser cookies.",
        epilog=(
            "examples:\n"
            "  yt auth\n"
            "  yt playlists\n"
            "  yt -p <playlist> show\n"
            "  yt search 'nightcore rather be' -f videos -n 5\n"
            "  yt -p <playlist> add 'nightcore aurelle' --dry\n"
            "  yt -p <playlist> add 'nightcore aurelle' 'nightcore let you go'\n"
            "  yt -p <playlist> remove 12 15-18 --yes\n"
            "  yt -p <playlist> move 85 --after 84\n"
            "  yt -j -p <playlist> show | jq '.tracks[0]'\n"
            "\n"
            "exit codes: 0 ok, 1 internal, 2 usage, 3 auth, 4 not found, 5 refused, 6 unverified, 7 network, 8 api"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    auth = sub.add_parser("auth", parents=[common], help="extract cookies from a browser and verify the session")
    auth.set_defaults(fn=cmd_auth)

    playlists = sub.add_parser("playlists", parents=[common], help="list library playlists")
    playlists.set_defaults(fn=cmd_playlists)

    show = sub.add_parser("show", parents=[common], help="list playlist tracks with positions")
    show.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="show at most N tracks")
    show.set_defaults(fn=cmd_show)

    search = sub.add_parser("search", parents=[common], help="search YouTube Music")
    search.add_argument("query")
    search.add_argument("-f", "--filter", default="songs", choices=["songs", "videos", "albums", "artists", "playlists"])
    search.add_argument("-n", "--limit", type=int, default=10, metavar="N")
    search.set_defaults(fn=cmd_search)

    add = sub.add_parser("add", parents=[common], help="add tracks by search query or videoId")
    add.add_argument("queries", nargs="+", metavar="QUERY")
    add.add_argument("--id", action="store_true", help="treat arguments as videoIds")
    add.add_argument("--dupes", action="store_true", help="add even if the videoId is already in the playlist")
    add.add_argument("--dry", action="store_true", help="resolve matches without adding")
    add.set_defaults(fn=cmd_add)

    remove = sub.add_parser("remove", parents=[common], help="remove tracks by position, range or videoId")
    remove.add_argument("targets", nargs="+", metavar="TARGET")
    remove.add_argument("--dry", action="store_true")
    remove.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    remove.set_defaults(fn=cmd_remove)

    move = sub.add_parser("move", parents=[common], help="move a track to a position or anchor")
    move.add_argument("src", metavar="SRC", help="1-based position or videoId")
    move.add_argument("dst", nargs="?", type=int, metavar="DST", help="1-based destination position")
    move.add_argument("--before", metavar="TARGET")
    move.add_argument("--after", metavar="TARGET")
    move.add_argument("--dry", action="store_true")
    move.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    move.set_defaults(fn=cmd_move)

    return parser


def report(out: Out, err: Failure) -> int:
    if out.json_mode:
        doc = {"ok": False, "cmd": out.cmd, "error": {"code": err.code, "message": err.message, "hint": err.hint}}
        print(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"error: {err.message}", file=sys.stderr)
        if err.hint:
            print(f"hint: {err.hint}", file=sys.stderr)
    return err.exit_code


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        return run(argv)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = False
    rest: list[str] = []
    for arg in argv:
        if arg in ("--json", "-j"):
            json_mode = True
        else:
            rest.append(arg)

    out = Out(json_mode)
    try:
        args = build_parser().parse_args(rest)
    except Failure as err:
        return report(out, err)

    out.cmd = args.cmd
    out.verbose = bool(getattr(args, "verbose", False))
    out.json_mode = json_mode or bool(getattr(args, "json", False))

    try:
        cookie_file = getattr(args, "cookie_file", None)
        sess = Session(
            out,
            browser=getattr(args, "browser", None),
            cookie_file=Path(cookie_file).expanduser() if cookie_file else None,
        )
        return args.fn(args, out, sess)
    except Failure as err:
        return report(out, err)
    except KeyboardInterrupt:
        return report(out, Failure("internal", "interrupted"))
    except Exception as err:  # noqa: BLE001
        if out.verbose:
            import traceback

            traceback.print_exc()
        return report(out, Failure("internal", f"{type(err).__name__}: {err}"))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
