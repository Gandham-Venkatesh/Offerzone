"""Command line:  python -m lootwatch  [--once | --doctor | --test-ntfy]  [options]"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .config import Settings, load_env_file
from .db import DB
from .doctor import run_doctor
from .fetch import Fetcher
from .links import LinkResolver, set_extra_store_domains
from .monitor import Monitor
from .notify import Notifier, build_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lootwatch",
        description="Watch deal channels, feeds and store pages for price errors and push them through ntfy.",
        epilog="Every option can also be set as an environment variable or in a .env file (see .env.example).",
    )
    mode = parser.add_argument_group("what to do (default: scan forever)")
    mode.add_argument("--once", action="store_true", help="run one scan and exit")
    mode.add_argument("--doctor", action="store_true", help="check every source once and print what works; pushes nothing")
    mode.add_argument("--test-ntfy", action="store_true", help="send one test push and exit")
    mode.add_argument("--max-runtime", type=float, metavar="SECONDS", help="keep scanning for about this long, then exit")
    mode.add_argument("--dry-run", action="store_true", help="scan normally but only log alerts instead of pushing them")

    cfg = parser.add_argument_group("settings")
    cfg.add_argument("--env-file", default=".env", help="file with KEY=VALUE settings (default: .env)")
    cfg.add_argument("--ntfy-server", help="ntfy server URL (default: https://ntfy.sh)")
    cfg.add_argument("--ntfy-topic", help="your ntfy topic; treat it like a password")
    cfg.add_argument("--ntfy-token", help="ntfy access token (tk_...), optional")
    cfg.add_argument("--min-discount", type=float, help="percent off needed for an alert (default: 80)")
    cfg.add_argument("--interval", type=int, help="seconds between scans (default: 60)")
    cfg.add_argument("--workers", type=int, help="parallel fetch threads (default: 8)")
    cfg.add_argument("--db", help="SQLite state file (default: lootwatch.db)")
    cfg.add_argument("--channels-file", help="Telegram channels, one per line (default: channels.txt)")
    cfg.add_argument("--watchlist-file", help="product / search URLs to price-check every scan (default: watchlist.txt)")
    cfg.add_argument("--traps-file", help="'keywords | max price' lines (default: traps.txt)")
    cfg.add_argument("--feeds-file", help="RSS/Atom feed URLs (default: feeds.txt)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging; with --doctor, also print every post and product it read")
    parser.add_argument("--version", action="version", version=f"lootwatch {__version__}")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose or os.environ.get("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("lootwatch")

    load_env_file(Path(args.env_file))
    settings = Settings.from_env({
        "ntfy_server": args.ntfy_server.rstrip("/") if args.ntfy_server else None,
        "ntfy_topic": args.ntfy_topic,
        "ntfy_token": args.ntfy_token,
        "min_discount": args.min_discount,
        "interval": args.interval,
        "workers": args.workers,
        "db_path": args.db,
        "channels_file": args.channels_file,
        "watchlist_file": args.watchlist_file,
        "traps_file": args.traps_file,
        "feeds_file": args.feeds_file,
        "dry_run": True if args.dry_run else None,
    })
    settings.load_lists()
    set_extra_store_domains(settings.extra_store_domains)
    notifier = Notifier(settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token,
                        settings.ntfy_health_topic, dry_run=settings.dry_run)

    if args.test_ntfy:
        if not notifier.configured:
            log.error("NTFY_TOPIC is not set (use --ntfy-topic, the NTFY_TOPIC variable, or .env)")
            return 1
        result = notifier.send(build_payload(
            title="99% off · ₹400 (MRP ₹60,000) — test alert",
            message="This is a test from your loot monitor. Real price-error alerts will look and sound like this one.",
            priority=5, tags=["rotating_light"], click="https://www.flipkart.com/",
            actions=[{"label": "Open deal", "url": "https://www.flipkart.com/"}],
        ))
        log.info("test push %s", "sent - check your phone" if result.ok else f"FAILED: {result.error}")
        return 0 if result.ok else 1

    if args.doctor:
        return run_doctor(settings, verbose=args.verbose)

    if not (settings.channels or settings.feeds or settings.watchlist):
        log.error("nothing to watch: %s, %s and %s are all empty or missing",
                  settings.channels_file, settings.feeds_file, settings.watchlist_file)
        return 1

    db = DB(settings.db_path)
    monitor = Monitor(settings, db, Fetcher(settings.use_curl_cffi, profile=settings.curl_impersonate), notifier, LinkResolver())

    def _stop(signum, _frame):
        log.info("stopping (signal %s)", signum)
        monitor.stop()

    signal.signal(signal.SIGTERM, _stop)
    undelivered = 0
    try:
        undelivered = monitor.run(once=args.once, max_runtime=args.max_runtime)
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        db.close()
    if undelivered:
        # A non-zero exit is how a scheduled run (GitHub Actions, systemd) gets itself noticed.
        log.error("%d alert(s) could not be delivered through ntfy and were given up on", undelivered)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
