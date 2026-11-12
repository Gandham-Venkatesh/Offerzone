"""SQLite state: what has been seen and alerted, price history, source health, counters."""

from __future__ import annotations

import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

HISTORY_SPACING = 12 * 3600  # seconds between two stored readings of an unchanged price

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, item_id TEXT NOT NULL, seen_at INTEGER NOT NULL,
    PRIMARY KEY (source, item_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL, tier INTEGER NOT NULL, price REAL,
    title TEXT, source TEXT, payload TEXT NOT NULL,
    created_at INTEGER NOT NULL, sent_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_fp ON alerts (fingerprint, created_at);
CREATE TABLE IF NOT EXISTS sightings (
    fingerprint TEXT NOT NULL, source TEXT NOT NULL, seen_at INTEGER NOT NULL,
    PRIMARY KEY (fingerprint, source)
);
CREATE TABLE IF NOT EXISTS products (
    key TEXT PRIMARY KEY, store TEXT, title TEXT, url TEXT,
    first_seen INTEGER, last_seen INTEGER, last_price REAL, mrp REAL,
    quiet_price REAL, quiet_alerted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS price_obs (
    key TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS price_obs_key ON price_obs (key, ts);
CREATE TABLE IF NOT EXISTS health (
    source TEXT PRIMARY KEY, ok_count INTEGER NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0, last_ok INTEGER, last_fail INTEGER, last_error TEXT, last_items INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class PriceInfo:
    baseline: Optional[float]  # the product's usual price before this observation
    observations: int  # how many earlier observations the baseline rests on
    last_price: Optional[float]
    first_seen: bool
    last_age: Optional[float] = None  # seconds since it was last read; None on the first sighting
    # The price at which it has already been dealt with: reported in an alert (quiet_alerted), or
    # simply there when its page was first read and so part of what is normal. None: neither yet.
    quiet_price: Optional[float] = None
    quiet_alerted: bool = False


@dataclass
class PendingAlert:
    id: int
    payload: str
    created_at: int
    attempts: int
    tier: int


class DB:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(products)")}
            if "quiet_price" not in columns:
                # A state file from before these columns existed: everything in it was either part
                # of a baseline or already reported, so nothing in it is news.
                self._conn.execute("ALTER TABLE products ADD COLUMN quiet_price REAL")
                self._conn.execute("ALTER TABLE products ADD COLUMN quiet_alerted INTEGER NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE products SET quiet_price = last_price")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- seen items -----------------------------------------------------
    def unseen(self, source: str, item_ids: Iterable[str]) -> Set[str]:
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return set()
        with self._lock:
            marks = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT item_id FROM seen WHERE source = ? AND item_id IN ({marks})", [source] + ids
            ).fetchall()
        return set(ids) - {r[0] for r in rows}

    def mark_seen(self, source: str, item_ids: Iterable[str], now: Optional[float] = None) -> None:
        now = int(now or time.time())
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen (source, item_id, seen_at) VALUES (?, ?, ?)",
                [(source, i, now) for i in item_ids],
            )

    # ---- alerts (also the retry outbox) ---------------------------------
    def last_alert(self, fingerprint: str, since: float) -> Optional[Tuple[int, int, Optional[float], int]]:
        """(id, tier, price, created_at) of the newest alert for this deal since ``since``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tier, price, created_at FROM alerts WHERE fingerprint = ? AND created_at >= ? "
                "AND dropped = 0 ORDER BY created_at DESC LIMIT 1",
                (fingerprint, int(since)),
            ).fetchone()
        return tuple(row) if row else None

    def alerts_near_price(self, price: float, since: float) -> List[Tuple[int, int, Optional[float], int, str, str]]:
        """(id, tier, price, created_at, fingerprint, title) of the alerts since ``since`` priced within 1% of ``price``."""
        margin = max(1.0, price * 0.01)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tier, price, created_at, fingerprint, title FROM alerts WHERE created_at >= ? AND dropped = 0 "
                "AND price BETWEEN ? AND ? ORDER BY created_at DESC LIMIT 40",
                (int(since), price - margin, price + margin),
            ).fetchall()
        return [tuple(row) for row in rows]

    def add_alert(self, fingerprint: str, tier: int, price: Optional[float], title: str, source: str,
                  payload: str, now: Optional[float] = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO alerts (fingerprint, tier, price, title, source, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fingerprint, tier, price, title[:300], source, payload, int(now or time.time())),
            )
            return int(cur.lastrowid)

    def mark_sent(self, alert_id: int, now: Optional[float] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE alerts SET sent_at = ?, attempts = attempts + 1 WHERE id = ?",
                (int(now or time.time()), alert_id),
            )

    def mark_attempt(self, alert_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE alerts SET attempts = attempts + 1 WHERE id = ?", (alert_id,))

    def drop_alert(self, alert_id: int) -> str:
        """Give up on an alert; returns its fingerprint so the caller can reopen that deal."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE alerts SET dropped = 1 WHERE id = ?", (alert_id,))
            row = self._conn.execute("SELECT fingerprint FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return str(row[0]) if row else ""

    def pending_alerts(self) -> List[PendingAlert]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload, created_at, attempts, tier FROM alerts "
                "WHERE sent_at IS NULL AND dropped = 0 ORDER BY tier DESC, created_at"
            ).fetchall()
        return [PendingAlert(*r) for r in rows]

    def sent_counts(self, since: float) -> Dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) FROM alerts WHERE sent_at >= ? GROUP BY tier", (int(since),)
            ).fetchall()
        return {int(t): int(c) for t, c in rows}

    # ---- cross-channel sightings ---------------------------------------
    def add_sighting(self, fingerprint: str, source: str, window: float, now: Optional[float] = None) -> int:
        """Record that ``source`` posted this deal; return how many sources did within ``window`` seconds."""
        now = int(now or time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sightings (fingerprint, source, seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(fingerprint, source) DO UPDATE SET seen_at = excluded.seen_at",
                (fingerprint, source, now),
            )
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE fingerprint = ? AND seen_at >= ?",
                (fingerprint, now - int(window)),
            ).fetchone()
        return int(row[0])

    # ---- price history ---------------------------------------------------
    def record_price(self, key: str, store: str, title: str, url: str, price: float,
                     mrp: Optional[float], now: Optional[float] = None) -> PriceInfo:
        """Store an observation and return the product's usual price as it stood before it.

        The baseline is the median of the stored history, so one odd reading
        neither creates nor hides a drop. History is thinned to one point per
        twelve hours unless the price changes: thousands of products are followed,
        and the whole state file travels from run to run.
        """
        now = int(now or time.time())
        with self._lock, self._conn:
            product = self._conn.execute(
                "SELECT last_price, last_seen, quiet_price, quiet_alerted FROM products WHERE key = ?", (key,)
            ).fetchone()
            history = [r[0] for r in self._conn.execute(
                "SELECT price FROM price_obs WHERE key = ? ORDER BY ts DESC LIMIT 48", (key,)
            ).fetchall()]
            newest = self._conn.execute(
                "SELECT ts, price FROM price_obs WHERE key = ? ORDER BY ts DESC LIMIT 1", (key,)
            ).fetchone()
            last_price = float(product[0]) if product and product[0] is not None else None
            info = PriceInfo(
                # Readings are kept for three weeks, the product itself for six: one that comes
                # back after a month is compared with the price it was last seen at.
                baseline=statistics.median(history) if history else last_price,
                observations=len(history) or (1 if last_price is not None else 0),
                last_price=last_price,
                first_seen=product is None,
                last_age=float(now - product[1]) if product and product[1] is not None else None,
                quiet_price=float(product[2]) if product and product[2] is not None else None,
                quiet_alerted=bool(product[3]) if product else False,
            )
            if newest is None or abs(newest[1] - price) > 0.005 or now - newest[0] >= HISTORY_SPACING:
                self._conn.execute("INSERT INTO price_obs (key, ts, price) VALUES (?, ?, ?)", (key, now, price))
            self._conn.execute(
                "INSERT INTO products (key, store, title, url, first_seen, last_seen, last_price, mrp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET title = excluded.title, url = excluded.url, "
                "last_seen = excluded.last_seen, last_price = excluded.last_price, "
                "mrp = COALESCE(excluded.mrp, products.mrp)",
                (key, store, title[:300], url, now, now, price, mrp),
            )
        return info

    def mark_quiet(self, key: str, price: float, alerted: bool) -> None:
        """Remember that this product at this price has been reported (``alerted``), or was there
        when its page was first read and so is normal for it."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE products SET quiet_price = ?, quiet_alerted = ? WHERE key = ?",
                               (price, 1 if alerted else 0, key))

    def clear_quiet(self, key: str) -> None:
        """Forget that: the alert about it never reached anyone."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE products SET quiet_price = NULL, quiet_alerted = 0 WHERE key = ?", (key,))

    def product_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])

    # ---- source health ---------------------------------------------------
    def health_ok(self, source: str, items: int, now: Optional[float] = None) -> None:
        now = int(now or time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO health (source, ok_count, streak, last_ok, last_items) VALUES (?, 1, 0, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET ok_count = ok_count + 1, streak = 0, "
                "last_ok = excluded.last_ok, last_items = excluded.last_items",
                (source, now, items),
            )

    def health_fail(self, source: str, error: str, now: Optional[float] = None) -> int:
        """Record a failure; returns the number of failures in a row."""
        now = int(now or time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO health (source, fail_count, streak, last_fail, last_error) VALUES (?, 1, 1, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET fail_count = fail_count + 1, streak = streak + 1, "
                "last_fail = excluded.last_fail, last_error = excluded.last_error",
                (source, now, error[:200]),
            )
            row = self._conn.execute("SELECT streak FROM health WHERE source = ?", (source,)).fetchone()
        return int(row[0])

    def last_ok(self, source: str) -> Optional[float]:
        """When this source was last read successfully, or None if it never was."""
        with self._lock:
            row = self._conn.execute("SELECT last_ok FROM health WHERE source = ?", (source,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def health_rows(self) -> List[Tuple[str, int, int, int, Optional[int], Optional[int], Optional[str], Optional[int]]]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT source, ok_count, fail_count, streak, last_ok, last_fail, last_error, last_items "
                "FROM health ORDER BY source"
            ).fetchall())

    def forget_health(self, keep: Iterable[str]) -> None:
        """Drop health rows of sources that are no longer configured."""
        keep = set(keep)
        with self._lock, self._conn:
            for (source,) in self._conn.execute("SELECT source FROM health").fetchall():
                if source not in keep:
                    self._conn.execute("DELETE FROM health WHERE source = ?", (source,))
                    self._conn.execute("DELETE FROM kv WHERE key = ?", ("reported:" + source,))

    # ---- small key/value store -------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: object) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + excluded.value",
                (key, amount),
            )
            return int(self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()[0])

    # ---- housekeeping ----------------------------------------------------
    def prune(self, now: Optional[float] = None) -> None:
        now = int(now or time.time())
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (now - 14 * 86400,))
            # A price tracker lists thousands of drops a day, and none stays on its page for an hour.
            self._conn.execute("DELETE FROM seen WHERE seen_at < ? AND item_id LIKE 'ph%'", (now - 2 * 86400,))
            self._conn.execute("DELETE FROM sightings WHERE seen_at < ?", (now - 2 * 86400,))
            self._conn.execute("DELETE FROM alerts WHERE created_at < ?", (now - 30 * 86400,))
            self._conn.execute("DELETE FROM price_obs WHERE ts < ?", (now - 21 * 86400,))
            self._conn.execute("DELETE FROM products WHERE last_seen < ?", (now - 45 * 86400,))
            self._conn.execute("DELETE FROM kv WHERE key LIKE 'day:%' AND key < ?",
                               ("day:" + time.strftime("%Y-%m-%d", time.gmtime(now - 10 * 86400)),))
