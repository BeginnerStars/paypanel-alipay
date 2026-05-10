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
from .crypto import decrypt_secret, encrypt_secret
from .db import all_rows, connect, execute, init_db, one, settings_map


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return value.rstrip("/")


def host_name(value: str) -> str:
    value = value.split(",", 1)[0].strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urllib.parse.urlparse(value).netloc
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    return value.rsplit(":", 1)[0]


def panel_settings_value(key: str, default: str = "") -> str:
    try:
        return settings_map().get(key, default).strip()
    except Exception:
        return default.strip()


def site_name() -> str:
    return panel_settings_value("site_name", get_settings().site_name) or "PayPanel Alipay"


def panel_base_url() -> str:
    configured_domain = host_name(panel_settings_value("panel_domain"))
    if configured_domain:
        scheme = "https" if normalize_base_url(get_settings().base_url).startswith("https://") else "http"
        return f"{scheme}://{configured_domain}"
    return normalize_base_url(get_settings().base_url)


def callback_base_url() -> str:
    configured = normalize_base_url(panel_settings_value("callback_base_url"))
    return configured or panel_base_url()



def bound_panel_domain(panel_settings: dict[str, str] | None = None) -> str:
    panel_settings = panel_settings or settings_map()
    configured = host_name(panel_settings.get("panel_domain", ""))
    if configured:
        return configured
    parsed = urllib.parse.urlparse(panel_base_url())
    return host_name(parsed.netloc)



PAY_TYPE_LABELS = {
    "precreate": "当面付",
    "wap": "手机网站支付",
    "page": "电脑网站支付",
}
PAY_TYPE_ORDER = ("precreate", "wap", "page")
DEFAULT_PRODUCT_CODES = {
    "precreate": "FACE_TO_FACE_PAYMENT",
    "page": "FAST_INSTANT_TRADE_PAY",
    "wap": "QUICK_WAP_WAY",
}


