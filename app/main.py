from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import pyotp
import qrcode
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alipay
from .alipay import AlipayAccount
from .auth import SESSION_COOKIE, make_session, read_session, require_login, verify_credentials
from .config import get_settings
from .db import all_rows, connect, execute, init_db, one, settings_map

app = FastAPI(title="PayPanel Alipay", version="0.1.0")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


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
        raise HTTPException(status_code=400, detail="金额格式不正确")
    if value <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于 0")
    return str(value)


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


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


async def polling_worker() -> None:
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
                await asyncio.to_thread(query_order, int(order["id"]))
        await asyncio.sleep(interval)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(polling_worker())


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    if read_session(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "settings": settings_map(), "error": ""})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), otp: str = Form("")) -> Response:
    if not verify_credentials(username, password, otp):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "settings": settings_map(), "error": "用户名、密码或验证码错误"},
            status_code=401,
        )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session(username), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    require_login(request)
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
    return templates.TemplateResponse("dashboard.html", {"request": request, "stats": stats, "recent": recent})


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request, q: str = "", status: str = "") -> HTMLResponse:
    require_login(request)
    clauses = []
    params: list[Any] = []
    if q:
        clauses.append("(out_trade_no LIKE ? OR subject LIKE ? OR trade_no LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = all_rows(f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT 200", tuple(params))
    return templates.TemplateResponse("orders.html", {"request": request, "orders": rows, "q": q, "status": status})


@app.get("/orders/new", response_class=HTMLResponse)
def new_order(request: Request) -> HTMLResponse:
    require_login(request)
    accounts = all_rows("SELECT id, name FROM accounts WHERE enabled = 1 ORDER BY name")
    return templates.TemplateResponse("new_order.html", {"request": request, "accounts": accounts})


