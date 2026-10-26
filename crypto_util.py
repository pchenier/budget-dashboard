"""Encryption at rest for third-party tokens (Plaid, Wise, brokers).

Fernet (AES-128-CBC + HMAC-SHA256). The key is TOKEN_ENC_KEY when set, otherwise
derived with HKDF from JWT_SECRET — the one secret both fiscit.com (Next.js) and
this app already share, so either side can write a token the other can read
(see vault-vercel/lib/token-crypto.ts, which must stay byte-compatible).

Stored values look like "enc:v1:<fernet token>". Anything without the prefix is
a legacy plaintext value and is returned as-is until the boot migration rewrites it.
"""
import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = 'enc:v1:'
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        explicit = os.getenv('TOKEN_ENC_KEY', '').strip()
        secret = explicit or os.getenv('JWT_SECRET') or os.getenv('SECRET_KEY') or 'vault-local-secret-do-not-deploy'
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'fiscit-token-encryption',
                   info=b'v1').derive(secret.encode())
        _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    if value is None or value == '' or is_encrypted(value):
        return value
    return PREFIX + _get_fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not is_encrypted(value):
        return value
    return _get_fernet().decrypt(value[len(PREFIX):].encode()).decode()
