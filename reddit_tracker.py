#!/usr/bin/env python3
"""Watch subreddits for new posts and send each one to WhatsApp.

All configuration comes from environment variables (or a .env file); see
.env.example and README.md.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol

import requests

log = logging.getLogger("reddit_tracker")

WHATSAPP_MAX_CHARS = 4000  # WhatsApp caps a message at 4096 characters
MAX_SEEN_IDS = 5000
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


# --------------------------------------------------------------------------- #
# Reddit
# --------------------------------------------------------------------------- #


@dataclass
class Post:
    id: str
    subreddit: str
    title: str
    author: str
    permalink: str
    created_utc: float
    body: str = ""
    flair: Optional[str] = None

    @property
    def short_url(self) -> str:
        return f"https://redd.it/{self.id}"


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited by Reddit, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class RedditClient:
    """Fetches the newest posts across several subreddits in a single request.

    With OAuth credentials it uses oauth.reddit.com (about 100 requests per
    minute). Without them it tries the public JSON listing and, if Reddit
    blocks that (common from cloud/datacenter IPs), switches to the public
    RSS feed for the rest of the run.
    """

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(
        self,
        user_agent: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.client_id = client_id
        self.client_secret = client_secret
        self.clock = clock
        self.mode = "oauth" if client_id and client_secret else "json"
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    # -- public -------------------------------------------------------------

    def fetch_new(self, subreddits: Iterable[str], limit: int = 100) -> list[Post]:
        multi = "+".join(subreddits)
        if self.mode == "oauth":
            return self._fetch_oauth(multi, limit)
        if self.mode == "json":
            try:
                return self._fetch_json(multi, limit)
            except _Blocked:
                log.warning(
                    "Reddit blocked the anonymous JSON API from this IP; "
                    "switching to the RSS feed. Set REDDIT_CLIENT_ID/SECRET "
                    "for faster, more reliable polling."
                )
                self.mode = "rss"
        return self._fetch_rss(multi, limit)

    # -- transports ---------------------------------------------------------

    def _fetch_oauth(self, multi: str, limit: int) -> list[Post]:
        resp = self._get(
            f"https://oauth.reddit.com/r/{multi}/new",
            params={"limit": limit, "raw_json": 1},
            headers={"Authorization": f"bearer {self._access_token()}"},
        )
        if resp.status_code == 401:  # token revoked or expired early
            self._token = None
            resp = self._get(
                f"https://oauth.reddit.com/r/{multi}/new",
                params={"limit": limit, "raw_json": 1},
                headers={"Authorization": f"bearer {self._access_token()}"},
            )
        resp.raise_for_status()
        return _parse_listing(resp.json())

    def _fetch_json(self, multi: str, limit: int) -> list[Post]:
        resp = self._get(
            f"https://www.reddit.com/r/{multi}/new.json",
            params={"limit": limit, "raw_json": 1},
        )
        if resp.status_code == 403 or "json" not in resp.headers.get("Content-Type", ""):
            raise _Blocked()
        resp.raise_for_status()
        return _parse_listing(resp.json())

    def _fetch_rss(self, multi: str, limit: int) -> list[Post]:
        resp = self._get(
            f"https://www.reddit.com/r/{multi}/new/.rss", params={"limit": limit}
        )
        resp.raise_for_status()
        return _parse_atom(resp.text)

    # -- helpers ------------------------------------------------------------

    def _get(self, url: str, **kwargs) -> requests.Response:
        resp = self.session.get(url, timeout=20, **kwargs)
        if resp.status_code == 429:
            raise RateLimited(_header_float(resp, "x-ratelimit-reset", 60.0))
        return resp

    def _access_token(self) -> str:
        if self._token and self.clock() < self._token_expires_at:
            return self._token
        resp = self.session.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Reddit OAuth failed: {data}")
        self._token = data["access_token"]
        # Refresh a minute before Reddit says it expires.
        self._token_expires_at = self.clock() + float(data.get("expires_in", 3600)) - 60
        return self._token

    def min_poll_interval(self) -> float:
        """Rough lower bound that keeps us inside Reddit's rate limits."""
        return 2.0 if self.mode == "oauth" else 60.0


class _Blocked(Exception):
    pass


