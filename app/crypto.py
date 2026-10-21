"""At-rest encryption for secrets in config.yaml + the session signing key.

Secrets (Portainer API key, notification webhook URL) are written to
config.yaml as ``enc:v1:<fernet-token>``. The Fernet key lives in a
DIFFERENT volume (DATA_DIR/keys/) than the config file (CONFIG_DIR), so a
leaked or backed-up config.yaml alone does not reveal the API key.

Key resolution order:
  1. PUS_SECRET_KEY env var (any string - stretched to a Fernet key)
  2. DATA_DIR/keys/secret.key (auto-generated on first start, chmod 600)

Plaintext values found in an existing config.yaml are accepted on load and
re-written encrypted on the next save (transparent migration).
"""
import base64
import hashlib
import os
import secrets as _secrets
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
SECRET_KEYS = ("portainer_api_key", "notify_webhook")

_lock = threading.Lock()
_fernet = None
_session_key = None


class DecryptError(RuntimeError):
    pass


def _keys_dir() -> Path:
    from .config import DATA_DIR
    d = DATA_DIR / "keys"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _read_or_create(path: Path, nbytes: int) -> bytes:
    if path.exists():
        data = path.read_bytes().strip()
        if data:
            return data
    raw = base64.urlsafe_b64encode(_secrets.token_bytes(nbytes))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    tmp.replace(path)
    return raw


def _get_fernet() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is not None:
            return _fernet
        env = os.environ.get("PUS_SECRET_KEY", "")
        if env:
            key = base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())
        else:
            key = _read_or_create(_keys_dir() / "secret.key", 32)
        _fernet = Fernet(key)
        return _fernet


def session_key() -> bytes:
    """Flask session signing key (separate file from the Fernet key)."""
    global _session_key
    with _lock:
        if _session_key is None:
            _session_key = _read_or_create(_keys_dir() / "session.key", 48)
        return _session_key


def reset_cache() -> None:
    """Tests: forget cached keys (DATA_DIR changed)."""
    global _fernet, _session_key
    with _lock:
        _fernet = None
        _session_key = None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: str) -> str:
    if not value or is_encrypted(value):
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value):
    if not is_encrypted(value):
        return value
    try:
        return _get_fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise DecryptError(
            "config.yaml contains an encrypted secret that cannot be decrypted "
            "with the current key (data volume replaced or PUS_SECRET_KEY "
            "changed). Re-enter the Portainer API key in Settings.") from e
