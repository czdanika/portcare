"""Web Push notifications (VAPID + RFC 8291 aes128gcm) - no extra dependency.

Works on iPhone/iPad (from the Home-Screen-installed app, iOS 16.4+), Android
and desktop browsers. Browsers only allow push on HTTPS (secure context).

State (DATA_DIR, i.e. the pus-data volume):
  keys/vapid_private.pem   server VAPID key (generated on first use, 0600)
  push.json                {"events": {...}, "subscriptions": [...], "last_send": {...}}

Every device (phone, tablet, browser) is its own subscription. Expired
subscriptions (HTTP 404/410 from the push service) are removed automatically.
"""
import base64
import json
import os
import struct
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

EVENTS = ("updates_available", "update_done", "update_failed")
DEFAULT_EVENTS = {"updates_available": True, "update_done": True, "update_failed": True}
RECORD_SIZE = 4096

_lock = threading.Lock()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _paths():
    from .config import DATA_DIR
    keys = DATA_DIR / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    return keys / "vapid_private.pem", DATA_DIR / "push.json"


def _contact() -> str:
    c = (os.environ.get("PUS_NOTIFY_CONTACT") or "").strip()
    if c and "@" in c and not c.startswith("mailto:"):
        c = "mailto:" + c
    if c.startswith("mailto:") or c.startswith("https://"):
        return c.rstrip("/")
    return "mailto:portcare@users.noreply.github.com"


# ------------------------------------------------------------------- VAPID
def _vapid_key() -> ec.EllipticCurvePrivateKey:
    priv_path, _ = _paths()
    with _lock:
        if not priv_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        return serialization.load_pem_private_key(priv_path.read_bytes(), None)


def vapid_public_key() -> str:
    """applicationServerKey for the browser (base64url, uncompressed P-256)."""
    raw = _vapid_key().public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64u(raw)


def _vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
              "sub": _contact()}
    signing_input = (_b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
                     + "." + _b64u(json.dumps(claims, separators=(",", ":")).encode()))
    der = _vapid_key().sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = signing_input + "." + _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={vapid_public_key()}"


# -------------------------------------------------------------- encryption
def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            as_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 / RFC 8188 aes128gcm message body (single record)."""
    ua_pub_raw = _b64u_dec(p256dh)
    auth_secret = _b64u_dec(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_pub_raw = as_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ua_pub)
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_pub_raw + as_pub_raw + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("push payload too large")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!L", RECORD_SIZE) + bytes([len(as_pub_raw)]) + as_pub_raw
    return header + ciphertext


# ------------------------------------------------------------------- state
def load() -> dict:
    _, path = _paths()
    data = {"events": dict(DEFAULT_EVENTS), "subscriptions": [], "last_send": None}
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) or {}
        data["events"].update({k: bool(v) for k, v in (stored.get("events") or {}).items()
                               if k in EVENTS})
        data["subscriptions"] = stored.get("subscriptions") or []
        data["last_send"] = stored.get("last_send")
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    _, path = _paths()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def subscribe(sub: dict, user_agent: str = "") -> dict:
    endpoint = (sub or {}).get("endpoint") or ""
    keys = (sub or {}).get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("incomplete push subscription")
    try:  # reject garbage early (must be a valid P-256 point)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64u_dec(keys["p256dh"]))
    except (ValueError, TypeError) as e:
        raise ValueError("invalid p256dh key") from e
    with _lock:
        data = load()
        data["subscriptions"] = [s for s in data["subscriptions"] if s.get("endpoint") != endpoint]
        data["subscriptions"].append({
            "endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
            "ua": (user_agent or "")[:160],
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        data["subscriptions"] = data["subscriptions"][-20:]  # sanity cap
        save(data)
    return data


def unsubscribe(endpoint: str) -> dict:
    with _lock:
        data = load()
        data["subscriptions"] = [s for s in data["subscriptions"] if s.get("endpoint") != endpoint]
        save(data)
    return data


def set_events(events: dict) -> dict:
    with _lock:
        data = load()
        for k, v in (events or {}).items():
            if k in EVENTS:
                data["events"][k] = bool(v)
        save(data)
    return data


# -------------------------------------------------------------------- send
def send(event: str, title: str, body: str, url: str = "./", tag: str | None = None,
         force: bool = False) -> int:
    """Send to every subscribed device. Returns the number of deliveries."""
    data = load()
    if not force and not data["events"].get(event, False):
        return 0
    if not data["subscriptions"]:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or event},
                         ensure_ascii=False).encode("utf-8")
    ok, dead, results = 0, [], []
    for sub in data["subscriptions"]:
        host = urlparse(sub["endpoint"]).netloc
        try:
            body_bytes = encrypt(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
            r = requests.post(sub["endpoint"], data=body_bytes, timeout=15, headers={
                "Authorization": _vapid_header(sub["endpoint"]),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(24 * 3600),
                "Urgency": "normal",
            })
            if r.status_code < 300:
                ok += 1
                results.append({"host": host, "status": r.status_code, "ok": True})
            else:
                results.append({"host": host, "status": r.status_code, "ok": False,
                                "reason": (r.text or "")[:160]})
                if r.status_code in (404, 410):
                    dead.append(sub["endpoint"])
        except Exception as e:  # noqa: BLE001 - one bad device must not stop the rest
            results.append({"host": host, "status": None, "ok": False, "reason": str(e)[:160]})
    with _lock:
        d = load()
        if dead:
            d["subscriptions"] = [s for s in d["subscriptions"] if s.get("endpoint") not in dead]
        d["last_send"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "event": event, "results": results}
        save(d)
    return ok


def send_async(*args, **kwargs) -> None:
    threading.Thread(target=lambda: _safe_send(*args, **kwargs), daemon=True).start()


def _safe_send(*args, **kwargs):
    try:
        send(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"[push] send failed: {e}", file=sys.stderr, flush=True)
