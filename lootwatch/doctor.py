"""`--doctor`: fetch every configured source once and report, plainly, what works.

Nothing is stored and nothing is pushed. For each Telegram channel it also shows
how many of the posts currently on the page would have triggered an alert, so
thresholds can be tuned against real posts.
"""

from __future__ import annotations

import collections
import platform
import sys
import time

from . import __version__
from .config import Settings
from .fetch import HAS_CURL_CFFI, Fetcher
from .links import TRUSTED_KINDS, classify_host, deal_links, host_of
from .monitor import _age, _first_line, short_url
from .scoring import TIER_HIGH, TIER_URGENT, evaluate_text, is_side_product
from .sources.feeds import FeedError, parse_feed
from .sources.stores import extract_page
from .sources.telegram import parse_channel_page
from .textparse import fmt_inr


def _row(status: str, name: str, detail: str) -> str:
    return f"  {status:<5} {name:<34} {detail}"


def run_doctor(settings: Settings, out=sys.stdout) -> int:
    fetcher = Fetcher(settings.use_curl_cffi, profile=settings.curl_impersonate)
    now = time.time()
    print(f"lootwatch {__version__} doctor | python {platform.python_version()} | "
          f"browser-grade TLS (curl_cffi): "
          f"{'yes, profile ' + fetcher.profile if fetcher.impersonation else 'no' if not HAS_CURL_CFFI else 'off'}", file=out)
    print(f"ntfy: server {settings.ntfy_server} | topic {'set' if settings.ntfy_topic else 'NOT SET'} | "
          f"token {'set' if settings.ntfy_token else 'not set'}", file=out)
    print(f"alert rule: >= {settings.min_discount:.0f}% off; urgent when saving >= {fmt_inr(settings.urgent_min_saving)}, "
          f"a trap matches, or the post says price error | {len(settings.traps)} traps loaded", file=out)

    totals = collections.Counter()
    link_hosts = collections.Counter()

    print(f"\nTELEGRAM CHANNELS ({len(settings.channels)})", file=out)
    for channel in settings.channels:
        totals["tg"] += 1
        result = fetcher.get(f"{settings.telegram_base_url}/{channel}")
        if not result.ok:
            print(_row("FAIL", "@" + channel, result.problem), file=out)
            continue
        page = parse_channel_page(result.text, channel, result.final_url)
        if page.status != "ok":
            why = {"no_preview": "no public preview (private, renamed or deleted) - remove or replace it",
                   "empty": "page loaded but shows no posts"}.get(page.status, "page layout not recognised")
            print(_row("FAIL", "@" + channel, why), file=out)
            continue
        totals["tg_ok"] += 1
        stamps = [p.ts for p in page.posts if p.ts]
        newest = _age(now - max(stamps)) if stamps else "unknown age"
        tiers = collections.Counter()
        sample = ""
        for post in page.posts:
            link_hosts.update(host_of(link) for link in deal_links(post.links))
            verdict = evaluate_text(post.analysis_text, settings)
            if verdict:
                tiers[verdict.tier] += 1
                if not sample or verdict.tier == TIER_URGENT:
                    sample = f"{verdict.headline} — {_first_line(post.text, 50)} [{verdict.reason}]"
        detail = (f"{len(page.posts)} posts, newest {newest} | would alert: {tiers[TIER_URGENT]} urgent, "
                  f"{tiers[TIER_HIGH]} high")
        stale = stamps and now - max(stamps) > 3 * 86400
        print(_row("WARN" if stale else "OK", "@" + channel, detail + (" | quiet for days" if stale else "")), file=out)
        if sample:
            print(f"        e.g. {sample}", file=out)

    if link_hosts:
        print("\nLINK HOSTS IN THOSE POSTS (untrusted ones are pushed quietly with a warning, never as urgent)", file=out)
        for host, count in link_hosts.most_common(15):
            kind = classify_host(host)
            if kind in TRUSTED_KINDS:
                note = "trusted"
            elif kind == "generic":
                note = "trusted only when it redirects to a known store"
            else:
                note = "NOT RECOGNISED - trusted only when it redirects to a known store; if it is a real shop, add it to EXTRA_STORE_DOMAINS"
            print(f"  {count:>4} x {host:<28} {note}", file=out)

    if settings.feeds:
        print(f"\nFEEDS ({len(settings.feeds)})", file=out)
    for url in settings.feeds:
        totals["feed"] += 1
        result = fetcher.get(url)
        name = short_url(url, 34)
        if not result.ok:
            print(_row("FAIL", name, result.problem), file=out)
            continue
        try:
            items = parse_feed(result.text)
        except FeedError as exc:
            print(_row("FAIL", name, str(exc)), file=out)
            continue
        totals["feed_ok"] += 1
        stamps = [i.ts for i in items if i.ts]
        hits = sum(1 for i in items if evaluate_text(i.text, settings))
        print(_row("OK", name, f"{len(items)} entries, newest {_age(now - max(stamps)) if stamps else 'unknown age'}"
                               f" | would alert: {hits}"), file=out)

    if settings.watchlist:
        print(f"\nWATCHLIST ({len(settings.watchlist)}) - same-site pages are fetched a few seconds apart", file=out)
    for item in settings.watchlist:
        totals["watch"] += 1
        name = item.label or short_url(item.url, 34)
        result = fetcher.get(item.url)
        if not result.ok:
            print(_row("FAIL", name[:34], f"{host_of(item.url)}: {result.problem}"), file=out)
            continue
        page = extract_page(item.url, result.text)
        if page.kind == "none" or not page.offers:
            print(_row("FAIL", name[:34], page.note or "nothing found"), file=out)
            continue
        totals["watch_ok"] += 1
        offer = min(page.offers, key=lambda o: o.price) if page.kind == "listing" else page.offers[0]
        mrp = f" (MRP {fmt_inr(offer.mrp)})" if offer.mrp else ""
        lead = "cheapest" if page.kind == "listing" else "e.g."
        print(_row("OK", name[:34], f"{page.kind}: {len(page.offers)} product(s) | {lead} {offer.title[:48]} "
                                    f"{fmt_inr(offer.price)}{mrp}"), file=out)
        if page.kind == "listing" and item.target is not None:
            under = [o for o in page.offers if o.price <= item.target and not is_side_product(o.title)]
            if under:
                print(f"        note: {len(under)} of {len(page.offers)} are already at or under your {fmt_inr(item.target)} "
                      f"floor. They count as normal for this page and will never alert;", file=out)
                print("        if they are ordinary products, the floor is too high for this page.", file=out)
            else:
                print(f"        floor {fmt_inr(item.target)}: nothing is under it now, so anything that appears under it alerts.",
                      file=out)

    print("\nSUMMARY", file=out)
    print(f"  Telegram channels readable: {totals['tg_ok']}/{totals['tg']}", file=out)
    if totals["feed"]:
        print(f"  Feeds readable:             {totals['feed_ok']}/{totals['feed']}", file=out)
    if totals["watch"]:
        print(f"  Watchlist pages readable:   {totals['watch_ok']}/{totals['watch']}", file=out)
    if not settings.ntfy_topic:
        print("  NTFY_TOPIC is not set, so no push can be sent yet.", file=out)
    working = totals["tg_ok"] + totals["feed_ok"] + totals["watch_ok"]
    if working == 0:
        print("  Nothing is readable from this machine/network. The monitor would be blind here.", file=out)
        return 2
    return 0
