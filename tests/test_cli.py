"""The command line, the doctor report, settings loading and the optional curl_cffi transport."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lootwatch import fetch
from lootwatch.__main__ import main
from lootwatch.config import Settings, load_env_file
from lootwatch.doctor import run_doctor
from lootwatch.fetch import Fetcher, Policy, _Site
from tests.helpers import FakeWeb

_SETTING_NAMES = [
    "NTFY_SERVER", "NTFY_TOPIC", "NTFY_TOKEN", "NTFY_HEALTH_TOPIC", "MIN_DISCOUNT", "URGENT_MIN_SAVING",
    "WATCH_DROP_PCT", "CHECK_INTERVAL", "MAX_WORKERS", "MAX_POST_AGE_MIN", "DEDUP_HOURS", "TRENDING_CHANNELS",
    "HIGH_DISCOUNT", "URGENT_DISCOUNT", "MIN_SAVING", "BIG_SAVING", "LOOT_PRICE", "FLOOR_MRP_RATIO", "NEW_LISTING_DISCOUNT",
    "ABSENCE_HOURS", "LATE_POST_MIN", "USE_CHANNELS", "USE_STORE_PAGES", "CURL_IMPERSONATE",
    "TRENDING_WINDOW_MIN", "DAILY_ALERT_LIMIT", "STARTUP_NOTE", "HEARTBEAT_HOUR", "TZ_OFFSET_MINUTES",
    "HEALTHCHECK_URL", "RESOLVE_LINKS", "USE_CURL_CFFI", "TELEGRAM_BASE_URL", "DENY_WORDS",
    "EXTRA_STORE_DOMAINS", "DB_PATH", "CHANNELS_FILE", "WATCHLIST_FILE", "TRAPS_FILE", "FEEDS_FILE", "LOG_LEVEL",
    "SITE_GAPS", "TRACKER_DROP_PCT", "URGENT_DROP_PCT", "QUICK_COUPON_PCT", "QUICK_LOOTS",
]


class CleanEnv(unittest.TestCase):
    """Run each test in an empty folder with none of our settings in the environment."""

    def setUp(self):
        self.web = FakeWeb().start()
        self.folder = tempfile.mkdtemp()
        self._cwd = os.getcwd()
        os.chdir(self.folder)
        self._saved = {name: os.environ.pop(name) for name in _SETTING_NAMES if name in os.environ}

    def tearDown(self):
        for name in _SETTING_NAMES:
            os.environ.pop(name, None)
        os.environ.update(self._saved)
        os.chdir(self._cwd)
        self.web.stop()

    def write(self, name, text):
        Path(self.folder, name).write_text(text, encoding="utf-8")


class CommandLine(CleanEnv):
    def test_env_file_and_one_scan_end_to_end(self):
        self.write(".env", f'NTFY_SERVER={self.web.base}\nNTFY_TOPIC="from-env-file"\n'
                           f"TELEGRAM_BASE_URL={self.web.base}/s\nHEARTBEAT_HOUR=-1\nUSE_CURL_CFFI=off\n# comment\n")
        self.write("channels.txt", "# my channels\n@alpha\n")
        self.write("traps.txt", "nothing phone | 8000\n")
        self.web.post("alpha", 1, "Nothing Phone (3a) at just ₹400 😱", time.time() - 15)
        self.assertEqual(main(["--once"]), 0)
        titles = [p["title"] for p in self.web.pushes]
        self.assertEqual(len(titles), 2)
        self.assertEqual(titles[0], "Trap hit: ₹400 — Nothing Phone (3a)")
        self.assertEqual(self.web.pushes[0]["priority"], 5)
        self.assertEqual(self.web.pushes[0]["topic"], "from-env-file")
        self.assertEqual(titles[1], "Loot monitor started")
        self.assertIn("1 of 1 deal sources readable", self.web.pushes[1]["message"])
        self.assertTrue(Path(self.folder, "lootwatch.db").is_file())
        # A second run from the same folder is a restart: no repeat of either push.
        self.assertEqual(main(["--once"]), 0)
        self.assertEqual(len(self.web.pushes), 2)

    def test_flags_override_environment(self):
        os.environ["NTFY_TOPIC"] = "from-environment"
        os.environ["MIN_DISCOUNT"] = "95"
        settings = Settings.from_env({"ntfy_topic": "from-flag", "min_discount": None})
        self.assertEqual((settings.ntfy_topic, settings.min_discount), ("from-flag", 95.0))
        self.assertEqual(load_env_file(Path(self.folder, "missing.env")), 0)
        self.write(".env", "NTFY_TOPIC=from-file\nexport WATCH_DROP_PCT='40'\nbad line\n1BAD=x\n")
        self.assertEqual(load_env_file(Path(self.folder, ".env")), 1)  # NTFY_TOPIC was already set; only one new value
        self.assertEqual(Settings.from_env().watch_drop_pct, 40.0)
        self.assertEqual(os.environ["NTFY_TOPIC"], "from-environment")

    def test_env_example_is_loadable_as_written(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        self.assertGreater(load_env_file(example), 5)
        settings = Settings.from_env()
        self.assertEqual((settings.min_discount, settings.urgent_min_saving, settings.interval), (80.0, 2000.0, 60))
        self.assertEqual((settings.ntfy_topic, settings.ntfy_token, settings.ntfy_server), ("", "", "https://ntfy.sh"))
        self.assertEqual((settings.heartbeat_hour, settings.daily_alert_limit), (9, 200))
        self.assertEqual((settings.urgent_discount, settings.floor_mrp_ratio, settings.curl_impersonate), (95.0, 3.0, "chrome124"))
        self.assertEqual((settings.new_listing_discount, settings.absence_hours, settings.late_post_min), (85.0, 24.0, 60.0))

    def test_shipped_list_files_parse_cleanly(self):
        root = Path(__file__).resolve().parent.parent
        settings = Settings(channels_file=str(root / "channels.txt"), watchlist_file=str(root / "watchlist.txt"),
                            traps_file=str(root / "traps.txt"), feeds_file=str(root / "feeds.txt"))
        with self.assertNoLogs("lootwatch", level="WARNING"):
            settings.load_lists()
        self.assertEqual(len(settings.channels), 28)
        self.assertIn("desidime", settings.channels)
        self.assertEqual(len(settings.channels), len({c.lower() for c in settings.channels}))
        urls = [w.url for w in settings.watchlist]
        self.assertEqual(len(urls), 144)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertFalse(any("amazon." in u for u in urls), "Amazon pages ship switched off")
        self.assertEqual(sum("flipkart.com" in u for u in urls), 90)
        laptops = next(w for w in settings.watchlist if w.label == "Flipkart laptops")
        self.assertEqual((laptops.target, laptops.weight, laptops.every), (6000.0, 12.0, 0.0))
        apple = next(w for w in settings.watchlist if w.label == "Flipkart Apple phones")
        self.assertEqual((apple.target, apple.weight), (18000.0, 4.0))
        self.assertIn("facets.brand%255B%255D%3DApple", apple.url)
        popular = next(w for w in settings.watchlist if w.label == "Flipkart popular laptops")
        self.assertEqual((popular.target, popular.weight), (None, 1.0))
        croma = next(w for w in settings.watchlist if w.label == "Croma laptops")
        self.assertEqual((croma.target, croma.weight, croma.every), (15000.0, 4.0, 180.0))
        discounts = next(w for w in settings.watchlist if w.label == "Croma biggest discounts")
        self.assertEqual((discounts.target, discounts.every), (0.0, 300.0), "a discount-sorted page has no floor")
        self.assertTrue(all(w.target == 0 for w in settings.watchlist if "discount-desc" in w.url or "discount_dsc" in w.url))
        catalogue = next(w for w in settings.watchlist if w.label == "boAt catalogue")
        self.assertEqual((catalogue.target, catalogue.every), (None, 900.0))
        # every page a store's turn-taking has to serve still fits the picture the file's header gives
        flipkart_load = sum(w.weight for w in settings.watchlist if "flipkart.com" in w.url)
        self.assertLess(flipkart_load / 12 / 12, 3.0, "the !! pages must come round within three minutes")
        self.assertGreaterEqual(len(settings.traps), 4)
        self.assertEqual(settings.feeds, ["https://www.desidime.com/new", "https://www.dealsmagnet.com/new",
                                          "https://pricehistory.app/price-drop"])

    def test_test_ntfy_and_missing_topic(self):
        self.assertEqual(main(["--test-ntfy", "--ntfy-server", self.web.base]), 1)  # no topic anywhere
        self.assertEqual(main(["--test-ntfy", "--ntfy-server", self.web.base, "--ntfy-topic", "t1"]), 0)
        push = self.web.pushes[0]
        self.assertEqual((push["topic"], push["priority"]), ("t1", 5))
        self.assertIn("₹400", push["title"])

    def test_each_half_can_be_switched_off_per_machine(self):
        self.write("channels.txt", "alpha\n")
        self.write("watchlist.txt", f"{self.web.base}/p/x | 500\n")
        os.environ["USE_CHANNELS"] = "0"
        settings = Settings.from_env(); settings.load_lists()
        self.assertEqual((settings.channels, len(settings.watchlist)), ([], 1))
        os.environ["USE_CHANNELS"], os.environ["USE_STORE_PAGES"] = "1", "0"
        settings = Settings.from_env(); settings.load_lists()
        self.assertEqual((settings.channels, settings.watchlist), (["alpha"], []))

    def test_run_ends_with_an_error_when_an_alert_could_not_be_delivered(self):
        self.write("channels.txt", "alpha\n")
        os.environ.update({"TELEGRAM_BASE_URL": self.web.base + "/s", "NTFY_TOPIC": "t1", "HEARTBEAT_HOUR": "-1",
                           "STARTUP_NOTE": "0", "USE_CURL_CFFI": "off"})
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 15)
        self.web.ntfy_status = 400  # ntfy rejects the message outright
        self.assertEqual(main(["--once", "--ntfy-server", self.web.base]), 3)
        self.web.ntfy_status = 200
        self.assertEqual(main(["--once", "--ntfy-server", self.web.base]), 0)

    def test_nothing_to_watch_is_an_error(self):
        self.assertEqual(main(["--once", "--ntfy-server", self.web.base]), 1)

    def test_max_runtime_stops_the_loop(self):
        self.write("channels.txt", "alpha\n")
        os.environ["TELEGRAM_BASE_URL"] = self.web.base + "/s"
        self.web.post("alpha", 1, "hello", time.time() - 5)
        started = time.time()
        code = main(["--max-runtime", "2", "--interval", "15", "--dry-run", "--ntfy-server", self.web.base])
        self.assertEqual(code, 0)
        self.assertLess(time.time() - started, 10)
        self.assertEqual(self.web.hits["POST /"], 0)


class Handover(CleanEnv):
    """--handover-file: a shift does not end until the next run says it is ready."""

    def scan(self, *extra):
        self.write("channels.txt", "alpha\n")
        os.environ["TELEGRAM_BASE_URL"] = self.web.base + "/s"
        self.web.post("alpha", 1, "hello", time.time() - 5)
        started = time.time()
        code = main(["--max-runtime", "1", "--interval", "15", "--dry-run", "--ntfy-server", self.web.base, *extra])
        return code, time.time() - started

    def test_keeps_scanning_until_the_file_appears(self):
        flag = Path(self.folder, "relief-ready")
        timer = threading.Timer(4.0, lambda: flag.write_text("ready", encoding="utf-8"))
        timer.start()
        try:
            code, took = self.scan("--handover-file", str(flag), "--max-overtime", "120")
        finally:
            timer.cancel()
        self.assertEqual(code, 0)
        self.assertGreater(took, 3.5, "it must not leave before the next run is ready")
        self.assertLess(took, 30, "and must leave soon after")

    def test_gives_up_after_the_overtime_allowed(self):
        code, took = self.scan("--handover-file", str(Path(self.folder, "never")), "--max-overtime", "3")
        self.assertEqual(code, 0)
        self.assertLess(took, 25)

    def test_a_file_that_is_already_there_ends_the_shift_on_time(self):
        flag = Path(self.folder, "relief-ready")
        flag.write_text("ready", encoding="utf-8")
        code, took = self.scan("--handover-file", str(flag))
        self.assertEqual(code, 0)
        self.assertLess(took, 10)


class Lists(CleanEnv):
    def test_watchlist_marks_set_how_often_a_page_comes_round(self):
        from lootwatch.config import load_watchlist

        self.write("watchlist.txt", "!! https://a.example/core | 2500 | Core page\n"
                                    "! https://a.example/brand | 15000\n"
                                    "https://a.example/popular | | Popular\n"
                                    "!https://a.example/tight\n"
                                    "https://a.example/products.json | | Whole catalogue | every 15m\n"
                                    "https://a.example/slow | every 2 hours | 900\n")
        items = load_watchlist(Path(self.folder, "watchlist.txt"))
        self.assertEqual([(i.url, i.target, i.label, i.weight, i.every) for i in items], [
            ("https://a.example/core", 2500.0, "Core page", 12.0, 0.0), ("https://a.example/brand", 15000.0, "", 4.0, 0.0),
            ("https://a.example/popular", None, "Popular", 1.0, 0.0), ("https://a.example/tight", None, "", 4.0, 0.0),
            ("https://a.example/products.json", None, "Whole catalogue", 1.0, 900.0),
            ("https://a.example/slow", 900.0, "", 1.0, 7200.0)])

    def test_site_gaps_setting(self):
        try:
            fetch.set_site_gaps("flipkart.com=2-3.5, t.me = 0.1 , nonsense, croma.com=4")
            self.assertEqual((fetch.policy_for("www.flipkart.com").min_gap, fetch.policy_for("www.flipkart.com").max_gap), (2.0, 3.5))
            self.assertEqual(fetch.policy_for("t.me").min_gap, 0.3, "never faster than this")
            self.assertEqual((fetch.policy_for("api.croma.com").min_gap, fetch.policy_for("api.croma.com").max_gap), (4.0, 4.0))
            self.assertTrue(fetch.policy_for("www.flipkart.com").impersonate, "only the gap changes")
            self.assertEqual(fetch.policy_for("www.flipkart.com").base_cooldown, 300)
        finally:
            fetch.set_site_gaps("")
        self.assertEqual(fetch.policy_for("www.flipkart.com").min_gap, 3.0)


class Doctor(CleanEnv):
    def test_report(self):
        now = time.time()
        self.web.redirects["/r/x"] = "https://www.amazon.in/dp/B0ABCDEFGH"
        self.web.post("alpha", 1, 'Sony XM5 @ ₹400 (MRP ₹34,990) <a href="https://amzn.to/abc">x</a>', now - 90)
        self.web.post("alpha", 2, 'USB cable @ ₹99 (MRP ₹1,999) <a href="https://lootshop.example/p/1">x</a>', now - 30)
        self.web.post("quiet", 1, "hello", now - 10 * 86400)
        self.web.no_preview.add("ghost")
        self.web.pages["/feed.xml"] = '<?xml version="1.0"?><rss version="2.0"><channel><item><title>A @ ₹1 (MRP ₹9,000)</title></item></channel></rss>'
        product = {"@type": "Product", "name": "Gulab Jamun 1 kg", "offers": {"price": "249.00"}}
        self.web.pages["/p/gj"] = f'<script type="application/ld+json">{json.dumps(product)}</script>'
        self.web.pages["/p/blank"] = "<html><body></body></html>"
        self.web.status_for["/p/blocked"] = 403

        settings = Settings(ntfy_server=self.web.base, telegram_base_url=self.web.base + "/s", use_curl_cffi="off")
        settings.channels = ["alpha", "quiet", "ghost"]
        settings.feeds = [self.web.base + "/feed.xml"]
        from lootwatch.config import WatchItem

        listing = {"items": [{"id": "a", "name": "Mini Notebook 11 inch", "price": 999, "mrp": 1999},
                             {"id": "b", "name": "HP Laptop 15s", "price": 42990, "mrp": 59990},
                             {"id": "c", "name": "Laptop Skin compatible with HP", "price": 199, "mrp": 999}]}
        self.web.pages["/laptops"] = f'<script type="application/json">{json.dumps(listing)}</script>'
        settings.watchlist = [WatchItem(self.web.base + "/p/gj", label="Sweets"), WatchItem(self.web.base + "/p/blank"),
                              WatchItem(self.web.base + "/laptops", target=4000, label="Laptops floor"),
                              WatchItem(self.web.base + "/p/blocked"), WatchItem(self.web.base + "/p/after-the-refusal")]
        out = io.StringIO()
        code = run_doctor(settings, out)
        report = out.getvalue()
        self.assertEqual(code, 0)
        self.assertRegex(report, r"OK\s+@alpha\s+2 posts, newest 30s ago \| would alert: 1 urgent, 1 high, 0 quiet")
        self.assertIn("99% off · ₹400 (MRP ₹34,990)", report)
        self.assertRegex(report, r"WARN\s+@quiet .*quiet for days")
        self.assertRegex(report, r"FAIL\s+@ghost\s+no public preview")
        self.assertRegex(report, r"1 x amzn\.to\s+trusted")
        self.assertRegex(report, r"1 x lootshop\.example\s+NOT RECOGNISED")
        self.assertRegex(report, r"OK\s+127\.0\.0\.1.*feed\.xml\s+1 entries.*would alert: 1")
        self.assertRegex(report, r"OK\s+Sweets\s+product: 1 product\(s\) \| e\.g\. Gulab Jamun 1 kg ₹249")
        self.assertRegex(report, r"FAIL\s+127\.0\.0\.1.*blank\s+no product data found")
        self.assertRegex(report, r"FAIL\s+127\.0\.0\.1.*blocked\s+127\.0\.0\.1: HTTP 403")
        self.assertRegex(report, r"OK\s+Laptops floor\s+listing: 3 product\(s\) \| cheapest Laptop Skin compatible with HP ₹199")
        self.assertIn("note: 1 of 3 are already at or under your ₹4,000 floor", report)  # the notebook; the skin is an accessory
        self.assertIn("Telegram channels readable: 2/3", report)
        # Once a site refuses one page, its other pages are not tried again straight away - and the report says why.
        self.assertRegex(report, r"FAIL\s+127\.0\.0\.1.*after-the-refusal\s+127\.0\.0\.1: cooldown \d+s after HTTP 403")
        self.assertIn("Watchlist pages readable:   2/5", report)
        self.assertIn("NTFY_TOPIC is not set", report)
        self.assertEqual(self.web.hits["POST /"], 0, "the doctor never pushes")
        self.assertFalse(Path(self.folder, "lootwatch.db").exists(), "the doctor never writes state")

    def test_everything_unreachable_returns_an_error_code(self):
        settings = Settings(ntfy_server=self.web.base, telegram_base_url=self.web.base + "/s", use_curl_cffi="off")
        settings.channels = ["ghost"]
        out = io.StringIO()
        self.assertEqual(run_doctor(settings, out), 2)
        self.assertIn("would be blind", out.getvalue())
        self.assertNotIn("blocks or breaks Telegram", out.getvalue())  # it answered; it just has no preview

    def test_a_network_that_cannot_reach_telegram_is_named_as_such(self):
        settings = Settings(ntfy_server=self.web.base, telegram_base_url="http://127.0.0.1:9/s", use_curl_cffi="off")
        settings.channels = ["one", "two"]
        out = io.StringIO()
        run_doctor(settings, out)
        self.assertIn("this network blocks or breaks Telegram's", out.getvalue())
        self.assertIn("USE_CHANNELS=0", out.getvalue())


class Transport(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb().start()
        self.web.pages["/page"] = "<html><body>₹1,49,999 — ठीक</body></html>"

    def tearDown(self):
        self.web.stop()

    def test_plain_requests(self):
        result = Fetcher("off").get(self.web.base + "/page")
        self.assertTrue(result.ok)
        self.assertIn("₹1,49,999 — ठीक", result.text)
        self.assertEqual(result.final_url, self.web.base + "/page")
        missing = Fetcher("off").get(self.web.base + "/nope/deeper")
        self.assertEqual((missing.ok, missing.status, missing.problem), (False, 404, "HTTP 404"))
        dead = Fetcher("off").get("http://127.0.0.1:9/")
        self.assertFalse(dead.ok)
        self.assertIn("Error", dead.problem)

    def test_challenge_page_counts_as_a_refusal(self):
        self.web.pages["/captcha"] = ('<html><head><title>Robot Check</title></head><body><form action="/errors/validateCaptcha">'
                                      "Enter the characters you see below</form></body></html>")
        fetcher = Fetcher("off")
        result = fetcher.get(self.web.base + "/captcha")
        self.assertEqual((result.ok, result.blocked), (False, "challenge page (Robot Check)"))
        again = fetcher.get(self.web.base + "/page")  # same site: now cooling down
        self.assertTrue(again.skipped)
        self.assertTrue(again.error.startswith("cooldown"))

    def test_challenge_detection_is_not_fooled_by_ordinary_pages(self):
        captcha = '<form method="get" action="/errors/validateCaptcha">Type the characters</form>'
        self.assertTrue(fetch.looks_like_challenge("<html>" + captcha + "</html>"))
        self.assertTrue(fetch.looks_like_challenge("<html><head><title>Just a moment...</title></head></html>"))
        self.assertTrue(fetch.looks_like_challenge('<html><script src="https://x.awswaf.com/challenge.js"></script></html>'))
        # A full product page that merely carries a captcha widget for its login box is not a block.
        big_page = "<html><body>" + "<div>product</div>" * 3000 + '<div class="g-recaptcha"></div></body></html>'
        self.assertFalse(fetch.looks_like_challenge(big_page))
        self.assertFalse(fetch.looks_like_challenge("<html><body>Nothing Phone ₹24,999</body></html>"))

    def test_post_text_cannot_make_telegram_look_blocked(self):
        self.assertFalse(fetch.policy_for("t.me").sniff_challenge)
        self.assertTrue(fetch.policy_for("www.amazon.in").sniff_challenge)
        self.assertTrue(fetch.policy_for("www.amazon.in").impersonate)
        self.assertEqual(fetch.policy_for("www.flipkart.com").min_gap, 3.0)

    def test_amazon_waf_answer_counts_as_a_refusal(self):
        self.web.status_for["/waf"] = 202
        self.web.headers_for["/waf"] = {"x-amzn-waf-action": "challenge"}
        fetcher = Fetcher("off")
        result = fetcher.get(self.web.base + "/waf")
        self.assertEqual((result.ok, result.blocked), (False, "AWS WAF challenge"))
        self.assertGreater(fetcher.cooling_down(self.web.base + "/page"), 0)

    def test_time_budget_skips_instead_of_waiting(self):
        fetcher = Fetcher("off")
        with mock.patch.object(fetch, "_LOCAL_POLICY", Policy(5.0, 5.0, 1, 5)):
            first = fetcher.get(self.web.base + "/page")
            started = time.time()
            second = fetcher.get(self.web.base + "/page", deadline=time.time() + 1)
        self.assertTrue(first.ok)
        self.assertTrue(second.skipped)
        self.assertLess(time.time() - started, 1)

    @unittest.skipUnless(fetch.HAS_CURL_CFFI, "curl_cffi is not installed")
    def test_curl_cffi_transport(self):
        fetcher = Fetcher("on")
        site = _Site(Policy(0, 0, 1, 5, impersonate=True))
        result = fetcher._request(site, self.web.base + "/page", None)
        self.assertEqual((result.status, result.error), (200, ""))
        self.assertIn("₹1,49,999 — ठीक", result.text)
        self.assertEqual(type(site.session).__module__.split(".")[0], "curl_cffi")


if __name__ == "__main__":
    unittest.main()
