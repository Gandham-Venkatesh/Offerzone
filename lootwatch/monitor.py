"""The scan loop: fetch every source, judge what is new, push what matters, watch its own health."""

from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
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
from .scoring import TIER_HIGH, TIER_NAMES, TIER_NORMAL, TIER_URGENT, Verdict, evaluate_offer, evaluate_text
from .sources.feeds import FeedError, parse_feed
from .sources.stores import extract_page
from .sources.telegram import parse_channel_page

log = logging.getLogger("lootwatch")

OUTBOX_MAX_AGE = 30 * 60  # a loot alert delivered later than this is only noise
OUTBOX_MAX_ATTEMPTS = 8
FAIL_STREAK_NOTE = 10  # cycles in a row before a source is reported as broken
BLIND_STREAK = 3  # cycles in a row with every deal feed failing before the "blind" alarm
_TAGS = {TIER_URGENT: ["rotating_light"], TIER_HIGH: ["fire"], TIER_NORMAL: ["label"]}


def short_url(url: str, limit: int = 70) -> str:
    parts = urlsplit(url)
    text = (parts.hostname or "") + parts.path + ("?" + parts.query if parts.query else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


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

    def stop(self) -> None:
        self._stop.set()

    def _count(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += amount

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
        self.db.health_ok(source, len(page.posts))
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self.db.unseen(source, [str(p.post_id) for p in page.posts])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return
        self.db.mark_seen(source, fresh)
        now = time.time()
        for post in page.posts:
            if str(post.post_id) not in fresh:
                continue
            self._count("posts")
            if post.ts is None and first_contact:
                continue  # cannot tell how old it is; never alert on a channel's backlog
            if post.ts is not None and now - post.ts > self.s.max_post_age_min * 60:
                self._count("stale")
                continue
            try:
                self._handle_post(source, f"@{channel}", post.text, post.analysis_text, post.links, post.url, post.ts)
            except Exception:
                log.exception("failed to process %s", post.url)

    def _scan_feed(self, url: str, deadline: float) -> None:
        source = f"feed:{short_url(url, 60)}"
        result = self.fetcher.get(url, headers={"Accept": "application/atom+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5"},
                                  deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            return
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            return
        try:
            items = parse_feed(result.text)
        except FeedError as exc:
            self.db.health_fail(source, str(exc))
            self._count("source_failures")
            return
        self.db.health_ok(source, len(items))
        first_contact = self.db.get(f"boot:{source}") == ""
        fresh = self.db.unseen(source, [i.item_id for i in items])
        if first_contact:
            self.db.set(f"boot:{source}", int(time.time()))
        if not fresh:
            return
        self.db.mark_seen(source, fresh)
        now = time.time()
        for item in items:
            if item.item_id not in fresh:
                continue
            self._count("posts")
            if item.ts is None and first_contact:
                continue
            if item.ts is not None and now - item.ts > self.s.max_post_age_min * 60:
                self._count("stale")
                continue
            try:
                self._handle_post(source, host_of(url), item.text, item.text, item.links, item.url, item.ts)
            except Exception:
                log.exception("failed to process feed item %s", item.url)

    def _scan_watch(self, item: WatchItem, deadline: float) -> None:
        source = f"watch:{short_url(item.url, 60)}"
        result = self.fetcher.get(item.url, deadline=deadline)
        if result.skipped and not result.error.startswith("cooldown"):
            return
        self._watch_checked[item.url] = time.time()
        if not result.ok:
            self.db.health_fail(source, result.problem)
            self._count("source_failures")
            return
        page = extract_page(item.url, result.text)
        if page.kind == "none":
            self.db.health_fail(source, page.note)
            self._count("source_failures")
            return
        self.db.health_ok(source, len(page.offers))
        listing = page.kind == "listing"
        # The first read of a search/category page only records what is normal on it. Everything
        # after that is judged against that picture, so the page's permanently cheap items stay quiet.
        baseline = listing and self.db.get(f"boot:{source}") == ""
        for offer in page.offers:
            self._count("prices")
            info = self.db.record_price(offer.key, offer.store, offer.title, offer.url, offer.price, offer.mrp)
            verdict = evaluate_offer(offer.title, offer.price, offer.mrp, info, self.s,
                                     target=item.target, listing=listing, baseline=baseline)
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
        if verdict is None:
            return
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
                verdict.tier = TIER_NORMAL
                verdict.flags.append(f"link goes to {strange_host}, not a known store - check before you pay")
            else:
                # We simply could not tell (short link timed out, ...): do not bury it, do not shout either.
                verdict.tier = min(verdict.tier, TIER_HIGH)
                verdict.flags.append(f"could not check where the link goes: {unchecked} - look at the address before you pay")
            best_link = candidates[0]
        elif not candidates and verdict.needs_trusted_link:
            return
        fingerprint = fingerprint or text_fingerprint(text, verdict.price)

        lines = [text[:600].strip(), ""]
        lines.extend("⚠ " + flag for flag in verdict.flags)
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
        self._emit(fingerprint, verdict, payload, source)

    def _emit(self, fingerprint: str, verdict: Verdict, payload: Dict[str, object], source: str) -> bool:
        """De-duplicate, respect the daily budget, then send (or queue for retry)."""
        with self._emit_lock:
            now = time.time()
            sources = self.db.add_sighting(fingerprint, source, self.s.trending_window_min * 60, now)
            previous = self.db.last_alert(fingerprint, now - self.s.dedup_hours * 3600)
            if previous:
                _, prev_tier, prev_price, _ = previous
                cheaper = verdict.price is not None and prev_price is not None and verdict.price <= prev_price * 0.9
                louder = verdict.tier > prev_tier
                trending = (
                    sources >= self.s.trending_channels and prev_tier < TIER_URGENT
                    and self.db.get(f"trend:{fingerprint}") == ""
                )
                if not (cheaper or louder or trending):
                    self._count("duplicates")
                    return False
                if trending and not louder:
                    # Channels copy each other, so a repeat is weak evidence: one step louder, no more.
                    self.db.set(f"trend:{fingerprint}", int(now))
                    verdict.tier = min(TIER_URGENT, prev_tier + 1)
                    payload = dict(payload, priority=verdict.tier, tags=_TAGS[verdict.tier],
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
            log.info("ALERT [%s] %s (%s)", TIER_NAMES.get(verdict.tier, verdict.tier), payload.get("title"), source)
            self._deliver(alert_id, payload, verdict.tier)
            return True

    def _deliver(self, alert_id: int, payload: Dict[str, object], tier: int) -> bool:
        if not self.notifier.configured and not self.notifier.dry_run:
            if not self._warned_no_topic:
                log.warning("NTFY_TOPIC is not set: alerts are only being logged, not pushed")
                self._warned_no_topic = True
            self.db.mark_sent(alert_id)
            return False
        if time.time() < self._ntfy_pause_until:
            self.db.mark_attempt(alert_id)
            return False
        result = self.notifier.send(payload)
        if result.ok:
            self.db.mark_sent(alert_id)
            self._count(f"sent_{TIER_NAMES.get(tier, 'other')}")
            return True
        self.db.mark_attempt(alert_id)
        if result.status == 429:
            self._ntfy_pause_until = time.time() + min(900.0, max(30.0, result.retry_after))
        self._count("send_failures")
        return False

    def flush_outbox(self) -> None:
        """Retry alerts that could not be delivered; give up on ones too old to be useful."""
        now = time.time()
        for pending in self.db.pending_alerts():
            if now - pending.created_at > OUTBOX_MAX_AGE or pending.attempts >= OUTBOX_MAX_ATTEMPTS:
                self.db.drop_alert(pending.id)
                log.warning("giving up on an undelivered alert from %s", _age(now - pending.created_at))
                continue
            try:
                payload = json.loads(pending.payload)
            except ValueError:
                self.db.drop_alert(pending.id)
                continue
            self._deliver(pending.id, payload, pending.tier)

    # ---------------------------------------------------------- the cycle ---
    def _watch_batch(self) -> List[WatchItem]:
        """Watchlist entries to check this cycle: longest-unchecked first, capped per site by its pacing."""
        budget: Dict[str, int] = {}
        chosen: List[WatchItem] = []
        for item in sorted(self.s.watchlist, key=lambda w: self._watch_checked.get(w.url, 0.0)):
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

        jobs = [(self._scan_channel, channel) for channel in self.s.channels]
        jobs += [(self._scan_feed, url) for url in self.s.feeds]
        jobs += [(self._scan_watch, item) for item in self._watch_batch()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.s.workers, thread_name_prefix="scan") as pool:
            futures = {pool.submit(fn, arg, deadline): arg for fn, arg in jobs}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    log.exception("scan task failed: %r", futures[future])

        self.db.incr("hb:cycles")
        delta = collections.Counter(self.stats)
        delta.subtract(before)
        self.db.incr("hb:posts", delta.get("posts", 0))
        self._health_checks()
        self._heartbeat()
        delta["seconds"] = int(time.time() - started)
        return dict(delta)

    # -------------------------------------------------------- self-checks ---
    def _feed_sources(self) -> List[str]:
        return [f"tg:{c}" for c in self.s.channels] + [f"feed:{short_url(u, 60)}" for u in self.s.feeds]

    def _health_checks(self) -> None:
        rows = {row[0]: row for row in self.db.health_rows()}
        feeds = self._feed_sources()
        now = time.time()

        # 1. Everything that brings deals in is failing: say so loudly, the monitor is blind.
        streaks = [rows[s][3] for s in feeds if s in rows]
        blind = bool(feeds) and len(streaks) == len(feeds) and min(streaks) >= BLIND_STREAK
        if blind:
            last = float(self.db.get("blind_notified", "0") or 0)
            if now - last > 3 * 3600:
                errors = collections.Counter(str(rows[s][6]) for s in feeds)
                detail = "; ".join(f"{count}× {error}" for error, count in errors.most_common(3))
                sent = self.notifier.send_status(
                    "Loot monitor is blind",
                    f"All {len(feeds)} deal sources have failed {min(streaks)} checks in a row, so nothing can be "
                    f"caught right now.\n{detail}",
                    priority=4, tags=("warning",),
                )
                if sent.ok or not self.notifier.configured:
                    self.db.set("blind_notified", int(now))
            log.error("all %d deal sources are failing - the monitor is blind", len(feeds))
        elif self.db.get("blind_notified", "0") not in ("", "0") and feeds:
            self.db.set("blind_notified", 0)
            self.notifier.send_status("Loot monitor recovered", "Deal sources are readable again.", priority=3,
                                      tags=("white_check_mark",))

        # 2. Individual sources that have just crossed the "this is broken" line.
        configured = set(feeds) | {f"watch:{short_url(w.url, 60)}" for w in self.s.watchlist}
        broken = [rows[s] for s in sorted(configured) if s in rows and rows[s][3] == FAIL_STREAK_NOTE]
        if broken and not blind:
            listing = "\n".join(f"• {row[0]}: {row[6]}" for row in broken[:12])
            self.notifier.send_status(
                f"{len(broken)} source(s) stopped working",
                f"Failed {FAIL_STREAK_NOTE} checks in a row:\n{listing}\n\nRun the doctor command for details.",
                priority=2, tags=("warning",),
            )

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
        configured = set(self._feed_sources()) | {f"watch:{short_url(w.url, 60)}" for w in self.s.watchlist}
        failing = [r for r in rows if r[0] in configured and r[3] >= FAIL_STREAK_NOTE]
        message = (
            f"Since the last note: {self.db.get('hb:cycles', '0')} scans, {self.db.get('hb:posts', '0')} new posts read.\n"
            f"Alerts sent: {counts.get(TIER_URGENT, 0)} urgent, {counts.get(TIER_HIGH, 0)} high, "
            f"{counts.get(TIER_NORMAL, 0)} other.\n"
            f"Sources: {len(configured) - len(failing)} working, {len(failing)} failing."
        )
        if failing:
            message += "\n" + "\n".join(f"• {r[0]}: {r[6]}" for r in failing[:8])
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
            f"Alerts fire at {self.s.min_discount:.0f}% off or more; price errors arrive as urgent.",
            priority=3, tags=("rocket",),
        )
        if sent.ok:
            self.db.set("started_note", int(time.time()))

    # ------------------------------------------------------------ the loop ---
    def run(self, once: bool = False, max_runtime: Optional[float] = None) -> None:
        self.db.forget_health(set(self._feed_sources()) | {f"watch:{short_url(w.url, 60)}" for w in self.s.watchlist})
        log.info(
            "watching %d Telegram channels, %d feeds, %d watchlist pages | alert at >=%.0f%% off | every %ds%s",
            len(self.s.channels), len(self.s.feeds), len(self.s.watchlist), self.s.min_discount, self.s.interval,
            " | DRY RUN" if self.s.dry_run else "",
        )
        started = time.time()
        number = 0
        last_duration = 0.0
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
                break
            self._stop.wait(pause)
        self.flush_outbox()
