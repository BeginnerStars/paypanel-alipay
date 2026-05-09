from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .config import get_settings

PREFIX = "enc:v1:"
NONCE_SIZE = 16
TAG_SIZE = 32


def _key() -> bytes:
    return hashlib.sha256(get_settings().secret_key.encode()).digest()


def _stream(length: int, nonce: bytes) -> bytes:
    key = _key()
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


def encrypt_secret(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    plaintext = value.encode()
    nonce = secrets.token_bytes(NONCE_SIZE)
    stream = _stream(len(plaintext), nonce)
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(_key(), nonce + ciphertext, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + tag + ciphertext).decode()


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    raw = base64.urlsafe_b64decode(value[len(PREFIX) :].encode())
    if len(raw) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("加密密钥数据格式异常")
    nonce = raw[:NONCE_SIZE]
    tag = raw[NONCE_SIZE : NONCE_SIZE + TAG_SIZE]
    ciphertext = raw[NONCE_SIZE + TAG_SIZE :]
    expected = hmac.new(_key(), nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("加密密钥校验失败，请确认 APP_SECRET_KEY 未变更")
    stream = _stream(len(ciphertext), nonce)
    return bytes(a ^ b for a, b in zip(ciphertext, stream)).decode()
