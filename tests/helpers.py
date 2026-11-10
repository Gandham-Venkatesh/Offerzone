"""A tiny local web server that stands in for Telegram previews, short links, store pages and ntfy.

The Telegram page template mirrors the structure of https://t.me/s/<channel>
(message wrapper with data-post, js-message_text body, <time datetime> in the
footer). It is hand-built for tests; `python -m lootwatch --doctor` is what
checks the parser against the live site.
"""

from __future__ import annotations

import collections
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>{name} – Telegram</title></head>
<body class="widget_frame_base tgme_webpreview"><div class="tgme_page_wrap">
<main class="tgme_main"><section class="tgme_channel_history js-message_history">
<div class="tgme_channel_info"><div class="tgme_channel_info_header">
<div class="tgme_channel_info_header_title"><span dir="auto">{name} Deals</span></div></div></div>
{messages}
</section></main></div></body></html>"""

_MESSAGE = """<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{name}/{id}" data-view="x">
<div class="tgme_widget_message_bubble">
<div class="tgme_widget_message_author accent_color"><a class="tgme_widget_message_owner_name" href="https://t.me/{name}"><span dir="auto">{name}</span></a></div>
<div class="tgme_widget_message_text js-message_text" dir="auto">{html}</div>
{buttons}
<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short js-message_info">
<span class="tgme_widget_message_views">1.2K</span><span class="copyonly"> views</span>
<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/{name}/{id}"><time datetime="{iso}" class="time">10:20</time></a></span>
</div></div></div></div></div>"""

_LANDING = """<!DOCTYPE html><html><head><title>Telegram: Contact @{name}</title></head>
<body><div class="tgme_page"><div class="tgme_page_title">{name}</div>
<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve?domain={name}">View in Telegram</a></div>
</div></body></html>"""


def channel_html(name: str, posts: List[dict]) -> str:
    messages = []
    for post in posts:
        iso = datetime.fromtimestamp(post["ts"], tz=timezone.utc).isoformat(timespec="seconds")
        buttons = "".join(
            f'<div class="tgme_widget_message_inline_keyboard"><div class="tgme_widget_message_inline_row">'
            f'<a class="tgme_widget_message_inline_button url_button" href="{url}"><span>Buy Now</span></a></div></div>'
            for url in post.get("buttons", [])
        )
        messages.append(_MESSAGE.format(name=name, id=post["id"], html=post["html"], iso=iso, buttons=buttons))
    return _PAGE.format(name=name, messages="\n".join(messages))


class FakeWeb:
    def __init__(self) -> None:
        self.channels: Dict[str, List[dict]] = {}
        self.no_preview: set = set()
        self.redirects: Dict[str, str] = {}  # "/r/abc" -> target URL
        self.pages: Dict[str, str] = {}  # path -> body
        self.status_for: Dict[str, int] = {}  # path -> forced HTTP status
        self.pushes: List[dict] = []
        self.push_headers: List[dict] = []
        self.ntfy_status = 200
        self.ntfy_retry_after: Optional[str] = None
        self.hits: "collections.Counter[str]" = collections.Counter()
        self._server: Optional[ThreadingHTTPServer] = None
        self.port = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeWeb":
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8",
                      headers: Optional[dict] = None) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                web.hits[path] += 1
                if path in web.status_for:
                    return self._send(web.status_for[path], "refused")
                if path in web.redirects:
                    return self._send(302, headers={"Location": web.redirects[path]})
                if path.startswith("/s/"):
                    name = path[3:]
                    if name in web.no_preview or name not in web.channels:
                        return self._send(302, headers={"Location": f"/{name}"})
                    return self._send(200, channel_html(name, web.channels[name]))
                if path in web.pages:
                    kind = "application/xml" if web.pages[path].lstrip().startswith("<?xml") else "text/html; charset=utf-8"
                    return self._send(200, web.pages[path], kind)
                if path.count("/") == 1 and len(path) > 1:
                    return self._send(200, _LANDING.format(name=path[1:]))
                self._send(404, "not found")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                web.hits["POST " + self.path] += 1
                if web.ntfy_status != 200:
                    extra = {"Retry-After": web.ntfy_retry_after} if web.ntfy_retry_after else {}
                    return self._send(web.ntfy_status, '{"error":"nope"}', "application/json", extra)
                web.pushes.append(json.loads(raw.decode("utf-8")))
                web.push_headers.append({k.lower(): v for k, v in self.headers.items()})
                self._send(200, '{"id":"x"}', "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def post(self, channel: str, post_id: int, html: str, ts: float, buttons: Optional[List[str]] = None) -> None:
        self.channels.setdefault(channel, []).append({"id": post_id, "html": html, "ts": ts, "buttons": buttons or []})
