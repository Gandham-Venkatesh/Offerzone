"""The scan loop: fetch every source, judge what is new, push what matters, watch its own health."""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import requests

from .config import Settings, WatchItem
from .db import DB
from .fetch import Fetcher, policy_for
from .links import TRUSTED_KINDS, LinkResolver, classify_host, deal_links, host_of, product_key, text_fingerprint
from .notify import Notifier, build_payload
from .scoring import (TIER_HIGH, TIER_NAMES, TIER_NORMAL, TIER_URGENT, Verdict, evaluate_offer, evaluate_stated,
                      evaluate_text, evaluate_tracked)
from .sources.dealsites import SiteError, site_label, site_reader
from .sources.feeds import FeedError, parse_feed
from .sources.stores import extract_page, request_headers
from .sources.telegram import parse_channel_page

log = logging.getLogger("lootwatch")

OUTBOX_MAX_AGE = 30 * 60  # a loot alert delivered later than this is only noise
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
                 resolver: Optional[LinkResolver] = None) -> None:
        self.s = settings
        self.db = db
        self.fetcher = fetcher
        self.notifier = notifier
        self.resolver = resolver or LinkResolver()
        self.stats: "collections.Counter[str]" = collections.Counter()
        self._stats_lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._watch_checked: Dict[str, float] = {}
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
        # Whose turn it is survives a restart: with a long list and short runs, the pages at the
        # top of the file would otherwise be read again and again and the rest hardly ever.
        attempts = {row[0]: max(row[4] or 0, row[5] or 0) for row in db.health_rows()}
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
            return  # ran out of time this cycle; not the channel's fault
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            return
        page = parse_channel_page(result.text, channel, result.final_url)
        if page.status != "ok":
            reason = {
                "no_preview": "no public preview (private, renamed or deleted channel)",
                "empty": "page loaded but shows no posts",
            }.get(page.status, "page layout not recognised")
            self.db.health_fail(source, reason)
            self._count("source_failures")
            return
        previous_ok = self.db.last_ok(source)
        self.db.health_ok(source, len(page.posts))
        self._deal_source_read()
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self.db.unseen(source, [str(p.post_id) for p in page.posts])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return
        self.db.mark_seen(source, fresh)
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
                self._handle_post(source, f"@{channel}", post.text, post.analysis_text, post.links, post.url, post.ts)
            except Exception:
                log.exception("failed to process %s", post.url)

    def _scan_feed(self, url: str, deadline: float) -> None:
        """One line of feeds.txt: an RSS/Atom feed, or the new-deals page of a deal site."""
        source = source_key("feed", url)
        reader = site_reader(url)
        headers = None if reader else {"Accept": "application/atom+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5"}
        result = self.fetcher.get(url, headers=headers, deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            return
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            return
        try:
            items = reader(result.text, url, time.time()) if reader else parse_feed(result.text)
        except (FeedError, SiteError) as exc:
            self.db.health_fail(source, str(exc))
            self._count("source_failures")
            return
        previous_ok = self.db.last_ok(source)
        self.db.health_ok(source, len(items))
        self._deal_source_read()
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self.db.unseen(source, [i.item_id for i in items])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return
        self.db.mark_seen(source, fresh)
        now = time.time()
        max_age = self._max_post_age(previous_ok, now)
        label = site_label(url) if reader else host_of(url)
        for item in items:
            if item.item_id not in fresh:
                continue
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
                                               new_low=item.new_low)
                else:
                    verdict = evaluate_stated(item.title, item.body, item.price, item.mrp, self.s)
                if verdict:
                    self._announce(verdict, source, label, item.text, item.links, item.url, item.ts)
            except Exception:
                log.exception("failed to process feed item %s", item.url)

    def _scan_watch(self, item: WatchItem, deadline: float) -> None:
        source = source_key("watch", item.url)
        host = host_of(item.url)
        result = self.fetcher.get(item.url, headers=request_headers(item.url), deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            return
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
        if listing and len(page.offers) >= 5 and not any(o.mrp for o in page.offers) and self.db.get(f"mrps:{source}"):
            self.db.health_fail(source, "products are listed but none shows a list price any more (layout change?)")
            self._count("source_failures")
        else:
            self.db.health_ok(source, len(page.offers))
            if listing and any(o.mrp for o in page.offers) and not self.db.get(f"mrps:{source}"):
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
        verdict = evaluate_text(analysis_text, self.s)
        if verdict is not None:
            self._announce(verdict, source, label, text, links, post_url, ts)

    def _announce(self, verdict: Verdict, source: str, label: str, text: str, links: List[str],
                  post_url: str, ts: Optional[float]) -> None:
        """Check where a judged deal's links lead, then push it."""
        candidates = deal_links(links)
        best_link, fingerprint, trusted = "", None, False
        strange_host, unchecked = "", ""
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
            if kind in TRUSTED_KINDS:
                trusted = True
                if not best_link or kind == "store":
                    best_link = final
                if kind == "store":
                    fingerprint = product_key(final)
                    break
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
        fingerprint = fingerprint or text_fingerprint(text, verdict.price)

        lines = [text[:600].strip(), ""]
        lines.extend("⚠ " + flag for flag in verdict.flags)
        lines.extend("Note: " + note for note in verdict.notes)
        footer = [verdict.reason, label]
        if ts:
            footer.append("posted " + _age(time.time() - ts))
        if best_link:
            footer.append("→ " + host_of(best_link))
        lines.append(" · ".join(footer))
        subject = _first_line(text)
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
        self._emit(fingerprint, verdict, payload, source, posted=ts)

    def _emit(self, fingerprint: str, verdict: Verdict, payload: Dict[str, object], source: str,
              posted: Optional[float] = None) -> bool:
        """De-duplicate, respect the daily budget, then send (or queue for retry)."""
        with self._emit_lock:
            now = time.time()
            sources = self.db.add_sighting(fingerprint, source, self.s.trending_window_min * 60, now)
            previous = self.db.last_alert(fingerprint, now - self.s.dedup_hours * 3600)
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
            log.info("ALERT [%s] %s (%s%s)", TIER_NAMES.get(verdict.tier, verdict.tier), payload.get("title"),
                     self._display(source), f", posted {_age(now - posted)}" if posted else "")
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
        jobs = [(self._scan_watch, item) for item in self._watch_batch()]
        jobs += [(self._scan_channel, channel) for channel in channels]
        jobs += [(self._scan_feed, url) for url in self.s.feeds]
        # One thread per job (they mostly wait their turn for their site): a site that hangs then
        # holds up only its own pages, never another site's.
        threads = max(1, min(32, max(self.s.workers, len(jobs))))
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
        return [f"tg:{c}" for c in self.s.channels] + [source_key("feed", u) for u in self.s.feeds]

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
        if self.s.heartbeat_hour < 0:
            return
        local = time.gmtime(time.time() + self.s.tz_offset_minutes * 60)
        today = time.strftime("%Y-%m-%d", local)
        if local.tm_hour < self.s.heartbeat_hour or self.db.get("heartbeat_day") == today:
            return
        first_ever = self.db.get("heartbeat_day") == ""
        self.db.set("heartbeat_day", today)
        self.db.prune()
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
            f"Deal posts alert from {self.s.min_discount:.0f}% off, louder as the discount grows; a price error "
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
        log.info(
            "watching %d Telegram channels, %d feeds, %d watchlist pages | deal posts from %.0f%% off | every %ds%s",
            len(self.s.channels), len(self.s.feeds), len(self.s.watchlist), self.s.min_discount, self.s.interval,
            " | DRY RUN" if self.s.dry_run else "",
        )
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
                "scan #%d: %ds | %d new posts (%d too old) | %d prices | alerts sent: %d urgent, %d high, %d other"
                " | %d duplicates | %d source failures",
                number, stats.get("seconds", 0), stats.get("posts", 0), stats.get("stale", 0), stats.get("prices", 0),
                stats.get("sent_urgent", 0), stats.get("sent_high", 0), stats.get("sent_normal", 0),
                stats.get("duplicates", 0), stats.get("source_failures", 0),
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
        self.flush_outbox()
        return int(self.stats.get("undelivered", 0))
