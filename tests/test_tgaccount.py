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
        self.handlers = []
        self.dialogs = []
        self.min_ids = []

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))

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

    async def get_messages(self, chat, limit, min_id=0):
        if self.error:
            raise self.error
        self.min_ids.append(min_id)
        return [m for m in self.messages if m.id > min_id][:limit]

    def disconnect(self):
        asyncio.get_running_loop()  # like Telethon's, it works only on the client's own loop

        async def done():
            self.connected = False

        return done()


class JoiningClient(FakeClient):
    """Also answers the calls that join a public channel, mute it and archive it."""

    def __init__(self, join_error=None, **kwargs):
        super().__init__(**kwargs)
        self.join_error = join_error
        self.requests = []

    async def get_entity(self, name):
        return SimpleNamespace(id=700 + len(name), username=name)

    async def get_input_entity(self, channel):
        return SimpleNamespace(channel_id=channel.id)

    async def __call__(self, request):
        kind = type(request).__name__
        if kind == "CheckChatInviteRequest":
            return await super().__call__(request)
        if kind == "JoinChannelRequest" and self.join_error:
            raise self.join_error
        self.requests.append((kind, request))
        return SimpleNamespace()


class InviteClient(JoiningClient):
    """Answers an invite look-up with what the link leads to, and joins it."""

    def __init__(self, invite, import_error=None, **kwargs):
        super().__init__(**kwargs)
        self.invite, self.import_error = invite, import_error

    async def __call__(self, request):
        kind = type(request).__name__
        if kind == "CheckChatInviteRequest":
            self.asked.append(request.hash)
            return self.invite
        if kind == "ImportChatInviteRequest":
            if self.import_error:
                raise self.import_error
            self.requests.append((kind, request))
            return SimpleNamespace(chats=[SimpleNamespace(id=900, title=self.invite.title, broadcast=True)])
        return await super().__call__(request)


