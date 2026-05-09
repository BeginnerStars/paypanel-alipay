from __future__ import annotations

import base64
import html
import json
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


def _normalize_key(key: str, kind: str) -> str:
    key = key.strip().replace("\\n", "\n")
    if "BEGIN" in key:
        return key
    body = "\n".join(key[i : i + 64] for i in range(0, len(key), 64))
    return f"-----BEGIN {kind} KEY-----\n{body}\n-----END {kind} KEY-----\n"


def _run_openssl(args: list[str], data: bytes = b"") -> bytes:
    proc = subprocess.run(["openssl", *args], input=data, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="ignore") or "openssl 调用失败")
    return proc.stdout


def canonical(params: dict[str, Any]) -> str:
    cleaned = {
        key: str(value)
        for key, value in params.items()
        if key != "sign" and value is not None and str(value) != ""
    }
    return "&".join(f"{key}={cleaned[key]}" for key in sorted(cleaned))


def sign_content(content: str, private_key: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "private.pem"
        sig_path = Path(tmp) / "sign.bin"
        key_path.write_text(_normalize_key(private_key, "PRIVATE"))
        _run_openssl(["dgst", "-sha256", "-sign", str(key_path), "-out", str(sig_path)], content.encode())
        return base64.b64encode(sig_path.read_bytes()).decode()


def verify_content(content: str, signature: str, public_key: str) -> bool:
    try:
        decoded_signature = base64.b64decode(signature)
    except Exception:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "public.pem"
        sig_path = Path(tmp) / "sign.bin"
        key_path.write_text(_normalize_key(public_key, "PUBLIC"))
        sig_path.write_bytes(decoded_signature)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(key_path), "-signature", str(sig_path)],
            input=content.encode(),
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0


def response_sign_content(payload: str, response_key: str) -> str:
    marker = f'"{response_key}"'
    key_index = payload.find(marker)
    if key_index < 0:
        raise RuntimeError("支付宝响应中缺少响应节点，无法验签")
    colon_index = payload.find(":", key_index + len(marker))
    if colon_index < 0:
        raise RuntimeError("支付宝响应格式异常，无法验签")
    start = colon_index + 1
    while start < len(payload) and payload[start].isspace():
        start += 1
    decoder = json.JSONDecoder()
    _, offset = decoder.raw_decode(payload[start:])
    end = start + offset
    return payload[start:end]


def sign(params: dict[str, Any], private_key: str) -> str:
    return sign_content(canonical(params), private_key)


def verify(params: dict[str, Any], public_key: str) -> bool:
    if "sign" not in params:
        return False
    return verify_content(canonical(params), str(params["sign"]), public_key)


def common_params(
    account: AlipayAccount,
    method: str,
    biz_content: dict[str, Any],
    extra_params: dict[str, Any] | None = None,
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
    account: AlipayAccount,
    method: str,
    biz_content: dict[str, Any],
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = common_params(account, method, biz_content, extra_params)
    params["sign"] = sign(params, account.merchant_private_key)
    return params


def request_api(account: AlipayAccount, method: str, biz_content: dict[str, Any]) -> dict[str, Any]:
    extra_params = {"notify_url": account.notify_url} if method != "alipay.trade.query" else None
    params = build_signed_params(account, method, biz_content, extra_params)
    req = urllib.request.Request(
        account.gateway,
        data=urllib.parse.urlencode(params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = resp.read().decode()
    data = json.loads(payload)
    response_key = method.replace(".", "_") + "_response"
    response = data.get(response_key, {})
    if data.get("sign"):
        content = response_sign_content(payload, response_key)
        if not verify_content(content, str(data["sign"]), account.alipay_public_key):
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
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<body onload=\"document.forms[0].submit()\">"
        f'<form method="post" action="{html.escape(account.gateway, quote=True)}">{inputs}'
        '<noscript><button type="submit">继续支付</button></noscript></form></body></html>'
    )
