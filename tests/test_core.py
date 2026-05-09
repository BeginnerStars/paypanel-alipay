from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


class AuthTests(unittest.TestCase):
    def test_invalid_totp_secret_is_rejected_without_crashing(self) -> None:
        from app.auth import verify_totp

        self.assertFalse(verify_totp("not-a-valid-secret", "123456"))


class MainHelperTests(unittest.TestCase):
    def test_bounded_int_clamps_and_defaults(self) -> None:
        from app.main import bounded_int

        self.assertEqual(bounded_int("abc", 8, 3), 8)
        self.assertEqual(bounded_int("1", 8, 3), 3)
        self.assertEqual(bounded_int("999999", 8, 3, 60), 60)

    def test_domain_and_url_helpers_normalize_values(self) -> None:
        from app.main import host_name, normalize_base_url

        self.assertEqual(normalize_base_url("notify.example.com/"), "https://notify.example.com")
        self.assertEqual(normalize_base_url("http://pay.example.com/"), "http://pay.example.com")
        self.assertEqual(host_name("Pay.Example.Com:8443"), "pay.example.com")
        self.assertEqual(host_name("https://Pay.Example.Com/path"), "pay.example.com")

    def test_session_cookie_adds_secure_for_https_base_url(self) -> None:
        from app.config import get_settings
        from app.main import session_cookie

        old_base_url = os.environ.get("APP_BASE_URL")
        os.environ["APP_BASE_URL"] = "https://pay.example.com"
        get_settings.cache_clear()
        try:
            cookie = session_cookie("sid", "value")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("Secure", cookie)
        finally:
            if old_base_url is None:
                os.environ.pop("APP_BASE_URL", None)
            else:
                os.environ["APP_BASE_URL"] = old_base_url
            get_settings.cache_clear()


class TimeoutTests(unittest.TestCase):
    def test_expire_timeout_orders_closes_stale_pending_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APP_DATABASE_PATH"] = str(Path(tmp) / "paypanel.db")
            os.environ["APP_SECRET_KEY"] = "test-secret"
            from app.config import get_settings
            from app.db import execute, init_db, one
            from app.main import expire_timeout_orders

            get_settings.cache_clear()
            init_db()
            execute(
                """
                INSERT INTO settings(key, value) VALUES('order_timeout_minutes', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            created_at = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
            order_id = execute(
                """
                INSERT INTO orders(out_trade_no, amount, subject, pay_type, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                ("PP_TIMEOUT", "1.00", "timeout", "precreate", "WAIT_BUYER_PAY", created_at),
            )

            self.assertEqual(expire_timeout_orders(), 1)
            row = one("SELECT status, last_error FROM orders WHERE id = ?", (order_id,))
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "TRADE_CLOSED")
            self.assertEqual(row["last_error"], "订单超时自动关闭")


class DomainSettingsTests(unittest.TestCase):
    def test_callback_base_url_overrides_default_notify_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APP_DATABASE_PATH"] = str(Path(tmp) / "paypanel.db")
            os.environ["APP_SECRET_KEY"] = "test-secret"
            os.environ["APP_BASE_URL"] = "https://pay.example.com"
            os.environ["APP_CALLBACK_BASE_URL"] = "https://notify.example.com/"
            os.environ["APP_PANEL_DOMAIN"] = "pay-bound.example.com"

            from app.config import get_settings
            from app.db import init_db, settings_map
            from app.main import bound_panel_domain, default_notify_url, panel_base_url

            get_settings.cache_clear()
            init_db()
            self.assertEqual(settings_map()["callback_base_url"], "https://notify.example.com/")
            self.assertEqual(default_notify_url(), "https://notify.example.com/alipay/notify")
            self.assertEqual(bound_panel_domain(), "pay-bound.example.com")
            self.assertEqual(panel_base_url(), "https://pay-bound.example.com")

            for key in ("APP_DATABASE_PATH", "APP_SECRET_KEY", "APP_BASE_URL", "APP_CALLBACK_BASE_URL", "APP_PANEL_DOMAIN"):
                os.environ.pop(key, None)
            get_settings.cache_clear()


class AlipayCryptoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for RSA2 smoke test")
    def test_sign_and_verify_round_trip(self) -> None:
        from app.alipay import sign, verify

        with tempfile.TemporaryDirectory() as tmp:
            private_key = Path(tmp) / "private.pem"
            public_key = Path(tmp) / "public.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            params = {
                "app_id": "2021000000000000",
                "method": "alipay.trade.query",
                "charset": "utf-8",
                "sign_type": "RSA2",
                "timestamp": "2026-05-09 00:00:00",
                "version": "1.0",
                "biz_content": '{"out_trade_no":"PP1"}',
            }
            params["sign"] = sign(params, private_key.read_text())

            self.assertTrue(verify(params, public_key.read_text()))
            params["biz_content"] = '{"out_trade_no":"PP2"}'
            self.assertFalse(verify(params, public_key.read_text()))


if __name__ == "__main__":
    unittest.main()
