"""Invite-only channels read through a Telegram account (lootwatch/sources/tgaccount.py).

Telegram itself is never contacted: a stand-in client answers the way Telethon's does.
"""

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lootwatch.config import Settings, load_channels, load_private_channels
from lootwatch.db import DB
from lootwatch.fetch import Fetcher
from lootwatch.links import LinkResolver
from lootwatch.monitor import Monitor
from lootwatch.notify import Notifier
from lootwatch.sources.tgaccount import HAS_TELETHON, AccountReader, invite_code, message_post
from lootwatch.sources.telegram import ChannelPage, TgPost
from tests.helpers import FakeWeb


def entity(kind, **fields):
    """An object whose class is named like Telethon's (MessageEntityStrike, KeyboardButtonUrl, ...)."""
    return type(kind, (), {})() if not fields else type(kind, (), dict(fields))()


def at(text, part):
    """Where ``part`` starts in ``text``, counted the way Telegram counts (UTF-16 units)."""
    return len(text[:text.index(part)].encode("utf-16-le")) // 2


def length(part):
    return len(part.encode("utf-16-le")) // 2


class Messages(unittest.TestCase):
    def test_invite_codes(self):
        self.assertEqual(invite_code("https://t.me/+uV5wcTkUWJEwM2Y1"), "uV5wcTkUWJEwM2Y1")
        self.assertEqual(invite_code("t.me/joinchat/7rHGiXC8TnsxN2I9"), "7rHGiXC8TnsxN2I9")
        self.assertIsNone(invite_code("https://t.me/desidime"))
        self.assertIsNone(invite_code("@Loot_Dealsx"))

    def test_a_message_becomes_a_post(self):
        text = "🔥🔥 Price Error\nSony WH-1000XM5 ₹34,990 ₹4,999\n👉 Buy here\nhttps://amzn.to/abc"
        message = SimpleNamespace(
            id=4521, date=datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc), message=text,
            entities=[entity("MessageEntityStrike", offset=at(text, "₹34,990"), length=length("₹34,990")),
                      entity("MessageEntityTextUrl", offset=at(text, "Buy here"), length=8, url="https://fkrt.cc/xyz"),
                      entity("MessageEntityUrl", offset=at(text, "https://amzn.to/abc"), length=length("https://amzn.to/abc"))],
            reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[entity("KeyboardButtonUrl", url="https://amzn.to/abc"),
                                                                       entity("KeyboardButtonCallback")])]),
            media=SimpleNamespace(webpage=SimpleNamespace(url="https://www.amazon.in/dp/B0SONYXM51")),
        )
        post = message_post(message, 1357275556)
        self.assertEqual((post.post_id, post.channel, post.url), (4521, "c/1357275556", "https://t.me/c/1357275556/4521"))
        self.assertEqual(post.ts, datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(post.text, text)
        self.assertIn("(MRP ₹34,990)", post.analysis_text, "a crossed-out amount is the old price, emoji or not")
        self.assertEqual(post.links, ["https://fkrt.cc/xyz", "https://amzn.to/abc", "https://www.amazon.in/dp/B0SONYXM51"])

    def test_a_bare_picture_is_not_a_post(self):
        self.assertIsNone(message_post(SimpleNamespace(id=1, date=None, message="", entities=None), 1))
        self.assertIsNone(message_post(SimpleNamespace(id=2, date=None, message=None), 1))


class FakeClient:
    """Answers like Telethon's TelegramClient for the calls the reader makes."""

    def __init__(self, joined=True, messages=(), authorized=True, error=None):
        self.joined, self.messages, self.authorized, self.error = joined, list(messages), authorized, error
        self.connected = False
        self.asked = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def __call__(self, request):
        if self.error:
            raise self.error
        self.asked.append(request.hash)
        if self.joined:
            return SimpleNamespace(chat=SimpleNamespace(id=1357275556, title="Offerzone"))
        return SimpleNamespace(title="Offerzone")  # a ChatInvite: the link works, the account is not in

    async def get_messages(self, chat, limit):
        if self.error:
            raise self.error
        return self.messages[:limit]

    async def disconnect(self):
        self.connected = False


@unittest.skipUnless(HAS_TELETHON, "needs telethon")
class Reader(unittest.TestCase):
    def reader(self, client):
        reader = AccountReader("123", "hash", "session", timeout=5, client_factory=lambda: client)
        self.addCleanup(reader.close)
        return reader

    def test_reads_a_joined_channel_and_remembers_it(self):
        text = "Loot ₹99 (MRP ₹2,999) https://amzn.to/x"
        client = FakeClient(messages=[SimpleNamespace(id=12, date=datetime.now(timezone.utc), message=text, entities=[]),
                                      SimpleNamespace(id=11, date=datetime.now(timezone.utc), message="", entities=[])])
        reader = self.reader(client)
        page, error = reader.read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(error, "")
        self.assertEqual((page.status, page.title, [p.post_id for p in page.posts]), ("ok", "Offerzone", [12]))
        reader.read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(client.asked, ["uV5wcTkUWJEwM2Y1"], "the invite is looked up once per run")

    def test_says_what_is_wrong(self):
        page, error = self.reader(FakeClient(joined=False)).read("abcdefgh123")
        self.assertIsNone(page)
        self.assertIn("has not joined this channel", error)
        dead = self.reader(FakeClient(authorized=False))
        self.assertIn("does not work any more", dead.read("abcdefgh123")[1])
        self.assertIn("does not work any more", dead.read("abcdefgh123")[1], "and keeps saying so without asking again")

    def test_a_revoked_login_is_not_tried_again(self):
        error = type("AuthKeyDuplicatedError", (Exception,), {})("used from two places at once")
        client = FakeClient(error=error)
        reader = self.reader(client)
        self.assertIn("Telegram ended the saved login (AuthKeyDuplicatedError)", reader.read("abcdefgh123")[1])
        client.error = None
        self.assertIsNone(reader.read("abcdefgh123")[0], "a dead login stays dead for this run")

    def test_waits_when_telegram_asks_it_to(self):
        error = type("FloodWaitError", (Exception,), {"seconds": 120})("wait")
        client = FakeClient(error=error)
        reader = self.reader(client)
        self.assertIn("asked to wait 120s", reader.read("abcdefgh123")[1])
        client.error = None
        page, why = reader.read("abcdefgh123")
        self.assertIsNone(page)
        self.assertIn("asked to wait", why, "no request is made while Telegram wants a pause")
        self.assertEqual(client.asked, [])


class Lists(unittest.TestCase):
    def test_a_mark_makes_a_loot_channel(self):
        from lootwatch.config import load_loot_channels

        folder = tempfile.mkdtemp()
        path = Path(folder) / "channels.txt"
        path.write_text("! https://t.me/+7rHGiXC8TnsxN2I9 # Offerzone\n!Loot_Dealsx\ndesidime\n", encoding="utf-8")
        self.assertEqual(load_loot_channels(path), ["7rHGiXC8TnsxN2I9", "loot_dealsx"])
        self.assertEqual(load_channels(path), ["Loot_Dealsx", "desidime"])
        self.assertEqual(load_private_channels(path), ["7rHGiXC8TnsxN2I9"])

    def test_invite_links_are_kept_apart_from_public_channels(self):
        folder = tempfile.mkdtemp()
        path = Path(folder) / "channels.txt"
        path.write_text("Loot_Dealsx\nhttps://t.me/+7rHGiXC8TnsxN2I9 # Offerzone\n@desidime\nt.me/joinchat/uV5wcTkUWJEwM2Y1\n",
                        encoding="utf-8")
        with self.assertNoLogs("lootwatch", level="WARNING"):
            self.assertEqual(load_channels(path), ["Loot_Dealsx", "desidime"])
        self.assertEqual(load_private_channels(path), ["7rHGiXC8TnsxN2I9", "uV5wcTkUWJEwM2Y1"])

    def test_the_account_needs_all_three_secrets(self):
        self.assertFalse(Settings(tg_api_id="1", tg_api_hash="h").telegram_account)
        self.assertTrue(Settings(tg_api_id="1", tg_api_hash="h", tg_session="s").telegram_account)


class FakeAccount:
    def __init__(self):
        self.pages = {}
        self.errors = {}
        self.closed = False

    def read(self, code):
        if code in self.errors:
            return None, self.errors[code]
        return self.pages.get(code, ChannelPage("empty", title="Offerzone")), ""

    def close(self):
        self.closed = True


class InTheMonitor(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb().start()
        self.addCleanup(self.web.stop)
        folder = tempfile.mkdtemp()
        self.settings = Settings(ntfy_server=self.web.base, ntfy_topic="t", telegram_base_url=self.web.base + "/s",
                                 interval=15, heartbeat_hour=-1, startup_note=False, db_path=os.path.join(folder, "s.db"))
        self.settings.private_channels = ["7rHGiXC8TnsxN2I9"]
        self.account = FakeAccount()

    def monitor(self):
        monitor = Monitor(self.settings, DB(self.settings.db_path), Fetcher("off"),
                          Notifier(self.web.base, "t", "", ""), LinkResolver(allow_private=True), account=self.account)
        self.addCleanup(monitor.db.close)
        return monitor

    def page(self, *posts):
        self.account.pages["7rHGiXC8TnsxN2I9"] = ChannelPage("ok", title="Offerzone", posts=list(posts))

    def test_a_post_in_an_invite_only_channel_alerts_once(self):
        self.web.redirects["/r/1"] = "https://www.amazon.in/dp/B0SONYXM51"
        old = TgPost("c/1357275556", 1, time.time() - 7200, "Old loot ₹99 (MRP ₹9,999)", "Old loot ₹99 (MRP ₹9,999)")
        self.page(old)
        monitor = self.monitor()
        monitor.cycle()
        self.assertEqual(self.web.pushes, [], "the channel's backlog is not news")
        fresh = TgPost("c/1357275556", 2, time.time() - 20, "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)",
                       "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)", links=[self.web.base + "/r/1"])
        self.page(old, fresh)
        monitor.cycle()
        monitor.cycle()
        (push,) = self.web.pushes
        self.assertEqual(push["priority"], 5)
        self.assertIn("Offerzone", push["message"])
        self.assertEqual(push["actions"][1]["url"], "https://t.me/c/1357275556/2")
        self.assertEqual(monitor._display("tg:+7rHGiXC8TnsxN2I9"), "Offerzone (invite-only)")

    def test_a_marked_channel_rings_for_big_loots_with_shop_links_only(self):
        from unittest import mock

        from lootwatch import links
        from lootwatch.config import load_traps

        # The stand-in server plays a short-link service (like bit.ly): followed to see where it ends.
        patcher = mock.patch.object(links, "GENERIC_SHORTENERS", links.GENERIC_SHORTENERS + ("127.0.0.1",))
        patcher.start()
        self.addCleanup(patcher.stop)
        traps = Path(tempfile.mkdtemp()) / "traps.txt"
        traps.write_text("iphone | 12000\n", encoding="utf-8")
        self.settings.traps = load_traps(traps)
        self.settings.loot_channels = ["7rHGiXC8TnsxN2I9"]
        self.web.redirects["/r/amazon"] = "https://www.amazon.in/dp/B0BIGLOOT1"
        self.web.redirects["/r/elsewhere"] = self.web.base + "/some-shop"
        self.web.redirects["/r/telegram"] = "https://t.me/+AnotherChannel123"
        self.web.pages["/some-shop"] = "<html><body>a shop nobody knows</body></html>"
        now = time.time()

        def post(number, text, links):
            return TgPost("c/1357275556", number, now - 20, text, text, links=links)

        self.page(TgPost("c/1357275556", 1, now - 7200, "Welcome", "Welcome"))
        monitor = self.monitor()
        monitor.cycle()
        self.page(
            post(2, "BIG LOOT 😱🔥 Buy fast", [self.web.base + "/r/amazon"]),
            post(3, "BIG LOOT 🔥 hurry", [self.web.base + "/r/elsewhere"]),
            post(4, "BIGGEST LOOT : BUY NOW 👇", ["https://t.me/addlist/VETj13smDQVmMWE1"]),
            post(5, "BIGGEST LOOT : iPhone 17 at ONLY ₹2952 😱", ["https://t.me/addlist/zJpOTtO9ebM1NzM1"]),
            post(6, "MEGA LOOT 🔥", [self.web.base + "/r/telegram"]),
            post(7, "BIG LOOT: Lifetime free credit card, apply now", [self.web.base + "/r/amazon"]),
        )
        monitor.cycle()
        pushes = [(p["priority"], p["title"]) for p in self.web.pushes]
        self.assertEqual(pushes[0][0], 5, pushes)
        self.assertTrue(pushes[0][1].startswith("Big loot"), pushes)
        self.assertEqual(self.web.pushes[0]["click"], "https://www.amazon.in/dp/B0BIGLOOT1")
        # 3 leads to an unknown shop, 4 and 6 to other channels, 7 is a card promotion: none of them rings.
        # 5 names a product under its trap price but links only to a Telegram folder: quiet, with a warning.
        self.assertEqual(len(pushes), 2, pushes)
        self.assertEqual(pushes[1][0], 3)
        self.assertIn("links only to Telegram channels", self.web.pushes[1]["message"])

    def test_a_channel_that_cannot_be_read_is_reported_like_any_source(self):
        self.account.errors["7rHGiXC8TnsxN2I9"] = "the Telegram account has not joined this channel: open the invite link"
        monitor = self.monitor()
        monitor.cycle()
        row = next(r for r in monitor.db.health_rows() if r[0] == "tg:+7rHGiXC8TnsxN2I9")
        self.assertEqual(row[3], 1)
        self.assertIn("has not joined", row[6])

    def test_without_an_account_the_invite_links_are_skipped(self):
        monitor = Monitor(self.settings, DB(self.settings.db_path), Fetcher("off"), Notifier(self.web.base, "t", "", ""))
        self.addCleanup(monitor.db.close)
        self.assertIsNone(monitor.account)
        self.assertEqual(monitor._private, [])
        with self.assertLogs("lootwatch", level="INFO") as logs:
            monitor.run(once=True)
        self.assertTrue(any("invite-only channel(s)" in line and "skipped" in line for line in logs.output))

    def test_the_account_is_closed_when_the_run_ends(self):
        self.monitor().run(once=True)
        self.assertTrue(self.account.closed)


if __name__ == "__main__":
    unittest.main()