def a_channel(title="Dealdost", members=3_100_000, broadcast=True):
    """What Telegram says of an invite link the account is not in yet (a ChatInvite)."""
    return SimpleNamespace(title=title, participants_count=members, channel=True, broadcast=broadcast,
                           about="India's Most Trusted Deals Channel (Since 2015)")


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
        reader.close()
        self.assertFalse(client.connected, "the run ends with a clean goodbye to Telegram")

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

    def test_new_posts_are_handed_on_the_moment_telegram_sends_them(self):
        import threading

        client = FakeClient(messages=[SimpleNamespace(id=1, date=datetime.now(timezone.utc), message="Welcome", entities=[])])
        reader = self.reader(client)
        got, done = [], threading.Event()

        def on_post(code, post):
            got.append((code, post.post_id, post.url, post.links, threading.current_thread().name))
            done.set()

        reader.listen(on_post)
        reader.read("uV5wcTkUWJEwM2Y1")  # the first reading tells which chat the invite stands for
        (handler, builder), = client.handlers
        self.assertEqual(type(builder).__name__, "NewMessage")

        def arrive(message):
            asyncio.run_coroutine_threadsafe(handler(SimpleNamespace(message=message)), reader._loop).result(5)

        def message(chat, number, text):
            return SimpleNamespace(id=number, date=datetime.now(timezone.utc), message=text, entities=[],
                                   peer_id=SimpleNamespace(channel_id=chat))

        arrive(message(999, 5, "a message in another chat of the account"))
        arrive(message(1357275556, 6, ""))  # a bare picture
        arrive(message(1357275556, 7, "BIG LOOT : Boat Earbuds https://amzn.to/x"))
        self.assertTrue(done.wait(5))
        reader.close()
        self.assertEqual(len(got), 1, got)
        code, number, url, links, thread = got[0]
        self.assertEqual((code, number, url, links), ("uV5wcTkUWJEwM2Y1", 7, "https://t.me/c/1357275556/7", ["https://amzn.to/x"]))
        self.assertTrue(thread.startswith("telegram-live"), "the post is handled off the event loop")
        asyncio.run(handler(SimpleNamespace(message=message(1357275556, 8, "after the run ended"))))
        self.assertEqual(len(got), 1, "nothing is handed on once the reader is closed")

    def test_a_reading_of_newer_messages_only(self):
        def message(number, text):
            return SimpleNamespace(id=number, date=datetime.now(timezone.utc), message=text, entities=[])

        client = FakeClient(messages=[message(12, "Loot ₹99"), message(11, "")])
        reader = self.reader(client)
        self.assertEqual([p.post_id for p in reader.read("uV5wcTkUWJEwM2Y1", newer_only=True)[0].posts], [12])
        page, error = reader.read("uV5wcTkUWJEwM2Y1", newer_only=True)
        self.assertEqual((page.status, page.posts, error), ("ok", [], ""), "nothing new is not a failure")
        client.messages.insert(0, message(14, ""))  # a bare picture still moves the mark on
        reader.read("uV5wcTkUWJEwM2Y1", newer_only=True)
        client.messages.insert(0, message(15, "BIG LOOT https://amzn.to/x"))
        self.assertEqual([p.post_id for p in reader.read("uV5wcTkUWJEwM2Y1", newer_only=True)[0].posts], [15])
        self.assertEqual(client.min_ids, [0, 12, 12, 14])
        self.assertEqual(reader.read("uV5wcTkUWJEwM2Y1")[0].status, "ok", "an ordinary reading still reads the page")

    def test_public_channels_the_login_has_joined_are_pushed_too(self):
        import threading

        client = FakeClient(messages=[])
        client.dialogs = [SimpleNamespace(entity=SimpleNamespace(id=555, username="epicdeals_in", usernames=None)),
                          SimpleNamespace(entity=SimpleNamespace(id=556, username=None,
                                                                 usernames=[SimpleNamespace(username="LootersIndia")])),
                          SimpleNamespace(entity=SimpleNamespace(id=557, username="somefriend", usernames=None))]
        reader = self.reader(client)
        got, done = [], threading.Event()
        reader.listen(lambda code, post: (got.append((code, post.url)), done.set()),
                      public=["EpicDeals_in", "lootersindia", "desidime"])
        with self.assertLogs("lootwatch", level="INFO") as logs:
            reader.read("uV5wcTkUWJEwM2Y1")
        self.assertTrue(any("2 of the 3 public channels" in line and "other 1 are read from the web" in line
                            for line in logs.output), logs.output)
        (handler, _), = client.handlers
        message = SimpleNamespace(id=901, date=datetime.now(timezone.utc), message="Loot ₹99 https://amzn.to/x",
                                  entities=[], peer_id=SimpleNamespace(channel_id=556))
        asyncio.run_coroutine_threadsafe(handler(SimpleNamespace(message=message)), reader._loop).result(5)
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [("@lootersindia", "https://t.me/lootersindia/901")])

    def test_the_login_joins_the_public_channels_it_has_not_quietly(self):
        import threading

        client = JoiningClient(messages=[])
        client.dialogs = [SimpleNamespace(entity=SimpleNamespace(id=555, username="epicdeals_in", usernames=None))]
        reader = self.reader(client)
        self.assertIsNone(reader.missing_public(), "not known before the chat list is looked at")
        got, done = [], threading.Event()
        reader.listen(lambda code, post: (got.append((code, post.url)), done.set()), public=["epicdeals_in", "dealsmagnet"])
        reader.read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(reader.missing_public(), ["dealsmagnet"])
        self.assertEqual(reader.join("dealsmagnet"), "")
        kinds = [kind for kind, _ in client.requests]
        self.assertEqual(kinds, ["JoinChannelRequest", "UpdateNotifySettingsRequest", "EditPeerFoldersRequest"])
        self.assertEqual(client.requests[1][1].settings.mute_until, 2 ** 31 - 1, "muted for good")
        self.assertEqual(client.requests[2][1].folder_peers[0].folder_id, 1, "put in the archive")
        self.assertEqual(reader.missing_public(), [])
        # Its posts are pushed from now on.
        (handler, _), = client.handlers
        message = SimpleNamespace(id=77, date=datetime.now(timezone.utc), message="Loot ₹99 https://amzn.to/x",
                                  entities=[], peer_id=SimpleNamespace(channel_id=700 + len("dealsmagnet")))
        asyncio.run_coroutine_threadsafe(handler(SimpleNamespace(message=message)), reader._loop).result(5)
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [("@dealsmagnet", "https://t.me/dealsmagnet/77")])

    def test_a_join_limit_holds_up_joining_not_reading(self):
        error = type("FloodWaitError", (Exception,), {"seconds": 900})("wait")
        client = JoiningClient(messages=[], join_error=error)
        reader = self.reader(client)
        reader.listen(lambda code, post: None, public=["dealsmagnet"])
        reader.read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(reader.join("dealsmagnet"), "wait 900s")
        self.assertTrue(reader.join("dealsmagnet").startswith("wait 9"), "not asked again meanwhile")
        self.assertEqual(reader.quiet_for(), 0.0)
        page, error_text = reader.read("uV5wcTkUWJEwM2Y1", newer_only=True)
        self.assertEqual(error_text, "")

    def test_invite_only_channels_are_found_in_the_chat_list_without_their_invite_links(self):
        # Telegram refused invite-link look-ups for hours on 23-24 Nov 2026, when every run asked again.
        import threading

        offerzone = SimpleNamespace(id=1357275556, title="Offerzone", username=None, usernames=None)
        client = FakeClient(messages=[SimpleNamespace(id=5, date=datetime.now(timezone.utc), message="Loot ₹1 https://amzn.to/x",
                                                      entities=[])])
        client.dialogs = [SimpleNamespace(entity=SimpleNamespace(id=42, title="Offerzone", username="offerzone_public",
                                                                 usernames=None)),  # a public namesake: not it
                          SimpleNamespace(entity=offerzone)]
        reader = self.reader(client)
        reader.use_known({}, {"7rHGiXC8TnsxN2I9": "Offerzone"})
        got, done = [], threading.Event()
        reader.listen(lambda code, post: (got.append(code), done.set()), public=[])
        with self.assertLogs("lootwatch", level="INFO") as logs:
            page, error = reader.read("7rHGiXC8TnsxN2I9")
        self.assertEqual((error, page.title), ("", "Offerzone"))
        self.assertEqual(client.asked, [], "no invite-link look-up")
        self.assertEqual(reader.chat_id("7rHGiXC8TnsxN2I9"), 1357275556)
        self.assertIn("invite-only channels found in the login's chat list: 1 of 1", "\n".join(logs.output))
        # Its pushed posts are recognised at once.
        (handler, _), = client.handlers
        message = SimpleNamespace(id=6, date=datetime.now(timezone.utc), message="Loot ₹1 https://amzn.to/x", entities=[],
                                  peer_id=SimpleNamespace(channel_id=1357275556))
        asyncio.run_coroutine_threadsafe(handler(SimpleNamespace(message=message)), reader._loop).result(5)
        self.assertTrue(done.wait(5))
        self.assertEqual(got, ["7rHGiXC8TnsxN2I9"])

    def test_a_channel_known_from_an_earlier_run_is_found_by_its_id(self):
        client = FakeClient(messages=[])
        client.dialogs = [SimpleNamespace(entity=SimpleNamespace(id=1702197669, title="Offerzone Tricks 2.0", username=None,
                                                                 usernames=None))]
        reader = self.reader(client)
        reader.use_known({"uV5wcTkUWJEwM2Y1": 1702197669}, {"uV5wcTkUWJEwM2Y1": "Offerzone Tricks"})
        page, error = reader.read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(error, "")
        self.assertEqual(client.asked, [])

    def test_a_channel_the_login_has_not_joined_names_its_link(self):
        client = FakeClient(joined=False)
        page, error = self.reader(client).read("7rHGiXC8TnsxN2I9")
        self.assertIsNone(page)
        self.assertIn("open https://t.me/+7rHGiXC8TnsxN2I9 with it and join", error)

    def test_a_wait_telegram_asks_for_is_logged_with_its_cause(self):
        error = type("FloodWaitError", (Exception,), {"seconds": 3600})("A wait of 3600 seconds is required "
                                                                        "(caused by CheckChatInviteRequest)")
        with self.assertLogs("lootwatch", level="WARNING") as logs:
            self.reader(FakeClient(error=error)).read("7rHGiXC8TnsxN2I9")
        self.assertIn("wait 3600s (CheckChatInviteRequest)", logs.output[0])

    def test_an_invite_link_to_a_big_channel_is_joined_quietly(self):
        import threading

        client = InviteClient(a_channel(), messages=[])
        reader = self.reader(client)
        reader.use_known({}, {"6c3lXEpYo5U3Njc1": "Dealdost"})
        self.assertIsNone(reader.missing_private(), "not known before the chat list is looked at")
        got, done = [], threading.Event()
        reader.listen(lambda code, post: (got.append(code), done.set()), public=[])
        reader.read("7rHGiXC8TnsxN2I9")  # any reading looks at the chat list first
        self.assertEqual(reader.missing_private(), ["6c3lXEpYo5U3Njc1"])
        why, info = reader.join_invite("6c3lXEpYo5U3Njc1")
        self.assertEqual(why, "")
        self.assertIn('"Dealdost", 3,100,000 members: India\'s Most Trusted Deals Channel', info)
        self.assertIn("(joined, muted and archived)", info)
        kinds = [kind for kind, _ in client.requests]
        self.assertEqual(kinds, ["ImportChatInviteRequest", "UpdateNotifySettingsRequest", "EditPeerFoldersRequest"])
        self.assertEqual(client.requests[1][1].settings.mute_until, 2 ** 31 - 1, "muted for good")
        self.assertEqual(client.requests[2][1].folder_peers[0].folder_id, 1, "put in the archive")
        self.assertEqual((reader.missing_private(), reader.chat_id("6c3lXEpYo5U3Njc1")), ([], 900))
        (handler, _), = client.handlers
        message = SimpleNamespace(id=8, date=datetime.now(timezone.utc), message="Loot ₹1 https://amzn.to/x", entities=[],
                                  peer_id=SimpleNamespace(channel_id=900))
        asyncio.run_coroutine_threadsafe(handler(SimpleNamespace(message=message)), reader._loop).result(5)
        self.assertTrue(done.wait(5))
        self.assertEqual(got, ["6c3lXEpYo5U3Njc1"], "its posts are pushed from now on")

    def test_an_invite_link_to_a_group_chat_or_a_tiny_channel_is_left_alone(self):
        for invite, why in ((a_channel(broadcast=False), "a group chat, not a channel: left alone"),
                            (a_channel(members=300), "only 300 members: left alone")):
            client = InviteClient(invite, messages=[])
            self.assertEqual(self.reader(client).join_invite("6c3lXEpYo5U3Njc1")[0], why)
            self.assertEqual(client.requests, [], "nothing joined")

    def test_a_channel_the_account_is_in_already_is_left_as_the_owner_set_it(self):
        client = InviteClient(SimpleNamespace(chat=SimpleNamespace(id=901, title="My deals")), messages=[])
        reader = self.reader(client)
        self.assertEqual(reader.join_invite("6c3lXEpYo5U3Njc1"), ("", '"My deals" (the account is in it already)'))
        self.assertEqual(client.requests, [], "neither muted nor archived")
        self.assertEqual(reader.chat_id("6c3lXEpYo5U3Njc1"), 901)

    def test_a_join_request_or_a_dead_link_says_so(self):
        for error, why in ((type("InviteRequestSentError", (Exception,), {})("sent"),
                            "asked to join: the channel's admins have to accept the request"),
                           (type("InviteHashExpiredError", (Exception,), {})("expired"),
                            "the invite link no longer works (InviteHashExpiredError)")):
            client = InviteClient(a_channel(), import_error=error, messages=[])
            self.assertEqual(self.reader(client).join_invite("6c3lXEpYo5U3Njc1")[0], why)

    def test_while_joining_the_reading_never_looks_an_invite_link_up(self):
        client = InviteClient(a_channel(), messages=[])
        reader = self.reader(client)
        reader.use_known({}, {"6c3lXEpYo5U3Njc1": "Dealdost"})
        reader.joining = True
        page, error = reader.read("6c3lXEpYo5U3Njc1", newer_only=True)
        self.assertIn("has not joined this channel yet: the monitor joins it shortly", error)
        self.assertEqual(client.asked, [], "only the joining asks Telegram about the link")
        self.assertEqual(reader.join_invite("6c3lXEpYo5U3Njc1")[0], "")
        self.assertEqual(client.asked, ["6c3lXEpYo5U3Njc1"])
        page, error = reader.read("6c3lXEpYo5U3Njc1", newer_only=True)
        self.assertEqual(error, "")

    def test_a_link_the_account_is_not_in_is_looked_up_only_now_and_then(self):
        client = FakeClient(joined=False)
        reader = self.reader(client)
        for _ in range(3):
            page, error = reader.read("7rHGiXC8TnsxN2I9", newer_only=True)
            self.assertIn("has not joined this channel", error)
        self.assertEqual(client.asked, ["7rHGiXC8TnsxN2I9"], "the fast reading does not ask Telegram every few seconds")

    def test_without_listen_no_handler_is_added(self):
        client = FakeClient(messages=[])
        self.reader(client).read("uV5wcTkUWJEwM2Y1")
        self.assertEqual(client.handlers, [])

    def test_waits_when_telegram_asks_it_to(self):
        error = type("FloodWaitError", (Exception,), {"seconds": 120})("wait")
        client = FakeClient(error=error)
        reader = self.reader(client)
        self.assertIn("asked to wait 120s", reader.read("abcdefgh123")[1])
        self.assertGreater(reader.quiet_for(), 100)
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

    def test_the_names_channels_txt_gives_invite_only_channels(self):
        from lootwatch.config import load_private_names

        folder = tempfile.mkdtemp()
        path = Path(folder) / "channels.txt"
        path.write_text("Loot_Dealsx  # Trending (1.79M)\n! https://t.me/+7rHGiXC8TnsxN2I9 # Offerzone (3.9M)\n"
                        "! https://t.me/+uV5wcTkUWJEwM2Y1 # Offerzone Tricks (3.0M)\nt.me/+abcdefgh12345\n", encoding="utf-8")
        self.assertEqual(load_private_names(path), {"7rHGiXC8TnsxN2I9": "Offerzone", "uV5wcTkUWJEwM2Y1": "Offerzone Tricks"})

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
        self.on_post = None
        self.public = None
        self.reads = []
        self.read_codes = []
        self.titles = {"7rHGiXC8TnsxN2I9": "Offerzone"}

    def listen(self, on_post, public=()):
        self.on_post, self.public = on_post, list(public)

    def quiet_for(self):
        return 0.0

    def read(self, code, newer_only=False):
        self.reads.append(newer_only)
        self.read_codes.append(code)
        if code in self.errors:
            return None, self.errors[code]
        return self.pages.get(code, ChannelPage("empty", title="Offerzone")), ""

    def close(self):
        self.closed = True


