from __future__ import annotations

import base64
import html
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


FINAL_STATUSES = {"TRADE_SUCCESS", "TRADE_FINISHED", "TRADE_CLOSED"}
SUCCESS_STATUSES = {"TRADE_SUCCESS", "TRADE_FINISHED"}


@dataclass
class AlipayAccount:
    id: int
    app_id: str
    gateway: str
    merchant_private_key: str
    alipay_public_key: str
    app_cert_sn: str = ""
    alipay_root_cert_sn: str = ""
    notify_url: str = ""
    return_url: str = ""


def _normalize_private_key(key: str) -> bytes:
    key = key.strip().replace("\\n", "\n")
    if "BEGIN" in key:
        return key.encode()
    body = "\n".join(key[i : i + 64] for i in range(0, len(key), 64))
    return f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n".encode()


def _normalize_public_key(key: str) -> bytes:
    key = key.strip().replace("\\n", "\n")
    if "BEGIN" in key:
        return key.encode()
    body = "\n".join(key[i : i + 64] for i in range(0, len(key), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----\n".encode()


def canonical(params: dict[str, Any]) -> str:
    cleaned = {
        key: str(value)
        for key, value in params.items()
        if key != "sign" and value is not None and str(value) != ""
    }
    return "&".join(f"{key}={cleaned[key]}" for key in sorted(cleaned))


def sign(params: dict[str, Any], private_key: str) -> str:
    key = serialization.load_pem_private_key(_normalize_private_key(private_key), password=None)
    signature = key.sign(canonical(params).encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def verify(params: dict[str, Any], public_key: str) -> bool:
    if "sign" not in params:
        return False
    key = serialization.load_pem_public_key(_normalize_public_key(public_key))
    signature = base64.b64decode(str(params["sign"]))
    try:
        key.verify(signature, canonical(params).encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def common_params(
    account: AlipayAccount, method: str, biz_content: dict[str, Any], extra_params: dict[str, Any] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "app_id": account.app_id,
        "method": method,
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "format": "JSON",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    if extra_params:
        params.update({key: value for key, value in extra_params.items() if value})
    if account.app_cert_sn:
        params["app_cert_sn"] = account.app_cert_sn
    if account.alipay_root_cert_sn:
        params["alipay_root_cert_sn"] = account.alipay_root_cert_sn
    return params


def build_signed_params(
    account: AlipayAccount, method: str, biz_content: dict[str, Any], extra_params: dict[str, Any] | None = None
) -> dict[str, Any]:
    params = common_params(account, method, biz_content, extra_params)
    params["sign"] = sign(params, account.merchant_private_key)
    return params


def request_api(account: AlipayAccount, method: str, biz_content: dict[str, Any]) -> dict[str, Any]:
    extra_params = {"notify_url": account.notify_url} if method != "alipay.trade.query" else None
    params = build_signed_params(account, method, biz_content, extra_params)
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        account.gateway,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = resp.read().decode()
    data = json.loads(payload)
    response_key = method.replace(".", "_") + "_response"
    response = data.get(response_key, {})
    if data.get("sign"):
        signed_payload = {response_key: json.dumps(response, ensure_ascii=False, separators=(",", ":")), "sign": data["sign"]}
        if not verify(signed_payload, account.alipay_public_key):
            raise RuntimeError("支付宝响应签名验证失败")
    if response.get("code") != "10000":
        message = response.get("sub_msg") or response.get("msg") or "支付宝接口请求失败"
        raise RuntimeError(message)
    return response


def build_page_form(account: AlipayAccount, method: str, biz_content: dict[str, Any]) -> str:
    params = build_signed_params(
        account, method, biz_content, {"notify_url": account.notify_url, "return_url": account.return_url}
    )
    inputs = "".join(
        f'<input type="hidden" name="{html.escape(str(k), quote=True)}" value="{html.escape(str(v), quote=True)}">'
        for k, v in params.items()
    )
    return (
        "<!doctype html><html><body onload=\"document.forms[0].submit()\">"
        f'<form method="post" action="{account.gateway}">{inputs}'
        '<noscript><button type="submit">继续支付</button></noscript></form></body></html>'
    )
