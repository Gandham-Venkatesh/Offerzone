"""The scan loop: fetch every source, judge what is new, push what matters, watch its own health."""

from __future__ import annotations

import collections
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests

from .config import Settings, WatchItem
from .db import DB
from .fetch import Fetcher, policy_for
from .links import (TRUSTED_KINDS, LinkResolver, classify_host, deal_links, host_of, model_name, product_key,
                    same_product_name, text_fingerprint)
from .notify import Notifier, build_payload
from .scoring import (TIER_HIGH, TIER_NAMES, TIER_NORMAL, TIER_URGENT, Verdict, confirm_loot, evaluate_offer,
                      evaluate_stated, evaluate_text, evaluate_tracked, is_payment_app, looks_like_loot, tracker_near_miss,
                      why_skipped)
from .sources.dealsites import SiteError, site_label, site_reader
from .sources.feeds import FeedError, parse_feed
from .sources.stores import Offer, extract_page, request_headers
from .sources.telegram import parse_channel_page
from .textparse import fmt_inr

log = logging.getLogger("lootwatch")

OUTBOX_MAX_AGE = 30 * 60  # a loot alert delivered later than this is only noise
LOOT_CHECK_WINDOW = 3600  # how long a loot pushed without a list price waits for the price tracker to list it
# A deal under the bar for list-price-only deals that this many channels post within the window is
# pushed all the same (the crowd's pick: a Beardo perfume combo at 80% off, in nine channels in two
# minutes on 20 Nov 2026). From CROWD_MIN_DISCOUNT off.
# Before a price tracker's Flipkart drop is pushed, its Flipkart page is read once: the owner found
# the tracker's Flipkart watches sold out every time, even opened the second they rang (20-21 Nov).
STOCK_CHECK_SECONDS = 6.0
# Joining the public channels with the Telegram login (TG_JOIN_CHANNELS): after this long, this far
# apart, at most this many a run.
JOIN_START_SECONDS = 45.0
# A store's product already pushed is not pushed again at the same price, or dearer, for this long.
PRODUCT_REPEAT_HOURS = 72
# A slower pace Telegram asked for is kept by the next runs for this long.
SLOW_PACE_HOURS = 3
JOIN_GAP_SECONDS = 60.0
JOIN_PER_RUN = 10
# An invite link that could not be joined (a request the channel's admins have not accepted, a link
# that no longer works) is tried again after this long.
JOIN_RETRY_HOURS = 24
# An invite-only channel not marked "!" is asked for new posts this often; Telegram pushes them
# within seconds anyway. Asking every few seconds is kept for the marked ones, so that the login
# stays well inside what Telegram allows however many channels it reads.
PRIVATE_SLOW_SECONDS = 60.0
# How long Flipkart's answer on a product is kept: sold out stays sold out for half an hour, in stock
# is believed for two minutes.
STOCK_SOLD_OUT_MEMORY = 1800.0
STOCK_IN_STOCK_MEMORY = 120.0
# When Flipkart does not answer, an earlier "sold out" still counts for this long.
STOCK_SOLD_OUT_TRUST = 6 * 3600.0
CROWD_CHANNELS = 4
CROWD_WINDOW = 3600
CROWD_MIN_DISCOUNT = 75.0
TRACKER_MEMORY = 6 * 3600  # how long what the tracker said about a product counts
FAIL_STREAK_NOTE = 10  # scans in a row before a source is reported as broken
BLIND_STREAK = 3  # scans in a row without reading a single deal source before the "blind" alarm
SITE_FAIL_STREAK = 10  # scans in a row without reading any watched page of a store before saying so
_TAGS = {TIER_URGENT: ["rotating_light"], TIER_HIGH: ["fire"], TIER_NORMAL: ["label"]}


