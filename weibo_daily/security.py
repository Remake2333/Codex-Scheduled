"""Encrypted Playwright storage state; secrets never enter execution records."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken


class StopRun(Exception):
    """A safe, credential-free reason suitable for reports."""


def public_url(value: str) -> str:
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise StopRun("invalid_public_url")
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def safe_error(value: str) -> str:
    value = re.sub(r"https?://[^\s<>\"']+", lambda m: public_url(m[0]) if "@" not in m[0] else "[url]", value)
    value = re.sub(r"(?i)(cookie|authorization|token|password|ticket|secret)\s*[:=]\s*\S+", r"\1=[REDACTED]", value)
    return value[:300]


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)


def encrypt_session(storage: dict, key: bytes, account: str) -> bytes:
    if not account.isdigit():
        raise StopRun("numeric_account_id_required")
    value = json.dumps({"version": 1, "account": account, "storage": storage}).encode()
    result = Fernet(key).encrypt(value)
    if len(result) > 47_000:
        raise StopRun("encrypted_session_exceeds_github_secret_limit")
    return result


def decrypt_session(blob: bytes, key: bytes, account: str) -> dict:
    try:
        value = json.loads(Fernet(key).decrypt(blob))
        if value.get("version") != 1 or value.get("account") != account:
            raise ValueError("account mismatch")
        storage = value["storage"]
        if not isinstance(storage.get("cookies"), list) or not isinstance(storage.get("origins"), list):
            raise ValueError("invalid state")
        return storage
    except (ValueError, KeyError, TypeError, InvalidToken):
        raise StopRun("invalid_or_mismatched_encrypted_session") from None
