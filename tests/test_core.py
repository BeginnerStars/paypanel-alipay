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


    def test_login_page_uses_centering_wrapper_and_site_name(self) -> None:
        old_site_name = os.environ.get("APP_SITE_NAME")
        os.environ["APP_SITE_NAME"] = "我的收款台"
        from app.config import get_settings
        from app.main import page

        get_settings.cache_clear()
        try:
            html = page("登录", "<form></form>", logged_in=False).decode()
            self.assertIn('main class="login-page"', html)
            self.assertIn("登录 - 我的收款台", html)
            self.assertNotIn('class="container"', html)
        finally:
            if old_site_name is None:
                os.environ.pop("APP_SITE_NAME", None)
            else:
                os.environ["APP_SITE_NAME"] = old_site_name
            get_settings.cache_clear()

    def test_account_pay_type_helpers_preserve_business_specific_values(self) -> None:
        from app.main import account_form, account_values, normalize_pay_types, pay_type_labels

        self.assertEqual(normalize_pay_types("wap,page"), ("wap", "page"))
        self.assertEqual(pay_type_labels("precreate,wap"), "当面付、手机网站支付")
        form_html = account_form("/accounts")
        self.assertIn('type="checkbox" name="pay_types" value="precreate" checked', form_html)
        self.assertIn("business-wap", form_html)
        values = account_values({
            "pay_types": ["precreate", "wap"],
            "precreate_app_id": "face-app",
            "precreate_app_public_key": "face-app-public",
            "precreate_merchant_private_key": "face-private",
            "precreate_alipay_public_key": "face-alipay-public",
            "wap_app_id": "wap-app",
            "wap_return_url": "https://pay.example.com/return",
            "page_product_code": "",
            "wap_product_code": "CUSTOM_WAP",
        })
        self.assertEqual(values["pay_types"], "precreate,wap")
        self.assertEqual(values["app_id"], "face-app")
        self.assertEqual(values["precreate_app_id"], "face-app")
        self.assertEqual(values["wap_app_id"], "wap-app")
        self.assertEqual(values["wap_return_url"], "https://pay.example.com/return")
        self.assertEqual(values["page_product_code"], "FAST_INSTANT_TRADE_PAY")
        self.assertEqual(values["wap_product_code"], "CUSTOM_WAP")
        precreate = account_values({
            "pay_types": ["precreate"],
            "precreate_app_id": "face-app",
            "precreate_app_cert_sn": "APP_CERT",
            "precreate_alipay_root_cert_sn": "ROOT_CERT",
        })
        self.assertEqual(precreate["pay_types"], "precreate")
        self.assertEqual(precreate["return_url"], "")
        self.assertEqual(precreate["app_cert_sn"], "")
        self.assertEqual(precreate["alipay_root_cert_sn"], "")


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
            self.assertEqual(settings_map()["site_name"], "PayPanel Alipay")
            self.assertEqual(settings_map()["callback_base_url"], "https://notify.example.com/")
            self.assertEqual(default_notify_url(), "https://notify.example.com/alipay/notify")
            self.assertEqual(bound_panel_domain(), "pay-bound.example.com")
            self.assertEqual(panel_base_url(), "https://pay-bound.example.com")

            for key in ("APP_DATABASE_PATH", "APP_SECRET_KEY", "APP_BASE_URL", "APP_CALLBACK_BASE_URL", "APP_PANEL_DOMAIN"):
                os.environ.pop(key, None)
            get_settings.cache_clear()


