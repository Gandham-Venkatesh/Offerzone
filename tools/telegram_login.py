"""Run this once on your own computer to let the monitor read invite-only channels.

    pip install telethon
    python tools/telegram_login.py

It asks for the api_id and api_hash of an app you create at https://my.telegram.org (API
development tools), then logs in to Telegram the usual way: phone number, the code Telegram sends
you, and your two-step password if you set one. Nothing is sent anywhere but to Telegram.

It prints a long "session string". Put it, with the api_id and api_hash, into the repository's
Actions secrets as TG_SESSION, TG_API_ID and TG_API_HASH. The session string is a full login to
that Telegram account: never paste it anywhere else. If it ever leaks, end it in Telegram
(Settings -> Devices) and run this again. A separate account that only joins deal channels is the
safer choice.
"""

import re
import sys
from pathlib import Path

try:
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
    from telethon.tl import functions
except ImportError:
    sys.exit("Telethon is not installed: run  pip install telethon  first.")

INVITE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_\-]{8,64})")
# The invite-only channels of the shipped channels.txt, for when this file is run on its own.
SHIPPED_INVITES = ("7rHGiXC8TnsxN2I9", "uV5wcTkUWJEwM2Y1")


def invite_codes() -> list:
    here = Path(__file__).resolve().parent
    for folder in (here, here.parent, Path.cwd()):
        channels = folder / "channels.txt"
        if channels.exists():
            return list(dict.fromkeys(INVITE.findall(channels.read_text(encoding="utf-8"))))
    return list(SHIPPED_INVITES)


def to_clipboard(text: str) -> bool:
    """Put the text on the clipboard where that is easy (Windows: clip), so a long line cannot be cut."""
    import subprocess

    for command in (["clip"], ["pbcopy"]):
        try:
            subprocess.run(command, input=text, text=True, check=True, timeout=10)
            return True
        except Exception:
            continue
    return False


def main() -> None:
    api_id = int(input("api_id (a number, from my.telegram.org): ").strip())
    api_hash = input("api_hash (32 letters and digits): ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:  # asks for phone, code, password
        me = client.get_me()
        print(f"\nLogged in as {me.first_name or ''} {me.last_name or ''} (@{me.username or 'no username'}).")
        session = client.session.save()
        print("\nTG_SESSION (copy ALL of the next line into the GitHub secret; keep it private):\n")
        print(session)
        if to_clipboard(session):
            print("\n(It is on your clipboard as well: in GitHub, just paste it with Ctrl+V.)")
        print()
        for code in invite_codes():
            try:
                invite = client(functions.messages.CheckChatInviteRequest(code))
            except Exception as exc:  # an expired link, for instance
                print(f"  t.me/+{code}: could not check ({type(exc).__name__})")
                continue
            chat = getattr(invite, "chat", None)
            title = getattr(chat, "title", None) or getattr(invite, "title", code)
            print(f"  {title}: " + ("joined, the monitor can read it" if chat
                                    else f"NOT joined yet: open https://t.me/+{code} with this account and join"))


if __name__ == "__main__":
    main()