@app.post("/orders")
def create_order(
    request: Request,
    amount: str = Form(...),
    subject: str = Form("支付宝收款"),
    pay_type: str = Form("precreate"),
    account_id: int = Form(0),
) -> Response:
    require_login(request)
    amount = require_amount(amount)
    out_trade_no = datetime.now().strftime("PP%Y%m%d%H%M%S") + uuid4().hex[:8].upper()
    order_id = execute(
        "INSERT INTO orders(out_trade_no, amount, subject, pay_type) VALUES(?, ?, ?, ?)",
        (out_trade_no, amount, subject.strip() or "支付宝收款", pay_type),
    )
    try:
        build_payment(order_id, account_id or None)
    except Exception as exc:
        execute("UPDATE orders SET status = 'FAILED', last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (str(exc), order_id))
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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
            else:
                method = "alipay.trade.page.pay" if pay_type == "page" else "alipay.trade.wap.pay"
                product_code = "FAST_INSTANT_TRADE_PAY" if pay_type == "page" else "QUICK_WAP_WAY"
                biz_content["product_code"] = product_code
                if pay_type == "wap":
                    biz_content["quit_url"] = get_settings().base_url
                pay_url = f"{get_settings().base_url}/orders/{order_id}/pay"
                qr_code = pay_url
            execute(
                """
                UPDATE orders SET account_id = ?, status = 'WAIT_BUYER_PAY', qr_code = ?, pay_url = ?,
                    last_error = '', raw_response = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (account.id, qr_code, pay_url, json.dumps({"account_id": account.id}, ensure_ascii=False), order_id),
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


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int) -> HTMLResponse:
    require_login(request)
    order = one("SELECT o.*, a.name account_name FROM orders o LEFT JOIN accounts a ON a.id = o.account_id WHERE o.id = ?", (order_id,))
    if not order:
        raise HTTPException(status_code=404)
    qr = qr_data_uri(order["qr_code"]) if order["qr_code"] else ""
    return templates.TemplateResponse("order_detail.html", {"request": request, "order": order, "qr": qr})


@app.post("/orders/{order_id}/query")
def query_order_action(request: Request, order_id: int) -> Response:
    require_login(request)
    query_order(order_id)
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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


@app.get("/orders/{order_id}/pay", response_class=HTMLResponse)
def pay_redirect(order_id: int) -> HTMLResponse:
    order = one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order or not order["account_id"]:
        raise HTTPException(status_code=404)
    account = row_to_account(one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],)))
    method = "alipay.trade.page.pay" if order["pay_type"] == "page" else "alipay.trade.wap.pay"
    biz_content = {"out_trade_no": order["out_trade_no"], "total_amount": order["amount"], "subject": order["subject"]}
    biz_content["product_code"] = "FAST_INSTANT_TRADE_PAY" if order["pay_type"] == "page" else "QUICK_WAP_WAY"
    if order["pay_type"] == "wap":
        biz_content["quit_url"] = get_settings().base_url
    return HTMLResponse(alipay.build_page_form(account, method, biz_content))


@app.api_route("/alipay/notify", methods=["GET", "POST"])
async def alipay_notify(request: Request) -> PlainTextResponse:
    form = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    out_trade_no = form.get("out_trade_no", "")
    order = one("SELECT * FROM orders WHERE out_trade_no = ?", (out_trade_no,))
    if not order or not order["account_id"]:
        return PlainTextResponse("fail")
    account = row_to_account(one("SELECT * FROM accounts WHERE id = ?", (order["account_id"],)))
    if not alipay.verify(form, account.alipay_public_key):
        return PlainTextResponse("fail")
    update_order_status(int(order["id"]), form, json.dumps(form, ensure_ascii=False))
    return PlainTextResponse("success")


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request) -> HTMLResponse:
    require_login(request)
    rows = all_rows("SELECT * FROM accounts ORDER BY enabled DESC, name")
    return templates.TemplateResponse("accounts.html", {"request": request, "accounts": rows, "default_notify_url": default_notify_url()})


@app.post("/accounts")
def save_account(
    request: Request,
    name: str = Form(...),
    app_id: str = Form(...),
    gateway: str = Form("https://openapi.alipay.com/gateway.do"),
    merchant_private_key: str = Form(...),
    alipay_public_key: str = Form(...),
    app_cert_sn: str = Form(""),
    alipay_root_cert_sn: str = Form(""),
    notify_url: str = Form(""),
    return_url: str = Form(""),
) -> Response:
    require_login(request)
    execute(
        """
        INSERT INTO accounts(name, app_id, gateway, merchant_private_key, alipay_public_key,
                             app_cert_sn, alipay_root_cert_sn, notify_url, return_url)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, app_id, gateway, merchant_private_key, alipay_public_key, app_cert_sn, alipay_root_cert_sn, notify_url, return_url),
    )
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/toggle")
def toggle_account(request: Request, account_id: int) -> Response:
    require_login(request)
    execute("UPDATE accounts SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (account_id,))
    return RedirectResponse("/accounts", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    require_login(request)
    panel_settings = settings_map()
    provisioning_uri = ""
    if panel_settings.get("totp_secret"):
        provisioning_uri = pyotp.totp.TOTP(panel_settings["totp_secret"]).provisioning_uri(
            name=get_settings().admin_username,
            issuer_name="PayPanel Alipay",
        )
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": panel_settings, "totp_qr": qr_data_uri(provisioning_uri) if provisioning_uri else ""},
    )


@app.post("/settings")
def save_settings(
    request: Request,
    enable_account_rotation: str = Form("0"),
    enable_polling: str = Form("0"),
    poll_interval_seconds: str = Form("8"),
    poll_timeout_minutes: str = Form("30"),
    enable_2fa: str = Form("0"),
) -> Response:
    require_login(request)
    new_values = {
        "enable_account_rotation": "1" if enable_account_rotation == "1" else "0",
        "enable_polling": "1" if enable_polling == "1" else "0",
        "poll_interval_seconds": str(max(3, int(poll_interval_seconds or "8"))),
        "poll_timeout_minutes": str(max(1, int(poll_timeout_minutes or "30"))),
        "enable_2fa": "1" if enable_2fa == "1" else "0",
    }
    with connect() as conn:
        for key, value in new_values.items():
            conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/2fa/regenerate")
def regenerate_2fa(request: Request) -> Response:
    require_login(request)
    execute("INSERT INTO settings(key, value) VALUES('totp_secret', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (pyotp.random_base32(),))
    return RedirectResponse("/settings", status_code=303)
