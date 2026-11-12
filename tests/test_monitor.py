"""End-to-end: fake Telegram previews + fake ntfy on localhost, real monitor code in between."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from lootwatch.config import Settings, WatchItem, load_traps
from lootwatch.db import DB
from lootwatch.fetch import Fetcher, FetchResult
from lootwatch.links import LinkResolver
from lootwatch.monitor import BLIND_STREAK, FAIL_STREAK_NOTE, SITE_FAIL_STREAK, Monitor, short_url, source_key
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

    def monitor(self, token="", fetcher=None) -> Monitor:
        notifier = Notifier(self.settings.ntfy_server, self.settings.ntfy_topic, token,
                            self.settings.ntfy_health_topic, dry_run=self.settings.dry_run)
        monitor = Monitor(self.settings, DB(self.db_path), fetcher or Fetcher("off"), notifier,
                          LinkResolver(allow_private=True))
        self.monitors.append(monitor)
        return monitor

    @staticmethod
    def sql(monitor, statement, *args):
        """Reach into the state file, to move the clock: age a reading, an alert, a product."""
        with monitor.db._lock, monitor.db._conn:
            return monitor.db._conn.execute(statement, args).fetchall()

    def titles(self):
        return [p["title"] for p in self.web.pushes]

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

    def test_post_published_while_the_channel_went_unread_is_still_judged(self):
        now = time.time()
        self.settings.channels = ["alpha", "beta"]
        self.web.redirects["/r/np"] = "https://www.flipkart.com/nothing-phone/p/itm1?pid=MOBNOTHING000001"
        monitor = self.monitor()
        monitor.cycle()
        # The next run starts late: alpha was last read 25 minutes ago, and a price error was posted 22 minutes ago.
        self.sql(monitor, "UPDATE health SET last_ok = ? WHERE source = 'tg:alpha'", int(now - 25 * 60))
        self.web.post("alpha", 5, f'Price Error - Nothing Phone (3a) Pro ₹59,999 → ₹400 <a href="{self.web.base}/r/np">buy</a>',
                      now - 22 * 60)
        # beta was read a minute ago, so a 22-minute-old post there is one the monitor already chose to skip... or old news.
        self.web.post("beta", 5, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", now - 22 * 60)
        stats = monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertEqual(self.web.pushes[0]["priority"], 5)
        self.assertIn("Nothing Phone (3a) Pro", self.web.pushes[0]["title"])
        self.assertIn("posted 22m ago", self.web.pushes[0]["message"])
        self.assertEqual(stats["stale"], 1)

    def test_a_long_outage_does_not_replay_hours_of_old_posts(self):
        now = time.time()
        self.settings.channels = ["alpha"]
        monitor = self.monitor()
        monitor.cycle()
        self.sql(monitor, "UPDATE health SET last_ok = ? WHERE source = 'tg:alpha'", int(now - 3 * 3600))
        self.web.post("alpha", 5, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", now - 90 * 60)  # probably dead by now
        self.web.post("alpha", 6, "Price error: JBL Flip 6 @ ₹199 (MRP ₹11,999)", now - 50 * 60)
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertIn("JBL Flip 6", self.web.pushes[0]["title"])

    def test_trending_never_lifts_a_post_past_what_its_link_allows(self):
        now = time.time()
        self.web.pages["/shop/oneplus"] = "<html><body>pay here</body></html>"
        strange = f'Price error glitch!! OnePlus 12 at ₹99 <a href="{self.web.base}/shop/oneplus">order</a>'
        dead = 'Price error!! Sony XM5 @ ₹400 (MRP ₹34,990) <a href="http://127.0.0.1:9/x">buy</a>'
        monitor = self.monitor()
        for index, channel in enumerate(self.settings.channels):
            self.web.post(channel, 1, strange, now - 30 + index)
            self.web.post(channel, 2, dead, now - 30 + index)
            monitor.cycle()
        # Copied to three channels within minutes: exactly how a scam post spreads. One push each, never louder.
        self.assertEqual(sorted((p["priority"], p["tags"]) for p in self.web.pushes), [(3, ["warning"]), (4, ["warning"])])
        self.assertFalse(any("Now in" in title for title in self.titles()))

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
        for _ in range(3):
            monitor.cycle()  # still inside Retry-After: no publish attempt
        self.assertEqual(self.web.hits["POST /"], posts_before)
        self.assertEqual(monitor.db.pending_alerts()[0].attempts, 1, "waiting is not a failed attempt")
        monitor._ntfy_pause_until = 0
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)

    def test_undelivered_alert_is_kept_for_half_an_hour_however_many_tries_that_takes(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        self.web.ntfy_status = 403  # a bad token, or ntfy refusing this address
        monitor = self.monitor()
        for _ in range(12):
            monitor.cycle()
        pending = monitor.db.pending_alerts()
        self.assertEqual(len(pending), 1, "a dozen failed tries in a few minutes must not write it off")
        self.assertGreaterEqual(pending[0].attempts, 12)
        self.web.ntfy_status = 200
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertEqual(monitor.stats["undelivered"], 0)

    def test_alert_older_than_half_an_hour_is_given_up_and_the_run_reports_it(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        self.web.ntfy_status = 403
        monitor = self.monitor()
        monitor.cycle()
        self.sql(monitor, "UPDATE alerts SET created_at = created_at - 1860")
        self.assertEqual(monitor.run(once=True), 1, "run() reports what never reached the phone")
        self.assertEqual(monitor.db.pending_alerts(), [])
        self.web.ntfy_status = 200
        monitor.cycle()
        self.assertEqual(self.web.pushes, [], "too late to be useful: it is not sent after all")

    def test_message_that_ntfy_rejects_outright_is_not_sent_thirty_times(self):
        self.web.post("alpha", 1, "Price error: Sony XM5 @ ₹400 (MRP ₹34,990)", time.time() - 10)
        self.web.ntfy_status = 400
        monitor = self.monitor()
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(self.web.hits["POST /"], 1)
        self.assertEqual(monitor.db.pending_alerts(), [])
        self.assertEqual(monitor.stats["undelivered"], 1)

    def test_without_a_topic_nothing_is_recorded_as_sent(self):
        self.settings.ntfy_topic = ""
        self.web.redirects["/r/p"] = "https://www.amazon.in/dp/B0SONYXM51"
        link = f'<a href="{self.web.base}/r/p">x</a>'
        self.web.post("alpha", 1, f"Price error: Sony XM5 @ ₹400 (MRP ₹34,990) {link}", time.time() - 10)
        first = self.monitor()
        first.cycle()
        self.assertEqual(self.web.hits["POST /"], 0)
        self.assertEqual(first.db.sent_counts(0), {}, "the daily note must not count alerts that reached no phone")
        # The topic is set while the error is still live, and a second channel posts it.
        self.settings.ntfy_topic = "now-set"
        self.web.post("beta", 1, f"Sony XM5 price error ₹400 (MRP ₹34,990) {link}", time.time() - 5)
        self.monitor().cycle()
        self.assertEqual([(p["topic"], p["priority"]) for p in self.web.pushes], [("now-set", 5)])

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

    def test_blind_alarm_also_fires_when_sources_hang_instead_of_failing(self):
        class Hanging(Fetcher):
            """One request times out and uses up the scan; every other channel is never reached."""

            def get(self, url, headers=None, deadline=None):
                if url.endswith("/s/alpha"):
                    return FetchResult(url, error="timed out (ReadTimeout)")
                return FetchResult(url, error="skipped: cycle time budget used up", skipped=True)

        monitor = self.monitor(fetcher=Hanging("off"))
        for _ in range(BLIND_STREAK - 1):
            monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        monitor.cycle()
        self.assertEqual(self.titles(), ["Loot monitor is blind"])
        self.assertIn("not reached in the time a scan allows", self.web.pushes[0]["message"])
        self.assertIn("timed out", self.web.pushes[0]["message"])

    def test_broken_source_is_reported_once_and_again_only_after_it_has_worked(self):
        self.settings.channels = ["alpha", "ghost"]
        self.web.no_preview.add("ghost")
        monitor = self.monitor()
        for _ in range(FAIL_STREAK_NOTE - 1):
            monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        for _ in range(4):
            monitor.cycle()
        self.assertEqual(self.titles(), ["1 source(s) stopped working"])
        self.assertIn("@ghost: no public preview", self.web.pushes[0]["message"])
        # A restart in the middle of the trouble does not repeat the note either.
        restarted = self.monitor()
        for _ in range(3):
            restarted.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.web.no_preview.discard("ghost")
        self.web.channels["ghost"] = [{"id": 1, "html": "back again", "ts": time.time() - 86400, "buttons": []}]
        restarted.cycle()
        self.web.no_preview.add("ghost")
        for _ in range(FAIL_STREAK_NOTE):
            restarted.cycle()
        self.assertEqual(self.titles(), ["1 source(s) stopped working"] * 2)

    def test_whole_store_unreadable_raises_one_alarm_and_one_all_clear(self):
        self.settings.channels = []
        self.settings.watchlist = [WatchItem(url=self.web.base + "/laptops", target=4000, label="Laptops"),
                                   WatchItem(url=self.web.base + "/phones", target=2500, label="Phones")]
        page = self.listing_page([("L1", "HP Laptop 15s", 42990, 59990)])
        self.web.pages["/laptops"] = self.web.pages["/phones"] = page
        monitor = self.monitor()
        monitor.cycle()
        self.web.status_for["/laptops"] = self.web.status_for["/phones"] = 500
        for _ in range(SITE_FAIL_STREAK - 1):
            monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        for _ in range(5):
            monitor.cycle()
        self.assertEqual(self.titles(), ["Loot monitor: store pages unreadable (127.0.0.1)"])
        self.assertEqual(self.web.pushes[0]["priority"], 4)
        self.assertIn("HTTP 500", self.web.pushes[0]["message"])
        self.web.status_for.clear()
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(self.titles()[1:], ["Loot monitor: 127.0.0.1 is readable again"])

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
        self.assertIn("dropped 84% from the ₹24,499 it was seen at", push["message"])  # median of 24,999 and 23,999
        self.assertIn("84% off · ₹4,000 (was ₹24,499)", push["title"])
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
        broken = health[source_key("watch", self.web.base + "/product/js-only")]
        self.assertEqual(broken[3], 1)
        self.assertIn("no product data found", broken[6])

    def floor_page(self, *extra):
        """A cheapest-first phone page with its two ordinary entries, plus whatever is passed in."""
        regular = [("P1", "LAVA Bold N4 Lite (Empire Gold, 32 GB)", 8299, 9999), ("P2", "Ai+ Pulse 2 (Green, 64 GB)", 8999, 10999)]
        self.web.pages["/phones"] = self.listing_page(list(extra) + regular)

    def watch_phones(self) -> Monitor:
        self.settings.channels = []
        self.settings.traps = []
        self.settings.watchlist = [WatchItem(url=self.web.base + "/phones?sort=price_asc", target=2500, label="Phones")]
        self.floor_page()
        monitor = self.monitor()
        monitor.cycle()  # baseline
        return monitor

    def test_same_error_days_later_alerts_again(self):
        galaxy = ("P9", "Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 400, 129999)
        monitor = self.watch_phones()
        self.floor_page(galaxy)
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertTrue(self.web.pushes[0]["title"].startswith("Under the floor: 99% off · ₹400 (MRP ₹1,29,999)"))
        # The seller fixes it. On a cheapest-first page the phone simply disappears: its normal price is never seen.
        self.floor_page()
        monitor.cycle()
        # Three days pass with the page being read all along, then the same listing is mispriced again.
        self.sql(monitor, "UPDATE products SET last_seen = last_seen - 259200 WHERE key LIKE '%P9'")
        self.sql(monitor, "UPDATE alerts SET created_at = created_at - 259200, sent_at = sent_at - 259200")
        self.floor_page(("P9", galaxy[1], 450, 129999))
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 2)
        self.assertIn("₹450", self.web.pushes[1]["title"])

    def test_after_an_outage_of_the_monitor_itself_nothing_counts_as_returned(self):
        junk = ("P8", "I Kall K800 (Blue, 32 GB)", 2399, 7999)  # permanently under the floor, inflated list price
        self.settings.channels = []
        self.settings.traps = []
        self.settings.watchlist = [WatchItem(url=self.web.base + "/phones?sort=price_asc", target=2500, label="Phones")]
        self.floor_page(junk)
        monitor = self.monitor()
        monitor.cycle()  # baseline: the junk phone is part of the page's normal picture
        # The monitor is off for three days. Everything it knows is three days old, the page's last read included.
        self.sql(monitor, "UPDATE products SET last_seen = last_seen - 259200")
        self.sql(monitor, "UPDATE health SET last_ok = last_ok - 259200")
        self.monitor().cycle()
        self.assertEqual(self.web.pushes, [])

    def test_alert_that_never_got_through_lets_the_product_alert_again(self):
        monitor = self.watch_phones()
        self.web.ntfy_status = 403
        self.floor_page(("P9", "Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 400, 129999))
        monitor.cycle()
        self.assertEqual(len(monitor.db.pending_alerts()), 1)
        # Half an hour of failed deliveries later the alert is written off...
        self.sql(monitor, "UPDATE alerts SET created_at = created_at - 1860")
        monitor.cycle()
        self.assertEqual(monitor.stats["undelivered"], 1)
        # ...but the phone is still at ₹400 on the page, so a fresh alert was raised, and it gets through once ntfy works.
        self.assertEqual(len(monitor.db.pending_alerts()), 1)
        self.web.ntfy_status = 200
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertIn("Samsung Galaxy S25 Ultra", self.web.pushes[0]["title"])
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)

    def test_list_price_that_shows_up_a_scan_later_still_alerts(self):
        monitor = self.watch_phones()
        # First sighting: the card shows no crossed-out price, so there is nothing to judge it by.
        self.web.pages["/phones"] = self.ld_listing([("P9", "Google Pixel 9 (Obsidian, 256 GB)", 400, None),
                                                     ("P1", "LAVA Bold N4 Lite", 8299, 9999)])
        monitor.cycle()
        self.assertEqual(self.web.pushes, [])
        self.web.pages["/phones"] = self.ld_listing([("P9", "Google Pixel 9 (Obsidian, 256 GB)", 400, 79999),
                                                     ("P1", "LAVA Bold N4 Lite", 8299, 9999)])
        monitor.cycle()
        self.assertEqual(len(self.web.pushes), 1)
        self.assertIn("Under the floor", self.web.pushes[0]["title"])

    def test_dropped_digit_above_the_floor_is_caught_by_its_own_discount(self):
        monitor = self.watch_phones()
        self.floor_page(("P7", "Google Pixel 9 (Obsidian, 256 GB)", 7999, 79999),   # 90% off: a dropped digit
                        ("P6", "BOLTT EVO (Sky Blue, 64 GB)", 8999, 17999))          # 50% off: an ordinary no-name phone
        monitor.cycle()
        monitor.cycle()
        self.assertEqual([(p["priority"], p["title"]) for p in self.web.pushes],
                         [(4, "Far below list price: 90% off · ₹7,999 (MRP ₹79,999) — Google Pixel 9 (Obsidian, 256 GB)")])
        self.assertIn("new on this page at 90% off its list price ₹79,999", self.web.pushes[0]["message"])

    def test_whose_turn_it_is_survives_a_restart_and_marked_pages_come_round_more_often(self):
        self.settings.channels = []
        urls = [self.web.base + f"/page{n}" for n in range(4)]
        self.settings.watchlist = [WatchItem(url=urls[0], label="plain a"), WatchItem(url=urls[1], label="plain b"),
                                   WatchItem(url=urls[2], label="marked", weight=4.0), WatchItem(url=urls[3], label="new")]
        monitor = self.monitor()
        now = time.time()
        monitor.db.health_ok(source_key("watch", urls[0]), 5, now=now - 100)
        monitor.db.health_fail(source_key("watch", urls[1]), "HTTP 500", now=now - 300)
        monitor.db.health_ok(source_key("watch", urls[2]), 5, now=now - 60)
        restarted = self.monitor()
        order = [item.label for item in restarted._watch_batch()]
        # never read -> first; then the longest wait, with the marked page's 60 s counting as 240 s.
        self.assertEqual(order, ["new", "plain b", "marked", "plain a"])

    def ld_listing(self, products):
        nodes = []
        for sku, name, price, mrp in products:
            offer = {"@type": "Offer", "priceCurrency": "INR", "price": price}
            if mrp:
                offer["priceSpecification"] = [{"@type": "UnitPriceSpecification",
                                                "priceType": "https://schema.org/ListPrice", "price": mrp}]
            nodes.append({"@context": "https://schema.org", "@type": "Product", "name": name, "sku": sku, "offers": offer})
        return f'<html><head><script type="application/ld+json">{json.dumps(nodes)}</script></head><body></body></html>'

    def test_listing_that_stops_showing_list_prices_is_reported_as_failing(self):
        self.settings.channels = []
        self.settings.traps = []
        url = self.web.base + "/tvs"
        self.settings.watchlist = [WatchItem(url=url, target=2000, label="TVs")]
        tvs = [(f"T{n}", f"Billion {24 + n} inch LED TV", 5749 + 100 * n, 17500) for n in range(6)]
        self.web.pages["/tvs"] = self.ld_listing(tvs)
        monitor = self.monitor()
        monitor.cycle()
        source = source_key("watch", url)
        self.assertEqual({row[0]: row[3] for row in monitor.db.health_rows()}[source], 0)
        # A layout change: every product still parses, but the crossed-out prices are gone.
        self.web.pages["/tvs"] = self.ld_listing([(sku, name, price, None) for sku, name, price, _ in tvs])
        stats = monitor.cycle()
        row = {row[0]: row for row in monitor.db.health_rows()}[source]
        self.assertEqual(row[3], 1)
        self.assertIn("none shows a list price any more", row[6])
        self.assertEqual(stats["prices"], 6, "what can still be read is still read")

    def test_two_long_addresses_that_start_alike_keep_separate_state(self):
        self.settings.channels = []
        self.settings.traps = []
        common = "/mobiles/" + "x" * 70
        floor_url, popular_url = self.web.base + common + "/cheapest", self.web.base + common + "/popular"
        self.assertEqual(short_url(floor_url, 60), short_url(popular_url, 60))  # the old way of naming them
        self.settings.watchlist = [WatchItem(url=floor_url, target=2500, label="Cheapest phones"),
                                   WatchItem(url=popular_url, label="Popular phones")]
        junk = ("P8", "I Kall K800 (Blue, 32 GB)", 2399, 7999)
        self.web.pages[common + "/cheapest"] = self.listing_page([junk])
        self.web.pages[common + "/popular"] = self.listing_page([("P1", "LAVA Bold N4 Lite", 8299, 9999)])
        # The floor page fails on its first read; its twin is read. Then the floor page is read.
        self.web.status_for[common + "/cheapest"] = 500
        monitor = self.monitor()
        monitor.cycle()
        del self.web.status_for[common + "/cheapest"]
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(self.web.pushes, [], "the floor page's own first read is its baseline, whatever its twin did")
        health = {row[0]: row for row in monitor.db.health_rows()}
        self.assertEqual(len(health), 2)
        self.assertEqual(health[source_key("watch", floor_url)][2], 1)  # its one failure is on its own record
        self.assertEqual(health[source_key("watch", popular_url)][2], 0)

    def test_page_learnt_under_its_old_name_is_not_learnt_again(self):
        self.settings.channels = []
        self.settings.traps = []
        url = self.web.base + "/phones?sort=price_asc"
        self.settings.watchlist = [WatchItem(url=url, target=2500, label="Phones")]
        self.floor_page(("P9", "Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 400, 129999))
        monitor = self.monitor()
        monitor.db.set(f"boot:watch:{short_url(url, 60)}", 1700000000)  # state written by an earlier version
        monitor.run(once=True)
        self.assertEqual(len(self.web.pushes), 1, "no silent baseline read: the page was already known")


class StateFile(unittest.TestCase):
    def test_state_written_by_an_earlier_version_is_taken_over_quietly(self):
        import sqlite3

        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE products (key TEXT PRIMARY KEY, store TEXT, title TEXT, url TEXT, "
                     "first_seen INTEGER, last_seen INTEGER, last_price REAL, mrp REAL)")
        conn.execute("INSERT INTO products VALUES ('flipkart:JUNK1', 'flipkart.com', 'I Kall K800', 'u', 1, ?, 2399, 7999)",
                     (int(time.time()) - 60,))
        conn.commit()
        conn.close()
        db = DB(path)
        self.addCleanup(db.close)
        info = db.record_price("flipkart:JUNK1", "flipkart.com", "I Kall K800", "u", 2399, 7999)
        # Whatever the old file knew was either part of a baseline or already reported: not news now.
        self.assertEqual((info.quiet_price, info.quiet_alerted, info.first_seen), (2399.0, False, False))
        db.mark_quiet("flipkart:JUNK1", 400, alerted=True)
        info = db.record_price("flipkart:JUNK1", "flipkart.com", "I Kall K800", "u", 400, 7999)
        self.assertEqual((info.quiet_price, info.quiet_alerted), (400.0, True))
        db.clear_quiet("flipkart:JUNK1")
        self.assertIsNone(db.record_price("flipkart:JUNK1", "flipkart.com", "I Kall K800", "u", 400, 7999).quiet_price)
        db.close()
        DB(path).close()  # opening it a second time changes nothing


if __name__ == "__main__":
    unittest.main()
