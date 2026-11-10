"""ntfy push notifications (published as JSON, so ₹ and emoji survive in titles)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

import requests

log = logging.getLogger("lootwatch")

MAX_TITLE = 180
MAX_MESSAGE_BYTES = 3000  # ntfy turns bodies over 4096 bytes into attachments


def _clip_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", "ignore") + "..."


@dataclass
class SendResult:
    ok: bool
    status: int = 0
    retry_after: float = 0.0
    error: str = ""


def build_payload(title: str, message: str, priority: int = 3, tags: Sequence[str] = (), click: str = "",
                  actions: Sequence[Dict[str, str]] = ()) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "title": title.replace("\n", " ")[:MAX_TITLE],
        "message": _clip_bytes(message, MAX_MESSAGE_BYTES),
        "priority": max(1, min(5, int(priority))),
    }
    if tags:
        payload["tags"] = list(tags)[:5]
    if click.startswith(("http://", "https://")):
        payload["click"] = click
    views = [a for a in actions if a.get("url", "").startswith(("http://", "https://"))][:3]
    if views:
        payload["actions"] = [{"action": "view", "label": a["label"][:40], "url": a["url"]} for a in views]
    return payload


class Notifier:
    def __init__(self, server: str, topic: str, token: str = "", health_topic: str = "", dry_run: bool = False,
                 timeout: float = 10.0) -> None:
        self.server = server.rstrip("/")
        self.topic = topic.strip()
        self.token = token.strip()
        self.health_topic = health_topic.strip() or self.topic
        self.dry_run = dry_run
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    def send(self, payload: Dict[str, object], topic: Optional[str] = None) -> SendResult:
        """Publish one notification. Retries brief network/5xx trouble; a 429 is reported back."""
        body = dict(payload)
        body["topic"] = topic or self.topic
        if self.dry_run or not body["topic"]:
            log.info("[%s] would push (priority %s): %s | %s", "dry-run" if self.dry_run else "no ntfy topic set",
                     body.get("priority"), body.get("title"), str(body.get("message", "")).replace("\n", " / ")[:200])
            return SendResult(ok=self.dry_run, error="" if self.dry_run else "NTFY_TOPIC is not set")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = SendResult(ok=False, error="not attempted")
        for attempt in range(3):
            try:
                resp = self._session.post(self.server + "/", data=data, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last = SendResult(ok=False, error=type(exc).__name__)
            else:
                if resp.status_code == 200:
                    return SendResult(ok=True, status=200)
                retry_after = 0.0
                value = resp.headers.get("Retry-After", "")
                if value.strip().isdigit():
                    retry_after = float(value)
                last = SendResult(ok=False, status=resp.status_code, retry_after=retry_after,
                                  error=f"HTTP {resp.status_code}: {resp.text[:120]}")
                if resp.status_code < 500:
                    break  # 4xx (bad token, rate limit, ...) will not improve by retrying right now
            if attempt < 2:
                time.sleep(1.0 + 2.0 * attempt)
        log.error("ntfy publish failed: %s", last.error)
        return last

    def send_status(self, title: str, message: str, priority: int = 2, tags: Sequence[str] = ("information_source",)) -> SendResult:
        return self.send(build_payload(title, message, priority, tags), topic=self.health_topic)
