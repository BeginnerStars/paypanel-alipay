#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.alipay import AlipayAccount, post_api, sign, verify


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    app_id = env("ALIPAY_APP_ID")
    merchant_private_key = env("ALIPAY_MERCHANT_PRIVATE_KEY")
    alipay_public_key = env("ALIPAY_PUBLIC_KEY")
    app_public_key = os.environ.get("ALIPAY_APP_PUBLIC_KEY", "").strip()
    gateway = os.environ.get("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do").strip()

    params = {
        "app_id": app_id,
        "method": "alipay.trade.query",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": "2026-05-09 00:00:00",
        "version": "1.0",
        "biz_content": '{"out_trade_no":"PAYPANEL_LOCAL_SIGN_TEST"}',
    }
    params["sign"] = sign(params, merchant_private_key)
    if app_public_key:
        if not verify(params, app_public_key):
            print("Local RSA2 sign/verify failed: private key does not match app public key", file=sys.stderr)
            return 2
        print("Local RSA2 sign/verify: OK")
    else:
        print("Local RSA2 signing: OK (set ALIPAY_APP_PUBLIC_KEY to verify the pair)")

    account = AlipayAccount(
        id=0,
        app_id=app_id,
        gateway=gateway,
        merchant_private_key=merchant_private_key,
        alipay_public_key=alipay_public_key,
    )
    out_trade_no = "PAYPANEL_CHECK_" + datetime.now().strftime("%Y%m%d%H%M%S")
    response = post_api(account, "alipay.trade.query", {"out_trade_no": out_trade_no})
    code = response.get("code", "")
    sub_code = response.get("sub_code", "")
    message = response.get("sub_msg") or response.get("msg") or ""
    print(f"Gateway response: code={code} sub_code={sub_code} message={message}")
    if code == "10000":
        print("Gateway/API check: OK")
        return 0
    if code == "40004" and sub_code in {"ACQ.TRADE_NOT_EXIST", "TRADE_NOT_EXIST"}:
        print("Gateway/API check: OK (signed request accepted; test order does not exist as expected)")
        return 0
    print("Gateway/API check: returned an unexpected business error", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
