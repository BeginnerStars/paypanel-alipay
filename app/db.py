from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings
from .crypto import encrypt_secret


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    app_id TEXT NOT NULL,
    gateway TEXT NOT NULL DEFAULT 'https://openapi.alipay.com/gateway.do',
    merchant_private_key TEXT NOT NULL,
    alipay_public_key TEXT NOT NULL,
    app_cert_sn TEXT DEFAULT '',
    alipay_root_cert_sn TEXT DEFAULT '',
    notify_url TEXT DEFAULT '',
    return_url TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    out_trade_no TEXT NOT NULL UNIQUE,
    account_id INTEGER,
    amount TEXT NOT NULL,
    subject TEXT NOT NULL,
    pay_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    qr_code TEXT DEFAULT '',
    pay_url TEXT DEFAULT '',
    trade_no TEXT DEFAULT '',
    buyer_logon_id TEXT DEFAULT '',
    raw_response TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    poll_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TEXT DEFAULT '',
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
"""

DEFAULT_SETTINGS = {
    "enable_account_rotation": "1",
    "enable_polling": "1",
    "poll_interval_seconds": "8",
    "poll_timeout_minutes": "30",
    "order_timeout_minutes": "30",
    "enable_2fa": "0",
    "totp_secret": "",
    "panel_domain": "",
    "enforce_panel_domain": "0",
    "callback_base_url": "",
    "ssl_enabled": "0",
    "ssl_certfile": "",
    "ssl_keyfile": "",
}


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    runtime_settings = get_settings()
    env_defaults = {
        "panel_domain": runtime_settings.panel_domain,
        "enforce_panel_domain": runtime_settings.enforce_panel_domain,
        "callback_base_url": runtime_settings.callback_base_url,
        "ssl_enabled": runtime_settings.ssl_enabled,
        "ssl_certfile": runtime_settings.ssl_certfile,
        "ssl_keyfile": runtime_settings.ssl_keyfile,
    }
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in {**DEFAULT_SETTINGS, **env_defaults}.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        rows = conn.execute("SELECT id, merchant_private_key, alipay_public_key FROM accounts").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE accounts SET merchant_private_key = ?, alipay_public_key = ? WHERE id = ?",
                (encrypt_secret(row["merchant_private_key"]), encrypt_secret(row["alipay_public_key"]), row["id"]),
            )


def one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(query, params).fetchone()


def all_rows(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(query, params).fetchall()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(query, params)
        return int(cur.lastrowid)


def settings_map() -> dict[str, str]:
    rows = all_rows("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in rows}