class RememberingAccount(FakeAccount):
    def __init__(self):
        super().__init__()
        self.known = None

    def use_known(self, chats, names):
        self.known = (chats, names)

    def chat_id(self, code):
        return 1357275556


class RememberingInTheMonitor(unittest.TestCase):
    def test_the_chat_id_of_an_invite_link_is_kept_for_the_next_run(self):
        folder = tempfile.mkdtemp()
        settings = Settings(ntfy_server="http://127.0.0.1:9", ntfy_topic="t", db_path=os.path.join(folder, "s.db"),
                            startup_note=False, heartbeat_hour=-1)
        settings.private_channels = ["7rHGiXC8TnsxN2I9"]
        settings.private_names = {"7rHGiXC8TnsxN2I9": "Offerzone"}
        first = Monitor(settings, DB(settings.db_path), Fetcher("off"), Notifier(settings.ntfy_server, "t", "", ""),
                        account=RememberingAccount())
        self.assertEqual(first.account.known, ({}, {"7rHGiXC8TnsxN2I9": "Offerzone"}))
        first._scan_private("7rHGiXC8TnsxN2I9", time.time() + 30)
        first.db.close()
        second = Monitor(settings, DB(settings.db_path), Fetcher("off"), Notifier(settings.ntfy_server, "t", "", ""),
                         account=RememberingAccount())
        self.addCleanup(second.db.close)
        self.assertEqual(second.account.known[0], {"7rHGiXC8TnsxN2I9": 1357275556})


