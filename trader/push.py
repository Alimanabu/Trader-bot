"""Push-уведомления на телефон и компьютер через Web Push (RFC 8291 + VAPID), без сторонних служб.

Устройство подписывается в браузере (PWA), подписка хранится в журнале. Сервер шифрует сообщение
ключом устройства и отправляет его через push-службу браузера (Google, Mozilla, Apple).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class VapidKeys:
    """Пара ключей сервера. private_pem хранится в журнале, public отдаётся браузеру."""

    def __init__(self, private_pem: str):
        from cryptography.hazmat.primitives import serialization
        self.private_pem = private_pem
        self.key = serialization.load_pem_private_key(private_pem.encode(), password=None)

    @classmethod
    def generate(cls) -> "VapidKeys":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        return cls(pem)

    def public_b64u(self) -> str:
        from cryptography.hazmat.primitives import serialization
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64u(raw)

    def jwt(self, audience: str, subject: str, ttl: int = 12 * 3600) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        header = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = _b64u(json.dumps({"aud": audience, "exp": int(time.time()) + ttl, "sub": subject}, separators=(",", ":")).encode())
        signing = f"{header}.{claims}".encode()
        der = self.key.sign(signing, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{header}.{claims}." + _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def encrypt(plaintext: bytes, p256dh: str, auth: str) -> bytes:
    """Шифрование aes128gcm по RFC 8291 для ключей подписки браузера."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_pub_raw = _b64u_dec(p256dh)
    auth_secret = _b64u_dec(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_pub_raw = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ua_pub)
    salt = os.urandom(16)
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=b"WebPush: info\x00" + ua_pub_raw + as_pub_raw).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    record = plaintext + b"\x02"
    ciphertext = AESGCM(cek).encrypt(nonce, record, None)
    header = salt + struct.pack("!I", 4096) + bytes([len(as_pub_raw)]) + as_pub_raw
    return header + ciphertext


class PushSender:
    def __init__(self, keys: VapidKeys, subject: str = "mailto:botz@example.com", http=None):
        self.keys = keys
        self.subject = subject
        self._http = http

    def send(self, subscription: dict, payload: dict, ttl: int = 3600) -> int:
        """Отправить одно уведомление. Возвращает HTTP-статус (404/410 = подписка мертва)."""
        import httpx
        endpoint = subscription["endpoint"]
        keys = subscription.get("keys") or {}
        body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), keys["p256dh"], keys["auth"])
        u = urlparse(endpoint)
        token = self.keys.jwt(f"{u.scheme}://{u.netloc}", self.subject)
        headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
                   "Urgency": "high", "Authorization": f"vapid t={token}, k={self.keys.public_b64u()}"}
        client = self._http or httpx.Client(timeout=15.0)
        try:
            r = client.post(endpoint, content=body, headers=headers)
            return r.status_code
        finally:
            if self._http is None:
                client.close()
