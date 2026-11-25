"""Invite-only Telegram channels, read through a Telegram account (optional).

A channel without a public username has no web preview, so the only way to read it is as a
member: logged in as a Telegram account that has joined it. This uses Telegram's own API through
the Telethon library, with an api_id and api_hash from my.telegram.org and a login saved as a
"session string" (tools/telegram_login.py makes one). Without those three, or without Telethon,
invite links in channels.txt are simply skipped.

The monitor asks every few seconds for messages newer than the last one it saw (next to nothing
for Telegram), since Telegram's own push of a new post can lag half a minute behind on a channel
with millions of members; the push is taken as well, whichever comes first. Public channels in
channels.txt that the account has joined are pushed the same way, and the account joins those it
has not (TG_JOIN_CHANNELS, on unless set to 0): one at a time, each muted for good and archived,
so the owner's phone stays quiet. The same goes for the invite links in channels.txt, after a look
at what each one leads to: only a broadcast channel with INVITE_MIN_MEMBERS members or more is
joined, never a group chat. Nothing else is ever written. The session string is a full
login to
that account, so it is kept as a secret, and a separate account that has only joined deal
channels is the safer choice. Telegram ends a session that is used from two places at the same
moment, which is why only the scan runs use it, never the doctor or a test.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .telegram import ChannelPage, TgPost

log = logging.getLogger("lootwatch")

try:  # optional: only needed for invite-only channels
    from telethon import TelegramClient, events  # type: ignore
    from telethon.sessions import StringSession  # type: ignore
    from telethon.tl import functions, types  # type: ignore

    HAS_TELETHON = True
except Exception:  # pragma: no cover - depends on what is installed
    HAS_TELETHON = False

_INVITE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_\-]{8,64})")
# A login that can no longer work: asking again every minute would only make things worse.
_DEAD_LOGIN = ("AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked", "SessionExpired", "UserDeactivated",
               "AuthKeyInvalid", "PhoneNumberBanned")
# An invite link the account is not a member of is looked up again only after this long: the fast
# reading would otherwise ask Telegram about it every few seconds.
NOT_MEMBER_RECHECK = 600.0
# The smallest channel an invite link is joined for.
INVITE_MIN_MEMBERS = 1000


def invite_code(entry: str) -> Optional[str]:
    """The code of an invite link ("https://t.me/+uV5w..." -> "uV5w..."), or None."""
    match = _INVITE.search(entry.strip().lstrip("!").strip())
    return match.group(1) if match else None


def _utf16_slice(text: str, offset: int, length: int) -> str:
    data = text.encode("utf-16-le")  # Telegram counts positions in UTF-16 units (an emoji is two)
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", "ignore")


def _mark_struck(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """The text with crossed-out amounts marked as the old price, as the web reader does."""
    if not spans:
        return text
    data = text.encode("utf-16-le")
    out: List[str] = []
    position = 0
    for offset, length in sorted(spans):
        start, end = offset * 2, (offset + length) * 2
        if start < position:
            continue
        out.append(data[position:start].decode("utf-16-le", "ignore"))
        struck = data[start:end].decode("utf-16-le", "ignore")
        out.append(f" (MRP {struck}) " if re.search(r"\d", struck) else struck)
        position = end
    out.append(data[position:].decode("utf-16-le", "ignore"))
    return "".join(out)


def message_post(message: object, chat_id: int, channel: Optional[str] = None) -> Optional[TgPost]:
    """One channel message as a post; None for one without text (a bare picture, a service note).

    ``channel`` is a public channel's username, for links like t.me/<username>/<id>; without it
    the link is the members' kind, t.me/c/<chat id>/<id>.
    """
    text = getattr(message, "message", None) or ""
    if not text.strip():
        return None
    links: List[str] = []
    struck: List[Tuple[int, int]] = []
    for entity in getattr(message, "entities", None) or []:
        kind = type(entity).__name__
        if kind == "MessageEntityTextUrl" and getattr(entity, "url", ""):
            links.append(entity.url)
        elif kind == "MessageEntityUrl":
            links.append(_utf16_slice(text, entity.offset, entity.length))
        elif kind == "MessageEntityStrike":
            struck.append((entity.offset, entity.length))
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            if getattr(button, "url", None):
                links.append(button.url)  # "Buy Now" buttons
    preview = getattr(getattr(message, "media", None), "webpage", None)
    if getattr(preview, "url", None):
        links.append(preview.url)
    # Telegram marks every link it sees; a link written out in the text is taken all the same.
    links += [found.rstrip(".,;:!?") for found in re.findall(r"https?://[^\s<>\"'()\[\]]+", text)]
    links = [link if re.match(r"^https?://", link, re.I) else "https://" + link for link in links if link]
    date = getattr(message, "date", None)
    return TgPost(channel=channel or f"c/{chat_id}", post_id=int(getattr(message, "id")), ts=date.timestamp() if date else None,
                  text=text, analysis_text=_mark_struck(text, struck), links=list(dict.fromkeys(links)))


class AccountError(RuntimeError):
    pass


class AccountReader:
    """Reads invite-only channels through one logged-in Telegram account.

    Telethon works in asyncio; the monitor's scan works in threads. So the client lives in an event
    loop of its own, on a thread of its own, and every call waits for its answer there.
    """

    def __init__(self, api_id: str, api_hash: str, session: str, timeout: float = 25.0, client_factory=None) -> None:
        self.timeout = timeout
        self._api_id, self._api_hash, self._session = api_id, api_hash, session
        self._factory = client_factory
        self._client = None
        self._chats: Dict[str, object] = {}
        self.titles: Dict[str, str] = {}
        self._dead = ""  # why the login no longer works, once that is known
        self._quiet_until = 0.0  # Telegram asked us to wait (FloodWait)
        self._join_quiet_until = 0.0  # Telegram wants no more channels joined for a while (reading goes on)
        self._chat_codes: Dict[int, str] = {}  # channel id -> invite code, or "@username": which live posts are ours
        self._not_member: Dict[str, Tuple[float, str]] = {}  # invite code -> (look it up again after, why not)
        self.codes: List[str] = []  # every invite code of channels.txt
        # Set when the monitor joins the channels it is not in (join_invite): an invite link not in the
        # chat list is then never looked up by the reading, only by the joining, which a refusal holds
        # up without stopping the reading of the rest (Offerzone could not be read for hours on 23-24
        # Nov 2026, when every run looked its links up).
        self.joining = False
        self._newest: Dict[str, int] = {}  # invite code -> newest message id read
        self._public: Dict[str, str] = {}  # public channels to take pushed posts from: lower-case name -> name
        self.known_chats: Dict[str, int] = {}  # invite code -> channel id, kept from an earlier run
        self.names: Dict[str, str] = {}  # invite code -> the channel's name as channels.txt gives it
        self._public_checked = False
        self._on_post: Optional[Callable[[str, TgPost], None]] = None
        self._listening = False
        self._live: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._closed = False
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="telegram-account", daemon=True)
        self._thread.start()

    def _call(self, coroutine, timeout: Optional[float] = None):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout or self.timeout)

    @property
    def dead(self) -> str:
        return self._dead

    def quiet_for(self) -> float:
        """Seconds Telegram still wants the account to wait before asking again."""
        return max(0.0, self._quiet_until - time.time())

    def listen(self, on_post: Callable[[str, TgPost], None], public: Sequence[str] = ()) -> None:
        """Also hand on each new post the moment Telegram sends it, as ``on_post(code, post)``.

        For the invite-only channels read at least once (a reading tells which chat an invite
        code stands for), and for those of the ``public`` channels the account has joined (their
        ``code`` is "@username"); only while connected. The reading on a schedule goes on as
        before and catches whatever arrives in between. ``on_post`` runs on a worker thread,
        never on the event loop, since what it does with a post (following its links, pushing
        the alert) takes time.
        """
        self._public = {name.lower(): name for name in public}
        self._live = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram-live")
        self._on_post = on_post

    async def _new_message(self, event) -> None:
        """A new message in one of the account's chats: those of our channels are passed on."""
        try:
            message = getattr(event, "message", None)
            chat_id = getattr(getattr(message, "peer_id", None), "channel_id", None)
            code = self._chat_codes.get(chat_id) if chat_id is not None else None
            if code is None or self._on_post is None or self._live is None:
                return  # the account's other chats are none of our business
            post = message_post(message, chat_id, channel=code[1:] if code.startswith("@") else None)
            if post is not None:
                self._live.submit(self._hand_on, code, post)
        except Exception:  # one odd message must not stop the rest
            log.exception("could not take a live Telegram post")

    def _hand_on(self, code: str, post: TgPost) -> None:
        on_post = self._on_post
        if on_post is None:
            return
        try:
            on_post(code, post)
        except Exception:
            log.exception("could not handle the live post %s", post.url)

    async def _disconnect(self) -> None:
        # On the client's own loop: Telethon picks the loop from the thread it is called on, and
        # from any other thread it ends up working on a loop the connection does not belong to.
        await self._client.disconnect()

    async def _stop_listening(self) -> None:
        self._on_post = None  # set on the loop itself, so no message is half way through

    async def _connected(self):
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                self._client = TelegramClient(StringSession(self._session), int(self._api_id), self._api_hash,
                                              connection_retries=2, request_retries=1, flood_sleep_threshold=0)
        if not self._client.is_connected():
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise AccountError("the saved Telegram login (TG_SESSION) does not work any more: "
                                   "make a new one with tools/telegram_login.py")
        if self._on_post is not None and not self._listening:
            self._client.add_event_handler(self._new_message, events.NewMessage() if HAS_TELETHON else None)
            self._listening = True
        if not self._public_checked and ((self._public and self._on_post is not None) or self.known_chats or self.names
                                         or self.codes):
            await self._find_public()
        return self._client

    def use_known(self, chats: Dict[str, int], names: Dict[str, str]) -> None:
        """Which chat each invite link stood for in an earlier run, and the names channels.txt gives the
        channels: they are then found in the account's chat list, with no look-up of the invite link.
        Telegram allows those look-ups only so often, and refused them for hours on 23-24 Nov 2026,
        when every run asked again: Offerzone could not be read, nor its pushed posts recognised."""
        self.known_chats = {code: int(chat) for code, chat in chats.items() if chat}
        self.names = {code: name for code, name in names.items() if name}
        self.codes = list(dict.fromkeys(list(names) + list(chats)))

    def chat_id(self, code: str) -> Optional[int]:
        chat = self._chats.get(code)
        if chat is None:
            return None
        return int(getattr(chat, "id", 0) or 0) or None

    async def _find_public(self) -> None:
        """Which public channels the account has joined: Telegram pushes their posts too.

        Found in the account's own chat list, once a run: looking the names up one by one is
        something Telegram allows only a few hundred times a day.
        """
        self._public_checked = True
        joined: List[str] = []
        by_id = {chat: code for code, chat in self.known_chats.items()}
        by_name = {name.strip().lower(): code for code, name in self.names.items()}
        try:
            async for dialog in self._client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                chat = int(getattr(entity, "id", 0) or 0)
                code = by_id.get(chat)
                if code is None and getattr(entity, "username", None) is None:  # invite-only: no public name
                    code = by_name.get((getattr(entity, "title", "") or "").strip().lower())
                if code is not None and code not in self._chats:
                    self._chats[code] = entity
                    self.titles[code] = getattr(entity, "title", "") or code
                    self._chat_codes[chat] = code
                    continue
                names = [getattr(entity, "username", None)]
                names += [getattr(extra, "username", None) for extra in getattr(entity, "usernames", None) or []]
                for name in names:
                    wanted = self._public.get((name or "").lower())
                    if wanted and wanted not in joined:
                        self._chat_codes[int(getattr(entity, "id", 0))] = "@" + wanted
                        joined.append(wanted)
                        break
        except Exception as exc:  # the reading from the web goes on regardless
            log.warning("could not list the Telegram account's chats (%s): public channels are read from "
                        "the web only", type(exc).__name__)
            return
        found = [code for code in list(self.known_chats) + list(self.names) if code in self._chats]
        if self.known_chats or self.names:
            log.info("invite-only channels found in the login's chat list: %d of %d%s", len(set(found)),
                     len(set(self.known_chats) | set(self.names)),
                     "" if set(found) >= set(self.known_chats) | set(self.names) else
                     " (the others are looked up through their invite links)")
        if not self._public:
            return
        others = len(self._public) - len(joined)
        if not joined:
            log.info("the Telegram login has joined none of the %d public channels, so they are read from the web "
                     "once a minute (join them with it to get their posts within seconds)", len(self._public))
            return
        log.info("Telegram pushes the posts of %d of the %d public channels as they appear (the login has joined "
                 "them)%s", len(joined), len(self._public),
                 f"; the other {others} are read from the web once a minute" if others else "")

    def missing_public(self) -> Optional[List[str]]:
        """The public channels of channels.txt the account has not joined; None until its chat list
        has been looked at (the first reading through the account does that)."""
        if not self._public_checked:
            return None
        joined = {code[1:].lower() for code in self._chat_codes.values() if code.startswith("@")}
        return [name for key, name in self._public.items() if key not in joined]

    def missing_private(self) -> Optional[List[str]]:
        """The invite codes of channels.txt whose channels the account is not in (as far as its chat
        list tells); None until that list has been looked at."""
        if not self._public_checked:
            return None
        return [code for code in self.codes if code not in self._chats]

    def join_invite(self, code: str) -> Tuple[str, str]:
        """Join the channel an invite link leads to, muted for good and archived, once a look at the
        link shows it is a broadcast channel (not a group chat) with INVITE_MIN_MEMBERS members or
        more. Returns (why not, or "" when it is joined; what the link leads to: its title, size and
        description, for the log)."""
        with self._lock:  # one request at a time on this login
            if self._dead:
                return self._dead, ""
            if time.time() < self._join_quiet_until:
                return f"wait {int(self._join_quiet_until - time.time())}s", ""
            try:
                return self._call(self._join_invite(code))
            except Exception as exc:
                kind, seconds = type(exc).__name__, getattr(exc, "seconds", None)
                if "FloodWait" in kind and seconds:
                    self._join_quiet_until = time.time() + float(seconds) + 5
                    return f"wait {int(seconds)}s", ""
                if any(dead in kind for dead in _DEAD_LOGIN):
                    self._dead = (f"Telegram ended the saved login ({kind}): make a new TG_SESSION with "
                                  "tools/telegram_login.py")
                    log.error("%s", self._dead)
                    return self._dead, ""
                if "InviteRequestSent" in kind:
                    return "asked to join: the channel's admins have to accept the request", ""
                if "InviteHash" in kind:
                    return f"the invite link no longer works ({kind})", ""
                if "ChannelsTooMuch" in kind:
                    return "the account is in too many channels already (Telegram allows 500)", ""
                return f"{kind}: {str(exc)[:80]}", ""

    async def _join_invite(self, code: str) -> Tuple[str, str]:
        client = await self._connected()
        invite = await client(functions.messages.CheckChatInviteRequest(code))
        chat = getattr(invite, "chat", None)
        if chat is not None and not hasattr(invite, "expires"):  # ChatInviteAlready: a member already
            # One of the owner's own channels, perhaps: taken as it is, neither muted nor archived.
            info = f'"{getattr(chat, "title", "") or code}" (the account is in it already)'
            joined = False
        else:  # ChatInvite, or ChatInvitePeek (a preview for a while): not a member
            title = getattr(invite, "title", "") or getattr(chat, "title", "") or code
            members = int(getattr(invite, "participants_count", 0) or getattr(chat, "participants_count", 0) or 0)
            about = " ".join((getattr(invite, "about", "") or "").split())[:120]
            info = f'"{title}", {members:,} members' + (f": {about}" if about else "")
            if chat is None:
                channel, broadcast = getattr(invite, "channel", None), getattr(invite, "broadcast", None)
            else:
                channel, broadcast = True, getattr(chat, "broadcast", None)
            if not (channel and broadcast):
                return "a group chat, not a channel: left alone", info
            if members and members < INVITE_MIN_MEMBERS:
                return f"only {members:,} members: left alone", info
            result = await client(functions.messages.ImportChatInviteRequest(code))
            chat = next((c for c in getattr(result, "chats", None) or [] if getattr(c, "id", None)), chat)
            if chat is None:
                return "joined, but Telegram did not say which channel it is", info
            joined = True
        chat_id = int(getattr(chat, "id", 0) or 0)
        other = self._chat_codes.get(chat_id)
        if other is not None and other != code:
            return f"the same channel as another link in channels.txt ({other})", info
        self._chats[code] = chat
        self.titles[code] = getattr(chat, "title", "") or code
        self._chat_codes[chat_id] = code
        self._not_member.pop(code, None)
        if joined:
            info += " (joined, muted and archived)"
            peer = await client.get_input_entity(chat)
            await client(functions.account.UpdateNotifySettingsRequest(  # muted until 2038: for good
                peer=types.InputNotifyPeer(peer), settings=types.InputPeerNotifySettings(mute_until=2 ** 31 - 1)))
            await client(functions.folders.EditPeerFoldersRequest(  # folder 1 is the archive
                folder_peers=[types.InputFolderPeer(peer=peer, folder_id=1)]))
        return "", info

    def join(self, name: str) -> str:
        """Join a public channel with the account, muted for good and archived, so that its posts come
        pushed within seconds while the owner's phone stays quiet: "" when done, else why not ("wait
        Ns" when Telegram wants no more channels joined for a while; reading is not held up by that)."""
        with self._lock:  # one request at a time on this login
            if self._dead:
                return self._dead
            if time.time() < self._join_quiet_until:
                return f"wait {int(self._join_quiet_until - time.time())}s"
            try:
                return self._call(self._join(name))
            except Exception as exc:
                kind, seconds = type(exc).__name__, getattr(exc, "seconds", None)
                if "FloodWait" in kind and seconds:
                    self._join_quiet_until = time.time() + float(seconds) + 5
                    return f"wait {int(seconds)}s"
                if any(dead in kind for dead in _DEAD_LOGIN):
                    self._dead = (f"Telegram ended the saved login ({kind}): make a new TG_SESSION with "
                                  "tools/telegram_login.py")
                    log.error("%s", self._dead)
                    return self._dead
                return f"{kind}: {str(exc)[:80]}"

    async def _join(self, name: str) -> str:
        client = await self._connected()
        channel = await client.get_entity(name)
        await client(functions.channels.JoinChannelRequest(channel))
        peer = await client.get_input_entity(channel)
        await client(functions.account.UpdateNotifySettingsRequest(  # muted until 2038: for good
            peer=types.InputNotifyPeer(peer), settings=types.InputPeerNotifySettings(mute_until=2 ** 31 - 1)))
        await client(functions.folders.EditPeerFoldersRequest(  # folder 1 is the archive
            folder_peers=[types.InputFolderPeer(peer=peer, folder_id=1)]))
        self._chat_codes[int(getattr(channel, "id", 0))] = "@" + self._public.get(name.lower(), name)
        return ""

    async def _fetch(self, code: str, limit: int, min_id: int = 0):
        client = await self._connected()
        chat = self._chats.get(code)
        if chat is None:
            if self.joining and self._public_checked:
                raise AccountError("the Telegram account has not joined this channel yet: the monitor joins it "
                                   "shortly (TG_JOIN_CHANNELS)")
            held = self._not_member.get(code)
            if held is not None and time.time() < held[0]:
                raise AccountError(held[1])  # asked a moment ago: the answer has not changed
            invite = await client(functions.messages.CheckChatInviteRequest(code))
            chat = getattr(invite, "chat", None)
            if chat is None:  # types.ChatInvite: the link works, but this account is not a member
                why = (f"the Telegram account has not joined this channel: open https://t.me/+{code} with it and join"
                       " (or leave TG_JOIN_CHANNELS on: the monitor joins it)")
                self._not_member[code] = (time.time() + NOT_MEMBER_RECHECK, why)
                raise AccountError(why)
            self._chats[code] = chat
            self.titles[code] = getattr(chat, "title", "") or code
            self._chat_codes[int(getattr(chat, "id", 0))] = code
        if min_id:
            return chat, await client.get_messages(chat, limit=limit, min_id=min_id)
        return chat, await client.get_messages(chat, limit=limit)

    def read(self, code: str, limit: int = 20, newer_only: bool = False) -> Tuple[Optional[ChannelPage], str]:
        """(page, "") or (None, why it could not be read).

        ``newer_only``: only the messages newer than the newest one read before (none, most of the
        time: the page is then "ok" with no posts).
        """
        with self._lock:  # one request at a time on this login
            if self._dead:
                return None, self._dead
            if time.time() < self._quiet_until:
                return None, f"Telegram asked to wait {int(self._quiet_until - time.time())}s more"
            try:
                min_id = self._newest.get(code, 0) if newer_only else 0
                chat, messages = self._call(self._fetch(code, limit, min_id))
            except AccountError as exc:
                if "login" in str(exc):
                    self._dead = str(exc)
                return None, str(exc)
            except Exception as exc:  # network trouble, or Telegram refusing
                name = type(exc).__name__
                seconds = getattr(exc, "seconds", None)
                if "FloodWait" in name and seconds:
                    self._quiet_until = time.time() + float(seconds) + 5
                    caused = re.search(r"caused by (\w+)", str(exc))
                    log.warning("Telegram asked the login to wait %ds (%s); pushed posts are still taken meanwhile",
                                int(seconds), caused.group(1) if caused else name)
                    return None, f"Telegram asked to wait {int(seconds)}s before asking again"
                if any(dead in name for dead in _DEAD_LOGIN):
                    self._dead = (f"Telegram ended the saved login ({name}): make a new TG_SESSION with "
                                  "tools/telegram_login.py")
                    log.error("%s", self._dead)
                    return None, self._dead
                if "InviteHash" in name:
                    return None, f"the invite link no longer works ({name}): ask the channel for a new one"
                return None, f"could not read the channel through the Telegram account ({name}: {str(exc)[:80]})"
        chat_id = int(getattr(chat, "id", 0))
        newest = max((int(getattr(m, "id", 0) or 0) for m in messages or []), default=0)
        if newest > self._newest.get(code, 0):
            self._newest[code] = newest
        posts = [post for post in (message_post(m, chat_id) for m in messages or []) if post]
        posts.sort(key=lambda post: post.post_id)
        status = "ok" if posts or min_id else "empty"
        return ChannelPage(status, title=self.titles.get(code, code), posts=posts), ""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._live is not None:
            try:
                self._call(self._stop_listening(), 5)
            except Exception:
                self._on_post = None
            self._live.shutdown(wait=True)  # posts already taken are seen through
        try:
            if self._client is not None and self._client.is_connected():
                self._call(self._disconnect(), 10)
        except Exception:  # the run is ending anyway
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()
