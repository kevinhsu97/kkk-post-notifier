#!/usr/bin/env python3
"""Lightweight Instagram + YouTube + TikTok + Threads new-post notifier. Python 3 stdlib only.

Runs on GitHub Actions (see .github/workflows/notifier.yml).
Secrets come from environment variables (GitHub Actions secrets), never from the repo:
  IG_GRAPH_TOKEN, THREADS_TOKEN, DISCORD_WEBHOOK_URL, GH_SECRETS_PAT (to rotate THREADS_TOKEN).
State (seen IDs, no secrets) lives in state.json and is committed back to the repo.
DRY_RUN=1 prints what would be posted without posting or changing state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("NOTIFIER_STATE_PATH") or os.path.join(DIR, "state.json")
# Logs go to stdout/stderr only (GitHub Actions log), always scrubbed of secrets.
ERRORS_LOG = "errors"
RUN_LOG = "run"
TOKEN_ENV = "IG_GRAPH_TOKEN"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
THREADS_TOKEN_ENV = "THREADS_TOKEN"
GH_SECRETS_PAT_ENV = "GH_SECRETS_PAT"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY") or "kevinhsu97/kkk-post-notifier"
GIT_COMMIT_ENABLED = os.environ.get("NOTIFIER_GIT_COMMIT") == "1"
IG_EXPIRY_WARN_WITHIN_SEC = 7 * 24 * 3600
IG_USER_ID = "17841404792529913"
GRAPH_BASE = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media"
YT_CHANNEL_ID = "UCDB4QsMv7r07vf7EFNYeXzA"
YT_FEED_URL = (
    f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
)
ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
TZ = ZoneInfo("Asia/Taipei")
MAX_POSTS_PER_RUN = 5
LOG_MAX_LINES = 200
USER_AGENT = "KKK-Notifier/1.0"
REQUEST_TIMEOUT = 30
YT_FETCH_RETRIES = 2
YT_RETRY_DELAY_SEC = 1
TT_HANDLE = "kevinhsu_97"
TT_EMBED_URL = f"https://www.tiktok.com/embed/@{TT_HANDLE}"
TT_FETCH_RETRIES = 2
TT_RETRY_DELAY_SEC = 1
TT_FAIL_ALERT_AFTER = 12
THREADS_USER_ID = "29134743799518116"
THREADS_GRAPH_BASE = "https://graph.threads.net/v1.0/me/threads"
THREADS_DEBUG_URL = "https://graph.threads.net/debug_token"
THREADS_REFRESH_URL = "https://graph.threads.com/refresh_access_token"
THREADS_REFRESH_AFTER_SEC = 7 * 24 * 3600
THREADS_MIN_AGE_BEFORE_REFRESH_SEC = 24 * 3600
THREADS_EXPIRY_WARN_WITHIN_SEC = 7 * 24 * 3600
THREADS_REPOST_TYPE = "REPOST_FACADE"
_TT_VIDEO_URL_RE = re.compile(
    rf"https?://(?:www\.)?tiktok\.com/@{re.escape(TT_HANDLE)}/video/(\d{{10,25}})",
    re.IGNORECASE,
)
_TT_ID_RE = re.compile(r"^\d{10,25}$")

# Patterns used only to scrub secrets from logs and exception text.
_TOKEN_QS_RE = re.compile(r"access_token=[^&\s\"']+", re.IGNORECASE)
_WEBHOOK_RE = re.compile(
    r"https://(?:(?:canary|ptb)\.)?(?:discord|discordapp)\.com/api/webhooks/\S+",
    re.IGNORECASE,
)


class PlatformError(Exception):
    """A platform check failed; already logged to errors.log."""


def now_ts() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"


def _read_secret_env(name: str) -> str:
    """Read a secret from an environment variable (GitHub Actions secret)."""
    value = os.environ.get(name)
    if value is None:
        raise OSError(f"environment variable {name} is not set")
    return "".join(ch for ch in value if ch not in "\r\n ")


_ALL_SECRETS: list[str] = []


def _register_secret(value: str) -> None:
    if value and value not in _ALL_SECRETS:
        _ALL_SECRETS.append(value)


def redact(text: str, *secrets: str) -> str:
    if text is None:
        return ""
    out = str(text)
    for secret in tuple(secrets) + tuple(_ALL_SECRETS):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    out = _TOKEN_QS_RE.sub("access_token=[REDACTED]", out)
    out = _WEBHOOK_RE.sub("[REDACTED]", out)
    return out


def _gh_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def append_log(path: str, line: str) -> None:
    line = redact(line.rstrip("\n"))
    if path == ERRORS_LOG:
        print(line, file=sys.stderr, flush=True)
    else:
        print(line, flush=True)


def _summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(redact(line) + "\n\n")
    except OSError:
        pass


def log_error(msg: str, *secrets: str) -> None:
    text = redact(msg, *secrets)
    append_log(ERRORS_LOG, f"{now_ts()} {text}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{_gh_escape(text)}", flush=True)
    _summary(f"WARNING {now_ts()}: {text}")


def die(msg: str, *secrets: str, code: int = 1) -> None:
    log_error(msg, *secrets)
    sys.exit(code)


def load_state() -> dict | None:
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"Failed to read state file: {e}")
    if not isinstance(data, dict):
        die("State file is not a JSON object")
    ids = data.get("seen_ids", [])
    if not isinstance(ids, list):
        die("State file seen_ids is not a list")
    data["seen_ids"] = [str(x) for x in ids]
    yt = data.get("youtube")
    if yt is None:
        pass
    elif not isinstance(yt, dict):
        die("State file youtube is not an object")
    else:
        yt_ids = yt.get("seen_ids", [])
        if not isinstance(yt_ids, list):
            die("State file youtube.seen_ids is not a list")
        yt["seen_ids"] = [str(x) for x in yt_ids]
        data["youtube"] = yt
    tt = data.get("tiktok")
    if tt is None:
        pass
    elif not isinstance(tt, dict):
        die("State file tiktok is not an object")
    else:
        if "seen_ids" in tt:
            tt_ids = tt.get("seen_ids")
            if not isinstance(tt_ids, list):
                die("State file tiktok.seen_ids is not a list")
            tt["seen_ids"] = [str(x) for x in tt_ids]
        if "consecutive_failures" in tt and tt["consecutive_failures"] is not None:
            try:
                tt["consecutive_failures"] = int(tt["consecutive_failures"])
            except (TypeError, ValueError):
                die("State file tiktok.consecutive_failures is not a number")
        data["tiktok"] = tt
    th = data.get("threads")
    if th is None:
        pass
    elif not isinstance(th, dict):
        die("State file threads is not an object")
    else:
        if "seen_ids" in th:
            th_ids = th.get("seen_ids")
            if not isinstance(th_ids, list):
                die("State file threads.seen_ids is not a list")
            th["seen_ids"] = [str(x) for x in th_ids]
        if (
            "consecutive_refresh_failures" in th
            and th["consecutive_refresh_failures"] is not None
        ):
            try:
                th["consecutive_refresh_failures"] = int(
                    th["consecutive_refresh_failures"]
                )
            except (TypeError, ValueError):
                die("State file threads.consecutive_refresh_failures is not a number")
        data["threads"] = th
    return data


def save_state(state: dict) -> None:
    payload: dict = {
        "seen_ids": list(state.get("seen_ids") or []),
        "updated_at": now_ts(),
    }
    # Preserve any extra top-level keys (except ones we rewrite).
    for key, value in state.items():
        if key in ("seen_ids", "updated_at", "youtube", "tiktok", "threads"):
            continue
        payload[key] = value
    yt = state.get("youtube")
    if isinstance(yt, dict):
        yt_out = {
            "seen_ids": list(yt.get("seen_ids") or []),
            "updated_at": yt.get("updated_at") or now_ts(),
        }
        for key, value in yt.items():
            if key not in ("seen_ids", "updated_at"):
                yt_out[key] = value
        payload["youtube"] = yt_out
    tt = state.get("tiktok")
    if isinstance(tt, dict):
        tt_out: dict = {}
        # Only persist seen_ids after a successful baseline/update.
        # A failures-only object must not look like an empty seen set.
        if "seen_ids" in tt:
            tt_out["seen_ids"] = list(tt.get("seen_ids") or [])
        tt_out["updated_at"] = tt.get("updated_at") or now_ts()
        if "consecutive_failures" in tt:
            try:
                tt_out["consecutive_failures"] = int(tt.get("consecutive_failures") or 0)
            except (TypeError, ValueError):
                tt_out["consecutive_failures"] = 0
        for key, value in tt.items():
            if key not in ("seen_ids", "updated_at", "consecutive_failures"):
                tt_out[key] = value
        payload["tiktok"] = tt_out
    th = state.get("threads")
    if isinstance(th, dict):
        th_out: dict = {}
        # Only persist seen_ids after a successful baseline/update.
        # Refresh-metadata-only must not look like an empty seen set.
        if "seen_ids" in th:
            th_out["seen_ids"] = list(th.get("seen_ids") or [])
        th_out["updated_at"] = th.get("updated_at") or now_ts()
        for key, value in th.items():
            if key not in ("seen_ids", "updated_at"):
                th_out[key] = value
        payload["threads"] = th_out
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=os.path.dirname(os.path.abspath(STATE_PATH)),
        capture_output=True,
        text=True,
        timeout=120,
    )


def commit_state(message: str) -> bool:
    """Commit and push state.json. Returns True when the remote has the state.

    No-op (returns True) when NOTIFIER_GIT_COMMIT is not enabled (local testing).
    """
    if not GIT_COMMIT_ENABLED:
        return True
    rel = os.path.basename(STATE_PATH)
    _git("add", rel)
    diff = _git("diff", "--cached", "--quiet", "--", rel)
    if diff.returncode != 0:
        res = _git("commit", "-m", message, "--", rel)
        if res.returncode != 0:
            log_error(f"git commit failed: {res.stderr.strip()[:300]}")
            return False
    for attempt in range(1, 6):
        push = _git("push", "origin", "HEAD:main")
        if push.returncode == 0:
            return True
        append_log(
            RUN_LOG,
            f"{now_ts()} git push attempt {attempt} failed: {push.stderr.strip()[:200]}",
        )
        pull = _git("pull", "--rebase", "-X", "theirs", "origin", "main")
        if pull.returncode != 0:
            _git("rebase", "--abort")
        time.sleep(2 * attempt)
    log_error("git push of state.json failed after 5 attempts")
    return False


def check_ig_token_expiry(token: str) -> None:
    """Warn (Actions log + job summary) when the IG token expires within 7 days.

    The IG token is a long-lived Facebook user token (about 60 days). It cannot be
    refreshed without the Meta app secret, so it has to be regenerated by hand.
    """
    url = "https://graph.facebook.com/v21.0/oauth/access_token_info?" + urllib.parse.urlencode(
        {"access_token": token}
    )
    status, data, err = _http_get_json(url)
    if err or data is None or "expires_in" not in data:
        append_log(RUN_LOG, f"{now_ts()} instagram token expiry check unavailable (HTTP {status})")
        return
    try:
        remaining = int(data.get("expires_in"))
    except (TypeError, ValueError):
        return
    expiry = datetime.now(TZ) + timedelta(seconds=remaining)
    expiry_s = expiry.strftime("%Y-%m-%d %H:%M") + " Asia/Taipei"
    append_log(RUN_LOG, f"{now_ts()} instagram token expires about {expiry_s}")
    if remaining <= IG_EXPIRY_WARN_WITHIN_SEC:
        log_error(
            f"WARNING: Instagram access token expires in {remaining / 86400:.1f} days "
            f"(about {expiry_s}). Generate a new long-lived token and update the "
            f"IG_GRAPH_TOKEN repo secret before then."
        )


def _ig_fail(msg: str, *secrets: str) -> None:
    raise PlatformError(redact(msg, *secrets))


def fetch_media(token: str) -> list[dict]:
    params = {
        "fields": "id,caption,media_type,permalink,timestamp",
        "limit": "10",
        "access_token": token,
    }
    url = GRAPH_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        err_code = ""
        err_msg = body[:300]
        err_type = ""
        try:
            payload = json.loads(body)
            err = payload.get("error") or {}
            err_code = err.get("code", "")
            err_type = err.get("type", "")
            err_msg = err.get("message", err_msg)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        _ig_fail(
            f"API error HTTP {e.code} code={err_code} type={err_type}: {err_msg}",
            token,
        )
    except urllib.error.URLError as e:
        _ig_fail(f"API request failed: {getattr(e, 'reason', e)}", token)
    except (TimeoutError, json.JSONDecodeError, OSError) as e:
        _ig_fail(f"API request failed: {e}", token)

    if isinstance(data, dict) and data.get("error"):
        err = data["error"] or {}
        _ig_fail(
            f"API error code={err.get('code', '')} type={err.get('type', '')}: "
            f"{err.get('message', data)}",
            token,
        )

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _ig_fail("API response missing data list", token)
    return items


def _http_get(url: str, accept: str | None = None) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept
            or "application/atom+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers.get("content-type") or ""
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type") or ""


def fetch_youtube_feed() -> list[dict]:
    """Return video dicts from the official channel Atom feed.

    A non-200 / non-XML response is an error, never an empty channel.
    YouTube's feed server often answers 404/500 during outages.
    """
    last_err = "YouTube feed request failed"
    body = b""
    status = 0
    for attempt in range(1, YT_FETCH_RETRIES + 1):
        try:
            status, body, _ctype = _http_get(YT_FEED_URL)
        except urllib.error.URLError as e:
            last_err = f"YouTube feed request failed: {getattr(e, 'reason', e)}"
            if attempt < YT_FETCH_RETRIES:
                time.sleep(YT_RETRY_DELAY_SEC)
            continue
        except (TimeoutError, OSError) as e:
            last_err = f"YouTube feed request failed: {e}"
            if attempt < YT_FETCH_RETRIES:
                time.sleep(YT_RETRY_DELAY_SEC)
            continue
        if status == 200:
            break
        last_err = f"YouTube feed HTTP {status}"
        if attempt < YT_FETCH_RETRIES:
            time.sleep(YT_RETRY_DELAY_SEC)
    else:
        raise PlatformError(last_err)

    text = body.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if not stripped.startswith("<") or stripped.lstrip("<").lower().startswith(
        "!doctype html"
    ) or stripped.lower().startswith("<html"):
        raise PlatformError(
            f"YouTube feed HTTP {status} returned HTML instead of Atom XML"
        )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise PlatformError(f"YouTube feed XML parse failed: {e}") from e

    tag = root.tag
    if tag != f"{{{ATOM_NS}}}feed" and tag != "feed":
        raise PlatformError(f"YouTube feed root is <{tag}>, not Atom feed")

    def q(ns: str, name: str) -> str:
        return f"{{{ns}}}{name}"

    entries: list[dict] = []
    node_entries = root.findall(q(ATOM_NS, "entry")) or root.findall("entry")
    for entry in node_entries:
        vid = ""
        yt_el = entry.find(q(YT_NS, "videoId"))
        if yt_el is not None and yt_el.text:
            vid = yt_el.text.strip()
        if not vid:
            id_el = entry.find(q(ATOM_NS, "id"))
            if id_el is None:
                id_el = entry.find("id")
            raw_id = (id_el.text or "").strip() if id_el is not None else ""
            if raw_id.startswith("yt:video:"):
                vid = raw_id.split(":", 2)[-1]
        if not vid:
            continue
        title_el = entry.find(q(ATOM_NS, "title"))
        if title_el is None:
            title_el = entry.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        href = ""
        for link in entry.findall(q(ATOM_NS, "link")) + entry.findall("link"):
            rel = link.get("rel") or "alternate"
            if rel == "alternate" and link.get("href"):
                href = link.get("href") or ""
                break
        if not href:
            href = f"https://www.youtube.com/watch?v={vid}"
        pub_el = entry.find(q(ATOM_NS, "published"))
        if pub_el is None:
            pub_el = entry.find("published")
        published = (pub_el.text or "").strip() if pub_el is not None else ""
        entries.append(
            {
                "id": vid,
                "title": title,
                "url": href,
                "timestamp": published,
            }
        )
    return entries


YT_VIDEOS_URL = "https://www.youtube.com/@gykevinh/videos"
YT_SHORTS_URL = "https://www.youtube.com/@gykevinh/shorts"
_SKIP_TITLES = {
    "加入待播清單",
    "儲存至播放清單",
    "分享",
    "Add to queue",
    "Save",
    "Share",
}


def _text_of(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("simpleText"):
            return str(node["simpleText"])
        if node.get("content") and isinstance(node["content"], str):
            return node["content"]
        runs = node.get("runs") or []
        if isinstance(runs, list):
            return "".join(
                x.get("text", "") for x in runs if isinstance(x, dict)
            )
    return ""


def _find_video_id(obj) -> str:
    if isinstance(obj, dict):
        vid = obj.get("videoId")
        if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return vid
        for key in ("watchEndpoint", "reelWatchEndpoint"):
            inner = obj.get(key)
            if isinstance(inner, dict):
                found = _find_video_id(inner)
                if found:
                    return found
        for value in obj.values():
            found = _find_video_id(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_video_id(value)
            if found:
                return found
    return ""


def _parse_yt_initial_data(html: str) -> list[dict]:
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    items: list[dict] = []
    seen: set[str] = set()

    def add(vid: str, title: str) -> None:
        if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return
        title = (title or "").strip()
        if title in _SKIP_TITLES:
            title = ""
        if vid in seen:
            if title:
                for item in items:
                    if item["id"] == vid and not item["title"]:
                        item["title"] = title
            return
        seen.add(vid)
        items.append(
            {
                "id": vid,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "timestamp": "",
            }
        )

    def _nested_title(obj, depth: int = 0) -> str:
        if depth > 10:
            return ""
        if isinstance(obj, dict):
            if "title" in obj:
                text = _text_of(obj["title"]).strip()
                if text and text not in _SKIP_TITLES:
                    return text
            for value in obj.values():
                text = _nested_title(value, depth + 1)
                if text:
                    return text
        elif isinstance(obj, list):
            for value in obj:
                text = _nested_title(value, depth + 1)
                if text:
                    return text
        return ""

    def walk(obj) -> None:
        if isinstance(obj, dict):
            if "videoRenderer" in obj:
                vr = obj["videoRenderer"]
                add(vr.get("videoId") or "", _text_of(vr.get("title")))
            if "gridVideoRenderer" in obj:
                vr = obj["gridVideoRenderer"]
                add(vr.get("videoId") or "", _text_of(vr.get("title")))
            if "reelItemRenderer" in obj:
                rr = obj["reelItemRenderer"]
                add(
                    rr.get("videoId") or "",
                    _text_of(rr.get("headline") or rr.get("title")),
                )
            if "shortsLockupViewModel" in obj:
                block = obj["shortsLockupViewModel"]
                overlay = block.get("overlayMetadata") or {}
                title = _text_of(
                    overlay.get("primaryText") if isinstance(overlay, dict) else None
                ) or _text_of(block.get("title"))
                add(_find_video_id(block), title)
            if "lockupViewModel" in obj:
                block = obj["lockupViewModel"]
                title = _text_of(block.get("title"))
                if not title:
                    md = block.get("metadata") or {}
                    if isinstance(md, dict):
                        title = _text_of(md.get("title"))
                if not title:
                    title = _nested_title(block)
                add(_find_video_id(block), title)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return items


def fetch_youtube_channel_pages() -> list[dict]:
    """Official channel tabs, used only if the Atom feed is down."""
    merged: list[dict] = []
    seen: set[str] = set()
    errors = []
    for url in (YT_VIDEOS_URL, YT_SHORTS_URL):
        try:
            status, body, _ctype = _http_get(url)
        except urllib.error.URLError as e:
            errors.append(f"{url}: {getattr(e, 'reason', e)}")
            continue
        except (TimeoutError, OSError) as e:
            errors.append(f"{url}: {e}")
            continue
        if status != 200:
            errors.append(f"{url} HTTP {status}")
            continue
        items = _parse_yt_initial_data(body.decode("utf-8", errors="replace"))
        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            merged.append(item)
    if not merged:
        detail = "; ".join(str(e) for e in errors) or "no video ids parsed"
        raise PlatformError(f"YouTube channel page fallback failed ({detail})")
    return merged


def fetch_youtube_items() -> list[dict]:
    """Prefer the official Atom feed; fall back to official channel pages."""
    try:
        return fetch_youtube_feed()
    except PlatformError as feed_err:
        try:
            items = fetch_youtube_channel_pages()
        except PlatformError as page_err:
            raise PlatformError(
                f"{feed_err}; channel page fallback also failed: {page_err}"
            ) from page_err
        append_log(
            RUN_LOG,
            f"{now_ts()} youtube feed unavailable ({feed_err}); "
            f"used channel page fallback items={len(items)}",
        )
        return items


def _tiktok_video_url(vid: str) -> str:
    return f"https://www.tiktok.com/@{TT_HANDLE}/video/{vid}"


def _as_tiktok_id(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return ""
    if _TT_ID_RE.fullmatch(text):
        return text
    return ""


def _tiktok_item(vid: str, desc: str = "") -> dict:
    # Embed items have no timestamp; TikTok snowflake IDs increase over time,
    # so the ID itself gives oldest-first ordering.
    return {
        "id": vid,
        "caption": (desc or "").strip(),
        "url": _tiktok_video_url(vid),
        "timestamp": vid,
    }


def _items_from_video_list(video_list: list) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for raw in video_list:
        if not isinstance(raw, dict):
            continue
        if raw.get("privateItem") is True:
            continue
        author = raw.get("authorUniqueId")
        if isinstance(author, str) and author.strip() and author.strip() != TT_HANDLE:
            continue
        vid = _as_tiktok_id(raw.get("id"))
        if not vid or vid in seen:
            continue
        desc = raw.get("desc")
        if desc is None:
            desc = ""
        elif not isinstance(desc, str):
            desc = str(desc)
        seen.add(vid)
        items.append(_tiktok_item(vid, desc))
    return items


def _parse_frontity_videos(html: str) -> list[dict] | None:
    """Parse __FRONTITY_CONNECT_STATE__. None means that block was unusable."""
    match = re.search(
        r'<script[^>]*\bid=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    )
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    lists: list[list] = []
    src_data = None
    if isinstance(data, dict):
        source = data.get("source")
        if isinstance(source, dict):
            src_data = source.get("data")
    if isinstance(src_data, dict):
        preferred = src_data.get(f"/embed/@{TT_HANDLE}")
        if not isinstance(preferred, dict):
            preferred = src_data.get(f"/embed/@{TT_HANDLE}/")
        candidates = []
        if isinstance(preferred, dict):
            candidates.append(preferred)
        for value in src_data.values():
            if isinstance(value, dict) and value not in candidates:
                candidates.append(value)
        for block in candidates:
            video_list = block.get("videoList")
            if isinstance(video_list, list):
                lists.append(video_list)
                break
    if not lists:
        return None
    return _items_from_video_list(lists[0])


def _parse_tiktok_video_urls(html: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for vid in _TT_VIDEO_URL_RE.findall(html):
        if vid in seen:
            continue
        seen.add(vid)
        items.append(_tiktok_item(vid, ""))
    return items


def parse_tiktok_embed(html: str) -> list[dict]:
    """Extract videos from a TikTok creator embed page.

    Prefer the Frontity state (IDs plus captions). If that block is missing,
    fall back to unique /@handle/video/{id} URLs, without captions.
    """
    items = _parse_frontity_videos(html)
    if items is None:
        items = _parse_tiktok_video_urls(html)
    return items


def fetch_tiktok_items() -> list[dict]:
    """Return the latest videos from the public embed page.

    A failed fetch or a page with zero video IDs is an error, never an
    empty-channel baseline. Callers must not reset seen IDs on failure.
    """
    last_err = "TikTok embed request failed"
    body = b""
    status = 0
    for attempt in range(1, TT_FETCH_RETRIES + 1):
        try:
            status, body, _ctype = _http_get(
                TT_EMBED_URL, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
            )
        except urllib.error.URLError as e:
            last_err = f"TikTok embed request failed: {getattr(e, 'reason', e)}"
            if attempt < TT_FETCH_RETRIES:
                time.sleep(TT_RETRY_DELAY_SEC)
            continue
        except (TimeoutError, OSError) as e:
            last_err = f"TikTok embed request failed: {e}"
            if attempt < TT_FETCH_RETRIES:
                time.sleep(TT_RETRY_DELAY_SEC)
            continue
        if status == 200:
            break
        last_err = f"TikTok embed HTTP {status}"
        if attempt < TT_FETCH_RETRIES:
            time.sleep(TT_RETRY_DELAY_SEC)
    else:
        raise PlatformError(last_err)

    html = body.decode("utf-8", errors="replace")
    items = parse_tiktok_embed(html)
    if not items:
        raise PlatformError(
            "TikTok embed page returned zero video IDs; left existing seen IDs unchanged"
        )
    return items


def record_tiktok_failure(state: dict | None, err: str) -> dict | None:
    """Increment consecutive TikTok failures without touching seen IDs."""
    if state is None:
        return None
    current = dict(state)
    tt = dict(current.get("tiktok") or {}) if isinstance(current.get("tiktok"), dict) else {}
    try:
        fails = int(tt.get("consecutive_failures") or 0) + 1
    except (TypeError, ValueError):
        fails = 1
    tt["consecutive_failures"] = fails
    tt["updated_at"] = now_ts()
    current["tiktok"] = tt
    save_state(current)
    if fails >= TT_FAIL_ALERT_AFTER and fails % TT_FAIL_ALERT_AFTER == 0:
        log_error(
            f"TikTok embed method looks broken: {fails} consecutive failures "
            f"(last error: {err}). Existing TikTok seen IDs were not reset."
        )
    return current


def _parse_state_time(value) -> datetime | None:
    """Parse a state timestamp into Asia/Taipei, or a unix seconds value."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), TZ)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    suffixes = (" Asia/Taipei", " CST", "")
    for suffix in suffixes:
        if suffix and not text.endswith(suffix):
            continue
        candidate = text[: -len(suffix)] if suffix else text
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=TZ)
            except ValueError:
                continue
    return None