def _header_float(resp: requests.Response, name: str, default: float) -> float:
    try:
        return float(resp.headers.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_listing(data: dict) -> list[Post]:
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        posts.append(
            Post(
                id=d["id"],
                subreddit=d.get("subreddit", ""),
                title=d.get("title", ""),
                author=d.get("author", "[deleted]"),
                permalink="https://www.reddit.com" + d.get("permalink", ""),
                created_utc=float(d.get("created_utc", 0)),
                body=d.get("selftext", "") or "",
                flair=d.get("link_flair_text") or None,
            )
        )
    return posts


def _parse_atom(text: str) -> list[Post]:
    root = ET.fromstring(text)
    posts = []
    for entry in root.findall("a:entry", ATOM_NS):
        raw_id = entry.findtext("a:id", "", ATOM_NS)
        category = entry.find("a:category", ATOM_NS)
        link = entry.find("a:link", ATOM_NS)
        published = entry.findtext("a:published", "", ATOM_NS)
        content = entry.findtext("a:content", "", ATOM_NS)
        author = entry.findtext("a:author/a:name", "", ATOM_NS)
        posts.append(
            Post(
                id=raw_id.removeprefix("t3_"),
                subreddit=category.get("term", "") if category is not None else "",
                title=entry.findtext("a:title", "", ATOM_NS),
                author=author.removeprefix("/u/") or "[deleted]",
                permalink=link.get("href", "") if link is not None else "",
                created_utc=_parse_iso(published),
                body=_strip_html(content),
            )
        )
    return posts


def _parse_iso(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class CallMeBotNotifier:
    """Free personal WhatsApp messages via https://www.callmebot.com."""

    URL = "https://api.callmebot.com/whatsapp.php"

    def __init__(self, phone: str, apikey: str, session: Optional[requests.Session] = None):
        self.phone = phone
        self.apikey = apikey
        self.session = session or requests.Session()

    def send(self, text: str) -> None:
        resp = self.session.get(
            self.URL,
            params={"phone": self.phone, "text": text, "apikey": self.apikey},
            timeout=30,
        )
        # CallMeBot answers 200 with an HTML page even for a bad API key.
        if resp.status_code != 200 or "apikey is invalid" in resp.text.lower():
            raise RuntimeError(f"CallMeBot error {resp.status_code}: {_strip_html(resp.text)[:200]}")


class TwilioNotifier:
    """WhatsApp via Twilio's Messages API (sandbox or an approved sender)."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        session: Optional[requests.Session] = None,
    ):
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        self.auth = (account_sid, auth_token)
        self.from_number = _whatsapp_addr(from_number)
        self.to_number = _whatsapp_addr(to_number)
        self.session = session or requests.Session()

    def send(self, text: str) -> None:
        resp = self.session.post(
            self.url,
            auth=self.auth,
            data={"From": self.from_number, "To": self.to_number, "Body": text},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"Twilio error {resp.status_code}: {resp.text[:300]}")


class ConsoleNotifier:
    """Prints messages instead of sending them; handy for trying things out."""

    def send(self, text: str) -> None:
        print("-" * 60 + "\n" + text + "\n" + "-" * 60, flush=True)


def _whatsapp_addr(number: str) -> str:
    number = number.strip()
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #


def format_post(post: Post) -> str:
    title = post.title if len(post.title) <= 300 else post.title[:297] + "..."
    meta = f"u/{post.author}" + (f" · {post.flair}" if post.flair else "")
    return f"🆕 r/{post.subreddit}\n*{title}*\n{meta}\n{post.short_url}"


def format_digest(posts: list[Post]) -> str:
    header = f"🆕 {len(posts)} new posts"
    lines = [header]
    for i, post in enumerate(posts):
        title = post.title if len(post.title) <= 120 else post.title[:117] + "..."
        entry = f"\n• r/{post.subreddit}: {title}\n  {post.short_url}"
        if sum(map(len, lines)) + len(entry) > WHATSAPP_MAX_CHARS - 40:
            lines.append(f"\n…and {len(posts) - i} more")
            break
        lines.append(entry)
    return "".join(lines)


class SeenStore:
    """Remembers which post IDs were already handled, across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: list[str] = []
        self._set: set[str] = set()
        self.existed = path.exists()
        if self.existed:
            try:
                self.ids = json.loads(path.read_text())["seen"]
            except (ValueError, KeyError, OSError) as exc:
                log.warning("Could not read %s (%s); starting fresh", path, exc)
                self.ids = []
            self._set = set(self.ids)

    def __contains__(self, post_id: str) -> bool:
        return post_id in self._set

    def add(self, post_id: str) -> None:
        if post_id not in self._set:
            self.ids.append(post_id)
            self._set.add(post_id)

    def save(self) -> None:
        if len(self.ids) > MAX_SEEN_IDS:
            self.ids = self.ids[-MAX_SEEN_IDS:]
            self._set = set(self.ids)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"seen": self.ids}))
        tmp.replace(self.path)


