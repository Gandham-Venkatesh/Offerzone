"""Run this once on your own computer to let the monitor read invite-only channels.

    pip install telethon
    python tools/telegram_login.py

It asks for the api_id and api_hash of an app you create at https://my.telegram.org (API
development tools), then logs in to Telegram the usual way: phone number, the code Telegram sends
you, and your two-step password if you set one. Nothing is sent anywhere but to Telegram.

At the end it prints a long "session string". Put it, with the api_id and api_hash, into the
repository's Actions secrets as TG_SESSION, TG_API_ID and TG_API_HASH. The session string is a full
login to that Telegram account: never paste it anywhere else. If it ever leaks, end it in Telegram
(Settings -> Devices) and run this again. A separate account that only joins deal channels is the
safer choice.
"""

import sys
from pathlib import Path

try:
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
    from telethon.tl import functions
except ImportError:
    sys.exit("Telethon is not installed: run  pip install telethon  first.")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from lootwatch.config import load_private_channels
except Exception:  # run from somewhere else: the membership check is only a convenience
    load_private_channels = None


def main() -> None:
    api_id = int(input("api_id (a number, from my.telegram.org): ").strip())
    api_hash = input("api_hash (32 letters and digits): ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:  # asks for phone, code, password
        me = client.get_me()
        print(f"\nLogged in as {me.first_name or ''} {me.last_name or ''} (@{me.username or 'no username'}).")
        channels = Path(__file__).resolve().parent.parent / "channels.txt"
        if load_private_channels and channels.exists():
            for code in load_private_channels(channels):
                invite = client(functions.messages.CheckChatInviteRequest(code))
                chat = getattr(invite, "chat", None)
                title = getattr(chat, "title", None) or getattr(invite, "title", code)
                print(f"  {title}: " + ("joined, can be read" if chat else "NOT joined yet: open t.me/+" + code + " and join"))
        print("\nTG_SESSION (copy all of the next line into the GitHub secret; keep it private):\n")
        print(client.session.save())


if __name__ == "__main__":
    main()
