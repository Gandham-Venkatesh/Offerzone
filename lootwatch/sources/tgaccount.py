"""Invite-only Telegram channels, read through a Telegram account (optional).

A channel without a public username has no web preview, so the only way to read it is as a
member: logged in as a Telegram account that has joined it. This uses Telegram's own API through
the Telethon library, with an api_id and api_hash from my.telegram.org and a login saved as a
"session string" (tools/telegram_login.py makes one). Without those three, or without Telethon,
invite links in channels.txt are simply skipped.

Only reading is done: one request per channel per scan. The session string is a full login to
that account, so it is kept as a secret, and a separate account that has only joined deal
channels is the safer choice. Telegram ends a session that is used from two places at the same
moment, which is why only the scan runs use it, never the doctor or a test.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .telegram import ChannelPage, TgPost

log = logging.getLogger("lootwatch")

try:  # optional: only needed for invite-only channels
    from telethon import TelegramClient  # type: ignore
    from telethon.sessions import StringSession  # type: ignore
    from telethon.tl import functions, types  # type: ignore

    HAS_TELETHON = True
except Exception:  # pragma: no cover - depends on what is installed
    HAS_TELETHON = False

_INVITE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_\-]{8,64})")
# A login that can no longer work: asking again every minute would only make things worse.
_DEAD_LOGIN = ("AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked", "SessionExpired", "UserDeactivated",
               "AuthKeyInvalid", "PhoneNumberBanned")


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


def message_post(message: object, chat_id: int) -> Optional[TgPost]:
    """One channel message as a post; None for one without text (a bare picture, a service note)."""
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
    links = [link if re.match(r"^https?://", link, re.I) else "https://" + link for link in links if link]
    date = getattr(message, "date", None)
    return TgPost(channel=f"c/{chat_id}", post_id=int(getattr(message, "id")), ts=date.timestamp() if date else None,
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
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="telegram-account", daemon=True)
        self._thread.start()

    def _call(self, coroutine, timeout: Optional[float] = None):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout or self.timeout)

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
        return self._client

    async def _fetch(self, code: str, limit: int):
        client = await self._connected()
        chat = self._chats.get(code)
        if chat is None:
            invite = await client(functions.messages.CheckChatInviteRequest(code))
            chat = getattr(invite, "chat", None)
            if chat is None:  # types.ChatInvite: the link works, but this account is not a member
                raise AccountError("the Telegram account has not joined this channel: open the invite link with it and join")
            self._chats[code] = chat
            self.titles[code] = getattr(chat, "title", "") or code
        return chat, await client.get_messages(chat, limit=limit)

    def read(self, code: str, limit: int = 20) -> Tuple[Optional[ChannelPage], str]:
        """(page, "") or (None, why it could not be read)."""
        with self._lock:  # one request at a time on this login
            if self._dead:
                return None, self._dead
            if time.time() < self._quiet_until:
                return None, f"Telegram asked to wait {int(self._quiet_until - time.time())}s more"
            try:
                chat, messages = self._call(self._fetch(code, limit))
            except AccountError as exc:
                if "login" in str(exc):
                    self._dead = str(exc)
                return None, str(exc)
            except Exception as exc:  # network trouble, or Telegram refusing
                name = type(exc).__name__
                seconds = getattr(exc, "seconds", None)
                if "FloodWait" in name and seconds:
                    self._quiet_until = time.time() + float(seconds) + 5
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
        posts = [post for post in (message_post(m, chat_id) for m in messages or []) if post]
        posts.sort(key=lambda post: post.post_id)
        return ChannelPage("ok" if posts else "empty", title=self.titles.get(code, code), posts=posts), ""

    def close(self) -> None:
        try:
            if self._client is not None and self._client.is_connected():
                self._call(self._client.disconnect(), 10)
        except Exception:  # the run is ending anyway
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
