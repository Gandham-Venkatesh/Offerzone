"""End-to-end: fake Telegram previews + fake ntfy on localhost, real monitor code in between."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from lootwatch.config import Settings, WatchItem, load_traps
from lootwatch.db import DB
from lootwatch.fetch import Fetcher
from lootwatch.links import LinkResolver
from lootwatch.monitor import BLIND_STREAK, Monitor
from lootwatch.notify import Notifier
from tests.helpers import FakeWeb

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>d</title>{items}</channel></rss>"""


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb().start()
        self.folder = tempfile.mkdtemp()
        self.db_path = os.path.join(self.folder, "state", "lootwatch.db")
        traps = Path(self.folder) / "traps.txt"
        traps.write_text("nothing phone | 8000\n", encoding="utf-8")
        self.settings = Settings(
            ntfy_server=self.web.base, ntfy_topic="test-topic", telegram_base_url=self.web.base + "/s",
            interval=15, heartbeat_hour=-1, startup_note=False, db_path=self.db_path,
        )
        self.settings.traps = load_traps(traps)
        self.settings.channels = ["alpha", "beta", "gamma"]
        for name in self.settings.channels:  # every channel starts with one old, harmless post
            self.web.channels[name] = [{"id": 0, "html": "Welcome to the channel", "ts": time.time() - 86400, "buttons": []}]
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.db.close()
        self.web.stop()

    def monitor(self, token="") -> Monitor:
        notifier = Notifier(self.settings.ntfy_server, self.settings.ntfy_topic, token,
                            self.settings.ntfy_health_topic, dry_run=self.settings.dry_run)
        monitor = Monitor(self.settings, DB(self.db_path), Fetcher("off"), notifier, LinkResolver(allow_private=True))
        self.monitors.append(monitor)
        return monitor

    def deals(self):
        return [p for p in self.web.pushes if p["priority"] >= 3 and "monitor" not in p["title"].lower()
                and "source" not in p["title"].lower()]

    # -- the core promise ---------------------------------------------------
    def test_price_error_is_pushed_once_across_channels_and_restarts(self):
        now = time.time()
        self.web.redirects["/r/a1"] = "https://www.amazon.in/dp/B0NOTHING1?tag=alpha-21"
        self.web.redirects["/r/b1"] = "https://www.amazon.in/gp/product/B0NOTHING1?tag=beta-21&linkCode=x"
        # History that was on the page before the monitor ever ran, including an old "price error".
        self.web.post("alpha", 10, "Price Error!! Old TV @ ₹99 (MRP ₹50,000)", now - 7200)
        self.web.post("alpha", 11, "Good morning, deals coming soon", now - 3600)
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(self.web.pushes, [], "backlog must never be pushed")

        # A real one appears.
        self.web.post("alpha", 12,
                      f'<b>Price Error</b> 😱<br/>Nothing Phone (3a) Pro 5G <s>₹59,999</s> ₹400<br/>'
                      f'<a href="{self.web.base}/r/a1">{self.web.base}/r/a1</a>', now - 20)
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        push = self.web.pushes[0]
        self.assertEqual(push["topic"], "test-topic")
        self.assertEqual(push["priority"], 5)
        self.assertEqual(push["tags"], ["rotating_light"])
        self.assertEqual(push["title"], "Trap hit: 99% off · ₹400 (MRP ₹59,999) — Nothing Phone (3a) Pro 5G")
        self.assertEqual(push["click"], "https://www.amazon.in/dp/B0NOTHING1?tag=alpha-21")
        self.assertEqual([a["label"] for a in push["actions"]], ["Open deal", "Open post"])
        self.assertEqual(push["actions"][1]["url"], "https://t.me/alpha/12")
        self.assertIn("Nothing Phone (3a) Pro 5G", push["message"])
        self.assertIn("@alpha", push["message"])
        self.assertIn("→ amazon.in", push["message"])

        # Same product from another channel through a different short link: no second push.
        self.web.post("beta", 5, f'Nothing phone 3a pro price error ₹400 <a href="{self.web.base}/r/b1">buy</a>', now - 5)
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)

        # Restart with the same state file: still nothing new.
        self.monitor().cycle()
        self.assertEqual(len(self.web.pushes), 1)

    def test_same_deal_at_a_clearly_lower_price_is_pushed_again(self):
        now = time.time()
        self.web.redirects["/r/p"] = "https://www.amazon.in/dp/B0SONYXM51"
        link = f'<a href="{self.web.base}/r/p">x</a>'
        self.web.post("alpha", 1, f"Sony XM5 @ ₹3,499 (MRP ₹34,990) {link}", now - 30)
        monitor = self.monitor()
        monitor.cycle()
        self.web.post("beta", 1, f"Sony XM5 @ ₹3,399 (MRP ₹34,990) {link}", now - 20)  # 3% lower: same deal
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.web.post("gamma", 1, f"Sony XM5 @ ₹999 (MRP ₹34,990) {link}", now - 10)
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 2)
        self.assertTrue(self.web.pushes[1]["title"].startswith("Lower now: 97% off · ₹999"))

    def test_trending_across_channels_upgrades_a_quiet_deal_to_urgent(self):
        now = time.time()
        self.web.redirects["/r/c"] = "https://www.flipkart.com/cable/p/itmcable1?pid=ACCCABLE00000001"
        text = f'boAt cable @ ₹99 (MRP ₹1,999) <a href="{self.web.base}/r/c">buy</a>'
        monitor = self.monitor()
        self.web.post("alpha", 1, text, now - 30)
        monitor.cycle()
        self.assertEqual([p["priority"] for p in self.web.pushes], [4])  # small saving: high, not urgent
        self.web.post("beta", 1, text, now - 20)
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.web.post("gamma", 1, text, now - 10)
        monitor.cycle()
        self.assertEqual([p["priority"] for p in self.web.pushes], [4, 5])
        self.assertTrue(self.web.pushes[1]["title"].startswith("Now in 3 channels:"))
        self.web.post("alpha", 2, text, now - 5)  # a re-post must not re-trigger it
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 2)

    def test_link_to_an_unknown_site_is_shown_quietly_with_a_warning(self):
        self.web.pages["/shop/oneplus"] = "<html><body>pay here</body></html>"
        self.web.post("alpha", 1, f'Price error glitch!! OnePlus 12 at ₹99 <a href="{self.web.base}/shop/oneplus">order</a>',
                      time.time() - 10)
        self.monitor().cycle()
        self.assertEqual(len(self.web.pushes), 1)
        push = self.web.pushes[0]
        self.assertEqual(push["priority"], 3)
        self.assertEqual(push["tags"], ["warning"])
        self.assertIn("not a known store", push["message"])

    def test_link_that_cannot_be_checked_is_not_buried(self):
        # A short link that times out or is dead proves nothing either way: keep it audible, with a note.
        self.web.post("alpha", 1, 'Price error!! Sony XM5 @ ₹400 (MRP ₹34,990) <a href="http://127.0.0.1:9/x">buy</a>',
                      time.time() - 10)
        self.monitor().cycle()
        push = self.web.pushes[0]
        self.assertEqual((push["priority"], push["tags"]), (4, ["warning"]))
        self.assertIn("could not check where the link goes", push["message"])

    def test_short_link_landing_on_an_unknown_shop_names_that_shop(self):
        self.web.redirects["/r/s"] = "/shop/landing"
        self.web.pages["/shop/landing"] = "<html><body>pay here</body></html>"
        self.web.post("alpha", 1, f'Glitch! iPhone at ₹99 <a href="{self.web.base}/r/s">order</a>', time.time() - 10)
        self.monitor().cycle()
        self.assertEqual(self.web.pushes[0]["priority"], 3)
        self.assertIn("link goes to 127.0.0.1, not a known store", self.web.pushes[0]["message"])

    def test_deal_community_links_are_trusted_without_being_fetched(self):
        self.web.post("alpha", 1, 'Sony XM5 @ ₹400 (MRP ₹34,990) <a href="https://www.desidime.com/deals/sony-xm5">details</a>',
                      time.time() - 10)
        self.monitor().cycle()
        push = self.web.pushes[0]
        self.assertEqual((push["priority"], push["tags"]), (5, ["rotating_light"]))
        self.assertEqual(push["click"], "https://www.desidime.com/deals/sony-xm5")

    def test_ordinary_and_expired_posts_stay_silent(self):
        now = time.time()
        self.web.post("alpha", 1, "boAt Rockerz 450 @ ₹1,499 (MRP ₹3,990) https://amzn.to/x", now - 10)
        self.web.post("alpha", 2, "Upto 90% off on fashion, extra 10% cashback", now - 10)
        self.web.post("alpha", 3, "Price error deal over ❌ expired", now - 10)
        self.web.post("alpha", 4, "Recharge ₹199 get ₹50 cashback, refer and earn ₹500", now - 10)
        monitor = self.monitor()
        stats = monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.assertEqual(stats["posts"], 4 + 3)  # these four plus each channel's welcome post
        self.assertEqual(stats["stale"], 3)

    # -- delivery ------------------------------------------------------------
    def test_alert_survives_an_ntfy_outage(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990) https://amzn.to/x", time.time() - 10)
        monitor = self.monitor()
        monitor.resolver = LinkResolver(budget=0.1)  # do not try to reach amzn.to from the test
        self.web.ntfy_status = 500
        monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.assertEqual(len(monitor.db.pending_alerts()), 1)
        self.web.ntfy_status = 200
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertEqual(monitor.db.pending_alerts(), [])
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1, "must not be delivered twice")

    def test_rate_limited_ntfy_is_left_alone_for_a_while(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        self.web.ntfy_status, self.web.ntfy_retry_after = 429, "120"
        monitor = self.monitor()
        monitor.cycle()
        posts_before = self.web.hits["POST /"]
        self.web.ntfy_status = 200
        monitor.cycle()  # still inside Retry-After: no publish attempt
        self.assertEqual(self.web.hits["POST /"], posts_before)
        monitor._ntfy_pause_until = 0
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)

    def test_token_is_sent_as_bearer_and_never_in_the_body(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        self.monitor(token="tk_secret123").cycle()
        self.assertEqual(self.web.push_headers[0]["authorization"], "Bearer tk_secret123")
        self.assertNotIn("tk_secret123", json.dumps(self.web.pushes[0]))

    def test_dry_run_pushes_nothing(self):
        self.settings.dry_run = True
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(self.web.hits["POST /"], 0)
        self.assertEqual(monitor.stats["sent_urgent"], 1)  # counted as handled, not left in the outbox
        self.assertEqual(monitor.db.pending_alerts(), [])

    def test_daily_budget_keeps_room_for_urgent_alerts(self):
        self.settings.daily_alert_limit = 2
        now = time.time()
        for index in range(4):
            self.web.post("alpha", index + 1, f"Cable model {index}X{index} @ ₹99 (MRP ₹1,999)", now - 10)
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual([p["priority"] for p in self.web.pushes], [4, 4])  # two high alerts, then the budget bites
        self.web.post("beta", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", now - 5)
        monitor.cycle()
        self.assertEqual([p["priority"] for p in self.web.pushes], [4, 4, 5])  # urgent still gets through

    # -- watching its own health ----------------------------------------------
    def test_broken_channels_do_not_stop_the_others(self):
        self.settings.channels = ["alpha", "ghost", "beta"]
        self.web.no_preview.add("ghost")
        self.web.post("beta", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        health = {row[0]: row for row in monitor.db.health_rows()}
        self.assertEqual(health["tg:ghost"][3], 1)
        self.assertIn("no public preview", health["tg:ghost"][6])
        self.assertEqual(health["tg:alpha"][3], 0)

    def test_blind_alarm_when_every_source_fails_and_recovery_note_after(self):
        for name in self.settings.channels:
            self.web.status_for[f"/s/{name}"] = 500
        monitor = self.monitor()
        for _ in range(BLIND_STREAK - 1):
            monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        monitor.cycle()
        self.assertEqual([p["title"] for p in self.web.pushes], ["Loot monitor is blind"])
        self.assertEqual(self.web.pushes[0]["priority"], 4)
        self.assertIn("HTTP 500", self.web.pushes[0]["message"])
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1, "the alarm must not repeat every cycle")
        self.web.status_for.clear()
        monitor.cycle()
        self.assertEqual(self.web.pushes[-1]["title"], "Loot monitor recovered")

    def test_refused_site_is_left_alone_instead_of_hammered(self):
        self.settings.channels = ["alpha"]
        self.web.status_for["/s/alpha"] = 429
        monitor = self.monitor()
        for _ in range(4):
            monitor.cycle()
        self.assertEqual(self.web.hits["/s/alpha"], 1)
        self.assertGreater(monitor.fetcher.cooling_down(self.web.base + "/s/alpha"), 0)
        health = {row[0]: row for row in monitor.db.health_rows()}
        self.assertEqual(health["tg:alpha"][3], 4)  # every skipped scan still counts as a failure...
        self.assertRegex(health["tg:alpha"][6], r"cooldown \d+s after HTTP 429")  # ...and says why

    def test_startup_note_waits_until_a_topic_is_set(self):
        self.settings.startup_note = True
        self.settings.ntfy_topic = ""
        first = self.monitor()
        first.run(once=True)
        self.assertEqual(self.web.hits["POST /"], 0)
        self.assertEqual(first.db.get("started_note"), "", "the note must not be used up before it can be delivered")
        self.settings.ntfy_topic = "now-set"
        self.monitor().run(once=True)
        self.assertEqual([(p["topic"], p["title"]) for p in self.web.pushes], [("now-set", "Loot monitor started")])
        self.monitor().run(once=True)
        self.assertEqual(len(self.web.pushes), 1)

    def test_heartbeat_once_a_day(self):
        self.settings.heartbeat_hour = 0
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        monitor = self.monitor()
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)  # first day: only the deal, nothing to summarise yet
        monitor.db.set("heartbeat_day", "2000-01-01")  # pretend the last note was on an earlier day
        monitor.cycle()
        note = self.web.pushes[-1]
        self.assertEqual((note["title"], note["priority"]), ("Loot monitor is running", 2))
        self.assertIn("3 scans", note["message"])
        self.assertIn("1 urgent", note["message"])
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 2)

    # -- feeds and watchlist ------------------------------------------------------
    def test_feed_entries_are_judged_like_posts(self):
        self.settings.channels = []
        self.settings.feeds = [self.web.base + "/feed.xml"]
        stamp = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - 30))
        old = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - 86400))
        item = "<item><title>{t}</title><link>https://www.amazon.in/dp/{a}</link><guid>{a}</guid><pubDate>{d}</pubDate></item>"
        self.web.pages["/feed.xml"] = RSS.format(items=item.format(t="Old one @ ₹1 (MRP ₹9,999)", a="B0OLDOLD01", d=old))
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.web.pages["/feed.xml"] = RSS.format(items=item.format(t="JBL Speaker @ ₹499 (MRP ₹9,999)", a="B0JBLJBL01", d=stamp))
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertEqual(self.web.pushes[0]["click"], "https://www.amazon.in/dp/B0JBLJBL01")
        self.assertEqual(self.web.pushes[0]["priority"], 5)

    def product_page(self, price):
        data = {"@context": "https://schema.org", "@type": "Product", "name": "Nothing Phone (3a) 5G", "sku": "np3a",
                "offers": {"@type": "Offer", "priceCurrency": "INR", "price": price}}
        return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'

    def test_watchlist_alerts_on_a_drop_from_its_own_history(self):
        self.settings.channels = []
        self.settings.traps = []
        url = self.web.base + "/product/np3a"
        self.settings.watchlist = [WatchItem(url=url, label="My Nothing Phone")]
        self.web.pages["/product/np3a"] = self.product_page(24999)
        monitor = self.monitor()
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.web.pages["/product/np3a"] = self.product_page(23999)  # ordinary movement
        monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.web.pages["/product/np3a"] = self.product_page(4000)  # the glitch
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        push = self.web.pushes[0]
        self.assertEqual(push["priority"], 5)
        self.assertIn("My Nothing Phone", push["title"])
        self.assertIn("dropped 84% from its usual ₹24,499", push["message"])  # median of 24,999 and 23,999
        self.assertEqual(push["click"], url)
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1, "a price that stays low is reported once")
        self.assertEqual(monitor.db.product_count(), 1)

    def listing_page(self, products):
        data = {"props": {"pageProps": {"results": [
            {"productId": pid, "name": name, "sellingPrice": price, "mrp": mrp} for pid, name, price, mrp in products
        ]}}}
        return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'

    def test_category_floor_catches_a_price_error_without_any_channel(self):
        self.settings.channels = []
        self.settings.traps = []
        url = self.web.base + "/laptops?sort=price_asc"
        self.settings.watchlist = [WatchItem(url=url, target=4000, label="Laptops, cheapest first")]
        toy = ("L0", "Kids Toy Laptop Learning Game", 999, 1999)  # the page's permanently cheap item
        hp = ("L1", "HP Laptop 15s, Core i5, 16GB, Backlit Keyboard", 42990, 59990)
        dell = ("L2", "Dell Inspiron 15, Ryzen 5, 16GB", 45000, 62000)
        self.web.pages["/laptops"] = self.listing_page([toy, hp, dell])
        monitor = self.monitor()
        monitor.cycle()  # first read: baseline only
        monitor.cycle()
        self.assertEqual(self.web.pushes, [], "what is already on the page when watching starts is not news")

        self.web.pages["/laptops"] = self.listing_page([toy, hp, ("L2", dell[1], 600, 62000)])  # the price error
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        push = self.web.pushes[0]
        self.assertEqual(push["priority"], 5)
        self.assertEqual(push["title"], "Under the floor: 99% off · ₹600 (MRP ₹62,000) — Dell Inspiron 15, Ryzen 5, 16GB")
        self.assertIn("fell from ₹45,000 to under this page's ₹4,000 floor", push["message"])
        self.assertIn("Laptops, cheapest first", push["message"])
        self.assertIn("read from the store", push["message"])

        # A product nobody has seen before appears at an absurd price; an accessory does too.
        acer = ("L3", "Acer Aspire Lite, Core i3, 8GB", 450, 38990)
        skin = ("L4", "Laptop Skin Sticker compatible with HP 15.6 inch", 199, 999)
        self.web.pages["/laptops"] = self.listing_page([toy, hp, ("L2", dell[1], 600, 62000), acer, skin])
        monitor.cycle()
        monitor.cycle()
        self.assertEqual([p["title"].split(" — ")[1] for p in self.web.pushes],
                         ["Dell Inspiron 15, Ryzen 5, 16GB", "Acer Aspire Lite, Core i3, 8GB"])
        self.assertIn("new under this page's ₹4,000 floor, list price ₹38,990", self.web.pushes[1]["message"])
        # Restart: the state file remembers the page, so nothing repeats and no new baseline is needed.
        self.monitor().cycle()
        self.assertEqual(len(self.web.pushes), 2)

    def test_watchlist_target_price_and_unreadable_page(self):
        self.settings.channels = []
        self.settings.traps = []
        self.settings.watchlist = [WatchItem(url=self.web.base + "/product/a", target=5000),
                                   WatchItem(url=self.web.base + "/product/js-only")]
        self.web.pages["/product/a"] = self.product_page(4999)
        self.web.pages["/product/js-only"] = "<html><body><div id='root'></div></body></html>"
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertIn("at or under your target ₹5,000", self.web.pushes[0]["message"])
        health = {row[0]: row for row in monitor.db.health_rows()}
        broken = [row for name, row in health.items() if "js-only" in name][0]
        self.assertEqual(broken[3], 1)
        self.assertIn("no product data found", broken[6])


if __name__ == "__main__":
    unittest.main()
