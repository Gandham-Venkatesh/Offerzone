"""Settings (environment variables / .env) and the plain-text list files."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("lootwatch")

# Posts containing any of these are ignored outright, so keep the list to things a real product
# deal never says. ("credit card" is deliberately absent: half of all genuine deals mention a card offer.)
DEFAULT_DENY_WORDS = (
    "quiz answers, refer and earn, refer & earn, demat, personal loan, instant loan, rummy, betting, "
    "casino, lottery, mod apk, colour prediction, color prediction"
)


def load_env_file(path: Path) -> int:
    """Load KEY=VALUE lines into os.environ without overriding what is already set."""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()  # allow "KEY=80   # note"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def read_lines(path: Path) -> List[str]:
    """Non-empty lines of a list file. '#' starts a comment (at line start or after a space)."""
    if not path.is_file():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#\s", line, maxsplit=1)[0].strip()
        if line:
            lines.append(line)
    return lines


_CHANNEL = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?:[/?].*)?$")
_BARE_CHANNEL = re.compile(r"^@?(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_channel(entry: str) -> Optional[str]:
    """Accept '@name', 'name' or a t.me link; private invite links cannot be read publicly."""
    entry = entry.strip()
    if "/+" in entry or "joinchat" in entry:
        return None
    match = _CHANNEL.match(entry) or _BARE_CHANNEL.match(entry)
    return match.group("name") if match else None


def load_channels(path: Path) -> List[str]:
    channels: List[str] = []
    seen = set()
    for line in read_lines(path):
        name = parse_channel(line)
        if not name:
            log.warning("channels: skipping %r (need a public @username; invite links cannot be read)", line)
            continue
        if name.lower() not in seen:
            seen.add(name.lower())
            channels.append(name)
    return channels


@dataclass
class WatchItem:
    url: str
    target: Optional[float] = None  # alert when the price is at or below this
    label: str = ""


def load_watchlist(path: Path) -> List[WatchItem]:
    """Lines look like:  URL  |  5000  |  optional label   (only the URL is required)."""
    items: List[WatchItem] = []
    seen = set()
    for line in read_lines(path):
        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        if not re.match(r"^https?://", url, re.I):
            log.warning("watchlist: skipping %r (not an http(s) URL)", line)
            continue
        if url in seen:
            continue
        seen.add(url)
        item = WatchItem(url=url)
        for part in parts[1:]:
            number = re.fullmatch(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)", part, re.I)
            if number and item.target is None:
                item.target = float(number.group(1).replace(",", ""))
            elif part:
                item.label = part
        items.append(item)
    return items


@dataclass
class Trap:
    """Alert when a deal mentions one of ``phrases`` at a price at or below ``max_price``."""

    phrases: Tuple[str, ...]
    max_price: float
    pattern: "re.Pattern[str]"

    @property
    def name(self) -> str:
        return self.phrases[0]


def _phrase_regex(phrase: str) -> str:
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip().lower()) if w]
    return r"[\s\-()]*".join(words)


def load_traps(path: Path) -> List[Trap]:
    """Lines look like:  nothing phone, cmf phone | 8000"""
    traps: List[Trap] = []
    for line in read_lines(path):
        left, sep, right = line.rpartition("|")
        number = re.fullmatch(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)", right.strip(), re.I)
        phrases = tuple(p.strip().lower() for p in left.split(",") if p.strip())
        if not sep or not number or not phrases:
            log.warning("traps: skipping %r (expected 'keywords | max price')", line)
            continue
        body = "|".join(_phrase_regex(p) for p in phrases)
        pattern = re.compile(r"(?<![a-z0-9])(?:" + body + r")(?![a-z0-9])", re.I)
        traps.append(Trap(phrases, float(number.group(1).replace(",", "")), pattern))
    return traps


def _get(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _num(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


def _flag(name: str, default: bool) -> bool:
    return _get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    ntfy_health_topic: str = ""

    min_discount: float = 80.0  # % off needed for an alert
    urgent_min_saving: float = 2000.0  # rupees saved needed to make it an URGENT alert
    watch_drop_pct: float = 50.0  # % drop against a watched product's own usual price

    interval: int = 60
    workers: int = 8
    max_post_age_min: float = 20.0
    dedup_hours: float = 12.0
    trending_channels: int = 3
    trending_window_min: float = 20.0
    daily_alert_limit: int = 200

    startup_note: bool = True  # one "monitor started" push the first time a state file is used
    heartbeat_hour: int = 9  # local hour for the daily "still alive" note; -1 turns it off
    tz_offset_minutes: int = 330  # IST
    healthcheck_url: str = ""

    resolve_links: bool = True
    use_curl_cffi: str = "auto"  # auto | on | off
    telegram_base_url: str = "https://t.me/s"
    deny_words: str = DEFAULT_DENY_WORDS
    extra_store_domains: str = ""

    db_path: str = "lootwatch.db"
    channels_file: str = "channels.txt"
    watchlist_file: str = "watchlist.txt"
    traps_file: str = "traps.txt"
    feeds_file: str = "feeds.txt"

    dry_run: bool = False

    channels: List[str] = field(default_factory=list)
    watchlist: List[WatchItem] = field(default_factory=list)
    traps: List[Trap] = field(default_factory=list)
    feeds: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, overrides: Optional[Dict[str, object]] = None) -> "Settings":
        s = cls(
            ntfy_server=_get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=_get("NTFY_TOPIC", ""),
            ntfy_token=_get("NTFY_TOKEN", ""),
            ntfy_health_topic=_get("NTFY_HEALTH_TOPIC", ""),
            min_discount=_num("MIN_DISCOUNT", 80.0),
            urgent_min_saving=_num("URGENT_MIN_SAVING", 2000.0),
            watch_drop_pct=_num("WATCH_DROP_PCT", 50.0),
            interval=int(_num("CHECK_INTERVAL", 60)),
            workers=int(_num("MAX_WORKERS", 8)),
            max_post_age_min=_num("MAX_POST_AGE_MIN", 20.0),
            dedup_hours=_num("DEDUP_HOURS", 12.0),
            trending_channels=int(_num("TRENDING_CHANNELS", 3)),
            trending_window_min=_num("TRENDING_WINDOW_MIN", 20.0),
            daily_alert_limit=int(_num("DAILY_ALERT_LIMIT", 200)),
            startup_note=_flag("STARTUP_NOTE", True),
            heartbeat_hour=int(_num("HEARTBEAT_HOUR", 9)),
            tz_offset_minutes=int(_num("TZ_OFFSET_MINUTES", 330)),
            healthcheck_url=_get("HEALTHCHECK_URL", ""),
            resolve_links=_flag("RESOLVE_LINKS", True),
            use_curl_cffi=_get("USE_CURL_CFFI", "auto").lower(),
            telegram_base_url=_get("TELEGRAM_BASE_URL", "https://t.me/s").rstrip("/"),
            deny_words=_get("DENY_WORDS", DEFAULT_DENY_WORDS),
            extra_store_domains=_get("EXTRA_STORE_DOMAINS", ""),
            db_path=_get("DB_PATH", "lootwatch.db"),
            channels_file=_get("CHANNELS_FILE", "channels.txt"),
            watchlist_file=_get("WATCHLIST_FILE", "watchlist.txt"),
            traps_file=_get("TRAPS_FILE", "traps.txt"),
            feeds_file=_get("FEEDS_FILE", "feeds.txt"),
        )
        for key, value in (overrides or {}).items():
            if value is not None and hasattr(s, key):
                setattr(s, key, value)
        s.interval = max(15, s.interval)
        s.workers = max(1, min(32, s.workers))
        return s

    def load_lists(self) -> None:
        self.channels = load_channels(Path(self.channels_file))
        self.watchlist = load_watchlist(Path(self.watchlist_file))
        self.traps = load_traps(Path(self.traps_file))
        self.feeds = [u for u in read_lines(Path(self.feeds_file)) if re.match(r"^https?://", u, re.I)]

    @property
    def deny_pattern(self) -> Optional["re.Pattern[str]"]:
        words = [w.strip().lower() for w in self.deny_words.split(",") if w.strip()]
        if not words:
            return None
        return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])", re.I)
