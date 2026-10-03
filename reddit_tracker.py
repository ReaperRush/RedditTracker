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
from dataclasses import dataclass, field
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
    tags: list[str] = field(default_factory=list)  # matched HIGHLIGHTS labels
    also_in: list[str] = field(default_factory=list)  # cross-posted to these subs

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
        if resp.status_code == 403:
            raise RuntimeError(
                "Reddit is blocking this server's IP address. Create a Reddit app "
                "and set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (see README)."
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
    def send(self, text: str, silent: bool = False) -> None:
        """Deliver text. silent=True asks for no sound/vibration where supported."""
        ...


class CallMeBotNotifier:
    """Free personal WhatsApp messages via https://www.callmebot.com."""

    URL = "https://api.callmebot.com/whatsapp.php"

    def __init__(self, phone: str, apikey: str, session: Optional[requests.Session] = None):
        self.phone = phone
        self.apikey = apikey
        self.session = session or requests.Session()

    def send(self, text: str, silent: bool = False) -> None:
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

    def send(self, text: str, silent: bool = False) -> None:
        resp = self.session.post(
            self.url,
            auth=self.auth,
            data={"From": self.from_number, "To": self.to_number, "Body": text},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"Twilio error {resp.status_code}: {resp.text[:300]}")


class WahaNotifier:
    """WhatsApp through a self-hosted WAHA gateway (https://waha.devlike.pro).

    WAHA logs in to a WhatsApp account the way WhatsApp Web does. Link a
    second number (e.g. WhatsApp Business on a spare SIM) as the sender:
    messages from your own account to yourself don't trigger a notification.
    """

    def __init__(
        self,
        base_url: str,
        to_number: str,
        waha_session: str = "default",
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.url = base_url.rstrip("/") + "/api/sendText"
        to_number = to_number.strip()
        self.chat_id = to_number if "@" in to_number else re.sub(r"\D", "", to_number) + "@c.us"
        self.waha_session = waha_session
        self.headers = {"X-Api-Key": api_key} if api_key else {}
        self.session = session or requests.Session()

    def send(self, text: str, silent: bool = False) -> None:
        resp = self.session.post(
            self.url,
            json={"session": self.waha_session, "chatId": self.chat_id, "text": text},
            headers=self.headers,
            timeout=30,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"WAHA error {resp.status_code}: {resp.text[:300]}")


class TelegramNotifier:
    """Messages from your own Telegram bot (create one with @BotFather)."""

    def __init__(self, bot_token: str, chat_id: str, session: Optional[requests.Session] = None):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send(self, text: str, silent: bool = False) -> None:
        resp = self.session.post(
            self.url,
            json={
                "chat_id": self.chat_id,
                "text": _telegram_html(text),
                "parse_mode": "HTML",
                "disable_notification": silent,
            },
            timeout=30,
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text[:300]}")


def _telegram_html(text: str) -> str:
    """Escape for Telegram's HTML mode, turning WhatsApp-style *bold* lines into <b>."""
    lines = []
    for line in text.split("\n"):
        m = re.fullmatch(r"\*(.+)\*", line)
        lines.append(f"<b>{html.escape(m.group(1))}</b>" if m else html.escape(line))
    return "\n".join(lines)


def find_telegram_chats(bot_token: str) -> dict[str, str]:
    """Chats that have messaged the bot recently, as {chat_id: name}."""
    resp = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", timeout=30)
    resp.raise_for_status()
    chats = {}
    for update in resp.json().get("result", []):
        chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat")
        if chat:
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            )
            chats[str(chat["id"])] = name or chat.get("username", "")
    return chats


class ConsoleNotifier:
    """Prints messages instead of sending them; handy for trying things out."""

    def send(self, text: str, silent: bool = False) -> None:
        print("-" * 60 + ("  (silent)" if silent else "") + "\n" + text + "\n" + "-" * 60, flush=True)


def _whatsapp_addr(number: str) -> str:
    number = number.strip()
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #


class Matcher:
    """Case-insensitive search for any of several terms as whole words.

    "rig" won't match "original" and "sale" won't match "resale", but digits
    may touch a term, so "rtx" still matches "RTX4060". Spaces in a term
    also match hyphens or nothing ("gaming pc" ~ "gaming-pc", "gamingpc").
    Terms that start or end with punctuation ("?", "₹") match anywhere.
    """

    def __init__(self, terms: Iterable[str]):
        parts = []
        for term in (t.strip() for t in terms):
            if not term:
                continue
            body = re.escape(term).replace(r"\ ", r"[\s_-]*")
            before = r"(?<![a-z])" if term[0].isalpha() else ""
            after = r"(?![a-z])" if term[-1].isalpha() else ""
            parts.append(before + body + after)
        self.pattern = re.compile("|".join(parts), re.I) if parts else None

    def __bool__(self) -> bool:
        return self.pattern is not None

    def search(self, text: str) -> bool:
        return bool(self.pattern and self.pattern.search(text))


def parse_highlights(value: Optional[str]) -> dict[str, list[str]]:
    """Parse "WTS: wts, selling, -wtb; Gaming PC: gaming pc" into groups.

    A term starting with "-" excludes: the label isn't applied if it matches.
    """
    groups: dict[str, list[str]] = {}
    for chunk in (value or "").split(";"):
        if not chunk.strip():
            continue
        label, sep, terms = chunk.partition(":")
        if not sep or not label.strip():
            raise SystemExit(f"HIGHLIGHTS entry {chunk.strip()!r} should look like 'Label: term, term'")
        groups[label.strip()] = _split(terms, seps=",")
    return groups


def crosspost_key(post: Post) -> Optional[str]:
    """Same author + same title = the same listing posted to several subs."""
    if post.author in ("", "[deleted]", "anonymous"):
        return None
    title = re.sub(r"[^a-z0-9]+", " ", post.title.lower()).strip()
    return f"{post.author.lower()}|{title}"


def _subs(post: Post) -> str:
    return ", ".join(f"r/{s}" for s in [post.subreddit, *post.also_in])


def format_post(post: Post, priority: bool = False) -> str:
    title = post.title if len(post.title) <= 300 else post.title[:297] + "..."
    meta = f"u/{post.author}" + (f" · {post.flair}" if post.flair else "")
    if post.tags:
        head = f"{'🚨' if priority else '🔥'} {' · '.join(post.tags)}\n{_subs(post)}"
    else:
        head = f"🆕 {_subs(post)}"
    return f"{head}\n*{title}*\n{meta}\n{post.short_url}"


def format_digest(posts: list[Post]) -> str:
    header = f"🆕 {len(posts)} new posts"
    lines = [header]
    for i, post in enumerate(posts):
        title = post.title if len(post.title) <= 120 else post.title[:117] + "..."
        entry = f"\n• {_subs(post)}: {title}\n  {post.short_url}"
        if sum(map(len, lines)) + len(entry) > WHATSAPP_MAX_CHARS - 40:
            lines.append(f"\n…and {len(posts) - i} more")
            break
        lines.append(entry)
    return "".join(lines)


class SeenStore:
    """Remembers handled post IDs and cross-post keys, across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: list[str] = []
        self.keys: list[str] = []
        self.existed = path.exists()
        if self.existed:
            try:
                data = json.loads(path.read_text())
                self.ids = list(data["seen"])
                self.keys = list(data.get("crosspost_keys", []))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                log.warning("Could not read %s (%s); starting fresh", path, exc)
                self.ids, self.keys = [], []
        self._ids = set(self.ids)
        self._keys = set(self.keys)

    def __contains__(self, post_id: str) -> bool:
        return post_id in self._ids

    def has_key(self, key: Optional[str]) -> bool:
        return key is not None and key in self._keys

    def add(self, post_id: str, key: Optional[str] = None) -> None:
        if post_id not in self._ids:
            self.ids.append(post_id)
            self._ids.add(post_id)
        if key is not None and key not in self._keys:
            self.keys.append(key)
            self._keys.add(key)

    def save(self) -> None:
        self.ids = self.ids[-MAX_SEEN_IDS:]
        self.keys = self.keys[-MAX_SEEN_IDS:]
        self._ids, self._keys = set(self.ids), set(self.keys)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"seen": self.ids, "crosspost_keys": self.keys}))
        tmp.replace(self.path)


class Tracker:
    def __init__(
        self,
        client: RedditClient,
        notifier: Notifier,
        subreddits: list[str],
        store: SeenStore,
        keywords: Optional[list[str]] = None,
        highlights: Optional[dict[str, list[str]]] = None,
        only_highlights: Optional[list[str]] = None,
        priority_highlights: Optional[list[str]] = None,
        notify_on_start: bool = False,
        digest_threshold: int = 5,
    ):
        self.client = client
        self.notifier = notifier
        self.subreddits = subreddits
        self.store = store
        self.keywords = Matcher(keywords or [])
        self.highlights = {
            label: (
                Matcher(t for t in terms if not t.startswith("-")),
                Matcher(t[1:] for t in terms if t.startswith("-")),
            )
            for label, terms in (highlights or {}).items()
        }
        # Each entry is a label, or labels joined with "+" that must all match
        # ("WTS+Gaming PC" = a gaming PC that's for sale).
        self.only_highlights = self._label_sets("ONLY_HIGHLIGHTS", only_highlights)
        # When set, only these posts make a sound; everything else arrives silently.
        self.priority_highlights = self._label_sets("PRIORITY_HIGHLIGHTS", priority_highlights)
        self.digest_threshold = digest_threshold
        # On the very first run, treat what's already there as seen so you
        # don't get blasted with the last 100 posts.
        self._seeding = not store.existed and not notify_on_start

    def _label_sets(self, setting: str, entries: Optional[list[str]]) -> list[frozenset[str]]:
        sets = [frozenset(l.strip() for l in e.split("+") if l.strip()) for e in entries or []]
        unknown = set().union(*sets) - set(self.highlights) if sets else set()
        if unknown:
            raise SystemExit(
                f"{setting} names {sorted(unknown)}, which aren't labels in HIGHLIGHTS "
                f"({sorted(self.highlights) or 'none defined'})"
            )
        return [s for s in sets if s]

    @staticmethod
    def _any_set_in(sets: list[frozenset[str]], tags: list[str]) -> bool:
        return any(s <= set(tags) for s in sets)

    def matches(self, post: Post) -> bool:
        return not self.keywords or self.keywords.search(f"{post.title}\n{post.body}")

    def tags_for(self, post: Post) -> list[str]:
        # The subreddit is matched too, so a rule can say "any post in
        # r/IndiaUsedTech counts" by listing r/IndiaUsedTech as a term.
        text = f"{post.title}\n{post.flair or ''}\nr/{post.subreddit}"
        return [
            label
            for label, (include, exclude) in self.highlights.items()
            if include.search(text) and not exclude.search(text)
        ]

    def poll_once(self) -> list[Post]:
        posts = self.client.fetch_new(self.subreddits)
        new = sorted(
            (p for p in posts if p.id not in self.store),
            key=lambda p: p.created_utc,
        )

        # Group cross-posts so one listing in three subs is one message.
        groups: dict[str, list[Post]] = {}
        for p in new:
            groups.setdefault(crosspost_key(p) or f"id:{p.id}", []).append(p)

        def mark(group: list[Post]) -> None:
            for p in group:
                self.store.add(p.id, crosspost_key(p))

        if self._seeding:
            for group in groups.values():
                mark(group)
            self.store.save()
            self._seeding = False
            log.info("First run: marked %d existing posts as seen", len(new))
            return []

        priority: list[list[Post]] = []
        highlighted: list[list[Post]] = []
        regular: list[list[Post]] = []
        for key, group in groups.items():
            first = group[0]
            if self.store.has_key(key) or not self.matches(first):
                mark(group)  # already notified via another sub, or filtered out
                continue
            first.also_in = sorted({p.subreddit for p in group} - {first.subreddit})
            first.tags = self.tags_for(first)
            if self.only_highlights and not self._any_set_in(self.only_highlights, first.tags):
                mark(group)  # ONLY_HIGHLIGHTS is set and this post has none of them
                continue
            if self._any_set_in(self.priority_highlights, first.tags):
                priority.append(group)
            else:
                (highlighted if first.tags else regular).append(group)

        # Priority posts go first and make a sound. If PRIORITY_HIGHLIGHTS is
        # set, everything else is delivered silently. Highlighted posts always
        # get their own message; the rest are combined into one digest when
        # there's a burst.
        quiet = bool(self.priority_highlights)
        batches = [(format_post(g[0], priority=True), [g], False) for g in priority]
        batches += [(format_post(g[0]), [g], quiet) for g in highlighted]
        if len(regular) > self.digest_threshold:
            batches.append((format_digest([g[0] for g in regular]), regular, quiet))
        else:
            batches += [(format_post(g[0]), [g], quiet) for g in regular]

        sent: list[Post] = []
        try:
            for text, batch, silent in batches:
                self.notifier.send(text, silent=silent)
                for group in batch:
                    mark(group)
                    sent.append(group[0])
                    log.info("Notified: %s %s %r", _subs(group[0]), group[0].id, group[0].title)
        except Exception:
            # Unsent posts stay unseen, so they're retried next poll.
            log.exception("Failed to send notification")
        finally:
            if new:
                self.store.save()
        return sent

    def run(self, interval: float, stop: threading.Event) -> None:
        log.info(
            "Watching r/%s every %.0fs (mode=%s, keyword filter=%s, highlights=%s, only=%s, priority=%s)",
            "+".join(self.subreddits),
            interval,
            self.client.mode,
            "on" if self.keywords else "off",
            ", ".join(self.highlights) or "none",
            ", ".join("+".join(sorted(x)) for x in self.only_highlights) or "everything",
            ", ".join("+".join(sorted(x)) for x in self.priority_highlights) or "all",
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


def _env_number(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {value!r}")


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
    if provider == "waha":
        return WahaNotifier(
            os.getenv("WAHA_URL", "http://localhost:3000"),
            _require("WAHA_TO"),
            os.getenv("WAHA_SESSION", "default"),
            os.getenv("WAHA_API_KEY", "").strip() or None,
        )
    if provider == "telegram":
        return TelegramNotifier(_require("TELEGRAM_BOT_TOKEN"), _require("TELEGRAM_CHAT_ID"))
    if provider == "console":
        return ConsoleNotifier()
    raise SystemExit(
        f"Unknown WHATSAPP_PROVIDER: {provider!r} "
        "(use callmebot, waha, twilio, telegram or console)"
    )


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
    parser.add_argument("--test-message", action="store_true", help="send a test message and exit")
    parser.add_argument(
        "--telegram-chat-id",
        action="store_true",
        help="print the chat ID of whoever messaged your Telegram bot, and exit",
    )
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        # .env in the current directory, else next to this script.
        load_dotenv(Path.cwd() / ".env") or load_dotenv(Path(__file__).with_name(".env"))
    except ImportError:
        pass

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.telegram_chat_id:
        chats = find_telegram_chats(_require("TELEGRAM_BOT_TOKEN"))
        if not chats:
            print("No messages found. Send your bot any message in Telegram, then run this again.")
        for chat_id, name in chats.items():
            print(f"TELEGRAM_CHAT_ID={chat_id}    # {name}")
        return 0

    notifier = ConsoleNotifier() if args.dry_run else build_notifier()
    if args.test_message:
        notifier.send("✅ Reddit tracker is connected.")
        log.info("Test message sent")
        return 0

    subreddits = parse_subreddits(os.getenv("SUBREDDITS"))
    if not subreddits:
        raise SystemExit("Set SUBREDDITS, e.g. SUBREDDITS=buildapcsales,hardwareswap")

    client = build_client()
    default_interval = 15 if client.mode == "oauth" else 60
    interval = _env_number("POLL_INTERVAL_SECONDS", default_interval)

    tracker = Tracker(
        client,
        notifier,
        subreddits,
        SeenStore(Path(os.getenv("STATE_FILE", "state.json"))),
        keywords=_split(os.getenv("KEYWORDS"), seps=","),  # phrases may contain spaces
        highlights=parse_highlights(os.getenv("HIGHLIGHTS")),
        only_highlights=_split(os.getenv("ONLY_HIGHLIGHTS"), seps=","),
        priority_highlights=_split(os.getenv("PRIORITY_HIGHLIGHTS"), seps=","),
        notify_on_start=_env_bool("NOTIFY_ON_START"),
        digest_threshold=int(_env_number("DIGEST_THRESHOLD", 5)),
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
