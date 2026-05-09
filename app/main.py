from __future__ import annotations

import html
import json
import mimetypes
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import alipay
from .alipay import AlipayAccount
from .auth import (
    SESSION_COOKIE,
    make_session,
    provisioning_uri,
    random_totp_secret,
    read_session_cookie,
    verify_credentials,
)
from .config import get_settings
from .db import all_rows, connect, execute, init_db, one, settings_map


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"


def row_to_account(row: Any) -> AlipayAccount:
    return AlipayAccount(
        id=row["id"],
        app_id=row["app_id"],
        gateway=row["gateway"],
        merchant_private_key=row["merchant_private_key"],
        alipay_public_key=row["alipay_public_key"],
        app_cert_sn=row["app_cert_sn"] or "",
        alipay_root_cert_sn=row["alipay_root_cert_sn"] or "",
        notify_url=row["notify_url"] or default_notify_url(),
        return_url=row["return_url"] or get_settings().base_url,
    )


def default_notify_url() -> str:
    return f"{get_settings().base_url}/alipay/notify"


def require_amount(amount: str) -> str:
    try:
        value = Decimal(amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式不正确")
    if value <= 0:
        raise ValueError("金额必须大于 0")
    return str(value)


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def qr_img_src(text: str) -> str:
    return "https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=" + urllib.parse.quote(text)


def page(title: str, body: str, logged_in: bool = True) -> bytes:
    nav = ""
    if logged_in:
        nav = """
        <header class="topbar"><a class="brand" href="/">PayPanel Alipay</a><nav>
          <a href="/orders/new">发起收款</a><a href="/orders">订单</a><a href="/accounts">账户</a><a href="/settings">设置</a>
          <form action="/logout" method="post"><button>退出</button></form>
        </nav></header>
        """
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
    <link rel="stylesheet" href="/static/style.css"></head><body>{nav}<main class="container">{body}</main></body></html>""".encode()


def status_badge(status: str) -> str:
    return f'<span class="badge {e(status).lower()}">{e(status)}</span>'


def orders_table(rows: list[Any]) -> str:
    body = []
    for order in rows:
        body.append(
            "<tr>"
            f"<td><code>{e(order['out_trade_no'])}</code></td><td>{e(order['subject'])}</td>"
            f"<td>¥{e(order['amount'])}</td><td>{e(order['pay_type'])}</td>"
            f"<td>{status_badge(order['status'])}</td><td>{e(order['created_at'])}</td>"
            f"<td><a href=\"/orders/{int(order['id'])}\">详情</a></td></tr>"
        )
    if not body:
        body.append('<tr><td colspan="7" class="muted">暂无订单</td></tr>')
    return """
    <table><thead><tr><th>商户订单号</th><th>主题</th><th>金额</th><th>方式</th><th>状态</th><th>创建时间</th><th></th></tr></thead>
    <tbody>{}</tbody></table>
    """.format("".join(body))


def next_accounts(preferred_id: int | None = None) -> list[Any]:
    if preferred_id:
        row = one("SELECT * FROM accounts WHERE id = ? AND enabled = 1", (preferred_id,))
        return [row] if row else []
    panel_settings = settings_map()
    order_sql = "failure_count ASC, updated_at ASC" if panel_settings.get("enable_account_rotation") == "1" else "id ASC"
    return all_rows(f"SELECT * FROM accounts WHERE enabled = 1 ORDER BY {order_sql}")


def update_order_status(order_id: int, response: dict[str, Any], raw: str = "") -> None:
    status_value = response.get("trade_status") or response.get("status") or "WAIT_BUYER_PAY"
    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status_value in alipay.SUCCESS_STATUSES else ""
    execute(
        """
        UPDATE orders
        SET status = ?, trade_no = COALESCE(NULLIF(?, ''), trade_no),
            buyer_logon_id = COALESCE(NULLIF(?, ''), buyer_logon_id), raw_response = ?,
            last_error = '', poll_count = poll_count + 1, updated_at = CURRENT_TIMESTAMP,
            paid_at = CASE WHEN ? != '' THEN ? ELSE paid_at END
        WHERE id = ?
        """,
        (
            status_value,
            response.get("trade_no", ""),
            response.get("buyer_logon_id", ""),
            raw or json.dumps(response, ensure_ascii=False),
            paid_at,
            paid_at,
            order_id,
        ),
    )


def build_payment(order_id: int, preferred_account_id: int | None = None) -> None:
    order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        raise RuntimeError("订单不存在")
    accounts = next_accounts(preferred_account_id)
    if not accounts:
        raise RuntimeError("没有可用的支付宝账户")
    last_error = ""
    for account_row in accounts:
        account = row_to_account(account_row)
        try:
            biz_content = {
                "out_trade_no": order["out_trade_no"],
                "total_amount": order["amount"],
                "subject": order["subject"],
            }
            pay_type = order["pay_type"]
            if pay_type == "precreate":
                response = alipay.request_api(account, "alipay.trade.precreate", biz_content)
                qr_code = response.get("qr_code", "")
                pay_url = qr_code
                raw_response = json.dumps(response, ensure_ascii=False)
            else:
                method = "alipay.trade.page.pay" if pay_type == "page" else "alipay.trade.wap.pay"
                biz_content["product_code"] = "FAST_INSTANT_TRADE_PAY" if pay_type == "page" else "QUICK_WAP_WAY"
                if pay_type == "wap":
                    biz_content["quit_url"] = get_settings().base_url
                pay_url = f"{get_settings().base_url}/orders/{order_id}/pay"
                qr_code = pay_url
                raw_response = json.dumps({"method": method, "account_id": account.id}, ensure_ascii=False)
            execute(
                """
                UPDATE orders SET account_id = ?, status = 'WAIT_BUYER_PAY', qr_code = ?, pay_url = ?,
                    last_error = '', raw_response = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (account.id, qr_code, pay_url, raw_response, order_id),
            )
            execute("UPDATE accounts SET failure_count = 0, last_error = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (account.id,))
            return
        except Exception as exc:
            last_error = str(exc)
            execute(
                "UPDATE accounts SET failure_count = failure_count + 1, last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (last_error, account.id),
            )
    raise RuntimeError(last_error or "所有支付宝账户均请求失败")


def query_order(order_id: int) -> None:
    order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order or not order["account_id"]:
        return
    account_row = one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],))
    if not account_row:
        return
    account = row_to_account(account_row)
    try:
        response = alipay.request_api(account, "alipay.trade.query", {"out_trade_no": order["out_trade_no"]})
        update_order_status(order_id, response)
    except Exception as exc:
        execute("UPDATE orders SET last_error = ?, poll_count = poll_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (str(exc), order_id))


def polling_worker() -> None:
    while True:
        panel_settings = settings_map()
        interval = max(3, int(panel_settings.get("poll_interval_seconds", "8")))
        if panel_settings.get("enable_polling") == "1":
            timeout = datetime.now() - timedelta(minutes=int(panel_settings.get("poll_timeout_minutes", "30")))
            orders = all_rows(
                """
                SELECT * FROM orders
                WHERE status IN ('CREATED', 'WAIT_BUYER_PAY') AND created_at >= ?
                ORDER BY updated_at ASC LIMIT 20
                """,
                (timeout.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            for order in orders:
                query_order(int(order["id"]))
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    server_version = "PayPanelAlipay/0.2"

    def send_bytes(self, data: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        data = b""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode()
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def username(self) -> str | None:
        return read_session_cookie(self.headers.get("Cookie"))

    def require_login(self) -> bool:
        if self.username():
            return True
        self.redirect("/login")
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path.startswith("/static/"):
            return self.static(path)
        if path == "/login":
            return self.login_page()
        if path == "/alipay/notify":
            return self.alipay_notify({key: values[-1] for key, values in query.items()})
        if path.startswith("/orders/") and path.endswith("/pay"):
            return self.pay_redirect(int(path.split("/")[2]))
        if not self.require_login():
            return
        if path == "/":
            return self.dashboard()
        if path == "/orders":
            return self.orders(query)
        if path == "/orders/new":
            return self.new_order()
        if path.startswith("/orders/"):
            return self.order_detail(int(path.rsplit("/", 1)[-1]))
        if path == "/accounts":
            return self.accounts()
        if path == "/settings":
            return self.settings_page()
        self.not_found()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            return self.login()
        if path == "/alipay/notify":
            return self.alipay_notify(self.form())
        if not self.require_login():
            return
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"})
        if path == "/orders":
            return self.create_order()
        if path.startswith("/orders/") and path.endswith("/query"):
            return self.query_order_action(int(path.split("/")[2]))
        if path == "/accounts":
            return self.save_account()
        if path.startswith("/accounts/") and path.endswith("/toggle"):
            return self.toggle_account(int(path.split("/")[2]))
        if path == "/settings":
            return self.save_settings()
        if path == "/settings/2fa/regenerate":
            return self.regenerate_2fa()
        self.not_found()

    def static(self, path: str) -> None:
        rel = Path(path.removeprefix("/static/"))
        file_path = (STATIC_ROOT / rel).resolve()
        if STATIC_ROOT.resolve() not in file_path.parents or not file_path.is_file():
            return self.not_found()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_bytes(file_path.read_bytes(), content_type=content_type)

    def login_page(self, error: str = "") -> None:
        if self.username():
            return self.redirect("/")
        settings = settings_map()
        otp = '<label>2FA 验证码<input name="otp" inputmode="numeric" autocomplete="one-time-code" required></label>' if settings.get("enable_2fa") == "1" else ""
        err = f'<p class="error">{e(error)}</p>' if error else ""
        body = f"""
        <form class="card login-card" method="post" action="/login"><h1>登录 PayPanel</h1>{err}
          <label>用户名<input name="username" autocomplete="username" required></label>
          <label>密码<input type="password" name="password" autocomplete="current-password" required></label>{otp}
          <button class="primary">登录</button></form>
        """
        self.send_bytes(page("登录", body, logged_in=False))

    def login(self) -> None:
        data = self.form()
        if not verify_credentials(data.get("username", ""), data.get("password", ""), data.get("otp", "")):
            return self.login_page("用户名、密码或验证码错误")
        cookie = f"{SESSION_COOKIE}={make_session(data.get('username', ''))}; Path=/; HttpOnly; SameSite=Lax"
        self.redirect("/", {"Set-Cookie": cookie})

    def dashboard(self) -> None:
        stats = one(
            """
            SELECT COUNT(*) total_orders,
                   COALESCE(SUM(CASE WHEN status IN ('TRADE_SUCCESS','TRADE_FINISHED') THEN CAST(amount AS REAL) ELSE 0 END), 0) paid_amount,
                   SUM(CASE WHEN status IN ('TRADE_SUCCESS','TRADE_FINISHED') THEN 1 ELSE 0 END) paid_orders,
                   SUM(CASE WHEN status = 'WAIT_BUYER_PAY' THEN 1 ELSE 0 END) pending_orders
            FROM orders
            """
        )
        recent = all_rows("SELECT * FROM orders ORDER BY created_at DESC LIMIT 8")
        body = f"""
        <h1>概览</h1><section class="stats">
          <div class="card"><strong>{int(stats['total_orders'] or 0)}</strong><span>总订单</span></div>
          <div class="card"><strong>¥{float(stats['paid_amount'] or 0):.2f}</strong><span>已收金额</span></div>
          <div class="card"><strong>{int(stats['paid_orders'] or 0)}</strong><span>成功订单</span></div>
          <div class="card"><strong>{int(stats['pending_orders'] or 0)}</strong><span>待支付</span></div>
        </section><div class="actions"><a class="button primary" href="/orders/new">创建收款二维码</a></div>
        <h2>最近订单</h2>{orders_table(recent)}
        """
        self.send_bytes(page("概览", body))

    def orders(self, query: dict[str, list[str]]) -> None:
        q = query.get("q", [""])[-1]
        status = query.get("status", [""])[-1]
        clauses: list[str] = []
        params: list[Any] = []
        if q:
            clauses.append("(out_trade_no LIKE ? OR subject LIKE ? OR trade_no LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = all_rows(f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT 200", tuple(params))
        options = ["", "WAIT_BUYER_PAY", "TRADE_SUCCESS", "TRADE_FINISHED", "TRADE_CLOSED", "FAILED", "CREATED"]
        select = "".join(f'<option value="{e(item)}" {"selected" if status == item else ""}>{e(item or "全部状态")}</option>' for item in options)
        body = f"""
        <h1>订单记录</h1><form class="filters" method="get"><input name="q" value="{e(q)}" placeholder="搜索订单号、标题、支付宝交易号">
        <select name="status">{select}</select><button>查询</button></form>{orders_table(rows)}
        """
        self.send_bytes(page("订单", body))

    def new_order(self) -> None:
        accounts = all_rows("SELECT id, name FROM accounts WHERE enabled = 1 ORDER BY name")
        account_options = '<option value="0">自动选择 / 失败切换</option>' + "".join(
            f'<option value="{int(account["id"])}">{e(account["name"])}</option>' for account in accounts
        )
        body = f"""
        <h1>发起收款</h1><form class="card form" method="post" action="/orders">
          <label>金额（元）<input name="amount" inputmode="decimal" placeholder="99.00" required></label>
          <label>商品/备注标题<input name="subject" value="支付宝收款" required></label>
          <label>支付方式<select name="pay_type"><option value="precreate">当面付 / 预创建二维码</option><option value="page">电脑网站支付</option><option value="wap">手机网站支付</option></select></label>
          <label>指定账户（可选）<select name="account_id">{account_options}</select></label>
          <button class="primary">生成收款二维码</button></form>
        """
        self.send_bytes(page("发起收款", body))

    def create_order(self) -> None:
        data = self.form()
        try:
            amount = require_amount(data.get("amount", ""))
        except ValueError as exc:
            return self.send_bytes(page("金额错误", f'<p class="error">{e(exc)}</p><p><a href="/orders/new">返回</a></p>'), status=400)
        pay_type = data.get("pay_type", "precreate")
        if pay_type not in {"precreate", "page", "wap"}:
            pay_type = "precreate"
        out_trade_no = datetime.now().strftime("PP%Y%m%d%H%M%S") + uuid4().hex[:8].upper()
        order_id = execute(
            "INSERT INTO orders(out_trade_no, amount, subject, pay_type) VALUES(?, ?, ?, ?)",
            (out_trade_no, amount, data.get("subject", "").strip() or "支付宝收款", pay_type),
        )
        try:
            build_payment(order_id, int(data.get("account_id", "0") or 0) or None)
        except Exception as exc:
            execute("UPDATE orders SET status = 'FAILED', last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (str(exc), order_id))
        self.redirect(f"/orders/{order_id}")

    def order_detail(self, order_id: int) -> None:
        order = one("SELECT o.*, a.name account_name FROM orders o LEFT JOIN accounts a ON a.id = o.account_id WHERE o.id = ?", (order_id,))
        if not order:
            return self.not_found()
        qr = ""
        if order["qr_code"]:
            qr = f'<img src="{e(qr_img_src(order["qr_code"]))}" alt="支付二维码"><p>将二维码发送给客户扫码付款</p><textarea readonly>{e(order["qr_code"])}</textarea>'
        else:
            qr = '<p class="error">未生成二维码，请查看错误信息。</p>'
        body = f"""
        <h1>订单详情</h1><section class="grid-2"><div class="card"><dl>
          <dt>商户订单号</dt><dd><code>{e(order['out_trade_no'])}</code></dd><dt>支付宝交易号</dt><dd>{e(order['trade_no'] or '-')}</dd>
          <dt>金额</dt><dd>¥{e(order['amount'])}</dd><dt>标题</dt><dd>{e(order['subject'])}</dd><dt>支付方式</dt><dd>{e(order['pay_type'])}</dd>
          <dt>账户</dt><dd>{e(order['account_name'] or '-')}</dd><dt>状态</dt><dd>{status_badge(order['status'])}</dd><dt>创建时间</dt><dd>{e(order['created_at'])}</dd>
          <dt>支付时间</dt><dd>{e(order['paid_at'] or '-')}</dd><dt>错误</dt><dd class="error">{e(order['last_error'] or '-')}</dd></dl>
          <form method="post" action="/orders/{order_id}/query"><button>立即查询状态</button></form></div><div class="card qr-card">{qr}</div></section>
        """
        self.send_bytes(page("订单详情", body))

    def query_order_action(self, order_id: int) -> None:
        query_order(order_id)
        self.redirect(f"/orders/{order_id}")

    def pay_redirect(self, order_id: int) -> None:
        order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not order or not order["account_id"]:
            return self.not_found()
        account_row = one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],))
        if not account_row:
            return self.not_found()
        account = row_to_account(account_row)
        method = "alipay.trade.page.pay" if order["pay_type"] == "page" else "alipay.trade.wap.pay"
        biz_content = {"out_trade_no": order["out_trade_no"], "total_amount": order["amount"], "subject": order["subject"]}
        biz_content["product_code"] = "FAST_INSTANT_TRADE_PAY" if order["pay_type"] == "page" else "QUICK_WAP_WAY"
        if order["pay_type"] == "wap":
            biz_content["quit_url"] = get_settings().base_url
        self.send_bytes(alipay.build_page_form(account, method, biz_content).encode())

    def alipay_notify(self, form: dict[str, str]) -> None:
        order = one("SELECT * FROM orders WHERE out_trade_no = ?", (form.get("out_trade_no", ""),))
        if not order or not order["account_id"]:
            return self.send_bytes(b"fail", content_type="text/plain; charset=utf-8")
        account_row = one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],))
        if not account_row or not alipay.verify(form, row_to_account(account_row).alipay_public_key):
            return self.send_bytes(b"fail", content_type="text/plain; charset=utf-8")
        update_order_status(int(order["id"]), form, json.dumps(form, ensure_ascii=False))
        self.send_bytes(b"success", content_type="text/plain; charset=utf-8")

    def accounts(self) -> None:
        rows = all_rows("SELECT * FROM accounts ORDER BY enabled DESC, name")
        table_rows = []
        for account in rows:
            table_rows.append(
                f"<tr><td>{e(account['name'])}</td><td>{e(account['app_id'])}</td><td>{'启用' if account['enabled'] else '停用'}</td>"
                f"<td>{int(account['failure_count'])}</td><td>{e(account['last_error'])}</td>"
                f"<td><form method=\"post\" action=\"/accounts/{int(account['id'])}/toggle\"><button>{'停用' if account['enabled'] else '启用'}</button></form></td></tr>"
            )
        if not table_rows:
            table_rows.append('<tr><td colspan="6" class="muted">暂无账户</td></tr>')
        body = f"""
        <h1>支付宝账户</h1><p class="muted">默认异步通知地址：<code>{e(default_notify_url())}</code></p>
        <table><thead><tr><th>名称</th><th>App ID</th><th>状态</th><th>失败次数</th><th>最近错误</th><th></th></tr></thead><tbody>{''.join(table_rows)}</tbody></table>
        <h2>新增账户</h2><form class="card form" method="post" action="/accounts">
          <label>名称<input name="name" required></label><label>App ID<input name="app_id" required></label>
          <label>网关<input name="gateway" value="https://openapi.alipay.com/gateway.do" required></label>
          <label>应用私钥（PKCS8 PEM 或 Base64）<textarea name="merchant_private_key" rows="5" required></textarea></label>
          <label>支付宝公钥（PEM 或 Base64）<textarea name="alipay_public_key" rows="5" required></textarea></label>
          <label>应用公钥证书 SN（证书模式可填）<input name="app_cert_sn"></label><label>支付宝根证书 SN（证书模式可填）<input name="alipay_root_cert_sn"></label>
          <label>异步通知 URL（留空使用默认）<input name="notify_url" placeholder="{e(default_notify_url())}"></label><label>同步返回 URL<input name="return_url"></label>
          <button class="primary">保存账户</button></form>
        """
        self.send_bytes(page("账户", body))

    def save_account(self) -> None:
        data = self.form()
        execute(
            """
            INSERT INTO accounts(name, app_id, gateway, merchant_private_key, alipay_public_key,
                                 app_cert_sn, alipay_root_cert_sn, notify_url, return_url)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("name", ""),
                data.get("app_id", ""),
                data.get("gateway", "https://openapi.alipay.com/gateway.do"),
                data.get("merchant_private_key", ""),
                data.get("alipay_public_key", ""),
                data.get("app_cert_sn", ""),
                data.get("alipay_root_cert_sn", ""),
                data.get("notify_url", ""),
                data.get("return_url", ""),
            ),
        )
        self.redirect("/accounts")

    def toggle_account(self, account_id: int) -> None:
        execute("UPDATE accounts SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (account_id,))
        self.redirect("/accounts")

    def settings_page(self) -> None:
        panel = settings_map()
        uri = provisioning_uri(panel["totp_secret"], get_settings().admin_username) if panel.get("totp_secret") else ""
        totp = f'<img class="totp" src="{e(qr_img_src(uri))}" alt="TOTP 二维码"><p>用 Authenticator 扫码后，再开启 2FA。</p>' if uri else "<p>尚未生成 2FA 密钥。</p>"
        body = f"""
        <h1>设置</h1><form class="card form" method="post" action="/settings">
          <label class="checkbox"><input type="checkbox" name="enable_account_rotation" value="1" {'checked' if panel.get('enable_account_rotation') == '1' else ''}> 开启多账户轮询/失败切换</label>
          <label class="checkbox"><input type="checkbox" name="enable_polling" value="1" {'checked' if panel.get('enable_polling') == '1' else ''}> 开启订单状态自动轮询</label>
          <label>轮询间隔（秒）<input name="poll_interval_seconds" type="number" min="3" value="{e(panel.get('poll_interval_seconds', '8'))}"></label>
          <label>轮询超时（分钟）<input name="poll_timeout_minutes" type="number" min="1" value="{e(panel.get('poll_timeout_minutes', '30'))}"></label>
          <label class="checkbox"><input type="checkbox" name="enable_2fa" value="1" {'checked' if panel.get('enable_2fa') == '1' else ''}> 开启 2FA 登录</label>
          <button class="primary">保存设置</button></form><section class="card"><h2>2FA 密钥</h2>{totp}
          <form method="post" action="/settings/2fa/regenerate"><button>生成/重置 2FA 密钥</button></form></section>
        """
        self.send_bytes(page("设置", body))

    def save_settings(self) -> None:
        data = self.form()
        values = {
            "enable_account_rotation": "1" if data.get("enable_account_rotation") == "1" else "0",
            "enable_polling": "1" if data.get("enable_polling") == "1" else "0",
            "poll_interval_seconds": str(max(3, int(data.get("poll_interval_seconds") or "8"))),
            "poll_timeout_minutes": str(max(1, int(data.get("poll_timeout_minutes") or "30"))),
            "enable_2fa": "1" if data.get("enable_2fa") == "1" else "0",
        }
        with connect() as conn:
            for key, value in values.items():
                conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.redirect("/settings")

    def regenerate_2fa(self) -> None:
        execute("INSERT INTO settings(key, value) VALUES('totp_secret', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (random_totp_secret(),))
        self.redirect("/settings")

    def not_found(self) -> None:
        self.send_bytes(page("Not Found", "<h1>404</h1><p>页面不存在。</p>", logged_in=bool(self.username())), status=HTTPStatus.NOT_FOUND)


def run(host: str | None = None, port: int | None = None) -> None:
    init_db()
    settings = get_settings()
    host = host or settings.host
    port = port or settings.port
    threading.Thread(target=polling_worker, daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"PayPanel Alipay listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