class SecretStorageTests(unittest.TestCase):
    def test_account_secrets_are_encrypted_and_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APP_DATABASE_PATH"] = str(Path(tmp) / "paypanel.db")
            os.environ["APP_SECRET_KEY"] = "storage-secret"
            from app.config import get_settings
            from app.crypto import decrypt_secret, encrypt_secret
            from app.db import connect, execute, init_db, one

            get_settings.cache_clear()
            init_db()
            encrypted = encrypt_secret("plain-key")
            self.assertNotEqual(encrypted, "plain-key")
            self.assertTrue(encrypted.startswith("enc:v1:"))
            self.assertEqual(decrypt_secret(encrypted), "plain-key")

            execute(
                """
                INSERT INTO accounts(name, app_id, merchant_private_key, alipay_public_key)
                VALUES(?, ?, ?, ?)
                """,
                ("legacy", "app", "legacy-private", "legacy-public"),
            )
            init_db()
            row = one("SELECT merchant_private_key, alipay_public_key FROM accounts WHERE name = ?", ("legacy",))
            self.assertTrue(row["merchant_private_key"].startswith("enc:v1:"))
            self.assertTrue(row["alipay_public_key"].startswith("enc:v1:"))
            self.assertEqual(decrypt_secret(row["merchant_private_key"]), "legacy-private")
            self.assertEqual(decrypt_secret(row["alipay_public_key"]), "legacy-public")

            for key in ("APP_DATABASE_PATH", "APP_SECRET_KEY"):
                os.environ.pop(key, None)
            get_settings.cache_clear()


    def test_account_business_columns_are_added_to_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APP_DATABASE_PATH"] = str(Path(tmp) / "paypanel.db")
            os.environ["APP_SECRET_KEY"] = "migration-secret"
            from app.config import get_settings
            from app.db import connect, init_db, one

            get_settings.cache_clear()
            with connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE accounts (
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
                    INSERT INTO accounts(name, app_id, merchant_private_key, alipay_public_key)
                    VALUES('legacy', 'app', 'private', 'public');
                    """
                )
            init_db()
            row = one("""
                SELECT pay_types, precreate_product_code, page_product_code, wap_product_code,
                       precreate_app_id, precreate_merchant_private_key, wap_app_id, page_app_id
                FROM accounts WHERE name = ?
            """, ("legacy",))
            self.assertEqual(row["pay_types"], "precreate,page,wap")
            self.assertEqual(row["precreate_product_code"], "FACE_TO_FACE_PAYMENT")
            self.assertEqual(row["page_product_code"], "FAST_INSTANT_TRADE_PAY")
            self.assertEqual(row["wap_product_code"], "QUICK_WAP_WAY")
            self.assertEqual(row["precreate_app_id"], "app")
            self.assertEqual(row["wap_app_id"], "app")
            self.assertEqual(row["page_app_id"], "app")
            self.assertTrue(row["precreate_merchant_private_key"].startswith("enc:v1:"))

            for key in ("APP_DATABASE_PATH", "APP_SECRET_KEY"):
                os.environ.pop(key, None)
            get_settings.cache_clear()


class OrderCleanupTests(unittest.TestCase):
    def test_cleanup_orders_by_time_range_and_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APP_DATABASE_PATH"] = str(Path(tmp) / "paypanel.db")
            os.environ["APP_SECRET_KEY"] = "cleanup-secret"
            from app.config import get_settings
            from app.db import all_rows, execute, init_db
            from app.main import Handler

            get_settings.cache_clear()
            init_db()
            execute(
                "INSERT INTO orders(out_trade_no, amount, subject, pay_type, status, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("OLD", "1.00", "old", "precreate", "TRADE_CLOSED", "2026-01-01 00:00:00"),
            )
            execute(
                "INSERT INTO orders(out_trade_no, amount, subject, pay_type, status, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("NEW", "1.00", "new", "precreate", "WAIT_BUYER_PAY", "2026-02-01 00:00:00"),
            )

            class Dummy:
                form = lambda self: {"cleanup_mode": "range", "cleanup_start": "2026-01-01T00:00", "cleanup_end": "2026-01-31T23:59"}
                redirect = lambda self, location: location

            self.assertEqual(Handler.cleanup_orders(Dummy()), "/orders")
            self.assertEqual([row["out_trade_no"] for row in all_rows("SELECT out_trade_no FROM orders")], ["NEW"])

            class DummyDelete:
                redirect = lambda self, location: location

            remaining = all_rows("SELECT id FROM orders WHERE out_trade_no = 'NEW'")[0]["id"]
            self.assertEqual(Handler.delete_order(DummyDelete(), int(remaining)), "/orders")
            self.assertEqual(all_rows("SELECT out_trade_no FROM orders"), [])
            execute(
                "INSERT INTO orders(out_trade_no, amount, subject, pay_type, status, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("NEW", "1.00", "new", "precreate", "WAIT_BUYER_PAY", "2026-02-01 00:00:00"),
            )

            class DummyAll:
                form = lambda self: {"cleanup_mode": "all"}
                redirect = lambda self, location: location

            self.assertEqual(Handler.cleanup_orders(DummyAll()), "/orders")
            self.assertEqual(all_rows("SELECT out_trade_no FROM orders"), [])

            for key in ("APP_DATABASE_PATH", "APP_SECRET_KEY"):
                os.environ.pop(key, None)
            get_settings.cache_clear()

    def test_precreate_uses_key_mode_without_certificate_params(self) -> None:
        from app.alipay import AlipayAccount, common_params

        account = AlipayAccount(
            id=1,
            app_id="app",
            gateway="https://openapi.alipay.com/gateway.do",
            merchant_private_key="private",
            alipay_public_key="public",
            app_cert_sn="APP_CERT",
            alipay_root_cert_sn="ROOT_CERT",
            pay_types=("precreate",),
        )

        precreate = common_params(account, "alipay.trade.precreate", {"out_trade_no": "PP1"})
        self.assertNotIn("app_cert_sn", precreate)
        self.assertNotIn("alipay_root_cert_sn", precreate)
        query = common_params(account, "alipay.trade.query", {"out_trade_no": "PP1"})
        self.assertNotIn("app_cert_sn", query)
        self.assertNotIn("alipay_root_cert_sn", query)

        page_account = AlipayAccount(
            id=2,
            app_id="app",
            gateway="https://openapi.alipay.com/gateway.do",
            merchant_private_key="private",
            alipay_public_key="public",
            app_cert_sn="APP_CERT",
            alipay_root_cert_sn="ROOT_CERT",
            pay_types=("page",),
        )
        page = common_params(page_account, "alipay.trade.page.pay", {"out_trade_no": "PP2"})
        self.assertEqual(page["app_cert_sn"], "APP_CERT")
        self.assertEqual(page["alipay_root_cert_sn"], "ROOT_CERT")


class AlipayCryptoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for RSA2 smoke test")
    def test_sign_and_verify_round_trip(self) -> None:
        from app.alipay import response_sign_content, sign, sign_content, verify, verify_content

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

            response_value = r'{"code":"10000","msg":"Success","qr_code":"https:\/\/qr.alipay.com\/demo"}'
            response_sign = sign_content(response_value, private_key.read_text())
            payload = f'{{"alipay_trade_precreate_response":{response_value},"sign":"{response_sign}"}}'
            self.assertEqual(response_sign_content(payload, "alipay_trade_precreate_response"), response_value)
            self.assertTrue(verify_content(response_value, response_sign, public_key.read_text()))


if __name__ == "__main__":
    unittest.main()