def short_url(url: str, limit: int = 70) -> str:
    parts = urlsplit(url)
    text = (parts.hostname or "") + parts.path + ("?" + parts.query if parts.query else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def source_key(kind: str, url: str) -> str:
    """The name under which one feed or store page keeps its state.

    It is built from the whole address: two long addresses that differ only at the end (the same
    category sorted two ways) must not share a baseline or a health record.
    """
    return f"{kind}:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(max(0, seconds))}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


_HYPE_WORD = re.compile(
    r"(?:price|pricing|error|glitch|bug|loot|lo|deal|deals|steal|offer|offers|alert|fast|hurry|big|mega|super"
    r"|biggest|new|hot|best|grab|now|live|again|today|only|just|limited|stock|sale|buy|link|mrp|off|at|for|rs)",
    re.I,
)
_LEADING_HYPE = re.compile(
    r"^(?:(?:(?:big|mega|super|hot|biggest)\s+)?(?:price|pricing|loot|deal|steal|glitch|bug|error|alert)\b[\s:!\-–—|]*)+",
    re.I,
)
_PRICE_START = re.compile(r"\s*(?:@|₹|\(?\s*m\.?r\.?p\b|\brs\.?\s*\d|\b(?:at|for)\s+(?:just|only)\b|\bworth\b)", re.I)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# "₹999 after coupon", "with SBI card", "effective price": a post's price that the store's page will
# not show until the offer is applied.
_EFFECTIVE_PRICE = re.compile(
    r"coupon|\bcode\b|\bcards?\b|\bbank\b|cashback|effective|exchange|\bemi\b|super\s?coins?|pay\s?later|\bupi\b"
    r"|\bcart\b|\bbuy\s+\d|\bmin(?:imum)?\s+order|\bcombo\b|\bpack\s+of\b|\bper\s+(?:piece|unit|kg)\b", re.I)


_FILLER = frozenset("loot loots deal deals price apply applt coupon code only just big mega super offer offers buy now "
                    "link here at for the and get grab hurry fast flat off rs inr checkout".split())


def _says_little(subject: str) -> bool:
    """A post's first line that names no product: "Loot at 398.", "278 : https://fkrt.to/...", "Apply Coupon"."""
    words = re.findall(r"[a-z]{3,}", re.sub(r"https?://\S+", " ", subject.lower()))
    return not [word for word in words if word not in _FILLER]


def _first_line(text: str, limit: int = 70) -> str:
    """The line of a post that names the product, without the hype and the prices around it."""
    fallback = ""
    for line in text.splitlines():
        line = re.sub(r"[^\w\s()+.,'&/\-₹@:%|]", " ", line)  # drop emoji and decorations
        line = re.sub(r"\s+", " ", line).strip(" *_-•:!|")
        if len(line) < 4 or line.lower().startswith(("http://", "https://")):
            continue
        fallback = fallback or line
        head = _LEADING_HYPE.sub("", _PRICE_START.split(line, 1)[0]).strip(" -–—:@(|,")
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9+'.\-]*", head) if not _HYPE_WORD.fullmatch(w)]
        if len(words) >= 2:
            return _clip(head, limit)
    return _clip(fallback, limit)


class Monitor:
    def __init__(self, settings: Settings, db: DB, fetcher: Fetcher, notifier: Notifier,
                 resolver: Optional[LinkResolver] = None, account=None) -> None:
        self.s = settings
        self.db = db
        self.fetcher = fetcher
        self.notifier = notifier
        self.resolver = resolver or LinkResolver()
        self.stats: "collections.Counter[str]" = collections.Counter()
        self._stats_lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._verify_lock = threading.Lock()
        self._fast_stop = threading.Event()
        self._fast_thread: Optional[threading.Thread] = None
        self._feed_thread: Optional[threading.Thread] = None
        self._fast_feeds = [url for url in settings.fast_feeds if url in settings.feeds]
        self._push_lags: List[float] = []  # how late Telegram pushed posts, since the last scan line
        self._watch_checked: Dict[str, float] = {}
        self._near_logged: Dict[str, float] = {}  # tracker near misses logged lately: once an hour is enough
        self._stock_answers: Dict[str, Tuple[Offer, float]] = {}  # Flipkart's last word on a product, and when
        self._ntfy_pause_until = 0.0
        self._last_ping = 0.0
        self._warned_no_topic = False
        self._stop = threading.Event()
        self._cycle_no = 0
        self._deal_reads = 0  # deal sources read successfully in the scan under way
        self._site_scan: Dict[str, List[tuple]] = {}  # store host -> [(read ok, reason)] in that scan
        # Readable names for the sources, for notes and logs (state is kept under source_key()).
        self._names: Dict[str, str] = {f"tg:{c}": f"@{c}" for c in settings.channels}
        self._names.update({source_key("feed", u): short_url(u, 50) for u in settings.feeds})
        self._names.update({source_key("watch", w.url): w.label or short_url(w.url, 50) for w in settings.watchlist})
        self._watch_hosts = {source_key("watch", w.url): host_of(w.url) for w in settings.watchlist}
        # Invite-only channels need a Telegram account (sources/tgaccount.py); without one they are skipped.
        self.account = account
        if self.account is None and settings.private_channels and settings.telegram_account:
            from .sources.tgaccount import HAS_TELETHON, AccountReader

            if HAS_TELETHON:
                self.account = AccountReader(settings.tg_api_id, settings.tg_api_hash, settings.tg_session)
            else:
                log.warning("invite-only channels need the telethon package (pip install telethon); skipping them")
        self._private = list(settings.private_channels) if self.account is not None else []
        if self._private and hasattr(self.account, "missing_private"):
            # A link the login could not join lately (a request the admins have not accepted, a group
            # chat, a link that no longer works) is left alone until the joining is tried again.
            self._private = [code for code in self._private if not self._join_failed_lately(code)]
        if self._private and hasattr(self.account, "use_known"):
            known = {code: self.db.get(f"tg_invite:{code}") for code in self._private}
            self.account.use_known({code: int(chat) for code, chat in known.items() if chat.isdigit()},
                                   {code: settings.private_names.get(code, "") for code in self._private})
        self._names.update({f"tg:+{code}": f"invite-only channel {code[:6]}…" for code in self._private})
        # Whose turn it is survives a restart: with a long list and short runs, the pages at the
        # top of the file would otherwise be read again and again and the rest hardly ever.
        attempts = {row[0]: max(row[4] or 0, 0 if str(row[6] or "").startswith("cooldown") else (row[5] or 0))
                    for row in db.health_rows()}
        for item in settings.watchlist:
            last = attempts.get(source_key("watch", item.url))
            if last:
                self._watch_checked[item.url] = float(last)

    def stop(self) -> None:
        self._stop.set()

    def _count(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += amount

    def _display(self, source: str) -> str:
        return self._names.get(source, source)

    def _deal_source_read(self) -> None:
        with self._stats_lock:
            self._deal_reads += 1

    def _site_result(self, host: str, ok: bool, reason: str = "") -> None:
        with self._stats_lock:
            self._site_scan.setdefault(host, []).append((ok, reason))

    def _max_post_age(self, previous_ok: Optional[float], now: float) -> float:
        """How old a post may be and still be news.

        Normally MAX_POST_AGE_MIN. When the source could not be read for a while (a run that
        started late, an outage), everything published since it was last read still counts, up
        to LATE_POST_MIN: the monitor could not have seen it any earlier.
        """
        normal = self.s.max_post_age_min * 60
        if previous_ok is None:
            return normal
        return max(normal, min(now - previous_ok + 120, self.s.late_post_min * 60))

    # ------------------------------------------------------------ sources ---
    def _scan_channel(self, channel: str, deadline: float) -> None:
        source = f"tg:{channel}"
        result = self.fetcher.get(f"{self.s.telegram_base_url}/{channel}", deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            self._count("unreached")
            return  # ran out of time this cycle; not the channel's fault
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            return
        page = parse_channel_page(result.text, channel, result.final_url)
        self._take_channel_page(source, f"@{channel}", page)

    def _scan_private(self, code: str, deadline: float, newer_only: bool = False, record: bool = True) -> str:
        """An invite-only channel, read through the Telegram account. Returns what went wrong, if anything.

        ``newer_only``: ask only for messages newer than those read before. ``record``: count the
        reading in the source's health (the fast reading does so once a minute, like a scan).
        """
        source = f"tg:+{code}"
        page, error = self.account.read(code, newer_only=True) if newer_only else self.account.read(code)
        if page is None:
            failed = self.db.get(f"tg_join_fail:{code}")
            if failed and "has not joined" in error:
                error = f"the Telegram login could not join it: {failed.partition(':')[2]}"
            elif "has not joined" in error and self.s.tg_join_channels and hasattr(self.account, "missing_private"):
                return error  # the login joins it shortly (_join_channels): not a broken source
            if record:
                self.db.health_fail(source, error)
                self._count("source_failures")
            return error
        self._names[source] = f"{page.title} (invite-only)"
        chat = self.account.chat_id(code) if hasattr(self.account, "chat_id") else None
        if chat and self.db.get(f"tg_invite:{code}") != str(chat):
            self.db.set(f"tg_invite:{code}", chat)  # found by its chat id from now on, not its invite link
        self._take_channel_page(source, page.title, page, record=record)
        return ""

    def _read_private_often(self) -> None:
        """The invite-only channels, read every TG_FAST_SECONDS through the Telegram account.

        Telegram's own push of a new post (AccountReader.listen) came half a minute late on a
        channel with millions of members, so asking is faster: each reading asks only for the
        messages newer than the newest one seen, next to nothing for Telegram. If Telegram asks
        the account to slow down, it does.
        """
        gap = self.s.tg_fast_seconds
        stamp, _, kept = self.db.get("tg_fast_gap").partition(":")
        if stamp.isdigit() and time.time() - int(stamp) < SLOW_PACE_HOURS * 3600:
            gap = max(gap, float(kept or gap))  # Telegram asked an earlier run to slow down lately
            log.info("the invite-only channels are read every %.0fs: Telegram asked for a slower pace lately", gap)
        recorded: Dict[str, float] = {}
        asked: Dict[str, float] = {}
        while not self._fast_stop.is_set():
            started = time.time()
            for code in self._private:
                if self._fast_stop.is_set():
                    break
                if (not self._is_loot_channel(f"tg:+{code}")
                        and started - asked.get(code, 0.0) < max(gap, PRIVATE_SLOW_SECONDS)):
                    continue  # not marked "!": asked once a minute, its new posts pushed within seconds
                asked[code] = started
                record = started - recorded.get(code, 0.0) >= 60
                try:
                    error = self._scan_private(code, started + 30, newer_only=True, record=record)
                except Exception:
                    log.exception("could not read the invite-only channel %s", code)
                    error = ""
                if record:
                    recorded[code] = started
                if "before asking again" in error and gap < 60:
                    gap = min(60.0, gap * 2)
                    self.db.set("tg_fast_gap", f"{int(time.time())}:{gap:.0f}")
                    log.warning("Telegram asked the login to slow down: the invite-only channels are read every %.0fs "
                                "from now on", gap)
            self._fast_stop.wait(max(0.5, started + gap - time.time()))

    def _join_channels(self) -> None:
        """The channels of channels.txt that the Telegram login is not in, joined with it
        (TG_JOIN_CHANNELS, on unless set to 0): Telegram then pushes their posts within seconds,
        where public web pages are read once a minute, and an invite-only channel can be read at
        all. Muted for good and archived, so the owner's phone stays quiet. An invite link is joined
        only when it leads to a broadcast channel of some size, never a group chat, and the
        channel's latest posts then go to the log with what the rules make of them (_first_look),
        to judge it by. One at a time, JOIN_GAP_SECONDS apart and at most JOIN_PER_RUN a run:
        Telegram limits how many channels an account joins in a while, and when it says so the
        rest wait for a later run (reading goes on regardless). A link that could not be joined is
        tried again after JOIN_RETRY_HOURS."""
        waited = self.db.get("tg_join_wait_until")
        if waited.isdigit() and int(waited) > time.time():
            return  # Telegram asked an earlier run to wait before joining more: asking again would not help
        if self._fast_stop.wait(JOIN_START_SECONDS):  # the first readings look at the account's chat list
            return
        missing = self.account.missing_public()
        if missing is None and not self._fast_stop.wait(JOIN_START_SECONDS):
            missing = self.account.missing_public()
        invites = self._invites_to_join()
        if not missing and not invites:
            return
        if missing:
            log.info("joining %d public channel(s) with the Telegram login, each muted and archived: %s", len(missing),
                     ", ".join("@" + name for name in missing))
        tries = 0
        for name in (missing or [])[:JOIN_PER_RUN]:
            if tries and self._fast_stop.wait(JOIN_GAP_SECONDS):
                return
            tries += 1
            why = self.account.join(name)
            if not why:
                log.info("joined @%s with the Telegram login (muted, archived): its posts now come within seconds", name)
            elif why.startswith("wait"):
                self._join_later(why)
                return
            else:
                log.warning("could not join @%s with the Telegram login: %s", name, why)
        for code in invites[:max(0, JOIN_PER_RUN - tries)]:
            if tries and self._fast_stop.wait(JOIN_GAP_SECONDS):
                return
            tries += 1
            why, info = self.account.join_invite(code)
            if not why:
                chat = self.account.chat_id(code) if hasattr(self.account, "chat_id") else None
                if chat:
                    self.db.set(f"tg_invite:{code}", chat)
                title = (getattr(self.account, "titles", None) or {}).get(code) or code
                self._names[f"tg:+{code}"] = f"{title} (invite-only)"
                log.info("now reading the invite-only channel https://t.me/+%s through the Telegram login: %s", code, info)
                self._first_look(code)
            elif why.startswith("wait"):
                self._join_later(why)
                return
            else:
                self.db.set(f"tg_join_fail:{code}", f"{int(time.time())}:{why[:150]}")
                self._private = [other for other in self._private if other != code]  # cannot be read this run
                log.warning("did not join the invite-only channel https://t.me/+%s: %s%s", code, why,
                            f" | {info}" if info else "")

    def _join_later(self, why: str) -> None:
        seconds = int(re.sub(r"\D", "", why) or 3600)
        self.db.set("tg_join_wait_until", int(time.time()) + seconds)
        log.warning("Telegram wants no more channels joined for now (%s): the rest are joined by a later run", why)

    def _invites_to_join(self) -> List[str]:
        """The invite links of channels.txt whose channels the login is not in, but for those that
        could not be joined lately."""
        if not hasattr(self.account, "missing_private"):
            return []
        return [code for code in self.account.missing_private() or [] if not self._join_failed_lately(code)]

    def _join_failed_lately(self, code: str) -> bool:
        stamp = self.db.get(f"tg_join_fail:{code}").partition(":")[0]
        return stamp.isdigit() and time.time() - int(stamp) < JOIN_RETRY_HOURS * 3600

    def _first_look(self, code: str) -> None:
        """A newly joined invite-only channel's latest posts, in the log with what the rules make of
        each (none is pushed: they are old news), to judge the channel by what it really posts."""
        try:
            page, error = self.account.read(code)
        except Exception as exc:  # the channel is read in the usual way regardless
            page, error = None, type(exc).__name__
        title = (getattr(self.account, "titles", None) or {}).get(code) or code
        if page is None or not page.posts:
            log.info("first look at %s: nothing to read (%s)", title, error or getattr(page, "status", ""))
            return
        marked = self._is_loot_channel(f"tg:+{code}")
        now = time.time()
        tiers: collections.Counter = collections.Counter()
        for post in page.posts:
            try:
                verdict = evaluate_text(post.analysis_text, self.s, links=post.links, loot_channel=marked)
            except Exception:
                verdict = None
            tier = TIER_NAMES.get(verdict.tier, str(verdict.tier)) if verdict else "--"
            tiers[tier] += 1
            text = " ⏎ ".join(line.strip() for line in (post.text or "").splitlines() if line.strip())
            log.info("first look at %s: #%s %s | %s | %s", title, post.post_id,
                     _age(now - post.ts) if post.ts else "?", tier, text[:220])
        oldest = min((post.ts for post in page.posts if post.ts), default=None)
        log.info("first look at %s: %d posts%s; the rules would push %d urgent, %d high, %d normal", title,
                 len(page.posts), f", the oldest {_age(now - oldest)}" if oldest else "", tiers["urgent"],
                 tiers["high"], tiers["normal"])

    def _fast_reading(self) -> bool:
        return self._fast_thread is not None and self._fast_thread.is_alive()

    def _read_feeds_often(self) -> None:
        """Feeds marked "!" in feeds.txt (the price tracker), read every FAST_FEED_SECONDS.

        A site that refuses is left alone by the fetcher for a while, and from then on read half
        as often each time it refuses again, so that it never has a reason to shut us out for good.
        """
        gap = self.s.fast_feed_seconds
        recorded: Dict[str, float] = {}
        while not self._fast_stop.is_set():
            started = time.time()
            for url in self._fast_feeds:
                if self._fast_stop.is_set():
                    break
                record = started - recorded.get(url, 0.0) >= 60
                try:
                    outcome = self._scan_feed(url, started + max(gap, 15.0), record=record)
                except Exception:
                    log.exception("could not read %s", short_url(url))
                    outcome = ""
                if record:
                    recorded[url] = started
                if outcome == "refused" and gap < 60:
                    gap = min(60.0, gap * 2)
                    log.warning("%s refused a reading: read every %.0fs from now on", short_url(url), gap)
            self._fast_stop.wait(max(0.5, started + gap - time.time()))

    def _feeds_read_often(self) -> List[str]:
        alive = self._feed_thread is not None and self._feed_thread.is_alive()
        return self._fast_feeds if alive else []

    def _take_channel_page(self, source: str, label: str, page, record: bool = True) -> None:
        if page.status != "ok":
            reason = {
                "no_preview": "no public preview (private, renamed or deleted channel)",
                "empty": "page loaded but shows no posts",
            }.get(page.status, "page layout not recognised")
            if record:
                self.db.health_fail(source, reason)
                self._count("source_failures")
            return
        previous_ok = self.db.last_ok(source)
        if record:
            self.db.health_ok(source, len(page.posts))
            self._deal_source_read()
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self._claim(source, [str(p.post_id) for p in page.posts])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return
        now = time.time()
        max_age = self._max_post_age(previous_ok, now)
        for post in page.posts:
            if str(post.post_id) not in fresh:
                continue
            self._count("posts")
            if post.ts is None and first_contact:
                continue  # cannot tell how old it is; never alert on a channel's backlog
            if post.ts is not None and now - post.ts > max_age:
                self._count("stale")
                continue
            try:
                self._handle_post(source, label, post.text, post.analysis_text, post.links, post.url, post.ts)
            except Exception:
                log.exception("failed to process %s", post.url)

    def _still_there(self, verdict: Verdict, item) -> Optional[Verdict]:
        """A price tracker's Flipkart drop, checked on Flipkart itself before it is pushed: sold out
        there, or back at its old price, it is not pushed (and the log says so). When the page
        cannot be read, or does not say, it is pushed as it is: a check must never cost a loot."""
        url = next((link for link in item.links if host_of(link).endswith("flipkart.com") and "/p/" in link), "")
        if not url:
            return verdict
        verdict.stock_checked = True
        offer, problem, when = self._flipkart_offer(url)
        name = _first_line(item.title)[:70]
        if offer is not None and offer.in_stock is False:
            self._count("stock_sold_out")
            log.info("stock check: sold out on Flipkart already, not pushed (listed %s at %s%s) | %s",
                     _age(time.time() - item.ts) if item.ts else "?", fmt_inr(item.price), when, name)
            return None
        if offer is not None and offer.price > item.price * 1.15 + 1:
            self._count("stock_price_up")
            log.info("stock check: back at %s on Flipkart, not pushed (listed at %s) | %s", fmt_inr(offer.price),
                     fmt_inr(item.price), name)
            return None
        if offer is not None and offer.in_stock:
            self._count("stock_in_stock")
            verdict.notes.append(f"in stock on Flipkart just now, at {fmt_inr(offer.price)}")
            log.info("stock check: in stock at %s | %s", fmt_inr(offer.price), name)
        else:
            self._count("stock_unknown")
            self._unchecked(verdict, item.price, marked=False)
            log.info("stock check: %s; pushed as it is | %s", "the page does not say" if offer is not None else
                     f"could not read the page ({problem})", name)
        return verdict

    def _unchecked(self, verdict: Verdict, price: Optional[float], marked: bool) -> None:
        """Flipkart did not say whether it has the product: the push says so, and a give-away price
        that could not be checked comes at the ordinary priority (on Flipkart those were sold out,
        nearly every one: a ₹1 detergent bar, a ₹5 sauce on 21-22 Nov 2026)."""
        verdict.flags.append("stock not checked: Flipkart did not answer")
        if not marked and price is not None and price <= self.s.loot_price:
            verdict.tier = min(verdict.tier, TIER_NORMAL)
            verdict.max_tier = min(verdict.max_tier, TIER_NORMAL)

    def _flipkart_offer(self, url: str) -> Tuple[Optional[Offer], str, str]:
        """What Flipkart's own page says of a product now: (offer or None, why not, when it was read
        if not just now). The tracker lists a drop again every few minutes, and Flipkart refuses quick
        checks that come too often (ten of 36 on the night of 21-22 Nov 2026), so an answer is kept:
        "sold out" for half an hour, "in stock" for two minutes."""
        key, now = product_key(url) or url, time.time()
        kept = self._stock_answers.get(key)
        if kept is None:
            # A run lasts a quarter of an hour; "sold out" is kept in the state file, so the next run
            # knows it too (a ₹5 sauce found sold out twice was pushed by the next run on 22 Nov 2026).
            stamp, _, price = self.db.get("soldout:" + key).partition(":")
            if stamp.isdigit():
                kept = (Offer(key, "flipkart", "", float(price or 0), None, url, in_stock=False), float(stamp))
        if kept is not None:
            offer, at = kept
            if now - at < (STOCK_SOLD_OUT_MEMORY if offer.in_stock is False else STOCK_IN_STOCK_MEMORY):
                return offer, "", f", checked {_age(now - at)}"
        parts = urlsplit(url)
        if parts.hostname == "dl.flipkart.com":  # the app's link to a product page: its website's, same queue
            url = urlunsplit(("https", "www.flipkart.com", re.sub(r"^/dl(?=/)", "", parts.path), parts.query, ""))
        result = self.fetcher.peek(url, timeout=STOCK_CHECK_SECONDS)
        offer = None
        if result.ok:
            page = extract_page(url, result.text)
            offer = page.offers[0] if page.kind == "product" and page.offers else None
        if offer is not None and offer.in_stock is not None:
            self._stock_answers[key] = (offer, now)
            self.db.set("soldout:" + key, f"{int(now)}:{offer.price:.0f}" if offer.in_stock is False else "")
            return offer, "", ""
        if kept is not None and kept[0].in_stock is False and now - kept[1] < STOCK_SOLD_OUT_TRUST:
            # Flipkart does not answer now (it refuses checks that come too often), but it said "sold
            # out" a while ago: that still counts, rather than a push of something nobody can buy.
            return kept[0], "", f", checked {_age(now - kept[1])}; Flipkart did not answer now"
        return offer, result.problem or "no product data", ""

    def _pushed_already(self, fingerprint: str, verdict: Verdict) -> bool:
        """This deal was pushed lately, as loud: the push is held back anyway, so no check is worth making."""
        previous = self.db.last_alert(fingerprint, time.time() - self.s.dedup_hours * 3600)
        return previous is not None and verdict.tier <= previous[1]

    def _flipkart_now(self, verdict: Verdict, url: str, marked: bool, name: str, text: str) -> Optional[Verdict]:
        """A deal post's Flipkart product, looked at on Flipkart before it is pushed (as the price
        tracker's drops are). Sold out there: not pushed; from a channel the owner marked, pushed
        with a warning. Far dearer there than the post says, with no coupon or card named: over, not
        pushed. In stock: the push says so, and at what price. A page that cannot be read changes
        nothing: a check must never cost a loot."""
        verdict.stock_checked = True
        offer, problem, when = self._flipkart_offer(url)
        if offer is not None and offer.title:
            verdict.product_name = offer.title
        if offer is None or offer.in_stock is None:
            self._count("stock_unknown")
            self._unchecked(verdict, verdict.price, marked)
            log.info("stock check: %s; pushed as it is | %s", "the page does not say" if offer is not None else
                     f"could not read the page ({problem})", name)
            return verdict
        if offer.in_stock is False:
            self._count("stock_sold_out")
            if not marked:
                log.info("stock check: sold out on Flipkart already, not pushed%s | %s", f" ({when[2:]})" if when else "",
                         name)
                return None
            verdict.flags.append("sold out on Flipkart when checked")
            log.info("stock check: sold out on Flipkart already, pushed with a warning (a channel you marked) | %s", name)
            return verdict
        self._count("stock_in_stock")
        dearer = bool(verdict.price) and offer.price > verdict.price * 1.15 + 1
        if dearer and offer.price > verdict.price * 1.5 and not marked and not _EFFECTIVE_PRICE.search(text):
            self._count("stock_price_up")
            log.info("stock check: %s on Flipkart, not %s: over, not pushed | %s", fmt_inr(offer.price),
                     fmt_inr(verdict.price), name)
            return None
        if dearer:
            verdict.notes.append(f"in stock on Flipkart just now, at {fmt_inr(offer.price)} before any coupon or card offer")
        else:
            verdict.notes.append(f"in stock on Flipkart just now, at {fmt_inr(offer.price)}")
        log.info("stock check: in stock at %s | %s", fmt_inr(offer.price), name)
        return verdict

    def _push_summary(self) -> str:
        """How late Telegram pushed posts since the last scan line, to see what the push is worth."""
        with self._stats_lock:
            lags, self._push_lags = sorted(self._push_lags), []
        if not lags:
            return ""
        return f" | Telegram pushed {len(lags)} post(s) {lags[0]:.0f}-{lags[-1]:.0f}s after they were published"

    def _claim(self, source: str, ids: List[str]) -> Set[str]:
        """The ids not seen before, now marked seen: a post read two ways is handled only once."""
        with self._claim_lock:
            fresh = self.db.unseen(source, ids)
            if fresh:
                self.db.mark_seen(source, fresh)
        return fresh

    def _live_post(self, code: str, post) -> None:
        """A post Telegram pushed as it was published: ``code`` is an invite code, or "@username"
        for a public channel the account has joined."""
        if code.startswith("@"):
            source, label = f"tg:{code[1:]}", code
        else:
            source = f"tg:+{code}"
            label = (getattr(self.account, "titles", None) or {}).get(code) or self._display(source)
        if self.db.get(f"boot:{source}") == "":
            return  # not read once yet: that first reading decides what is old news
        age = max(0.0, time.time() - post.ts) if post.ts is not None else 0.0
        with self._stats_lock:
            self._push_lags.append(age)
        if not self._claim(source, [str(post.post_id)]):
            return  # a reading got there first
        self._count("posts")
        self._count("live")
        if age > self.s.max_post_age_min * 60:
            self._count("stale")
            return
        log.info("live post in %s, %.0fs after it was published: %s", label, age, post.url)
        self._handle_post(source, label, post.text, post.analysis_text, post.links, post.url, post.ts)

    def _scan_feed(self, url: str, deadline: float, record: bool = True) -> str:
        """One line of feeds.txt: an RSS/Atom feed, or the new-deals page of a deal site.

        Returns "refused" when the site refused the request, what else went wrong, or "".
        ``record``: count the reading in the source's health (a feed read every few seconds does
        so once a minute, like a scan).
        """
        source = source_key("feed", url)
        reader = site_reader(url)
        headers = None if reader else {"Accept": "application/atom+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5"}
        result = self.fetcher.get(url, headers=headers, deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            if record:
                self._count("unreached")
            return result.error
        if not result.ok:
            if record:
                self.db.health_fail(source, result.problem)
                self._count("source_failures")
            return "refused" if result.blocked else result.problem

        def failed(why: str) -> str:
            if record:
                self.db.health_fail(source, why)
                self._count("source_failures")
            return why

        try:
            items = reader(result.text, url, time.time()) if reader else parse_feed(result.text)
        except (FeedError, SiteError) as exc:
            return failed(str(exc))
        except Exception as exc:  # a page nobody foresaw: say so through the health record
            log.exception("could not read %s", short_url(url))
            return failed(f"the page could not be read ({type(exc).__name__}); has its layout changed?")
        previous_ok = self.db.last_ok(source)
        if record:
            self.db.health_ok(source, len(items))
            self._deal_source_read()
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self._claim(source, [i.item_id for i in items])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return ""
        now = time.time()
        max_age = self._max_post_age(previous_ok, now)
        label = site_label(url) if reader else host_of(url)
        for item in items:
            if item.item_id not in fresh:
                continue
            if getattr(item, "tracked", False) and item.links:  # an RSS item is never a tracker's
                product = product_key(item.links[0])
                if product:  # remembered even when too old to alert: it tells what the product usually costs
                    self.db.note_tracker(product, item.title, item.price, item.usual, item.low, item.high, item.mrp,
                                         item.new_low, item.in_stock, item.links[0], now)
                    # A loot a channel posted for it goes first, as "Checked loot" with the post's
                    # words; the tracker's own alert about the same product is then a repeat.
                    self._verify_loots([product])
            self._count("posts")
            if item.ts is None and first_contact:
                continue
            if item.ts is not None and now - item.ts > max_age:
                self._count("stale")
                continue
            try:
                if not reader:
                    self._handle_post(source, label, item.text, item.text, item.links, item.url, item.ts)
                    continue
                # A deal-site card states its prices in fields of their own; a tracker item is
                # nothing but measurements. Neither needs its numbers read out of the wording.
                if item.tracked:
                    verdict = evaluate_tracked(item.title, item.price, item.usual, self.s, low=item.low,
                                               new_low=item.new_low, mrp=item.mrp, in_stock=item.in_stock)
                    near = "" if verdict else tracker_near_miss(item.price, item.usual, item.low, item.new_low,
                                                                item.mrp, title=item.title)
                else:
                    verdict = evaluate_stated(item.title, item.body, item.price, item.mrp, self.s, links=item.links)
                    near = "" if verdict or not item.mrp or not item.price or item.price >= item.mrp * 0.25 else \
                        f"{(item.mrp - item.price) / item.mrp * 100:.0f}% off a list price of {fmt_inr(item.mrp)}"
                near_key = f"{item.url}:{int(item.price or 0)}"
                if near and time.time() - self._near_logged.get(near_key, 0.0) > 3600:
                    # Close to a loot, left out: the day's log shows it (once, not at every relisting).
                    self._near_logged[near_key] = time.time()
                    log.info("skipped %s %s: %s | %s", label, item.url, near, _first_line(item.title)[:80])
                if verdict and item.tracked:
                    verdict = self._still_there(verdict, item)
                if verdict and item.tracked and self._another_variant(item.title, verdict):
                    continue
                if verdict:
                    self._announce(verdict, source, label, item.text, item.links, item.url, item.ts)
            except Exception:
                log.exception("failed to process feed item %s", item.url)
        return ""

    def _another_variant(self, title: str, verdict: Verdict) -> bool:
        """Another size or colour of this product was pushed lately, at least as loud: a shoe that drops
        in nine sizes is one deal, not nine pushes."""
        model = model_name(title)
        if not model:
            return False
        key, now, name = "model:" + model, time.time(), title.strip().lower()[:120]
        stamp, _, rest = self.db.get(key).partition(":")
        tier, _, pushed = rest.partition(":")
        if (stamp.isdigit() and tier.isdigit() and now - int(stamp) < self.s.dedup_hours * 3600
                and int(tier) >= verdict.tier and pushed != name):
            log.info("not pushed: %s is another size or colour of a product pushed lately", title[:80])
            return True
        self.db.set(key, f"{int(now)}:{verdict.tier}:{name}")
        return False

    def _scan_watch(self, item: WatchItem, deadline: float) -> None:
        source = source_key("watch", item.url)
        host = host_of(item.url)
        result = self.fetcher.get(item.url, headers=request_headers(item.url), deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            self._count("unreached")
            return
        if not result.skipped:
            # Only a real request counts as the page's turn. While its store is being left alone
            # after a refusal, the page keeps its place: first in line when the store answers again.
            self._watch_checked[item.url] = time.time()
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            self._site_result(host, False, result.problem)
            return
        page = extract_page(item.url, result.text)
        if page.kind == "none":
            self.db.health_fail(source, page.note)
            self._count("source_failures")
            self._site_result(host, False, page.note)
            return
        self._site_result(host, True)
        previous_ok = self.db.last_ok(source)
        listing = page.kind == "listing"
        # A listing that used to show list prices and now shows none has changed its layout: the
        # products still parse, but the rules that lean on the list price have gone blind. Say so
        # through the health record, and keep judging what can still be judged.
        with_list_price = sum(1 for o in page.offers if o.mrp)
        if listing and len(page.offers) >= 5 and not with_list_price and self.db.get(f"mrps:{source}"):
            self.db.health_fail(source, "products are listed but none shows a list price any more (layout change?)")
            self._count("source_failures")
        else:
            self.db.health_ok(source, len(page.offers))
            # "This page shows list prices" is only said of a page where that is the rule (a third of
            # its cards or more): one iPhone with a crossed-out price does not make it so.
            if listing and with_list_price * 3 >= len(page.offers) and not self.db.get(f"mrps:{source}"):
                self.db.set(f"mrps:{source}", 1)
        # The first read of a search/category page only records what is normal on it. Everything
        # after that is judged against that picture, so the page's permanently cheap items stay quiet.
        baseline = listing and self.db.get(f"boot:{source}") == ""
        now = time.time()
        for offer in page.offers:
            self._count("prices")
            info = self.db.record_price(offer.key, offer.store, offer.title, offer.url, offer.price, offer.mrp)
            if info.last_age is not None:
                # "Back after a long absence" only means something if this page was being read
                # while the product was away. After an outage of the monitor itself, nothing left.
                seen_at = now - info.last_age
                if previous_ok is None or previous_ok - seen_at < self.s.absence_hours * 3600:
                    info.last_age = 0.0
            verdict = evaluate_offer(offer.title, offer.price, offer.mrp, info, self.s,
                                     target=item.target, listing=listing, baseline=baseline)
            if baseline or verdict:
                self.db.mark_quiet(offer.key, offer.price, alerted=bool(verdict))  # this price is no longer news
            if not verdict:
                continue
            name = offer.title if listing else (item.label or offer.title)
            where = f"{offer.store} · {item.label}" if (listing and item.label) else offer.store
            body = f"{offer.title}\n\n{verdict.reason} · {where} (read from the store)"
            payload = build_payload(
                title=f"{verdict.headline} — {name[:70]}", message=body, priority=verdict.tier,
                tags=_TAGS[verdict.tier], click=offer.url, actions=[{"label": "Open product", "url": offer.url}],
            )
            self._emit(offer.key, verdict, payload, source)
        if baseline:
            self.db.set(f"boot:{source}", int(time.time()))

    # ----------------------------------------------------- alert pipeline ---
    def _handle_post(self, source: str, label: str, text: str, analysis_text: str, links: List[str],
                     post_url: str, ts: Optional[float]) -> None:
        if self._is_trial_channel(source):
            # On trial ("?" in channels.txt): every post goes to the log with what the rules would make
            # of it, and nothing is pushed, so that a channel can be judged before it reaches a phone.
            verdict = evaluate_text(analysis_text, self.s, links=links)
            log.info("%s post %s (on trial, not pushed): %s | %s", label, post_url,
                     f"would be {TIER_NAMES.get(verdict.tier, verdict.tier)} ({verdict.reason})" if verdict
                     else "nothing: " + why_skipped(analysis_text, links), _first_line(text)[:90])
            return
        marked = self._is_loot_channel(source)
        verdict = evaluate_text(analysis_text, self.s, links=links, loot_channel=marked)
        if verdict is None and not marked:
            verdict = self._crowd_pick(source, analysis_text, links)
        if marked:
            # Every post of the channels the owner marked is accounted for, pushed or not.
            log.info("%s post %s: %s | %s", label, post_url,
                     f"judged {TIER_NAMES.get(verdict.tier, verdict.tier)} ({verdict.reason})" if verdict
                     else "not pushed: " + why_skipped(analysis_text, links), _first_line(text)[:70])
        elif verdict is None and looks_like_loot(analysis_text):
            # What a missed loot would look like, left out: the day's log shows it, and why.
            log.info("skipped %s %s: %s | %s", label, post_url, why_skipped(analysis_text, links),
                     _first_line(text)[:80])
        if verdict is not None:
            self._announce(verdict, source, label, text, links, post_url, ts)

    def _crowd_pick(self, source: str, text: str, links: List[str]) -> Optional[Verdict]:
        """A deal under the bar on its own that CROWD_CHANNELS channels post within the hour."""
        lower = dataclasses.replace(self.s, mrp_min_discount=CROWD_MIN_DISCOUNT,
                                    min_discount=min(self.s.min_discount, CROWD_MIN_DISCOUNT),
                                    urgent_min_saving=min(self.s.urgent_min_saving, self.s.min_saving))
        verdict = evaluate_text(text, lower, links=links)
        if verdict is None:
            return None
        channels = self.db.add_sighting("crowd:" + text_fingerprint(text, verdict.price), source, CROWD_WINDOW)
        if channels < CROWD_CHANNELS:
            return None
        verdict.tier = verdict.max_tier = min(verdict.tier, TIER_NORMAL)
        verdict.reason += f"; posted by {channels} channels within the hour"
        return verdict

    def _is_trial_channel(self, source: str) -> bool:
        """Is this a channel marked "?" (on trial) in channels.txt?"""
        if source.startswith("tg:+"):
            return source[4:] in self.s.trial_channels
        return source.startswith("tg:") and source[3:].lower() in self.s.trial_channels

    def _is_loot_channel(self, source: str) -> bool:
        """Is this a channel marked "!" in channels.txt?"""
        if source.startswith("tg:+"):
            return source[4:] in self.s.loot_channels
        return source.startswith("tg:") and source[3:].lower() in self.s.loot_channels

    def _announce(self, verdict: Verdict, source: str, label: str, text: str, links: List[str],
                  post_url: str, ts: Optional[float]) -> None:
        """Check where a judged deal's links lead, then push it."""
        candidates = deal_links(links)
        best_link, fingerprint, trusted, shop_link = "", None, False, False
        strange_host, unchecked, payment_app = "", "", ""
        marked = self._is_loot_channel(source)
        for link in candidates[:3]:
            host = host_of(link)
            kind = classify_host(host)
            final = link
            if kind not in ("store", "dealsite"):
                resolved = self.resolver.resolve(link) if self.s.resolve_links else None
                if resolved and resolved.kind == "store":
                    kind, final = "store", resolved.final
                elif kind in ("unknown", "generic"):
                    if resolved and resolved.trusted:
                        kind = resolved.kind
                    elif resolved and not resolved.error:
                        strange_host = strange_host or resolved.host or host  # it loaded, and it is not a shop we know
                    else:
                        unchecked = unchecked or f"{host} ({resolved.error if resolved else 'link checking is off'})"
            if kind == "store" and is_payment_app(host_of(final)) and not marked:
                payment_app = payment_app or host_of(final)  # vouchers, tickets, memberships: not a shop
                continue
            if kind in TRUSTED_KINDS:
                trusted = True
                shop_link = shop_link or kind in ("store", "affiliate")
                if not best_link or kind == "store":
                    best_link = final
                if kind == "store":
                    fingerprint = product_key(final)
                    break
        if payment_app and not shop_link:
            log.info("%s post %s: not pushed: its link leads to a payment app (%s)", label, post_url, payment_app)
            return
        if verdict.needs_store_link and not shop_link and (strange_host or not candidates or trusted):
            if marked:
                log.info("%s post %s: not pushed: its link does not lead into a shop (%s)", label, post_url,
                         strange_host or (host_of(candidates[0]) if candidates else "no link"))
            return  # the post was only worth a push for a link into a shop, and it leads elsewhere
        if links and not candidates and any(classify_host(host_of(link)) == "telegram" for link in links):
            # "BIGGEST LOOT: iPhone at ₹2,952" with nothing but a link to a Telegram folder is an
            # advert for other channels.
            verdict.tier = min(verdict.tier, TIER_NORMAL)
            verdict.max_tier = min(verdict.max_tier, TIER_NORMAL)
            verdict.flags.append("the post links only to Telegram channels, not to a shop")
        if candidates and not trusted:
            if verdict.needs_trusted_link:
                return
            if strange_host:
                # Possibly a look-alike shop: show it, quietly, and say why.
                verdict.tier = verdict.max_tier = TIER_NORMAL
                verdict.flags.append(f"link goes to {strange_host}, not a known store - check before you pay")
            else:
                # We simply could not tell (short link timed out, ...): do not bury it, do not shout either.
                verdict.tier = min(verdict.tier, TIER_HIGH)
                verdict.max_tier = min(verdict.max_tier, TIER_HIGH)
                verdict.flags.append(f"could not check where the link goes: {unchecked} - look at the address before you pay")
            best_link = candidates[0]
        elif not candidates and verdict.needs_trusted_link:
            return
        if (fingerprint and fingerprint.startswith("flipkart:") and not verdict.stock_checked
                and host_of(best_link).endswith("flipkart.com") and "/p/" in best_link
                and not self._pushed_already(fingerprint, verdict)):
            if self._flipkart_now(verdict, best_link, marked, _first_line(text)[:70], text) is None:
                return
        # A coupon is the same coupon whatever link a channel puts under it.
        fingerprint = verdict.key or fingerprint or text_fingerprint(text, verdict.price)
        subject = _first_line(text)
        if verdict.product_name and _says_little(subject):
            subject = _first_line(verdict.product_name)  # "Loot at 398.": the store's page names the product
        elif _says_little(subject) and fingerprint.startswith(("amazon:", "flipkart:")):
            # "199 : https://amzn.to/...": the price tracker may have listed the product lately.
            seen = self.db.tracker_item(fingerprint, time.time() - TRACKER_MEMORY)
            if seen and seen[0]:
                subject = _first_line(seen[0])
        # A loot posted without a list price is checked against the price tracker (Amazon and
        # Flipkart): at once if it has listed the product lately, otherwise for the next hour.
        product = fingerprint if fingerprint.startswith(("amazon:", "flipkart:")) else ""
        to_check = bool(product and verdict.label in ("Loot", "Quick loot") and verdict.mrp is None and verdict.price)
        if to_check:
            checked = self._records_say(product, verdict.price, subject)
            if checked is not None and checked.tier > verdict.tier:
                verdict.tier = verdict.max_tier = checked.tier
                verdict.was, verdict.discount, verdict.label = checked.was, checked.discount, checked.label
                verdict.reason = f"{verdict.reason}; {checked.reason}"
                to_check = False

        lines = [text[:600].strip(), ""]
        lines.extend("⚠ " + flag for flag in verdict.flags)
        lines.extend("Note: " + note for note in verdict.notes)
        footer = [verdict.reason, label]
        if ts:
            footer.append("posted " + _age(time.time() - ts))
        if best_link:
            footer.append("→ " + host_of(best_link))
        lines.append(" · ".join(footer))
        actions = []
        if best_link:
            actions.append({"label": "Open deal", "url": best_link})
        if post_url:
            actions.append({"label": "Open post", "url": post_url})
        payload = build_payload(
            title=verdict.headline + (f" — {subject}" if subject else ""),
            message="\n".join(lines), priority=verdict.tier,
            tags=["warning"] if verdict.flags else _TAGS[verdict.tier],
            click=best_link or post_url, actions=actions,
        )
        sent = self._emit(fingerprint, verdict, payload, source, posted=ts, name=subject)
        if sent and to_check:
            self.db.add_loot_check(product, fingerprint, verdict.price, subject, source, best_link, post_url, text)

    def _records_say(self, product: str, price: float, name: str) -> Optional[Verdict]:
        """What is known of a product's usual price, as a check of a loot's price: the price tracker's
        list (Amazon, Flipkart), then the bot's own readings of the store's pages (Flipkart). A
        verdict when the price stands far below what the product usually costs."""
        seen = self.db.tracker_item(product, time.time() - TRACKER_MEMORY)
        if seen:
            title, tracked_price, usual, low, _high, mrp, new_low, in_stock, _url, _seen_at = seen
            # Out of stock there, or a reading of its own above the loot price: not borne out by it.
            if in_stock and (tracked_price is None or tracked_price <= price * 1.15 + 1):
                verdict = confirm_loot(name or title or "", price, usual, self.s, low=low, new_low=bool(new_low),
                                       mrp=mrp)
                if verdict is not None:
                    return verdict
        record = self.db.product_record(product) if product.startswith("flipkart:") else None
        if record is not None:
            title, mrp, _last_price, _last_seen, usual, _readings = record
            return confirm_loot(name or title or "", price, usual, self.s, mrp=mrp,
                                where="against its Flipkart page as the monitor has read it")
        return None

    def _verify_loots(self, products: List[str]) -> None:
        """Loots pushed without a list price whose products the tracker has just listed: a big loot
        gets a second, louder push. Runs with the tracker's reading, never holding up a scan."""
        with self._verify_lock:
            now = time.time()
            for product, fingerprint, price, name, source, link, post_url, text, created in \
                    self.db.open_loot_checks(products, now - LOOT_CHECK_WINDOW):
                seen = self.db.tracker_item(product, now - TRACKER_MEMORY)
                if not seen or (seen[1] is not None and seen[1] > price * 1.15 + 1):
                    continue  # not borne out yet: a later reading may still
                self.db.close_loot_check(product)  # one answer per loot
                verdict = self._records_say(product, price, name)
                if verdict is None:
                    log.info("checked on the price tracker, not a big loot: %s", name or product)
                    continue
                lines = [f"Pushed earlier without a list price; {verdict.reason}.", "", (text or "")[:400].strip(), "",
                         " · ".join([self._display(source)] + ([f"→ {host_of(link)}"] if link else []))]
                actions = ([{"label": "Open deal", "url": link}] if link else []) + (
                    [{"label": "Open post", "url": post_url}] if post_url else [])
                payload = build_payload(
                    title=verdict.headline + (f" — {name}" if name else ""), message="\n".join(lines),
                    priority=verdict.tier, tags=_TAGS[verdict.tier], click=link or post_url, actions=actions,
                )
                self._emit(fingerprint, verdict, payload, source, posted=created)

    def _twin(self, name: str, price: float, since: float) -> Optional[tuple]:
        """An alert already raised for the same product at the same price, found by its name.

        A deal read from a deal site's page, and many a channel post, carries no link that can be
        followed to the store, so nothing ties it to the same deal arriving from elsewhere but
        what it is called and what it costs.
        """
        for alert_id, tier, alert_price, created_at, fingerprint, title in self.db.alerts_near_price(price, since):
            if same_product_name(name, str(title).rsplit(" — ", 1)[-1]):
                return alert_id, tier, alert_price, created_at, fingerprint
        return None

    def _emit(self, fingerprint: str, verdict: Verdict, payload: Dict[str, object], source: str,
              posted: Optional[float] = None, name: str = "") -> bool:
        """De-duplicate, respect the daily budget, then send (or queue for retry)."""
        with self._emit_lock:
            now = time.time()
            since = now - self.s.dedup_hours * 3600
            # A store's product the tracker lists again day after day ("LEGO Speed Champions" at ₹319, a
            # Zebronics soundbar at ₹1,170, both pushed again every 13-24 hours on 22-25 Nov 2026) is news
            # once: again only if it gets cheaper, or louder.
            remembered = now - max(self.s.dedup_hours, PRODUCT_REPEAT_HOURS) * 3600 if fingerprint.startswith(
                ("amazon:", "flipkart:", "myntra:", "ajio:")) else since
            previous = self.db.last_alert(fingerprint, remembered)
            if previous is None and name and verdict.price is not None:
                twin = self._twin(name, verdict.price, since)
                if twin:
                    previous, fingerprint = twin[:4], twin[4]  # from here on it is that deal
            sources = self.db.add_sighting(fingerprint, source, self.s.trending_window_min * 60, now)
            if previous:
                _, prev_tier, prev_price, _ = previous
                cheaper = verdict.price is not None and prev_price is not None and verdict.price <= prev_price * 0.9
                louder = verdict.tier > prev_tier
                raised = 0
                if (not louder and sources >= self.s.trending_channels and prev_tier < TIER_URGENT
                        and self.db.get(f"trend:{fingerprint}") == ""):
                    # Channels copy each other, so a repeat is weak evidence: one step louder, no more,
                    # and never past what this post's link and wording allow. Copy-paste across
                    # channels is also how scam posts spread.
                    raised = min(verdict.max_tier, prev_tier + 1)
                    raised = raised if raised > prev_tier else 0
                if not (cheaper or louder or raised):
                    self._count("duplicates")
                    return False
                if raised:
                    self.db.set(f"trend:{fingerprint}", int(now))
                    verdict.tier = raised
                    payload = dict(payload, priority=raised, tags=["warning"] if verdict.flags else _TAGS[raised],
                                   title=f"Now in {sources} channels: {payload.get('title', '')}"[:180])
                elif cheaper:
                    payload = dict(payload, title=f"Lower now: {payload.get('title', '')}"[:180])

            day_start = now - ((now + self.s.tz_offset_minutes * 60) % 86400)
            sent_today = sum(self.db.sent_counts(day_start).values())
            limit = self.s.daily_alert_limit
            over_budget = (
                sent_today >= limit + 40
                or (sent_today >= limit and verdict.tier < TIER_URGENT)
                or (sent_today >= limit * 0.7 and verdict.tier <= TIER_NORMAL)
            )
            if over_budget:
                self._count("over_budget")
                log.warning("daily alert budget reached (%d sent); not pushing: %s", sent_today, payload.get("title"))
                return False

            alert_id = self.db.add_alert(fingerprint, verdict.tier, verdict.price, str(payload.get("title", "")),
                                         source, json.dumps(payload, ensure_ascii=False), now)
            log.info("ALERT [%s] %s (%s%s%s)", TIER_NAMES.get(verdict.tier, verdict.tier), payload.get("title"),
                     self._display(source), f", posted {_age(now - posted)}" if posted else "",
                     f", opens {host_of(str(payload['click']))}" if payload.get("click") else "")
            self._deliver(alert_id, payload, verdict.tier)
            return True

    def _deliver(self, alert_id: int, payload: Dict[str, object], tier: int) -> bool:
        if not self.notifier.configured and not self.notifier.dry_run:
            if not self._warned_no_topic:
                log.warning("NTFY_TOPIC is not set: alerts are only being logged, not pushed")
                self._warned_no_topic = True
            # Nothing reached a phone, so this must neither count as sent nor hide a later copy of the deal.
            self.db.drop_alert(alert_id)
            return False
        if time.time() < self._ntfy_pause_until:
            return False  # ntfy asked us to wait; waiting is not a failed attempt
        result = self.notifier.send(payload)
        if result.ok:
            self.db.mark_sent(alert_id)
            self._count(f"sent_{TIER_NAMES.get(tier, 'other')}")
            return True
        self.db.mark_attempt(alert_id)
        self._count("send_failures")
        if result.status == 429:
            self._ntfy_pause_until = time.time() + min(900.0, max(30.0, result.retry_after))
        elif 400 <= result.status < 500 and result.status not in (401, 403):
            # ntfy refuses this very message (malformed, too large): sending it again cannot help,
            # and neither would raising the same alert again.
            self._give_up(alert_id, f"ntfy rejected it with HTTP {result.status}", reopen=False)
        return False

    def _give_up(self, alert_id: int, why: str, reopen: bool = True) -> None:
        """Write off an alert that could not be delivered.

        With ``reopen`` its deal may alert afresh: if it was a store product that is still at that
        price, the next scan raises a new alert, which gets its own half hour of retries.
        """
        fingerprint = self.db.drop_alert(alert_id)
        if fingerprint and reopen:
            self.db.clear_quiet(fingerprint)
        self._count("undelivered")
        log.warning("giving up on an alert: %s", why)

    def flush_outbox(self) -> None:
        """Retry alerts that could not be delivered; give up on ones too old to be useful."""
        now = time.time()
        for pending in self.db.pending_alerts():
            if now - pending.created_at > OUTBOX_MAX_AGE:
                self._give_up(pending.id, f"undelivered since {_age(now - pending.created_at)}")
                continue
            try:
                payload = json.loads(pending.payload)
            except ValueError:
                self.db.drop_alert(pending.id)
                continue
            self._deliver(pending.id, payload, pending.tier)

    # ---------------------------------------------------------- the cycle ---
    def _watch_batch(self) -> List[WatchItem]:
        """Watchlist entries to check this cycle, capped per site by its pacing.

        The most overdue page goes first. A page marked "!" or "!!" in the list counts its wait
        4 or 12 times over, so it comes round that much more often than an unmarked one.
        """
        budget: Dict[str, int] = {}
        chosen: List[WatchItem] = []
        now = time.time()

        def overdue(item: WatchItem) -> float:
            last = self._watch_checked.get(item.url)
            return float("inf") if not last else max(0.0, now - last) * item.weight

        for item in sorted(self.s.watchlist, key=overdue, reverse=True):
            last = self._watch_checked.get(item.url)
            if item.every and last and now - last < item.every - 5:
                continue  # read recently enough for this page
            host = (urlsplit(item.url).hostname or "").lower()
            if host not in budget:
                policy = policy_for(host)
                gap = max(0.5, (policy.min_gap + policy.max_gap) / 2)
                budget[host] = max(1, int(self.s.interval * 0.8 / gap))
            if budget[host] > 0 and not self.fetcher.cooling_down(item.url):
                budget[host] -= 1
                chosen.append(item)
            elif self.fetcher.cooling_down(item.url) and budget[host] > 0:
                budget[host] = 0  # one skipped attempt per cooling site keeps its failure streak honest
                chosen.append(item)
        return chosen

    def cycle(self, deadline_after: Optional[float] = None) -> Dict[str, int]:
        started = time.time()
        deadline = started + (deadline_after if deadline_after is not None else max(20.0, self.s.interval - 3.0))
        before = collections.Counter(self.stats)
        if self.db.get("heartbeat_at") == "":
            self.db.set("heartbeat_at", int(started))  # the daily summary counts from the very first scan
        self.flush_outbox()
        self._cycle_no += 1
        self._deal_reads = 0
        self._site_scan = {}

        # Store pages first, and the channels in a different order each scan: when time runs out,
        # it must not always be the same sources that go unread.
        channels = list(self.s.channels)
        if channels:
            shift = self._cycle_no % len(channels)
            channels = channels[shift:] + channels[:shift]
        # Deal sites first: a deal is on their page before it is anywhere else, and each is one
        # quick request to a site of its own.
        often = self._feeds_read_often()  # those are read every few seconds already
        jobs = [(self._scan_feed, url) for url in self.s.feeds if url not in often]
        jobs += [(self._scan_channel, channel) for channel in channels]
        if not self._fast_reading():  # otherwise they are read every few seconds already
            jobs += [(self._scan_private, code) for code in self._private]
        jobs += [(self._scan_watch, item) for item in self._watch_batch()]
        # One thread per job (they mostly wait their turn for their site): a site that hangs then
        # holds up only its own pages, never another site's, and nothing queues behind a store
        # whose dozen pages take the whole minute.
        threads = max(1, min(128, max(self.s.workers, len(jobs))))
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads, thread_name_prefix="scan") as pool:
            futures = {pool.submit(fn, arg, deadline): arg for fn, arg in jobs}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    log.exception("scan task failed: %r", futures[future])

        self.db.incr("hb:cycles")
        if self._feed_sources():
            if self._deal_reads:
                self.db.set("blind:scans", 0)
            else:
                self.db.incr("blind:scans")  # failed, refused or never reached in time: all the same to the user
        delta = collections.Counter(self.stats)
        delta.subtract(before)
        self.db.incr("hb:posts", delta.get("posts", 0))
        self._site_checks()
        self._health_checks()
        self._heartbeat()
        delta["seconds"] = int(time.time() - started)
        return dict(delta)

    # -------------------------------------------------------- self-checks ---
    def _feed_sources(self) -> List[str]:
        return ([f"tg:{c}" for c in self.s.channels] + [f"tg:+{code}" for code in self._private]
                + [source_key("feed", u) for u in self.s.feeds])

    def _all_sources(self) -> List[str]:
        return self._feed_sources() + [source_key("watch", w.url) for w in self.s.watchlist]

    def _site_checks(self) -> None:
        """One clear alarm when a whole store stops answering, instead of a trickle of page notes."""
        for host, outcomes in self._site_scan.items():
            if any(ok for ok, _ in outcomes):
                self.db.set(f"sitefail:{host}", 0)
                if self.db.get(f"sitealarm:{host}", "0") not in ("", "0"):
                    self.db.set(f"sitealarm:{host}", 0)
                    self.notifier.send_status(f"Loot monitor: {host} is readable again",
                                              f"The watched pages on {host} are being read again.",
                                              priority=3, tags=("white_check_mark",))
                continue
            streak = self.db.incr(f"sitefail:{host}")
            if streak >= SITE_FAIL_STREAK and self.db.get(f"sitealarm:{host}", "0") in ("", "0"):
                reason = collections.Counter(why for _, why in outcomes).most_common(1)[0][0]
                sent = self.notifier.send_status(
                    f"Loot monitor: store pages unreadable ({host})",
                    f"No watched page on {host} could be read in the last {streak} scans, so a price error there "
                    f"cannot be seen right now.\nLast reason: {reason}",
                    priority=4, tags=("warning",),
                )
                if sent.ok or not self.notifier.configured:
                    self.db.set(f"sitealarm:{host}", int(time.time()))
                log.error("no watched page on %s has been readable for %d scans: %s", host, streak, reason)

    def _health_checks(self) -> None:
        rows = {row[0]: row for row in self.db.health_rows()}
        feeds = self._feed_sources()
        now = time.time()

        # 1. Nothing that brings deals in could be read for several scans: say so loudly.
        blind_scans = int(self.db.get("blind:scans", "0") or 0)
        blind = bool(feeds) and blind_scans >= BLIND_STREAK
        if blind:
            last = float(self.db.get("blind_notified", "0") or 0)
            if now - last > 3 * 3600:
                errors = collections.Counter(
                    str(rows[s][6]) if s in rows and rows[s][3] else "not reached in the time a scan allows" for s in feeds
                )
                detail = "; ".join(f"{count}× {error}" for error, count in errors.most_common(3))
                sent = self.notifier.send_status(
                    "Loot monitor is blind",
                    f"None of the {len(feeds)} deal sources could be read in the last {blind_scans} scans, so "
                    f"nothing posted there can be caught right now.\n{detail}",
                    priority=4, tags=("warning",),
                )
                if sent.ok or not self.notifier.configured:
                    self.db.set("blind_notified", int(now))
            log.error("no deal source has been readable for %d scans - the monitor is blind", blind_scans)
        elif self.db.get("blind_notified", "0") not in ("", "0") and feeds:
            self.db.set("blind_notified", 0)
            self.notifier.send_status("Loot monitor recovered", "Deal sources are readable again.", priority=3,
                                      tags=("white_check_mark",))

        # 2. Individual sources that have crossed the "this is broken" line: reported once, and
        #    again only after they have worked in between. Pages of a store that is unreadable as
        #    a whole are covered by that store's own alarm.
        broken = []
        for source in sorted(self._all_sources()):
            row = rows.get(source)
            if row is None:
                continue
            reported = self.db.get(f"reported:{source}") != ""
            if row[3] == 0 and reported:
                self.db.set(f"reported:{source}", "")
            elif row[3] >= FAIL_STREAK_NOTE and not reported:
                host = self._watch_hosts.get(source)
                if host is None or self.db.get(f"sitealarm:{host}", "0") in ("", "0"):
                    broken.append(row)
        if broken and not blind:
            listing = "\n".join(f"• {self._display(row[0])}: {row[6]}" for row in broken[:12])
            sent = self.notifier.send_status(
                f"{len(broken)} source(s) stopped working",
                f"Failed {FAIL_STREAK_NOTE} checks in a row:\n{listing}\n\nRun the doctor command for details.",
                priority=2, tags=("warning",),
            )
            if sent.ok or not self.notifier.configured:
                for row in broken:
                    self.db.set(f"reported:{row[0]}", int(now))

        # 3. Dead-man's switch: an outside service notices if these pings ever stop.
        if self.s.healthcheck_url and not blind and now - self._last_ping >= 55:
            self._last_ping = now
            try:
                requests.get(self.s.healthcheck_url, timeout=6)
            except requests.RequestException:
                log.debug("healthcheck ping failed")

    def _heartbeat(self) -> None:
        local = time.gmtime(time.time() + self.s.tz_offset_minutes * 60)
        today = time.strftime("%Y-%m-%d", local)
        # The daily clean-up does not depend on the daily note: with thousands of products
        # followed, a state file that is never cleaned grows by a megabyte a day.
        if self.db.get("pruned_day") != today:
            self.db.set("pruned_day", today)
            self.db.prune()
        if self.s.heartbeat_hour < 0:
            return
        if local.tm_hour < self.s.heartbeat_hour or self.db.get("heartbeat_day") == today:
            return
        first_ever = self.db.get("heartbeat_day") == ""
        self.db.set("heartbeat_day", today)
        if first_ever:
            return  # started today: the first summary comes tomorrow
        since = float(self.db.get("heartbeat_at", "0") or 0)
        counts = self.db.sent_counts(since)
        rows = self.db.health_rows()
        configured = set(self._all_sources())
        failing = [r for r in rows if r[0] in configured and r[3] >= FAIL_STREAK_NOTE]
        unread = configured - {r[0] for r in rows}
        message = (
            f"Since the last note: {self.db.get('hb:cycles', '0')} scans, {self.db.get('hb:posts', '0')} new posts read.\n"
            f"Alerts sent: {counts.get(TIER_URGENT, 0)} urgent, {counts.get(TIER_HIGH, 0)} high, "
            f"{counts.get(TIER_NORMAL, 0)} other.\n"
            f"Sources: {len(configured) - len(failing) - len(unread)} working, {len(failing)} failing"
            + (f", {len(unread)} not read yet" if unread else "") + "."
        )
        if failing:
            message += "\n" + "\n".join(f"• {self._display(r[0])}: {r[6]}" for r in failing[:8])
        self.notifier.send_status("Loot monitor is running", message, priority=2, tags=("green_heart",))
        self.db.set("heartbeat_at", int(time.time()))
        self.db.set("hb:cycles", 0)
        self.db.set("hb:posts", 0)

    def _startup_note(self, stats: Dict[str, int]) -> None:
        """One push, the first time this state file is ever used, so you know the pipeline works end to end."""
        if not self.s.startup_note or self.db.get("started_note") != "":
            return
        if not self.notifier.configured and not self.notifier.dry_run:
            return  # no topic yet: keep the note for the first scan that can actually deliver it
        rows = {row[0]: row for row in self.db.health_rows()}
        feeds = self._feed_sources()
        working = sum(1 for s in feeds if s in rows and rows[s][3] == 0)
        sent = self.notifier.send_status(
            "Loot monitor started",
            f"First scan done: {working} of {len(feeds)} deal sources readable, "
            f"{len(self.s.watchlist)} watchlist pages, {len(self.s.traps)} traps.\n"
            f"Deal posts alert from {max(self.s.min_discount, self.s.mrp_min_discount):.0f}% off their list price "
            f"(favourites from {self.s.favourite_min_discount:.0f}%), louder as the discount grows; a price error "
            f"on a watched store page arrives as urgent.",
            priority=3, tags=("rocket",),
        )
        if sent.ok:
            self.db.set("started_note", int(time.time()))

    # ------------------------------------------------------------ the loop ---
    def _adopt_old_names(self) -> None:
        """Earlier versions named a page's state after the first 60 characters of its address.
        Carry each page's baseline flag over to its new name, so no page has to be re-learnt.
        (Two pages that shared an old name start afresh: nobody can say whose flag it was.)"""
        old_names: Dict[str, List[str]] = {}
        for item in self.s.watchlist:
            old_names.setdefault(f"watch:{short_url(item.url, 60)}", []).append(source_key("watch", item.url))
        for old, new in old_names.items():
            flag = self.db.get(f"boot:{old}")
            if flag and len(new) == 1 and self.db.get(f"boot:{new[0]}") == "":
                self.db.set(f"boot:{new[0]}", flag)

    @staticmethod
    def _relieved(handover_file: str) -> bool:
        """True once the handover file is there. It names the run that takes over, or says "none"
        when no next run could be started and waiting any longer is pointless."""
        try:
            with open(handover_file, encoding="utf-8") as handle:
                word = handle.read(100).strip()
        except OSError:
            return False
        if word == "none":
            log.warning("no next run could be started; stopping at the end of this shift")
        else:
            log.info("the next run is ready: handing over")
        return True

    def run(self, once: bool = False, max_runtime: Optional[float] = None,
            handover_file: Optional[str] = None, max_overtime: float = 0.0) -> int:
        """Scan until stopped. Returns how many alerts had to be written off undelivered.

        ``max_runtime`` is the length of a shift. With ``handover_file`` the shift does not simply
        end: scanning goes on until that file appears (whoever starts the next run creates it
        once that run is ready to take over), for at most ``max_overtime`` seconds more.
        """
        self._adopt_old_names()
        self.db.forget_health(self._all_sources())
        if self.s.private_channels and not self._private:
            log.info("%d invite-only channel(s) in %s are skipped: no Telegram account is set up (README: "
                     "\"Invite-only channels\")", len(self.s.private_channels), self.s.channels_file)
        log.info(
            "watching %d Telegram channels, %d feeds, %d watchlist pages | deal posts from %.0f%% off (%d favourites from "
            "%.0f%%) | every %ds%s",
            len(self.s.channels) + len(self._private), len(self.s.feeds), len(self.s.watchlist),
            max(self.s.min_discount, self.s.mrp_min_discount), len(self.s.favourites), self.s.favourite_min_discount,
            self.s.interval,
            " | DRY RUN" if self.s.dry_run else "",
        )
        if self._private and hasattr(self.account, "listen"):
            self.account.listen(self._live_post, public=list(self.s.channels))  # posts as Telegram pushes them
        if (self._private and self.s.tg_join_channels and not once and not self.s.dry_run
                and hasattr(self.account, "missing_public")):
            if hasattr(self.account, "missing_private"):
                self.account.joining = True  # links not in the chat list: looked up by the joining only
            threading.Thread(target=self._join_channels, name="telegram-join", daemon=True).start()
        if self._private and self.s.tg_fast_seconds > 0 and not once:
            self._fast_thread = threading.Thread(target=self._read_private_often, name="telegram-fast", daemon=True)
            self._fast_thread.start()
            log.info("the invite-only channels are read every %gs through the Telegram login", self.s.tg_fast_seconds)
        if self._fast_feeds and self.s.fast_feed_seconds > 0 and not once:
            self._feed_thread = threading.Thread(target=self._read_feeds_often, name="fast-feeds", daemon=True)
            self._feed_thread.start()
            log.info("read every %gs: %s", self.s.fast_feed_seconds, ", ".join(short_url(u) for u in self._fast_feeds))
        started = time.time()
        number = 0
        last_duration = 0.0
        overtime = False
        while not self._stop.is_set():
            number += 1
            cycle_started = time.time()
            stats = self.cycle(deadline_after=120.0 if once else None)
            last_duration = time.time() - cycle_started
            log.info(
                "scan #%d: %ds | %d new posts (%d too old%s) | %d prices | alerts sent: %d urgent, %d high, %d other"
                " | %d duplicates | %d source failures%s",
                number, stats.get("seconds", 0), stats.get("posts", 0), stats.get("stale", 0),
                f", {stats['live']} sent live by Telegram" if stats.get("live") else "", stats.get("prices", 0),
                stats.get("sent_urgent", 0), stats.get("sent_high", 0), stats.get("sent_normal", 0),
                stats.get("duplicates", 0), stats.get("source_failures", 0),
                (f" | {stats['unreached']} not reached in time" if stats.get("unreached") else "") + self._push_summary(),
            )
            self._startup_note(stats)
            if once:
                break
            pause = max(1.0, cycle_started + self.s.interval - time.time())
            if max_runtime is not None and time.time() + pause + last_duration > started + max_runtime:
                if not handover_file:
                    break
                if not overtime:
                    overtime = True
                    log.info("shift over after %d scans; staying on duty until the next run is ready", number)
                if self._relieved(handover_file):
                    break
                if time.time() + pause > started + max_runtime + max_overtime:
                    log.warning("no next run became ready in %ds of overtime; stopping anyway", int(max_overtime))
                    break
                wake = time.time() + pause
                while time.time() < wake and not self._stop.is_set() and not os.path.exists(handover_file):
                    self._stop.wait(1.0)
                if self._relieved(handover_file):
                    break
                continue
            self._stop.wait(pause)
        self._fast_stop.set()
        for thread in (self._fast_thread, self._feed_thread):
            if thread is not None:
                thread.join(timeout=30)
        self.flush_outbox()
        if self.account is not None:
            self.account.close()
        return int(self.stats.get("undelivered", 0))
