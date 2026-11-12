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
    "casino, lottery, mod apk, colour prediction, color prediction, hot threads"
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


# How much more often a page marked "!" or "!!" comes round than an unmarked one.
WATCH_WEIGHTS = {0: 1.0, 1: 4.0, 2: 12.0}


@dataclass
class WatchItem:
    url: str
    target: Optional[float] = None  # alert when the price is at or below this
    label: str = ""
    weight: float = 1.0  # its share of the store's reading time, against an unmarked page's 1
    every: float = 0.0  # seconds to leave between two reads of this page (0: as often as its turn comes)


def load_watchlist(path: Path) -> List[WatchItem]:
    """Lines look like:  URL  |  5000  |  optional label   (only the URL is required).

    A line may start with "!" or "!!": when a store has more pages than fit into one scan, such
    a page is read 4 or 12 times as often as an unmarked one. A field "every 15m" keeps a page
    from being read more often than that (a brand's whole catalogue does not need a look a minute).
    """
    items: List[WatchItem] = []
    seen = set()
    for line in read_lines(path):
        marks = re.match(r"^(!{1,2})\s*", line)
        if marks:
            line = line[marks.end():]
        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        if not re.match(r"^https?://", url, re.I):
            log.warning("watchlist: skipping %r (not an http(s) URL)", line)
            continue
        if url in seen:
            continue
        seen.add(url)
        item = WatchItem(url=url, weight=WATCH_WEIGHTS[len(marks.group(1)) if marks else 0])
        for part in parts[1:]:
            number = re.fullmatch(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)", part, re.I)
            pace = re.fullmatch(r"every\s+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)", part, re.I)
            if pace:
                item.every = float(pace.group(1)) * {"s": 1, "m": 60, "h": 3600}[pace.group(2)[0].lower()]
                continue
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
    """A phrase as a pattern: its words in order, however they are spaced or bracketed. After a
    phrase that ends in a letter a model number may follow at once ("galaxy z fold" matches
    "Galaxy Z Fold7"), but not more letters; after one that ends in a digit, nothing may
    ("oneplus 13" must not match "OnePlus 13R" or "OnePlus 135")."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip().lower()) if w]
    tail = r"(?![a-z0-9])" if phrase.strip()[-1:].isdigit() else r"(?![a-z])"
    return r"[\s\-()]*".join(words) + tail


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
        pattern = re.compile(r"(?<![a-z0-9])(?:" + body + r")", re.I)
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

    # The priority ladder for a stated discount (see scoring.py):
    min_discount: float = 80.0  # from here: a quiet alert, if it saves urgent_min_saving or more
    high_discount: float = 90.0  # from here: a high-priority alert, if it saves min_saving or more
    urgent_discount: float = 95.0  # from here: max priority, if it saves urgent_min_saving or more
    min_saving: float = 1000.0
    urgent_min_saving: float = 2000.0
    big_saving: float = 20000.0  # at high_discount or more, a saving this large is urgent on its own
    loot_price: float = 10.0  # a price this low ("Rs 1 deal") counts even when the rupee saving is small
    watch_drop_pct: float = 50.0  # % drop against a watched product's own usual price
    urgent_drop_pct: float = 80.0  # on a category page, a drop this deep is max priority; smaller ones are quieter
    tracker_drop_pct: float = 60.0  # a price tracker's drop counts from this % below the product's usual price
    quick_coupon_pct: float = 60.0  # a food / quick-commerce coupon alerts from this share of the order taken off (0: never)
    quick_loots: bool = True  # "Zepto loot @ 24": alert quietly although no list price can be checked
    floor_mrp_ratio: float = 3.0  # a NEW item under a page's floor only counts if its MRP is this many times the floor
    new_listing_discount: float = 85.0  # a NEW item above a floor alerts from this % off its list price
    absence_hours: float = 24.0  # a product unseen for this long is judged afresh when it returns

    interval: int = 60
    workers: int = 8
    max_post_age_min: float = 20.0
    late_post_min: float = 60.0  # after a gap in reading a channel, posts up to this old are still judged
    dedup_hours: float = 12.0
    trending_channels: int = 3
    trending_window_min: float = 20.0
    daily_alert_limit: int = 200

    startup_note: bool = True  # one "monitor started" push the first time a state file is used
    heartbeat_hour: int = 9  # local hour for the daily "still alive" note; -1 turns it off
    tz_offset_minutes: int = 330  # IST
    healthcheck_url: str = ""

    use_channels: bool = True  # read deal channels and feeds on this machine
    use_store_pages: bool = True  # read watchlist.txt store pages on this machine
    resolve_links: bool = True
    use_curl_cffi: str = "auto"  # auto | on | off
    curl_impersonate: str = "chrome124"  # browser profile for curl_cffi; newer ones are not always better
    telegram_base_url: str = "https://t.me/s"
    deny_words: str = DEFAULT_DENY_WORDS
    extra_store_domains: str = ""
    site_gaps: str = ""  # "flipkart.com=3-5,t.me=0.5-1": seconds between two requests to a site

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
            high_discount=_num("HIGH_DISCOUNT", 90.0),
            urgent_discount=_num("URGENT_DISCOUNT", 95.0),
            min_saving=_num("MIN_SAVING", 1000.0),
            urgent_min_saving=_num("URGENT_MIN_SAVING", 2000.0),
            big_saving=_num("BIG_SAVING", 20000.0),
            loot_price=_num("LOOT_PRICE", 10.0),
            watch_drop_pct=_num("WATCH_DROP_PCT", 50.0),
            urgent_drop_pct=_num("URGENT_DROP_PCT", 80.0),
            tracker_drop_pct=_num("TRACKER_DROP_PCT", 60.0),
            quick_coupon_pct=_num("QUICK_COUPON_PCT", 60.0),
            quick_loots=_flag("QUICK_LOOTS", True),
            floor_mrp_ratio=_num("FLOOR_MRP_RATIO", 3.0),
            new_listing_discount=_num("NEW_LISTING_DISCOUNT", 85.0),
            absence_hours=_num("ABSENCE_HOURS", 24.0),
            interval=int(_num("CHECK_INTERVAL", 60)),
            workers=int(_num("MAX_WORKERS", 8)),
            max_post_age_min=_num("MAX_POST_AGE_MIN", 20.0),
            late_post_min=_num("LATE_POST_MIN", 60.0),
            dedup_hours=_num("DEDUP_HOURS", 12.0),
            trending_channels=int(_num("TRENDING_CHANNELS", 3)),
            trending_window_min=_num("TRENDING_WINDOW_MIN", 20.0),
            daily_alert_limit=int(_num("DAILY_ALERT_LIMIT", 200)),
            startup_note=_flag("STARTUP_NOTE", True),
            heartbeat_hour=int(_num("HEARTBEAT_HOUR", 9)),
            tz_offset_minutes=int(_num("TZ_OFFSET_MINUTES", 330)),
            healthcheck_url=_get("HEALTHCHECK_URL", ""),
            use_channels=_flag("USE_CHANNELS", True),
            use_store_pages=_flag("USE_STORE_PAGES", True),
            resolve_links=_flag("RESOLVE_LINKS", True),
            use_curl_cffi=_get("USE_CURL_CFFI", "auto").lower(),
            curl_impersonate=_get("CURL_IMPERSONATE", "chrome124"),
            telegram_base_url=_get("TELEGRAM_BASE_URL", "https://t.me/s").rstrip("/"),
            deny_words=_get("DENY_WORDS", DEFAULT_DENY_WORDS),
            extra_store_domains=_get("EXTRA_STORE_DOMAINS", ""),
            site_gaps=_get("SITE_GAPS", ""),
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
        """Read the list files. A half that is switched off on this machine is left empty."""
        self.traps = load_traps(Path(self.traps_file))
        self.channels, self.feeds, self.watchlist = [], [], []
        if self.use_channels:
            self.channels = load_channels(Path(self.channels_file))
            self.feeds = [u for u in read_lines(Path(self.feeds_file)) if re.match(r"^https?://", u, re.I)]
        if self.use_store_pages:
            self.watchlist = load_watchlist(Path(self.watchlist_file))

    @property
    def deny_pattern(self) -> Optional["re.Pattern[str]"]:
        words = [w.strip().lower() for w in self.deny_words.split(",") if w.strip()]
        if not words:
            return None
        return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])", re.I)
