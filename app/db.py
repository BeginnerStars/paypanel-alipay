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
    pay_types TEXT NOT NULL DEFAULT 'precreate,page,wap',
    precreate_product_code TEXT DEFAULT 'FACE_TO_FACE_PAYMENT',
    page_product_code TEXT DEFAULT 'FAST_INSTANT_TRADE_PAY',
    wap_product_code TEXT DEFAULT 'QUICK_WAP_WAY',
    precreate_app_id TEXT DEFAULT '',
    precreate_app_public_key TEXT DEFAULT '',
    precreate_merchant_private_key TEXT DEFAULT '',
    precreate_alipay_public_key TEXT DEFAULT '',
    precreate_gateway TEXT DEFAULT 'https://openapi.alipay.com/gateway.do',
    precreate_notify_url TEXT DEFAULT '',
    wap_app_id TEXT DEFAULT '',
    wap_app_public_key TEXT DEFAULT '',
    wap_merchant_private_key TEXT DEFAULT '',
    wap_alipay_public_key TEXT DEFAULT '',
    wap_gateway TEXT DEFAULT 'https://openapi.alipay.com/gateway.do',
    wap_notify_url TEXT DEFAULT '',
    wap_return_url TEXT DEFAULT '',
    wap_app_cert_sn TEXT DEFAULT '',
    wap_alipay_root_cert_sn TEXT DEFAULT '',
    page_app_id TEXT DEFAULT '',
    page_app_public_key TEXT DEFAULT '',
    page_merchant_private_key TEXT DEFAULT '',
    page_alipay_public_key TEXT DEFAULT '',
    page_gateway TEXT DEFAULT 'https://openapi.alipay.com/gateway.do',
    page_notify_url TEXT DEFAULT '',
    page_return_url TEXT DEFAULT '',
    page_app_cert_sn TEXT DEFAULT '',
    page_alipay_root_cert_sn TEXT DEFAULT '',
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
    "site_name": "PayPanel Alipay",
    "panel_domain": "",
    "enforce_panel_domain": "0",
    "callback_base_url": "",
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
        "site_name": runtime_settings.site_name,
        "panel_domain": runtime_settings.panel_domain,
        "enforce_panel_domain": runtime_settings.enforce_panel_domain,
        "callback_base_url": runtime_settings.callback_base_url,
    }
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in {**DEFAULT_SETTINGS, **env_defaults}.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        ensure_columns(
            conn,
            "accounts",
            {
                "pay_types": "TEXT NOT NULL DEFAULT 'precreate,page,wap'",
                "precreate_product_code": "TEXT DEFAULT 'FACE_TO_FACE_PAYMENT'",
                "page_product_code": "TEXT DEFAULT 'FAST_INSTANT_TRADE_PAY'",
                "wap_product_code": "TEXT DEFAULT 'QUICK_WAP_WAY'",
                **business_columns(),
            },
        )
        migrate_account_business_columns(conn)
        secret_columns = ["merchant_private_key", "alipay_public_key"]
        for business in ("precreate", "wap", "page"):
            secret_columns.extend([f"{business}_merchant_private_key", f"{business}_alipay_public_key"])
        rows = conn.execute("SELECT * FROM accounts").fetchall()
        for row in rows:
            updates = {column: encrypt_secret(row[column]) for column in secret_columns if column in row.keys()}
            conn.execute(
                "UPDATE accounts SET " + ", ".join(f"{column} = ?" for column in updates) + " WHERE id = ?",
                (*updates.values(), row["id"]),
            )


def business_columns() -> dict[str, str]:
    columns: dict[str, str] = {}
    for business in ("precreate", "wap", "page"):
        columns[f"{business}_app_id"] = "TEXT DEFAULT ''"
        columns[f"{business}_app_public_key"] = "TEXT DEFAULT ''"
        columns[f"{business}_merchant_private_key"] = "TEXT DEFAULT ''"
        columns[f"{business}_alipay_public_key"] = "TEXT DEFAULT ''"
        columns[f"{business}_gateway"] = "TEXT DEFAULT 'https://openapi.alipay.com/gateway.do'"
        columns[f"{business}_notify_url"] = "TEXT DEFAULT ''"
    for business in ("wap", "page"):
        columns[f"{business}_return_url"] = "TEXT DEFAULT ''"
        columns[f"{business}_app_cert_sn"] = "TEXT DEFAULT ''"
        columns[f"{business}_alipay_root_cert_sn"] = "TEXT DEFAULT ''"
    return columns


def migrate_account_business_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM accounts").fetchall()
    for row in rows:
        pay_types = [item.strip() for item in (row["pay_types"] or "precreate,page,wap").split(",") if item.strip()]
        updates = {}
        for business in pay_types:
            if business not in {"precreate", "wap", "page"}:
                continue
            if not row[f"{business}_app_id"]:
                updates[f"{business}_app_id"] = row["app_id"]
            if not row[f"{business}_merchant_private_key"]:
                updates[f"{business}_merchant_private_key"] = row["merchant_private_key"]
            if not row[f"{business}_alipay_public_key"]:
                updates[f"{business}_alipay_public_key"] = row["alipay_public_key"]
            if not row[f"{business}_gateway"]:
                updates[f"{business}_gateway"] = row["gateway"]
            if not row[f"{business}_notify_url"]:
                updates[f"{business}_notify_url"] = row["notify_url"]
            if business in {"wap", "page"}:
                if not row[f"{business}_return_url"]:
                    updates[f"{business}_return_url"] = row["return_url"]
                if not row[f"{business}_app_cert_sn"]:
                    updates[f"{business}_app_cert_sn"] = row["app_cert_sn"]
                if not row[f"{business}_alipay_root_cert_sn"]:
                    updates[f"{business}_alipay_root_cert_sn"] = row["alipay_root_cert_sn"]
        if updates:
            conn.execute(
                "UPDATE accounts SET " + ", ".join(f"{column} = ?" for column in updates) + " WHERE id = ?",
                (*updates.values(), row["id"]),
            )


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
