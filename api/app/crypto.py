"""Symmetric encryption for camera credentials at rest.

POC note: key is derived from an env var for local demo purposes. In
production this must come from a KMS/secrets manager, never a container env
var, per the plan's security section (R23 / §25.10).
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet

_SECRET = os.environ.get("CAMERA_SECRET_KEY", "vi-poc-dev-key-change-me")
_key = base64.urlsafe_b64encode(hashlib.sha256(_SECRET.encode()).digest())
_fernet = Fernet(_key)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