class Tracker:
    def __init__(
        self,
        client: RedditClient,
        notifier: Notifier,
        subreddits: list[str],
        store: SeenStore,
        keywords: Optional[list[str]] = None,
        notify_on_start: bool = False,
        digest_threshold: int = 5,
    ):
        self.client = client
        self.notifier = notifier
        self.subreddits = subreddits
        self.store = store
        self.keywords = [k.lower() for k in (keywords or []) if k]
        self.digest_threshold = digest_threshold
        # On the very first run, treat what's already there as seen so you
        # don't get blasted with the last 100 posts.
        self._seeding = not store.existed and not notify_on_start

    def matches(self, post: Post) -> bool:
        if not self.keywords:
            return True
        haystack = f"{post.title}\n{post.body}".lower()
        return any(k in haystack for k in self.keywords)

    def poll_once(self) -> list[Post]:
        posts = self.client.fetch_new(self.subreddits)
        new = sorted(
            (p for p in posts if p.id not in self.store),
            key=lambda p: p.created_utc,
        )

        if self._seeding:
            for p in new:
                self.store.add(p.id)
            self.store.save()
            self._seeding = False
            log.info("First run: marked %d existing posts as seen", len(new))
            return []

        skipped = [p for p in new if not self.matches(p)]
        wanted = [p for p in new if self.matches(p)]
        for p in skipped:
            self.store.add(p.id)

        sent: list[Post] = []
        try:
            if len(wanted) > self.digest_threshold:
                self.notifier.send(format_digest(wanted))
                sent = wanted
            else:
                for p in wanted:
                    self.notifier.send(format_post(p))
                    sent.append(p)
        except Exception:
            # Unsent posts stay unseen, so they're retried next poll.
            log.exception("Failed to send WhatsApp message")
        finally:
            for p in sent:
                self.store.add(p.id)
                log.info("Notified: r/%s %s %r", p.subreddit, p.id, p.title)
            if new:
                self.store.save()
        return sent

    def run(self, interval: float, stop: threading.Event) -> None:
        log.info(
            "Watching r/%s every %.0fs (mode=%s%s)",
            "+".join(self.subreddits),
            interval,
            self.client.mode,
            f", keywords={self.keywords}" if self.keywords else "",
        )
        failures = 0
        while not stop.is_set():
            wait = interval
            try:
                self.poll_once()
                failures = 0
            except RateLimited as exc:
                log.warning("%s", exc)
                wait = max(interval, exc.retry_after + 1)
            except Exception as exc:  # network blips, Reddit 5xx, etc.
                failures += 1
                wait = min(interval * 2**failures, 600)
                log.warning("Poll failed (%s); retrying in %.0fs", exc, wait)
            wait = max(wait, self.client.min_poll_interval())
            stop.wait(wait)


# --------------------------------------------------------------------------- #
# Config / CLI
# --------------------------------------------------------------------------- #


def _split(value: Optional[str], seps: str = r"[,\s]+") -> list[str]:
    return [p.strip() for p in re.split(seps, value or "") if p.strip()]


def parse_subreddits(value: Optional[str]) -> list[str]:
    subs = []
    for s in _split(value):
        s = re.sub(r"^(/?r/)", "", s, flags=re.I).strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", s):
            raise SystemExit(f"Invalid subreddit name: {s!r}")
        subs.append(s)
    return subs


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting: {name}")
    return value


def build_notifier() -> Notifier:
    provider = os.getenv("WHATSAPP_PROVIDER", "callmebot").strip().lower()
    if provider == "callmebot":
        return CallMeBotNotifier(_require("CALLMEBOT_PHONE"), _require("CALLMEBOT_APIKEY"))
    if provider == "twilio":
        return TwilioNotifier(
            _require("TWILIO_ACCOUNT_SID"),
            _require("TWILIO_AUTH_TOKEN"),
            os.getenv("TWILIO_FROM", "whatsapp:+14155238886"),  # Twilio sandbox
            _require("TWILIO_TO"),
        )
    if provider == "console":
        return ConsoleNotifier()
    raise SystemExit(f"Unknown WHATSAPP_PROVIDER: {provider!r} (use callmebot, twilio or console)")


def build_client() -> RedditClient:
    username = os.getenv("REDDIT_USERNAME", "").strip() or "anonymous"
    user_agent = f"python:reddit-whatsapp-tracker:v1.0 (by /u/{username})"
    return RedditClient(
        user_agent,
        client_id=os.getenv("REDDIT_CLIENT_ID", "").strip() or None,
        client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip() or None,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument("--test-message", action="store_true", help="send a test WhatsApp message and exit")
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    notifier = ConsoleNotifier() if args.dry_run else build_notifier()
    if args.test_message:
        notifier.send("✅ Reddit tracker is connected to WhatsApp.")
        log.info("Test message sent")
        return 0

    subreddits = parse_subreddits(os.getenv("SUBREDDITS"))
    if not subreddits:
        raise SystemExit("Set SUBREDDITS, e.g. SUBREDDITS=buildapcsales,hardwareswap")

    client = build_client()
    default_interval = 15 if client.mode == "oauth" else 60
    interval = float(os.getenv("POLL_INTERVAL_SECONDS", default_interval))

    tracker = Tracker(
        client,
        notifier,
        subreddits,
        SeenStore(Path(os.getenv("STATE_FILE", "state.json"))),
        keywords=_split(os.getenv("KEYWORDS"), seps=","),  # phrases may contain spaces
        notify_on_start=_env_bool("NOTIFY_ON_START"),
        digest_threshold=int(os.getenv("DIGEST_THRESHOLD", "5")),
    )

    if args.once:
        tracker.poll_once()
        return 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    tracker.run(interval, stop)
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
