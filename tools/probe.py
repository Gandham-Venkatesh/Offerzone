"""Development aid, not part of the monitor: fetch a list of addresses the way the monitor would
(same client, same pacing) and keep what came back, so parsers can be written against real pages.

    python tools/probe.py <list file> <output folder>
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lootwatch.fetch import Fetcher  # noqa: E402
from lootwatch.sources.feeds import FeedError, parse_feed  # noqa: E402
from lootwatch.sources.stores import extract_page  # noqa: E402


def gist(text: str) -> str:
    title = re.search(r"<title[^>]*>(.*?)</title>", text[:200_000], re.I | re.S)
    plain = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text[:400_000])
    plain = re.sub(r"(?s)<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return f"[{(title.group(1).strip() if title else '')[:80]}] {plain[:260]}"


def main() -> int:
    urls = []
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line.split(" | ")[0].strip())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher("auto")
    print(f"probe: {len(urls)} addresses | browser-grade TLS: {fetcher.impersonation}")

    def one(job):
        number, url = job
        started = time.time()
        headers = None
        if re.search(r"\.(?:atom|rss|xml)(?:\?|$)|/feed/?(?:\?|$)|/\.rss", url):
            headers = {"Accept": "application/atom+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5"}
        elif re.search(r"\.json(?:\?|$)|/api/|api\.", url):
            headers = {"Accept": "application/json,text/plain,*/*"}
        result = fetcher.get(url, headers=headers)
        text = result.text or ""
        lines = [f"#{number:02d} {url}",
                 f"     status {result.status} | {len(text)} chars | {time.time() - started:.1f}s | "
                 f"problem: {result.problem or '-'} | final: {result.final_url[:140]}"]
        kind = "html"
        stripped = text.lstrip()[:200].lower()
        if stripped.startswith("{") or stripped.startswith("["):
            kind = "json"
        elif stripped.startswith("<?xml") or "<rss" in stripped or "<feed" in stripped:
            kind = "xml"
        if text:
            (out / f"{number:02d}.{kind}").write_text(text, encoding="utf-8")
        if kind == "xml":
            try:
                items = parse_feed(text)
                lines.append(f"     feed: {len(items)} entries; first: {items[0].text[:200]!r}" if items else "     feed: 0 entries")
            except FeedError as exc:
                lines.append(f"     feed error: {exc}")
        elif kind == "json":
            try:
                data = json.loads(text)
                shape = list(data.keys())[:25] if isinstance(data, dict) else f"list of {len(data)}"
                lines.append(f"     json: {shape}")
            except ValueError as exc:
                lines.append(f"     json error: {exc}")
        elif text:
            marks = {
                "rupee": text.count("₹"), "ld+json": text.count("application/ld+json"),
                "next_data": text.count("__NEXT_DATA__"), "nuxt": text.count("__NUXT__"),
                "initial_state": len(re.findall(r"__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__", text)),
                "atom/rss links": len(re.findall(r'type="application/(?:atom|rss)\+xml"', text)),
            }
            lines.append("     " + " | ".join(f"{k} {v}" for k, v in marks.items()))
            for feed in re.findall(r'<link[^>]+type="application/(?:atom|rss)\+xml"[^>]*>', text)[:4]:
                lines.append(f"     {feed[:200]}")
            try:
                page = extract_page(url, text)
                lines.append(f"     extract_page: {page.kind}, {len(page.offers)} offers, note: {page.note!r}")
                for offer in page.offers[:3]:
                    lines.append(f"       {offer.price} (MRP {offer.mrp}) {offer.title[:80]}")
            except Exception as exc:  # a probe must never die on one page
                lines.append(f"     extract_page crashed: {type(exc).__name__}: {exc}")
            lines.append(f"     {gist(text)}")
        return "\n".join(lines)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for report in pool.map(one, enumerate(urls, 1)):
            print(report, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