def _http_get_json(url: str) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        return 0, None, f"request failed: {getattr(e, 'reason', e)}"
    except (TimeoutError, OSError) as e:
        return 0, None, f"request failed: {e}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return status, None, f"HTTP {status} non-JSON response"
    if not isinstance(data, dict):
        return status, None, f"HTTP {status} response is not an object"
    return status, data, ""


def _write_secret_file(path: str, value: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_threads_secret(new_token: str) -> tuple[bool, str]:
    """Write the refreshed Threads token back to the THREADS_TOKEN repo secret.

    The value is passed to gh on stdin only, never on the command line or in logs.
    """
    pat = os.environ.get(GH_SECRETS_PAT_ENV) or ""
    if not pat:
        return False, f"{GH_SECRETS_PAT_ENV} is not set"
    _register_secret(pat)
    env = dict(os.environ)
    env["GH_TOKEN"] = pat
    try:
        res = subprocess.run(
            ["gh", "secret", "set", THREADS_TOKEN_ENV, "--repo", GITHUB_REPO],
            input=new_token,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, redact(str(e), new_token, pat)
    if res.returncode != 0:
        return False, redact((res.stderr or res.stdout).strip()[:300], new_token, pat)
    return True, ""


def debug_threads_token(token: str) -> dict:
    params = {"input_token": token, "access_token": token}
    url = THREADS_DEBUG_URL + "?" + urllib.parse.urlencode(params)
    status, data, err = _http_get_json(url)
    if err or data is None:
        raise PlatformError(f"Threads debug_token failed: {err or f'HTTP {status}'}")
    if data.get("error") and not data.get("data"):
        err_obj = data.get("error") or {}
        raise PlatformError(
            f"Threads debug_token error HTTP {status} code={err_obj.get('code', '')} "
            f"type={err_obj.get('type', '')}: {err_obj.get('message', '')}"
        )
    info = data.get("data")
    if not isinstance(info, dict):
        raise PlatformError("Threads debug_token response missing data")
    return info


def maybe_refresh_threads_token(
    token: str,
    state: dict,
    *,
    dry_run: bool,
    show_latest: bool,
) -> tuple[str, dict]:
    """Refresh the long-lived token about every 7 days, once it is more than 24h old."""
    current = dict(state or {})
    th = (
        dict(current.get("threads") or {})
        if isinstance(current.get("threads"), dict)
        else {}
    )
    now = datetime.now(TZ)
    last_refresh = _parse_state_time(th.get("last_refresh_at"))
    expires_at = _parse_state_time(th.get("token_expires_at"))
    discovered = False

    if last_refresh is None or expires_at is None:
        try:
            info = debug_threads_token(token)
        except PlatformError as e:
            if last_refresh is None:
                if True:
                    append_log(
                        RUN_LOG,
                        f"{now_ts()} threads token refresh skipped "
                        f"(could not determine token age: {e})",
                    )
                    current["threads"] = th
                    return token, current
        else:
            if info.get("is_valid") is False:
                log_error(
                    "Threads access token debug_token reports is_valid=false",
                    token,
                )
            if last_refresh is None and info.get("issued_at") is not None:
                last_refresh = _parse_state_time(info.get("issued_at"))
                discovered = True
            if expires_at is None and info.get("expires_at") is not None:
                expires_at = _parse_state_time(info.get("expires_at"))
                discovered = True

        if last_refresh is not None and not th.get("last_refresh_at"):
            th["last_refresh_at"] = (
                last_refresh.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"
            )
            discovered = True
        if expires_at is not None and not th.get("token_expires_at"):
            th["token_expires_at"] = (
                expires_at.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"
            )
            discovered = True
        current["threads"] = th
        if discovered and not dry_run and not show_latest:
            save_state(current)

    if last_refresh is None:
        current["threads"] = th
        return token, current

    age_sec = (now - last_refresh).total_seconds()
    remaining_sec = (expires_at - now).total_seconds() if expires_at else None
    due = (
        age_sec >= THREADS_MIN_AGE_BEFORE_REFRESH_SEC
        and age_sec >= THREADS_REFRESH_AFTER_SEC
    )
    in_danger = (
        remaining_sec is not None
        and remaining_sec <= THREADS_EXPIRY_WARN_WITHIN_SEC
        and age_sec >= THREADS_MIN_AGE_BEFORE_REFRESH_SEC
    )
    if in_danger and not due:
        last_attempt = _parse_state_time(th.get("last_refresh_attempt_at"))
        if last_attempt is None or (now - last_attempt).total_seconds() >= 24 * 3600:
            due = True

    if not due:
        current["threads"] = th
        return token, current

    if dry_run or show_latest:
        append_log(
            RUN_LOG,
            f"{now_ts()} threads skipped token refresh during dry-run/show-latest "
            "(would refresh)",
        )
        current["threads"] = th
        return token, current

    th["last_refresh_attempt_at"] = now_ts()

    def _fail(msg: str) -> tuple[str, dict]:
        try:
            fails = int(th.get("consecutive_refresh_failures") or 0) + 1
        except (TypeError, ValueError):
            fails = 1
        th["consecutive_refresh_failures"] = fails
        current["threads"] = th
        save_state(current)
        log_error(msg, token)
        if remaining_sec is not None and remaining_sec <= THREADS_EXPIRY_WARN_WITHIN_SEC:
            last_warn = _parse_state_time(th.get("last_expiry_warning_at"))
            if last_warn is None or (now - last_warn).total_seconds() >= 24 * 3600:
                days = max(0.0, remaining_sec / 86400)
                expiry_s = (
                    expires_at.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"
                    if expires_at
                    else "unknown"
                )
                log_error(
                    f"WARNING: Threads access token expires in {days:.1f} days "
                    f"(by {expiry_s}) and refresh keeps failing "
                    f"({fails} consecutive failures). Existing seen IDs were not reset. "
                    "A new token will need to be generated if refresh cannot succeed "
                    "before expiry."
                )
                th["last_expiry_warning_at"] = now_ts()
                current["threads"] = th
                save_state(current)
        return token, current

    params = {"grant_type": "th_refresh_token", "access_token": token}
    url = THREADS_REFRESH_URL + "?" + urllib.parse.urlencode(params)
    status, data, err = _http_get_json(url)
    if err or data is None:
        return _fail(f"Threads token refresh failed: {err or f'HTTP {status}'}")
    if data.get("error") or status >= 400:
        err_obj = data.get("error") or {}
        return _fail(
            f"Threads token refresh failed HTTP {status} code={err_obj.get('code', '')} "
            f"type={err_obj.get('type', '')}: {err_obj.get('message', data)}"
        )
    new_token = data.get("access_token")
    if not isinstance(new_token, str) or not new_token.strip():
        return _fail("Threads token refresh returned no access_token")
    new_token = "".join(ch for ch in new_token.strip() if ch not in ("\r", "\n", " "))
    expires_in = data.get("expires_in")
    try:
        expires_in_sec = int(expires_in)
    except (TypeError, ValueError):
        expires_in_sec = 60 * 24 * 3600
    _register_secret(new_token)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Mask the new value in all later log output of this job.
        print(f"::add-mask::{new_token}", flush=True)
    ok, why = update_threads_secret(new_token)
    if not ok:
        return _fail(
            f"Threads token refresh succeeded but updating the THREADS_TOKEN secret failed: {why}"
        )
    new_expiry = now + timedelta(seconds=expires_in_sec)
    th["last_refresh_at"] = now_ts()
    th["token_expires_at"] = (
        new_expiry.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"
    )
    th["consecutive_refresh_failures"] = 0
    current["threads"] = th
    save_state(current)
    append_log(
        RUN_LOG,
        f"{now_ts()} threads token refreshed expires_at={th['token_expires_at']}",
    )
    return new_token, current


def fetch_threads_items(token: str) -> list[dict]:
    params = {
        "fields": "id,text,permalink,timestamp,media_type",
        "limit": "10",
        "access_token": token,
    }
    url = THREADS_GRAPH_BASE + "?" + urllib.parse.urlencode(params)
    status, data, err = _http_get_json(url)
    if err or data is None:
        raise PlatformError(f"Threads API request failed: {err or f'HTTP {status}'}")
    if data.get("error"):
        err_obj = data.get("error") or {}
        raise PlatformError(
            f"Threads API error HTTP {status} code={err_obj.get('code', '')} "
            f"type={err_obj.get('type', '')}: {err_obj.get('message', data)}"
        )
    items = data.get("data")
    if not isinstance(items, list):
        raise PlatformError("Threads API response missing data list")
    return items


def _sort_oldest_first(items: list[dict]) -> list[dict]:
    def key(item: dict):
        return str(item.get("timestamp") or "")

    return sorted(items, key=key)


def shorten_caption(text: str, max_chars: int = 60) -> str:
    """Keep only the first one or two sentences of a caption (no AI)."""
    import re
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if not re.sub(r"[#@][^\s#@]+", "", ln).strip():
            continue
        lines.append(re.sub(r"\s*#[^\s#]+", "", ln).strip())
    sentences = []
    for ln in lines:
        parts = re.findall(r"[^。！？!?…~～]+[。！？!?…~～]*", ln)
        sentences.extend(p.strip() for p in parts if p.strip())
    if not sentences:
        return ""
    out = sentences[0]
    if len(out) > max_chars:
        return out[:max_chars].rstrip() + "…"
    if len(sentences) > 1 and len(out) + len(sentences[1]) + 1 <= max_chars:
        sep = "" if out[-1] in "。！？!?…~～" else " "
        out = out + sep + sentences[1]
    return out


def format_message(
    platform: str,
    kind: str,
    text: str | None,
    link: str,
) -> str:
    lines = [f"🔥 貴一在 {platform} 發新{kind}了！"]
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    text = shorten_caption(text)
    if text:
        lines.append(text)
    lines.append(f"👉 {link}")
    msg = "\n".join(lines)
    # Never include em dashes (or en dashes) in the outgoing message.
    return msg.replace("\u2014", "-").replace("\u2013", "-")


def format_ig_message(item: dict) -> str:
    media_type = item.get("media_type") or ""
    kind = "影片" if media_type == "VIDEO" else "貼文"
    caption = item.get("caption")
    permalink = item.get("permalink") or ""
    return format_message("Instagram", kind, caption, permalink)


def format_yt_message(item: dict) -> str:
    return format_message(
        "YouTube",
        "影片",
        item.get("title") or "",
        item.get("url") or "",
    )


def format_tt_message(item: dict) -> str:
    return format_message(
        "TikTok",
        "影片",
        item.get("caption") or "",
        item.get("url") or "",
    )


def format_th_message(item: dict) -> str:
    return format_message(
        "Threads",
        "貼文",
        item.get("text") or "",
        item.get("permalink") or "",
    )


class PostRejected(Exception):
    """Discord answered with an error status: the message was NOT posted."""


class PostUncertain(Exception):
    """No clear answer from Discord (timeout / network): it may have been posted."""


def post_discord(webhook: str, content: str, *secrets: str) -> None:
    url = webhook.rstrip("?&") + "?wait=true"
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            # wait=true: treat any 2xx as success.
            if resp.status < 200 or resp.status >= 300:
                raise PostRejected(f"Webhook failure HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:300]
        raise PostRejected(redact(f"Webhook failure HTTP {e.code}: {body_txt}", *secrets))
    except urllib.error.URLError as e:
        raise PostUncertain(redact(f"Webhook request failed: {getattr(e, 'reason', e)}", *secrets))
    except (TimeoutError, OSError) as e:
        raise PostUncertain(redact(f"Webhook request failed: {e}", *secrets))


def _seen_set_from_ids(ids: list | None) -> set[str]:
    return set(ids or [])


def _merge_ids(existing: list[str], ids: list[str]) -> list[str]:
    seen = set(existing)
    merged = existing[:]
    for mid in ids:
        if mid not in seen:
            merged.append(mid)
            seen.add(mid)
    return merged


def _process_items(
    *,
    platform_label: str,
    items_with_id: list[dict],
    seen_ids: list[str] | None,
    baseline: bool,
    dry_run: bool,
    show_latest: bool,
    formatter,
    webhook: str,
    secrets: tuple[str, ...],
    on_baseline,
    on_posted,
    on_extras,
) -> None:
    """Shared baseline / dry-run / post flow. Callbacks persist state."""
    if show_latest:
        if not items_with_id:
            print(f"[{platform_label}] No media items returned.")
            append_log(RUN_LOG, f"{now_ts()} {platform_label} show_latest items=0")
            return
        latest = items_with_id[0]
        print(formatter(latest))
        append_log(RUN_LOG, f"{now_ts()} {platform_label} show_latest id_present=1")
        return

    if baseline or seen_ids is None:
        ids = [str(it["id"]) for it in items_with_id]
        if dry_run:
            print(
                f"({platform_label} dry-run) would baseline {len(ids)} item(s) "
                "and post nothing"
            )
            append_log(
                RUN_LOG,
                f"{now_ts()} {platform_label} dry-run baseline "
                f"recorded={len(ids)} new_posts=0",
            )
            return
        new_ids = on_baseline(ids)
        reason = "baseline" if baseline else "baseline_missing_state"
        append_log(
            RUN_LOG,
            f"{now_ts()} {platform_label} {reason} recorded={len(ids)} "
            f"seen_total={len(new_ids)} new_posts=0",
        )
        return

    seen = _seen_set_from_ids(seen_ids)
    unseen = [it for it in items_with_id if str(it["id"]) not in seen]
    unseen = _sort_oldest_first(unseen)

    to_post = unseen[:MAX_POSTS_PER_RUN]
    extras = unseen[MAX_POSTS_PER_RUN:]

    if dry_run:
        for it in to_post:
            print(formatter(it))
            print("---")
        if extras:
            print(
                f"({platform_label} dry-run) {len(extras)} extra unseen item(s) "
                "would be marked seen without posting (cap 5)"
            )
        append_log(
            RUN_LOG,
            f"{now_ts()} {platform_label} dry-run new_posts={len(to_post)} "
            f"skipped_extras={len(extras)}",
        )
        return

    if extras:
        append_log(
            RUN_LOG,
            f"{now_ts()} {platform_label} cap: {len(unseen)} unseen, "
            f"posting {len(to_post)}, marking {len(extras)} extra(s) "
            "seen without posting",
        )

    if to_post and not webhook:
        die("Webhook file is empty", *secrets)

    posted = 0
    for it in to_post:
        mid = str(it["id"])
        # Dedup guarantee (at-most-once): record the ID as seen and push state.json
        # to the repo BEFORE posting. If that push fails, do not post at all.
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                snapshot = f.read()
        except OSError:
            snapshot = None
        on_posted(mid)
        if not commit_state(f"state: {platform_label} {mid} marked before posting"):
            die(
                f"{platform_label}: could not push state before posting {mid}; "
                "nothing was posted (will retry next run)",
                *secrets,
            )
        try:
            post_discord(webhook, formatter(it), *secrets)
        except PostRejected as e:
            # Discord definitely did not post it: un-mark so a later run can retry.
            if snapshot is not None:
                with open(STATE_PATH, "w", encoding="utf-8") as f:
                    f.write(snapshot)
                commit_state(f"state: {platform_label} {mid} unmarked (Discord rejected)")
            die(f"{platform_label}: {e}", *secrets)
        except PostUncertain as e:
            # It may have been posted. Keep it marked so it can never double-post.
            die(
                f"{platform_label}: {e}. {mid} stays marked as seen to avoid a "
                "double post; check #社群公告 and post it by hand if it is missing.",
                *secrets,
            )
        posted += 1

    extra_ids = [str(it["id"]) for it in extras]
    if extra_ids:
        on_extras(extra_ids)

    append_log(
        RUN_LOG,
        f"{now_ts()} {platform_label} new_posts={posted} "
        f"skipped_extras={len(extras)}",
    )


def run_instagram(
    token: str,
    webhook: str,
    state: dict | None,
    baseline: bool,
    dry_run: bool,
    show_latest: bool,
) -> dict | None:
    items = fetch_media(token)
    items_with_id = [it for it in items if it.get("id")]
    current_state = dict(state) if state else None

    def persist(st: dict) -> dict:
        save_state(st)
        return st

    def on_baseline(ids: list[str]) -> list[str]:
        nonlocal current_state
        existing = list((current_state or {}).get("seen_ids") or [])
        merged = _merge_ids(existing, ids)
        current_state = dict(current_state or {})
        current_state["seen_ids"] = merged
        persist(current_state)
        return merged

    def on_posted(mid: str) -> None:
        nonlocal current_state
        current_state = dict(current_state or {})
        current_state["seen_ids"] = _merge_ids(
            list(current_state.get("seen_ids") or []), [mid]
        )
        persist(current_state)

    def on_extras(extra_ids: list[str]) -> None:
        nonlocal current_state
        current_state = dict(current_state or {})
        current_state["seen_ids"] = _merge_ids(
            list(current_state.get("seen_ids") or []), extra_ids
        )
        persist(current_state)

    seen_ids = None if current_state is None else list(
        current_state.get("seen_ids") or []
    )
    # Instagram already has a state file; missing seen_ids list is treated
    # as baseline only when the whole state file was missing.
    _process_items(
        platform_label="instagram",
        items_with_id=items_with_id,
        seen_ids=seen_ids,
        baseline=baseline,
        dry_run=dry_run,
        show_latest=show_latest,
        formatter=format_ig_message,
        webhook=webhook,
        secrets=(token, webhook),
        on_baseline=on_baseline,
        on_posted=on_posted,
        on_extras=on_extras,
    )
    return current_state


def run_youtube(
    webhook: str,
    state: dict | None,
    baseline: bool,
    dry_run: bool,
    show_latest: bool,
    *secrets: str,
) -> dict | None:
    items = fetch_youtube_items()
    items_with_id = [it for it in items if it.get("id")]
    current_state = dict(state) if state else {}
    yt_state = current_state.get("youtube")
    yt_present = isinstance(yt_state, dict)
    seen_ids = None if not yt_present else list(yt_state.get("seen_ids") or [])

    def _write_yt(ids: list[str]) -> None:
        nonlocal current_state
        current_state = dict(current_state)
        current_state["youtube"] = {
            "seen_ids": ids,
            "updated_at": now_ts(),
        }
        save_state(current_state)

    def on_baseline(ids: list[str]) -> list[str]:
        existing = [] if seen_ids is None else list(seen_ids)
        merged = _merge_ids(existing, ids)
        _write_yt(merged)
        return merged

    def on_posted(mid: str) -> None:
        existing = list((current_state.get("youtube") or {}).get("seen_ids") or [])
        _write_yt(_merge_ids(existing, [mid]))

    def on_extras(extra_ids: list[str]) -> None:
        existing = list((current_state.get("youtube") or {}).get("seen_ids") or [])
        _write_yt(_merge_ids(existing, extra_ids))

    _process_items(
        platform_label="youtube",
        items_with_id=items_with_id,
        seen_ids=seen_ids,
        baseline=baseline,
        dry_run=dry_run,
        show_latest=show_latest,
        formatter=format_yt_message,
        webhook=webhook,
        secrets=secrets,
        on_baseline=on_baseline,
        on_posted=on_posted,
        on_extras=on_extras,
    )
    return current_state


def run_tiktok(
    webhook: str,
    state: dict | None,
    baseline: bool,
    dry_run: bool,
    show_latest: bool,
    *secrets: str,
) -> dict | None:
    items = fetch_tiktok_items()
    items_with_id = [it for it in items if it.get("id")]
    current_state = dict(state) if state else {}
    tt_state = current_state.get("tiktok")
    # A failures-only tiktok object (no seen_ids) must still baseline,
    # never treat a missing seen set as "already empty".
    tt_baselined = isinstance(tt_state, dict) and "seen_ids" in tt_state
    seen_ids = None if not tt_baselined else list(tt_state.get("seen_ids") or [])

    def _write_tt(ids: list[str]) -> None:
        nonlocal current_state
        current_state = dict(current_state)
        prev = current_state.get("tiktok") if isinstance(current_state.get("tiktok"), dict) else {}
        current_state["tiktok"] = {
            "seen_ids": ids,
            "updated_at": now_ts(),
            "consecutive_failures": 0,
        }
        # Preserve any extra tiktok keys except the ones we rewrite.
        for key, value in (prev or {}).items():
            if key not in ("seen_ids", "updated_at", "consecutive_failures"):
                current_state["tiktok"][key] = value
        save_state(current_state)

    def on_baseline(ids: list[str]) -> list[str]:
        existing = [] if seen_ids is None else list(seen_ids)
        merged = _merge_ids(existing, ids)
        _write_tt(merged)
        return merged

    def on_posted(mid: str) -> None:
        existing = list((current_state.get("tiktok") or {}).get("seen_ids") or [])
        _write_tt(_merge_ids(existing, [mid]))

    def on_extras(extra_ids: list[str]) -> None:
        existing = list((current_state.get("tiktok") or {}).get("seen_ids") or [])
        _write_tt(_merge_ids(existing, extra_ids))

    _process_items(
        platform_label="tiktok",
        items_with_id=items_with_id,
        seen_ids=seen_ids,
        baseline=baseline,
        dry_run=dry_run,
        show_latest=show_latest,
        formatter=format_tt_message,
        webhook=webhook,
        secrets=secrets,
        on_baseline=on_baseline,
        on_posted=on_posted,
        on_extras=on_extras,
    )
    return current_state


def run_threads(
    token: str,
    webhook: str,
    state: dict | None,
    baseline: bool,
    dry_run: bool,
    show_latest: bool,
    *secrets: str,
) -> dict | None:
    current_state = dict(state) if state else {}
    token, current_state = maybe_refresh_threads_token(
        token, current_state, dry_run=dry_run, show_latest=show_latest
    )
    items = fetch_threads_items(token)
    all_items = [it for it in items if it.get("id")]
    skipped_reposts = [
        it for it in all_items if (it.get("media_type") or "") == THREADS_REPOST_TYPE
    ]
    announceable = [
        it for it in all_items if (it.get("media_type") or "") != THREADS_REPOST_TYPE
    ]
    # Posts without a permalink cannot use the approved message format.
    # Leave them unseen so a later response that includes a link can still notify.
    missing_link = [
        it for it in announceable if not str(it.get("permalink") or "").strip()
    ]
    items_with_id = [
        it for it in announceable if str(it.get("permalink") or "").strip()
    ]
    if missing_link:
        append_log(
            RUN_LOG,
            f"{now_ts()} threads skipped_no_permalink={len(missing_link)}",
        )
    if skipped_reposts:
        append_log(
            RUN_LOG,
            f"{now_ts()} threads skipped_reposts={len(skipped_reposts)}",
        )

    th_state = current_state.get("threads")
    # A refresh-metadata-only threads object (no seen_ids) must still baseline.
    th_baselined = isinstance(th_state, dict) and "seen_ids" in th_state
    seen_ids = None if not th_baselined else list(th_state.get("seen_ids") or [])

    def _write_th(ids: list[str] | None) -> None:
        nonlocal current_state
        current_state = dict(current_state)
        prev = (
            current_state.get("threads")
            if isinstance(current_state.get("threads"), dict)
            else {}
        )
        out = dict(prev or {})
        if ids is not None:
            out["seen_ids"] = ids
        out["updated_at"] = now_ts()
        current_state["threads"] = out
        save_state(current_state)

    def _with_reposts(ids: list[str]) -> list[str]:
        extra = [str(it["id"]) for it in skipped_reposts]
        return _merge_ids(list(ids), extra)

    def on_baseline(ids: list[str]) -> list[str]:
        existing = [] if seen_ids is None else list(seen_ids)
        merged = _with_reposts(_merge_ids(existing, ids))
        _write_th(merged)
        return merged

    def on_posted(mid: str) -> None:
        existing = list((current_state.get("threads") or {}).get("seen_ids") or [])
        _write_th(_merge_ids(existing, [mid]))

    def on_extras(extra_ids: list[str]) -> None:
        existing = list((current_state.get("threads") or {}).get("seen_ids") or [])
        _write_th(_with_reposts(_merge_ids(existing, extra_ids)))

    _process_items(
        platform_label="threads",
        items_with_id=items_with_id,
        seen_ids=seen_ids,
        baseline=baseline,
        dry_run=dry_run,
        show_latest=show_latest,
        formatter=format_th_message,
        webhook=webhook,
        secrets=secrets + (token,),
        on_baseline=on_baseline,
        on_posted=on_posted,
        on_extras=on_extras,
    )

    # After a live non-baseline run, still mark current reposts as seen.
    if not dry_run and not show_latest and not (baseline or seen_ids is None):
        if skipped_reposts:
            existing = list((current_state.get("threads") or {}).get("seen_ids") or [])
            merged = _with_reposts(existing)
            if merged != existing:
                _write_th(merged)

    return current_state


def run(baseline: bool, dry_run: bool, show_latest: bool) -> int:
    try:
        token = _read_secret_env(TOKEN_ENV)
    except OSError as e:
        die(f"Failed to read IG token: {e}")
    if not token:
        die("IG token is empty")
    _register_secret(token)

    threads_token = ""
    threads_token_error = None
    try:
        threads_token = _read_secret_env(THREADS_TOKEN_ENV)
    except OSError as e:
        threads_token_error = f"Failed to read Threads token: {e}"
    else:
        _register_secret(threads_token)
        if not threads_token:
            threads_token_error = "Threads token is empty"

    webhook = ""
    need_webhook = not dry_run and not baseline and not show_latest
    if need_webhook:
        try:
            webhook = _read_secret_env(WEBHOOK_ENV)
        except OSError as e:
            die(f"Failed to read webhook: {e}", token, threads_token)
        _register_secret(webhook)
        if not webhook:
            die("Webhook is empty", token, threads_token)

    if os.environ.get(GH_SECRETS_PAT_ENV):
        _register_secret(os.environ.get(GH_SECRETS_PAT_ENV, "").strip())

    if not show_latest:
        check_ig_token_expiry(token)

    state = load_state()
    errors: list[str] = []

    try:
        state = run_instagram(
            token, webhook, state, baseline, dry_run, show_latest
        )
    except PlatformError as e:
        log_error(str(e), token, webhook)
        errors.append(str(e))
    # Re-load after IG so YouTube save cannot clobber a concurrent? single process.
    state = load_state() if os.path.exists(STATE_PATH) else state

    try:
        run_youtube(webhook, state, baseline, dry_run, show_latest, token, webhook)
    except PlatformError as e:
        log_error(str(e), token, webhook)
        errors.append(str(e))

    state = load_state() if os.path.exists(STATE_PATH) else state

    try:
        run_tiktok(webhook, state, baseline, dry_run, show_latest, token, webhook)
    except PlatformError as e:
        log_error(str(e), token, webhook)
        errors.append(str(e))
        if not dry_run and not show_latest:
            state = load_state() if os.path.exists(STATE_PATH) else state
            record_tiktok_failure(state, str(e))

    state = load_state() if os.path.exists(STATE_PATH) else state

    if threads_token_error:
        log_error(threads_token_error, token, webhook)
        errors.append(threads_token_error)
    else:
        try:
            run_threads(
                threads_token,
                webhook,
                state,
                baseline,
                dry_run,
                show_latest,
                token,
                threads_token,
                webhook,
            )
        except PlatformError as e:
            log_error(str(e), token, threads_token, webhook)
            errors.append(str(e))

    if not dry_run and not show_latest:
        if not commit_state("state: update after run"):
            errors.append("final state push failed")
    if errors:
        sys.exit(1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Instagram + YouTube + TikTok + Threads new-post notifier"
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Record all current media IDs as seen and post nothing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages that would be sent, without posting or updating state",
    )
    parser.add_argument(
        "--show-latest-format",
        action="store_true",
        help="Print the formatted message for the latest item on each platform and exit",
    )
    args = parser.parse_args(argv)
    if os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        args.dry_run = True
    if args.dry_run and args.baseline:
        print("Use only one of --baseline or --dry-run", file=sys.stderr)
        return 2
    try:
        return run(
            baseline=args.baseline,
            dry_run=args.dry_run,
            show_latest=args.show_latest_format,
        )
    except SystemExit:
        raise
    except Exception as e:
        # Last-resort handler; never include secrets (redact() covers all registered ones).
        die(f"Unhandled error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