def normalize_pay_types(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = list(value or [])
    selected = [item.strip() for item in raw if item.strip() in PAY_TYPE_LABELS]
    unique = [item for item in PAY_TYPE_ORDER if item in selected]
    return tuple(unique or ("precreate",))


def account_supports(account: Any, pay_type: str) -> bool:
    return pay_type in normalize_pay_types(account["pay_types"])


def pay_type_labels(value: str | list[str] | tuple[str, ...] | None) -> str:
    return "、".join(PAY_TYPE_LABELS[item] for item in normalize_pay_types(value))


def business_value(row: Any, business: str, field: str, legacy_field: str = "") -> str:
    value = row[f"{business}_{field}"] if f"{business}_{field}" in row.keys() else ""
    if value:
        return value
    return row[legacy_field] if legacy_field else ""


def row_to_account(row: Any, pay_type: str = "precreate") -> AlipayAccount:
    return AlipayAccount(
        id=row["id"],
        app_id=business_value(row, pay_type, "app_id", "app_id"),
        gateway=business_value(row, pay_type, "gateway", "gateway") or "https://openapi.alipay.com/gateway.do",
        merchant_private_key=decrypt_secret(business_value(row, pay_type, "merchant_private_key", "merchant_private_key")),
        alipay_public_key=decrypt_secret(business_value(row, pay_type, "alipay_public_key", "alipay_public_key")),
        app_public_key=business_value(row, pay_type, "app_public_key"),
        app_cert_sn=business_value(row, pay_type, "app_cert_sn", "app_cert_sn") if pay_type in {"wap", "page"} else "",
        alipay_root_cert_sn=business_value(row, pay_type, "alipay_root_cert_sn", "alipay_root_cert_sn") if pay_type in {"wap", "page"} else "",
        notify_url=business_value(row, pay_type, "notify_url", "notify_url") or default_notify_url(),
        return_url=business_value(row, pay_type, "return_url", "return_url") or panel_base_url(),
        pay_types=(pay_type,),
        precreate_product_code=row["precreate_product_code"] or DEFAULT_PRODUCT_CODES["precreate"],
        page_product_code=row["page_product_code"] or DEFAULT_PRODUCT_CODES["page"],
        wap_product_code=row["wap_product_code"] or DEFAULT_PRODUCT_CODES["wap"],
    )


def default_notify_url() -> str:
    return f"{callback_base_url()}/alipay/notify"


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


def pay_type_options(selected: str = "precreate") -> str:
    return "".join(
        f'<option value="{e(key)}" {"selected" if selected == key else ""}>{e(label)}</option>'
        for key, label in PAY_TYPE_LABELS.items()
    )


def checked_pay_type(value: str, selected: tuple[str, ...]) -> str:
    return "checked" if value in selected else ""


def business_inputs(account: Any | None, business: str, is_edit: bool) -> str:
    prefix = business + "_"
    app_id = business_value(account, business, "app_id", "app_id") if is_edit else ""
    app_public_key = business_value(account, business, "app_public_key") if is_edit else ""
    gateway = business_value(account, business, "gateway", "gateway") if is_edit else "https://openapi.alipay.com/gateway.do"
    alipay_public_key_help = "留空表示不修改已保存的支付宝公钥" if is_edit else "PEM 或 Base64"
    private_help = "留空表示不修改已保存的应用私钥" if is_edit else "PKCS8 PEM 或 Base64"
    private_required = "" if is_edit else "required"
    public_required = "" if is_edit else "required"
    notify_url = business_value(account, business, "notify_url", "notify_url") if is_edit else ""
    product_code = account[f"{business}_product_code"] if is_edit and f"{business}_product_code" in account.keys() else DEFAULT_PRODUCT_CODES[business]
    cert_fields = ""
    return_field = ""
    if business in {"wap", "page"}:
        return_url = business_value(account, business, "return_url", "return_url") if is_edit else ""
        app_cert_sn = business_value(account, business, "app_cert_sn", "app_cert_sn") if is_edit else ""
        alipay_root_cert_sn = business_value(account, business, "alipay_root_cert_sn", "alipay_root_cert_sn") if is_edit else ""
        cert_fields = f"""
        <label>应用公钥证书 SN（证书模式可填）<input name="{prefix}app_cert_sn" value="{e(app_cert_sn)}"></label>
        <label>支付宝根证书 SN（证书模式可填）<input name="{prefix}alipay_root_cert_sn" value="{e(alipay_root_cert_sn)}"></label>"""
        return_field = f'<label>同步返回 URL<input name="{prefix}return_url" value="{e(return_url)}" placeholder="{e(panel_base_url())}"></label>'
    return f"""
      <section class="business-field business-{business}">
        <h3>{e(PAY_TYPE_LABELS[business])}参数</h3>
        <label>APPID<input name="{prefix}app_id" value="{e(app_id)}" required></label>
        <label>应用公钥<textarea name="{prefix}app_public_key" rows="4" required>{e(app_public_key)}</textarea></label>
        <label>应用私钥（{e(private_help)}）<textarea name="{prefix}merchant_private_key" rows="5" {private_required}></textarea></label>
        <label>支付宝公钥（{e(alipay_public_key_help)}）<textarea name="{prefix}alipay_public_key" rows="5" {public_required}></textarea></label>
        <label>网关<input name="{prefix}gateway" value="{e(gateway)}" required></label>
        <label>异步通知 URL（留空使用默认）<input name="{prefix}notify_url" value="{e(notify_url)}" placeholder="{e(default_notify_url())}"></label>
        {return_field}
        {cert_fields}
        <label>{e(PAY_TYPE_LABELS[business])} product_code<input name="{prefix}product_code" value="{e(product_code or DEFAULT_PRODUCT_CODES[business])}"></label>
      </section>"""


def account_form(action: str, account: Any | None = None) -> str:
    is_edit = account is not None
    selected = normalize_pay_types(account["pay_types"] if is_edit else "precreate")
    button = "保存修改" if is_edit else "保存账户"
    name = account["name"] if is_edit else ""
    return f"""
    <form class="card form account-form" method="post" action="{e(action)}">
      <label>账户名称<input name="name" value="{e(name)}" required></label>
      <fieldset><legend>启用业务（可多选，默认优先当面付）</legend>
        <label class="checkbox"><input type="checkbox" name="pay_types" value="precreate" {checked_pay_type('precreate', selected)}> 当面付（alipay.trade.precreate）</label>
        <label class="checkbox"><input type="checkbox" name="pay_types" value="wap" {checked_pay_type('wap', selected)}> 手机网站支付（alipay.trade.wap.pay）</label>
        <label class="checkbox"><input type="checkbox" name="pay_types" value="page" {checked_pay_type('page', selected)}> 电脑网站支付（alipay.trade.page.pay）</label>
      </fieldset>
      <div class="business-hint business-precreate"><strong>当面付</strong>使用密钥模式，需要 APPID、应用公钥、应用私钥、支付宝公钥；请确认支付宝开放平台已开通/签约当面付，否则可能返回 ACCESS_FORBIDDEN。</div>
      <div class="business-hint business-wap"><strong>手机网站支付</strong>可使用独立 APPID/密钥/公钥/回调和 QUICK_WAP_WAY 产品码，不与当面付参数混用。</div>
      <div class="business-hint business-page"><strong>电脑网站支付</strong>可使用独立 APPID/密钥/公钥/回调和 FAST_INSTANT_TRADE_PAY 产品码，不与其他业务参数混用。</div>
      {business_inputs(account, 'precreate', is_edit)}
      {business_inputs(account, 'wap', is_edit)}
      {business_inputs(account, 'page', is_edit)}
      <button class="primary">{button}</button></form>
    <script>
    (function() {{
      var form = document.currentScript.previousElementSibling;
      if (!form || !form.classList.contains('account-form')) return;
      function refresh() {{
        var checked = Array.prototype.map.call(form.querySelectorAll('input[name="pay_types"]:checked'), function(input) {{ return input.value; }});
        if (!checked.length) {{ checked = ['precreate']; form.querySelector('input[value="precreate"]').checked = true; }}
        form.querySelectorAll('.business-field,.business-hint').forEach(function(el) {{
          var shown = checked.some(function(value) {{ return el.classList.contains('business-' + value); }});
          el.hidden = !shown;
          el.querySelectorAll('input,textarea,select').forEach(function(input) {{ input.disabled = !shown; }});
        }});
      }}
      form.querySelectorAll('input[name="pay_types"]').forEach(function(input) {{ input.addEventListener('change', refresh); }});
      refresh();
    }})();
    </script>"""


def account_values(data: dict[str, Any]) -> dict[str, str]:
    pay_types = normalize_pay_types(data.get("pay_types", []))
    values = {"name": data.get("name", "").strip(), "pay_types": ",".join(pay_types)}
    for business in PAY_TYPE_ORDER:
        enabled = business in pay_types
        prefix = business + "_"
        values[f"{prefix}app_id"] = data.get(f"{prefix}app_id", "").strip() if enabled else ""
        values[f"{prefix}app_public_key"] = data.get(f"{prefix}app_public_key", "").strip() if enabled else ""
        values[f"{prefix}merchant_private_key"] = data.get(f"{prefix}merchant_private_key", "").strip() if enabled else ""
        values[f"{prefix}alipay_public_key"] = data.get(f"{prefix}alipay_public_key", "").strip() if enabled else ""
        values[f"{prefix}gateway"] = data.get(f"{prefix}gateway", "https://openapi.alipay.com/gateway.do").strip() if enabled else ""
        values[f"{prefix}notify_url"] = data.get(f"{prefix}notify_url", "").strip() if enabled else ""
        values[f"{prefix}product_code"] = (data.get(f"{prefix}product_code", "").strip() or DEFAULT_PRODUCT_CODES[business]) if enabled else DEFAULT_PRODUCT_CODES[business]
        if business in {"wap", "page"}:
            values[f"{prefix}return_url"] = data.get(f"{prefix}return_url", "").strip() if enabled else ""
            values[f"{prefix}app_cert_sn"] = data.get(f"{prefix}app_cert_sn", "").strip() if enabled else ""
            values[f"{prefix}alipay_root_cert_sn"] = data.get(f"{prefix}alipay_root_cert_sn", "").strip() if enabled else ""
    primary = pay_types[0]
    values.update({
        "app_id": values[f"{primary}_app_id"],
        "gateway": values[f"{primary}_gateway"] or "https://openapi.alipay.com/gateway.do",
        "merchant_private_key": values[f"{primary}_merchant_private_key"],
        "alipay_public_key": values[f"{primary}_alipay_public_key"],
        "app_cert_sn": values.get(f"{primary}_app_cert_sn", ""),
        "alipay_root_cert_sn": values.get(f"{primary}_alipay_root_cert_sn", ""),
        "notify_url": values[f"{primary}_notify_url"],
        "return_url": values.get(f"{primary}_return_url", ""),
        "precreate_product_code": values["precreate_product_code"],
        "wap_product_code": values["wap_product_code"],
        "page_product_code": values["page_product_code"],
    })
    return values
def bounded_int(value: Any, default: int, minimum: int, maximum: int = 525_600) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def path_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def session_cookie(name: str, value: str, max_age: int | None = None) -> str:
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age is not None:
        parts.insert(2, f"Max-Age={max_age}")
    if panel_base_url().startswith("https://"):
        parts.append("Secure")
    return "; ".join(parts)


def page(title: str, body: str, logged_in: bool = True) -> bytes:
    nav = ""
    if logged_in:
        nav = f"""
        <header class="topbar"><a class="brand" href="/">{e(site_name())}</a><nav>
          <a href="/orders/new">发起收款</a><a href="/orders">订单</a><a href="/accounts">账户</a><a href="/settings">设置</a>
          <form action="/logout" method="post"><button>退出</button></form>
        </nav></header>
        """
    main_class = "container" if logged_in else "login-page"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)} - {e(site_name())}</title>
    <link rel="stylesheet" href="/static/style.css"></head><body>{nav}<main class="{main_class}">{body}</main></body></html>""".encode()


def status_badge(status: str) -> str:
    return f'<span class="badge {e(status).lower()}">{e(status)}</span>'


def orders_table(rows: list[Any]) -> str:
    body = []
    for order in rows:
        order_id = int(order["id"])
        body.append(
            "<tr>"
            f"<td><code>{e(order['out_trade_no'])}</code></td><td>{e(order['subject'])}</td>"
            f"<td>¥{e(order['amount'])}</td><td>{e(PAY_TYPE_LABELS.get(order['pay_type'], order['pay_type']))}</td>"
            f"<td>{status_badge(order['status'])}</td><td>{e(order['created_at'])}</td>"
            f'<td class="row-actions"><a class="button" href="/orders/{order_id}">详情</a>'
            f'<form method="post" action="/orders/{order_id}/delete" onsubmit="return confirm(&quot;确认删除该订单？&quot;)"><button class="danger">删除</button></form></td></tr>'
        )
    if not body:
        body.append('<tr><td colspan="7" class="muted">暂无订单</td></tr>')
    return """
    <table><thead><tr><th>商户订单号</th><th>主题</th><th>金额</th><th>方式</th><th>状态</th><th>创建时间</th><th></th></tr></thead>
    <tbody>{}</tbody></table>
    """.format("".join(body))


def next_accounts(preferred_id: int | None = None, pay_type: str = "precreate") -> list[Any]:
    if preferred_id:
        row = one("SELECT * FROM accounts WHERE id = ? AND enabled = 1", (preferred_id,))
        return [row] if row and account_supports(row, pay_type) else []
    panel_settings = settings_map()
    order_sql = "failure_count ASC, updated_at ASC" if panel_settings.get("enable_account_rotation") == "1" else "id ASC"
    return [row for row in all_rows(f"SELECT * FROM accounts WHERE enabled = 1 ORDER BY {order_sql}") if account_supports(row, pay_type)]


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


def order_timeout_minutes(panel_settings: dict[str, str] | None = None) -> int:
    panel_settings = panel_settings or settings_map()
    return bounded_int(panel_settings.get("order_timeout_minutes"), 30, 0)


def apply_order_timeout_biz_content(biz_content: dict[str, Any]) -> None:
    minutes = order_timeout_minutes()
    if minutes > 0:
        biz_content["timeout_express"] = f"{minutes}m"


def expire_timeout_orders(panel_settings: dict[str, str] | None = None) -> int:
    minutes = order_timeout_minutes(panel_settings)
    if minutes <= 0:
        return 0
    deadline = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE orders
            SET status = 'TRADE_CLOSED', last_error = '订单超时自动关闭', updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('CREATED', 'WAIT_BUYER_PAY') AND created_at < ?
            """,
            (deadline,),
        )
        return int(cur.rowcount or 0)