class JoiningAccount(FakeAccount):
    def __init__(self, missing, answers, invites=()):
        super().__init__()
        self.missing, self.answers, self.joined = missing, answers, []
        self.invites = list(invites)

    def missing_public(self):
        return self.missing

    def join(self, name):
        self.joined.append(name)
        return self.answers.get(name, "")

    def missing_private(self):
        return [code for code in self.invites if code not in self.joined]

    def join_invite(self, code):
        self.joined.append(code)
        why = self.answers.get(code, "")
        if not why:
            self.titles[code] = "Dealdost"
        return why, '"Dealdost", 3,100,000 members'

    def chat_id(self, code):
        return 900


class JoiningInTheMonitor(unittest.TestCase):
    def make_monitor(self, account):
        folder = tempfile.mkdtemp()
        settings = Settings(ntfy_server="http://127.0.0.1:9", ntfy_topic="t", db_path=os.path.join(folder, "s.db"),
                            startup_note=False, heartbeat_hour=-1)
        self.monitor = Monitor(settings, DB(settings.db_path), Fetcher("off"), Notifier(settings.ntfy_server, "t", "", ""))
        self.addCleanup(self.monitor.db.close)
        self.monitor.account = account
        return self.monitor

    def run_join(self, account, monitor=None):
        from unittest import mock

        from lootwatch import monitor as monitor_module

        monitor = monitor or self.make_monitor(account)
        with mock.patch.object(monitor_module, "JOIN_START_SECONDS", 0), \
                mock.patch.object(monitor_module, "JOIN_GAP_SECONDS", 0), \
                self.assertLogs("lootwatch", level="INFO") as logs:
            monitor._join_channels()
            log = __import__("logging").getLogger("lootwatch")
            log.info("(end of the joining)")
        return "\n".join(logs.output)

    def test_a_later_run_waits_as_long_as_telegram_asked(self):
        account = JoiningAccount(["a_one"], {})
        monitor = self.make_monitor(account)
        monitor.db.set("tg_join_wait_until", int(time.time()) + 600)
        self.run_join(account, monitor)
        self.assertEqual(account.joined, [])

    def test_joins_what_is_missing_and_says_so(self):
        account = JoiningAccount(["dealsmagnet", "ttsdeals"], {"ttsdeals": "UsernameNotOccupiedError: gone"})
        lines = self.run_join(account)
        self.assertEqual(account.joined, ["dealsmagnet", "ttsdeals"])
        self.assertIn("joined @dealsmagnet with the Telegram login (muted, archived)", lines)
        self.assertIn("could not join @ttsdeals", lines)

    def test_an_invite_link_is_joined_and_its_latest_posts_are_logged_for_judging(self):
        account = JoiningAccount([], {}, invites=["6c3lXEpYo5U3Njc1"])
        now = time.time()
        account.pages["6c3lXEpYo5U3Njc1"] = ChannelPage("ok", title="Dealdost", posts=[
            TgPost("c/900", 4, now - 7200, "Good morning", "Good morning"),
            TgPost("c/900", 5, now - 600, "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)",
                   "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)")])
        lines = self.run_join(account)
        self.assertEqual(account.joined, ["6c3lXEpYo5U3Njc1"])
        self.assertIn('now reading the invite-only channel https://t.me/+6c3lXEpYo5U3Njc1 through the Telegram login: '
                      '"Dealdost", 3,100,000 members', lines)
        self.assertIn("first look at Dealdost: #4 2.0h ago | -- | Good morning", lines)
        self.assertIn("first look at Dealdost: #5 10m ago | urgent | Price Error 😱 Sony WH-1000XM5", lines)
        self.assertIn("first look at Dealdost: 2 posts, the oldest 2.0h ago; the rules would push 1 urgent, 0 high, "
                      "0 normal", lines)
        self.assertEqual(self.monitor.db.get("tg_invite:6c3lXEpYo5U3Njc1"), "900", "found by its id from now on")
        self.assertEqual(self.monitor.stats["sent_urgent"], 0, "the first look pushes nothing: old news")

    def test_an_invite_link_that_could_not_be_joined_waits_a_day(self):
        why = "asked to join: the channel's admins have to accept the request"
        account = JoiningAccount([], {"6c3lXEpYo5U3Njc1": why}, invites=["6c3lXEpYo5U3Njc1"])
        lines = self.run_join(account)
        self.assertIn(f"did not join the invite-only channel https://t.me/+6c3lXEpYo5U3Njc1: {why}", lines)
        self.assertEqual(self.monitor._private, [], "not read for the rest of the run")
        account.joined.clear()
        self.run_join(account, self.monitor)
        self.assertEqual(account.joined, [], "not asked again within the day")
        # If read all the same, its failing readings say why.
        account.errors["6c3lXEpYo5U3Njc1"] = "the Telegram account has not joined this channel: open ..."
        self.assertEqual(self.monitor._scan_private("6c3lXEpYo5U3Njc1", time.time() + 30),
                         f"the Telegram login could not join it: {why}")

    def test_a_channel_about_to_be_joined_is_not_a_broken_source(self):
        account = JoiningAccount([], {}, invites=["6c3lXEpYo5U3Njc1"])
        account.errors["6c3lXEpYo5U3Njc1"] = "the Telegram account has not joined this channel: open ..."
        monitor = self.make_monitor(account)
        monitor._scan_private("6c3lXEpYo5U3Njc1", time.time() + 30)
        self.assertEqual([row for row in monitor.db.health_rows() if row[0] == "tg:+6c3lXEpYo5U3Njc1"], [])

    def test_stops_for_the_run_when_telegram_says_wait(self):
        account = JoiningAccount(["a_one", "b_two", "c_three"], {"b_two": "wait 900s"})
        lines = self.run_join(account)
        self.assertEqual(account.joined, ["a_one", "b_two"])
        self.assertIn("the rest are joined by a later run", lines)
        waited = int(self.monitor.db.get("tg_join_wait_until"))
        self.assertAlmostEqual(waited, time.time() + 900, delta=30)


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

    def test_a_post_sent_live_alerts_at_once_and_only_once(self):
        self.web.redirects["/r/1"] = "https://www.amazon.in/dp/B0SONYXM51"
        self.page(TgPost("c/1357275556", 1, time.time() - 7200, "Welcome", "Welcome"))
        monitor = self.monitor()
        monitor.run(once=True)  # the run starts listening, and reads the channel once
        self.assertIsNotNone(self.account.on_post)
        text = "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)"
        fresh = TgPost("c/1357275556", 2, time.time() - 2, text, text, links=[self.web.base + "/r/1"])
        self.account.on_post("7rHGiXC8TnsxN2I9", fresh)
        (push,) = self.web.pushes
        self.assertEqual(push["priority"], 5)
        self.assertIn("Offerzone", push["message"])
        self.page(TgPost("c/1357275556", 1, time.time() - 7200, "Welcome", "Welcome"), fresh)
        monitor.cycle()  # the scheduled reading finds the same post: it was handled already
        self.account.on_post("7rHGiXC8TnsxN2I9", fresh)  # and Telegram sending it twice changes nothing
        self.assertEqual(len(self.web.pushes), 1)

    def run_in_background(self, monitor):
        import threading

        runner = threading.Thread(target=monitor.run, daemon=True)
        runner.start()

        def stop():
            monitor._stop.set()
            runner.join(30)
            return runner

        self.addCleanup(stop)
        return stop

    def wait_for(self, condition, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline and not condition():
            time.sleep(0.02)
        return condition()

    def test_the_invite_only_channels_are_read_every_few_seconds(self):
        self.settings.tg_fast_seconds = 0.2
        self.settings.loot_channels = ["7rHGiXC8TnsxN2I9"]  # marked "!" in channels.txt
        self.web.redirects["/r/1"] = "https://www.amazon.in/dp/B0SONYXM51"
        welcome = TgPost("c/1357275556", 1, time.time() - 7200, "Welcome", "Welcome")
        self.page(welcome)
        monitor = self.monitor()
        stop = self.run_in_background(monitor)
        self.assertTrue(self.wait_for(lambda: len(self.account.reads) >= 3))
        self.assertTrue(all(self.account.reads), "only the fast reading reads them, asking for newer posts only")
        text = "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)"
        posted = time.time()
        self.page(welcome, TgPost("c/1357275556", 2, posted, text, text, links=[self.web.base + "/r/1"]))
        self.assertTrue(self.wait_for(lambda: self.web.pushes))
        self.assertLess(time.time() - posted, 5, "the alert goes out within seconds, not at the next scan")
        self.assertFalse(stop().is_alive())
        self.assertEqual(len(self.web.pushes), 1)
        self.assertTrue(self.account.closed)
        row = next(r for r in monitor.db.health_rows() if r[0] == "tg:+7rHGiXC8TnsxN2I9")
        self.assertEqual(row[1], 1, "its health is recorded once a minute, like any source's")

    def test_an_unmarked_invite_only_channel_is_asked_once_a_minute(self):
        # Telegram pushes its posts within seconds; asking every few seconds is for the marked ones.
        self.settings.tg_fast_seconds = 0.1
        self.settings.private_channels = ["7rHGiXC8TnsxN2I9", "6c3lXEpYo5U3Njc1"]
        self.settings.loot_channels = ["7rHGiXC8TnsxN2I9"]
        self.page(TgPost("c/1357275556", 1, time.time() - 7200, "Welcome", "Welcome"))
        stop = self.run_in_background(self.monitor())
        self.assertTrue(self.wait_for(lambda: self.account.read_codes.count("7rHGiXC8TnsxN2I9") >= 5))
        stop()
        self.assertEqual(self.account.read_codes.count("6c3lXEpYo5U3Njc1"), 1)

    def test_the_fast_reading_slows_down_when_telegram_asks(self):
        self.settings.tg_fast_seconds = 0.1
        self.account.errors["7rHGiXC8TnsxN2I9"] = "Telegram asked to wait 30s before asking again"
        monitor = self.monitor()
        with self.assertLogs("lootwatch", level="WARNING") as logs:
            stop = self.run_in_background(monitor)
            time.sleep(1.0)
            stop()
        self.assertTrue(any("asked the login to slow down" in line for line in logs.output), logs.output)
        self.assertLessEqual(len(self.account.reads), 5, "each refusal doubles the wait")
        row = next(r for r in monitor.db.health_rows() if r[0] == "tg:+7rHGiXC8TnsxN2I9")
        self.assertEqual(row[3], 1, "a refusal counts once a minute, not at every try")

    def test_a_post_pushed_from_a_joined_public_channel_rings_once(self):
        self.settings.channels = ["epicdeals_in"]
        self.web.redirects["/r/1"] = "https://www.amazon.in/dp/B0SONYXM51"
        monitor = self.monitor()
        monitor.db.set("boot:tg:epicdeals_in", 1)  # the channel has been read from the web before
        text = "Price Error 😱 Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)"
        monitor._live_post("@epicdeals_in", TgPost("epicdeals_in", 777, time.time() - 1, text, text,
                                                   links=[self.web.base + "/r/1"]))
        (push,) = self.web.pushes
        self.assertEqual(push["priority"], 5)
        self.assertEqual(push["actions"][1]["url"], "https://t.me/epicdeals_in/777")
        self.assertEqual(monitor._claim("tg:epicdeals_in", ["777"]), set(), "the web reading will find it handled")
        self.assertIn("pushed 1 post(s)", monitor._push_summary())

    def test_a_live_post_waits_for_the_first_reading_and_old_ones_are_dropped(self):
        monitor = self.monitor()
        listened = []
        self.account.listen = listened.append
        text = "Price Error Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)"
        monitor._live_post("7rHGiXC8TnsxN2I9", TgPost("c/1357275556", 3, time.time(), text, text))
        self.assertEqual(self.web.pushes, [], "before the channel was read once, its first reading decides")
        self.page(TgPost("c/1357275556", 1, time.time() - 7200, "Welcome", "Welcome"))
        monitor.cycle()
        monitor._live_post("7rHGiXC8TnsxN2I9", TgPost("c/1357275556", 4, time.time() - 3 * 3600, text, text))
        self.assertEqual(self.web.pushes, [], "a post hours old is not news, however it arrives")


if __name__ == "__main__":
    unittest.main()