def build_payment(order_id: int, preferred_account_id: int | None = None) -> None:
    order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        raise RuntimeError("订单不存在")
    accounts = next_accounts(preferred_account_id, order["pay_type"])
    if not accounts:
        raise RuntimeError(f"没有支持{PAY_TYPE_LABELS.get(order['pay_type'], order['pay_type'])}的可用支付宝账户")
    last_error = ""
    for account_row in accounts:
        account = row_to_account(account_row, order["pay_type"])
        try:
            biz_content = {
                "out_trade_no": order["out_trade_no"],
                "total_amount": order["amount"],
                "subject": order["subject"],
            }
            apply_order_timeout_biz_content(biz_content)
            pay_type = order["pay_type"]
            if pay_type == "precreate":
                if account.precreate_product_code:
                    biz_content["product_code"] = account.precreate_product_code
                response = alipay.request_api(account, "alipay.trade.precreate", biz_content)
                qr_code = response.get("qr_code", "")
                pay_url = qr_code
                raw_response = json.dumps(response, ensure_ascii=False)
            else:
                method = "alipay.trade.page.pay" if pay_type == "page" else "alipay.trade.wap.pay"
                biz_content["product_code"] = account.page_product_code if pay_type == "page" else account.wap_product_code
                if pay_type == "wap":
                    biz_content["quit_url"] = panel_base_url()
                pay_url = f"{panel_base_url()}/orders/{order_id}/pay"
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
    account = row_to_account(account_row, order["pay_type"])
    try:
        response = alipay.request_api(account, "alipay.trade.query", {"out_trade_no": order["out_trade_no"]})
        update_order_status(order_id, response)
    except Exception as exc:
        execute("UPDATE orders SET last_error = ?, poll_count = poll_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (str(exc), order_id))


def polling_worker() -> None:
    while True:
        panel_settings = settings_map()
        interval = bounded_int(panel_settings.get("poll_interval_seconds"), 8, 3)
        expire_timeout_orders(panel_settings)
        if panel_settings.get("enable_polling") == "1":
            timeout = datetime.now() - timedelta(minutes=bounded_int(panel_settings.get("poll_timeout_minutes"), 30, 1))
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

    def form(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode()
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values if key == "pay_types" else (values[-1] if values else "") for key, values in parsed.items()}

    def username(self) -> str | None:
        return read_session_cookie(self.headers.get("Cookie"))

    def host_is_allowed(self) -> bool:
        panel_settings = settings_map()
        if panel_settings.get("enforce_panel_domain") != "1":
            return True
        expected = bound_panel_domain(panel_settings)
        if not expected:
            return True
        supplied = host_name(self.headers.get("Host", ""))
        return supplied == expected

    def reject_bad_host(self) -> None:
        self.send_bytes(
            page("域名未绑定", f"<h1>域名未绑定</h1><p>请使用绑定域名访问：<code>{e(bound_panel_domain())}</code></p>", logged_in=False),
            status=421,
        )

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
        if path == "/healthz":
            return self.healthz()
        if not self.host_is_allowed():
            return self.reject_bad_host()
        if path == "/login":
            return self.login_page()
        if path == "/alipay/notify":
            return self.alipay_notify({key: values[-1] for key, values in query.items()})
        if path.startswith("/orders/") and path.endswith("/pay"):
            order_id = path_int(path.split("/")[2])
            return self.pay_redirect(order_id) if order_id is not None else self.not_found()
        if not self.require_login():
            return
        if path == "/":
            return self.dashboard()
        if path == "/orders":
            return self.orders(query)
        if path == "/orders/new":
            return self.new_order()
        if path.startswith("/orders/"):
            order_id = path_int(path.rsplit("/", 1)[-1])
            return self.order_detail(order_id) if order_id is not None else self.not_found()
        if path == "/accounts":
            return self.accounts()
        if path.startswith("/accounts/") and path.endswith("/edit"):
            account_id = path_int(path.split("/")[2])
            return self.edit_account(account_id) if account_id is not None else self.not_found()
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
        if not self.host_is_allowed():
            return self.reject_bad_host()
        if not self.require_login():
            return
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": session_cookie(SESSION_COOKIE, "", max_age=0)})
        if path == "/orders/cleanup":
            return self.cleanup_orders()
        if path == "/orders":
            return self.create_order()
        if path.startswith("/orders/") and path.endswith("/query"):
            order_id = path_int(path.split("/")[2])
            return self.query_order_action(order_id) if order_id is not None else self.not_found()
        if path.startswith("/orders/") and path.endswith("/delete"):
            order_id = path_int(path.split("/")[2])
            return self.delete_order(order_id) if order_id is not None else self.not_found()
        if path == "/accounts":
            return self.save_account()
        if path.startswith("/accounts/") and path.endswith("/update"):
            account_id = path_int(path.split("/")[2])
            return self.update_account(account_id) if account_id is not None else self.not_found()
        if path.startswith("/accounts/") and path.endswith("/delete"):
            account_id = path_int(path.split("/")[2])
            return self.delete_account(account_id) if account_id is not None else self.not_found()
        if path.startswith("/accounts/") and path.endswith("/toggle"):
            account_id = path_int(path.split("/")[2])
            return self.toggle_account(account_id) if account_id is not None else self.not_found()
        if path == "/settings":
            return self.save_settings()
        if path == "/settings/2fa/regenerate":
            return self.regenerate_2fa()
        self.not_found()

    def healthz(self) -> None:
        self.send_bytes(b"ok", content_type="text/plain; charset=utf-8")

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
        <form class="card login-card" method="post" action="/login"><h1>登录 {e(site_name())}</h1>{err}
          <label>用户名<input name="username" autocomplete="username" required></label>
          <label>密码<input type="password" name="password" autocomplete="current-password" required></label>{otp}
          <button class="primary">登录</button></form>
        """
        self.send_bytes(page("登录", body, logged_in=False))

    def login(self) -> None:
        data = self.form()
        if not verify_credentials(data.get("username", ""), data.get("password", ""), data.get("otp", "")):
            return self.login_page("用户名、密码或验证码错误")
        cookie = session_cookie(SESSION_COOKIE, make_session(data.get("username", "")))
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
        <section class="card"><h2>订单清理</h2><form class="form" method="post" action="/orders/cleanup">
          <label>开始时间<input type="datetime-local" name="cleanup_start"></label>
          <label>结束时间<input type="datetime-local" name="cleanup_end"></label>
          <button name="cleanup_mode" value="range">清理所选时段订单</button>
          <button name="cleanup_mode" value="all" onclick="return confirm('确认清除所有订单记录？此操作不可恢复。')">一键清除所有订单记录</button>
        </form></section>
        """
        self.send_bytes(page("订单", body))

    def cleanup_orders(self) -> None:
        data = self.form()
        mode = data.get("cleanup_mode", "range")
        if mode == "all":
            with connect() as conn:
                conn.execute("DELETE FROM orders")
            return self.redirect("/orders")
        start_at = data.get("cleanup_start", "").strip()
        end_at = data.get("cleanup_end", "").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if start_at:
            clauses.append("created_at >= ?")
            params.append(start_at.replace("T", " ") + (":00" if len(start_at) == 16 else ""))
        if end_at:
            clauses.append("created_at <= ?")
            params.append(end_at.replace("T", " ") + (":59" if len(end_at) == 16 else ""))
        if clauses:
            with connect() as conn:
                conn.execute("DELETE FROM orders WHERE " + " AND ".join(clauses), tuple(params))
        return self.redirect("/orders")


    def new_order(self) -> None:
        accounts = all_rows("SELECT id, name, pay_types FROM accounts WHERE enabled = 1 ORDER BY name")
        account_options = '<option value="0">自动选择 / 失败切换</option>' + "".join(
            f'<option value="{int(account["id"])}">{e(account["name"])}（{e(pay_type_labels(account["pay_types"]))}）</option>' for account in accounts
        )
        body = f"""
        <h1>发起收款</h1><form class="card form" method="post" action="/orders">
          <label>金额（元）<input name="amount" inputmode="decimal" placeholder="99.00" required></label>
          <label>商品/备注标题<input name="subject" value="支付宝收款" required></label>
          <label>支付方式<select name="pay_type">{pay_type_options()}</select></label>
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
          <dt>金额</dt><dd>¥{e(order['amount'])}</dd><dt>标题</dt><dd>{e(order['subject'])}</dd><dt>支付方式</dt><dd>{e(PAY_TYPE_LABELS.get(order['pay_type'], order['pay_type']))}</dd>
          <dt>账户</dt><dd>{e(order['account_name'] or '-')}</dd><dt>状态</dt><dd>{status_badge(order['status'])}</dd><dt>创建时间</dt><dd>{e(order['created_at'])}</dd>
          <dt>支付时间</dt><dd>{e(order['paid_at'] or '-')}</dd><dt>错误</dt><dd class="error">{e(order['last_error'] or '-')}</dd></dl>
          <div class="row-actions"><form method="post" action="/orders/{order_id}/query"><button>立即查询状态</button></form>
          <form method="post" action="/orders/{order_id}/delete" onsubmit="return confirm('确认删除该订单？')"><button class="danger">删除订单</button></form></div></div><div class="card qr-card">{qr}</div></section>
        """
        self.send_bytes(page("订单详情", body))

    def query_order_action(self, order_id: int) -> None:
        query_order(order_id)
        self.redirect(f"/orders/{order_id}")

    def delete_order(self, order_id: int) -> None:
        execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return self.redirect("/orders")

    def pay_redirect(self, order_id: int) -> None:
        order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not order or not order["account_id"]:
            return self.not_found()
        account_row = one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],))
        if not account_row:
            return self.not_found()
        account = row_to_account(account_row, order["pay_type"])
        method = "alipay.trade.page.pay" if order["pay_type"] == "page" else "alipay.trade.wap.pay"
        biz_content = {"out_trade_no": order["out_trade_no"], "total_amount": order["amount"], "subject": order["subject"]}
        apply_order_timeout_biz_content(biz_content)
        biz_content["product_code"] = account.page_product_code if order["pay_type"] == "page" else account.wap_product_code
        if order["pay_type"] == "wap":
            biz_content["quit_url"] = panel_base_url()
        self.send_bytes(alipay.build_page_form(account, method, biz_content).encode())

    def alipay_notify(self, form: dict[str, str]) -> None:
        order = one("SELECT * FROM orders WHERE out_trade_no = ?", (form.get("out_trade_no", ""),))
        if not order or not order["account_id"]:
            return self.send_bytes(b"fail", content_type="text/plain; charset=utf-8")
        account_row = one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],))
        if not account_row or not alipay.verify(form, row_to_account(account_row, order["pay_type"]).alipay_public_key):
            return self.send_bytes(b"fail", content_type="text/plain; charset=utf-8")
        update_order_status(int(order["id"]), form, json.dumps(form, ensure_ascii=False))
        self.send_bytes(b"success", content_type="text/plain; charset=utf-8")


    def accounts(self) -> None:
        rows = all_rows("SELECT * FROM accounts ORDER BY enabled DESC, name")
        table_rows = []
        for account in rows:
            business = pay_type_labels(account["pay_types"])
            table_rows.append(
                f"<tr><td>{e(account['name'])}</td><td>{e(account_display_app_id(account))}</td><td>{e(business)}</td><td>{'启用' if account['enabled'] else '停用'}</td>"
                f"<td>{int(account['failure_count'])}</td><td>{e(account['last_error'])}</td>"
                f"<td class=\"row-actions\"><a class=\"button\" href=\"/accounts/{int(account['id'])}/edit\">修改</a>"
                f"<form method=\"post\" action=\"/accounts/{int(account['id'])}/toggle\"><button>{'停用' if account['enabled'] else '启用'}</button></form>"
                f"<form method=\"post\" action=\"/accounts/{int(account['id'])}/delete\" onsubmit=\"return confirm('确认删除该账户？历史订单仍会保留。')\"><button class=\"danger\">删除</button></form></td></tr>"
            )
        if not table_rows:
            table_rows.append('<tr><td colspan="7" class="muted">暂无账户</td></tr>')
        body = f"""
        <h1>支付宝账户</h1><p class="muted">默认异步通知地址：<code>{e(default_notify_url())}</code></p>
        <table><thead><tr><th>名称</th><th>App ID</th><th>支付产品</th><th>状态</th><th>失败次数</th><th>最近错误</th><th></th></tr></thead><tbody>{''.join(table_rows)}</tbody></table>
        <h2>新增账户</h2>
        <p class="muted">请只勾选该支付宝应用已签约/开通的产品。电脑网站支付默认使用 FAST_INSTANT_TRADE_PAY；手机网站支付默认使用 QUICK_WAP_WAY；当面付预创建通常无需 product_code，若支付宝侧要求可填写对应产品码。</p>
        {account_form('/accounts')}
        """
        self.send_bytes(page("账户", body))

    def edit_account(self, account_id: int) -> None:
        account = one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if not account:
            return self.not_found()
        body = f"""
        <h1>修改支付宝账户</h1><p><a href="/accounts">返回账户列表</a></p>
        {account_form(f'/accounts/{account_id}/update', account)}
        """
        self.send_bytes(page("修改账户", body))

    def save_account(self) -> None:
        data = self.form()
        values = encrypted_account_values(account_values(data))
        columns = account_columns()
        execute(
            f"INSERT INTO accounts({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)})",
            tuple(values.get(column, "") for column in columns),
        )
        self.redirect("/accounts")

    def update_account(self, account_id: int) -> None:
        existing = one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if not existing:
            return self.not_found()
        data = self.form()
        values = encrypted_account_values(account_values(data), existing)
        columns = account_columns()
        with connect() as conn:
            conn.execute(
                "UPDATE accounts SET " + ", ".join(f"{column} = ?" for column in columns) + ", updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*[values.get(column, "") for column in columns], account_id),
            )
        self.redirect("/accounts")

    def delete_account(self, account_id: int) -> None:
        with connect() as conn:
            conn.execute("UPDATE orders SET account_id = NULL WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.redirect("/accounts")

    def toggle_account(self, account_id: int) -> None:
        execute("UPDATE accounts SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (account_id,))
        self.redirect("/accounts")


    def settings_page(self) -> None:
        panel = settings_map()
        uri = provisioning_uri(panel["totp_secret"], get_settings().admin_username, site_name()) if panel.get("totp_secret") else ""
        totp = f'<img class="totp" src="{e(qr_img_src(uri))}" alt="TOTP 二维码"><p>用 Authenticator 扫码后，再开启 2FA。</p>' if uri else "<p>尚未生成 2FA 密钥。</p>"
        body = f"""
        <h1>设置</h1><form class="card form" method="post" action="/settings">
          <label>站点名称<input name="site_name" value="{e(panel.get('site_name', site_name()))}" placeholder="PayPanel Alipay"></label>
          <label>绑定访问域名（如 pay.example.com）<input name="panel_domain" value="{e(panel.get('panel_domain', ''))}" placeholder="pay.example.com"></label>
          <label class="checkbox"><input type="checkbox" name="enforce_panel_domain" value="1" {'checked' if panel.get('enforce_panel_domain') == '1' else ''}> 仅允许绑定域名访问面板</label>
          <label>自定义回调域名/地址（留空使用 APP_BASE_URL）<input name="callback_base_url" value="{e(panel.get('callback_base_url', ''))}" placeholder="https://notify.example.com"></label>
          <label class="checkbox"><input type="checkbox" name="enable_account_rotation" value="1" {'checked' if panel.get('enable_account_rotation') == '1' else ''}> 开启多账户轮询/失败切换</label>
          <label class="checkbox"><input type="checkbox" name="enable_polling" value="1" {'checked' if panel.get('enable_polling') == '1' else ''}> 开启订单状态自动轮询</label>
          <label>轮询间隔（秒）<input name="poll_interval_seconds" type="number" min="3" value="{e(panel.get('poll_interval_seconds', '8'))}"></label>
          <label>轮询超时（分钟）<input name="poll_timeout_minutes" type="number" min="1" value="{e(panel.get('poll_timeout_minutes', '30'))}"></label>
          <label>订单超时关闭（分钟，0 表示不自动关闭）<input name="order_timeout_minutes" type="number" min="0" value="{e(panel.get('order_timeout_minutes', '30'))}"></label>
          <label class="checkbox"><input type="checkbox" name="enable_2fa" value="1" {'checked' if panel.get('enable_2fa') == '1' else ''}> 开启 2FA 登录</label>
          <button class="primary">保存设置</button></form><section class="card"><h2>2FA 密钥</h2>{totp}
          <form method="post" action="/settings/2fa/regenerate"><button>生成/重置 2FA 密钥</button></form></section>
        """
        self.send_bytes(page("设置", body))

    def save_settings(self) -> None:
        data = self.form()
        values = {
            "site_name": data.get("site_name", "").strip() or "PayPanel Alipay",
            "panel_domain": host_name(data.get("panel_domain", "")),
            "enforce_panel_domain": "1" if data.get("enforce_panel_domain") == "1" else "0",
            "callback_base_url": normalize_base_url(data.get("callback_base_url", "")),
            "enable_account_rotation": "1" if data.get("enable_account_rotation") == "1" else "0",
            "enable_polling": "1" if data.get("enable_polling") == "1" else "0",
            "poll_interval_seconds": str(bounded_int(data.get("poll_interval_seconds"), 8, 3)),
            "poll_timeout_minutes": str(bounded_int(data.get("poll_timeout_minutes"), 30, 1)),
            "order_timeout_minutes": str(bounded_int(data.get("order_timeout_minutes"), 30, 0)),
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

def account_columns() -> list[str]:
    columns = [
        "name", "app_id", "gateway", "merchant_private_key", "alipay_public_key",
        "app_cert_sn", "alipay_root_cert_sn", "notify_url", "return_url", "pay_types",
        "precreate_product_code", "page_product_code", "wap_product_code",
    ]
    for business in PAY_TYPE_ORDER:
        prefix = business + "_"
        columns.extend([
            prefix + "app_id", prefix + "app_public_key", prefix + "merchant_private_key",
            prefix + "alipay_public_key", prefix + "gateway", prefix + "notify_url",
        ])
        if business in {"wap", "page"}:
            columns.extend([prefix + "return_url", prefix + "app_cert_sn", prefix + "alipay_root_cert_sn"])
    return columns


def encrypted_account_values(values: dict[str, str], existing: Any | None = None) -> dict[str, str]:
    encrypted = dict(values)
    secret_columns = ["merchant_private_key", "alipay_public_key"]
    for business in PAY_TYPE_ORDER:
        secret_columns.extend([f"{business}_merchant_private_key", f"{business}_alipay_public_key"])
    for column in secret_columns:
        if encrypted.get(column):
            encrypted[column] = encrypt_secret(encrypted[column])
        elif existing is not None:
            encrypted[column] = existing[column]
        else:
            encrypted[column] = ""
    return encrypted


def account_display_app_id(account: Any) -> str:
    for business in normalize_pay_types(account["pay_types"]):
        app_id = business_value(account, business, "app_id", "app_id")
        if app_id:
            return app_id
    return account["app_id"]


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
