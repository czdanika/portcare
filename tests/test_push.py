"""Web Push: RFC 8291 encryption round-trip, VAPID JWT, subscriptions, API."""
import base64
import json
import os
import struct

import pytest
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(k, d):
    h = hmac.HMAC(k, hashes.SHA256())
    h.update(d)
    return h.finalize()


class FakeBrowser:
    """User agent side of RFC 8291 - decrypts what the server sends."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)

    def sub(self, endpoint="https://push.example.com/send/abc"):
        return {"endpoint": endpoint, "keys": {"p256dh": b64u(self.pub), "auth": b64u(self.auth)}}

    def decrypt(self, body: bytes) -> bytes:
        salt, rs, idlen = body[:16], struct.unpack("!L", body[16:20])[0], body[20]
        as_pub = body[21:21 + idlen]
        ct = body[21 + idlen:]
        assert rs == 4096
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        prk_key = _hmac(self.auth, shared)
        ikm = _hmac(prk_key, b"WebPush: info\x00" + self.pub + as_pub + b"\x01")
        prk = _hmac(salt, ikm)
        cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        pt = AESGCM(cek).decrypt(nonce, ct, None)
        assert pt.endswith(b"\x02")
        return pt[:-1]


def test_encrypt_roundtrip(tmp_state):
    from app import push
    br = FakeBrowser()
    msg = json.dumps({"title": "PortCare", "body": "árvíztűrő"}).encode()
    body = push.encrypt(msg, b64u(br.pub), b64u(br.auth))
    assert br.decrypt(body) == msg


def test_vapid_header_is_valid_es256(tmp_state):
    from app import push
    hdr = push._vapid_header("https://web.push.apple.com/abc/def")
    t_part, k_part = hdr[len("vapid t="):].split(", k=")
    head, claims, sig = t_part.split(".")
    c = json.loads(b64d(claims))
    assert c["aud"] == "https://web.push.apple.com" and c["sub"].startswith("mailto:")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(k_part))
    raw = b64d(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if bad
    assert k_part == push.vapid_public_key()


def test_subscribe_validation(tmp_state):
    from app import push
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "http://insecure", "keys": {"p256dh": "x", "auth": "y"}})
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "https://p.example/x", "keys": {"p256dh": b64u(b"garbage"), "auth": "y"}})
    br = FakeBrowser()
    push.subscribe(br.sub())
    push.subscribe(br.sub())  # same endpoint replaces
    assert len(push.load()["subscriptions"]) == 1


def test_send_delivers_and_prunes_dead(tmp_state, monkeypatch):
    from app import push
    alive, dead = FakeBrowser(), FakeBrowser()
    push.subscribe(alive.sub("https://push.example.com/alive"))
    push.subscribe(dead.sub("https://push.example.com/dead"))
    got = {}

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, ""

    def fake_post(url, data=None, headers=None, timeout=None):
        assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith("vapid t=")
        if url.endswith("/dead"):
            return R(410)
        got["payload"] = json.loads(alive.decrypt(data))
        return R(201)
    monkeypatch.setattr(push.requests, "post", fake_post)
    n = push.send("update_done", "PortCare: frissítés kész", "2 stack", url="./#history")
    assert n == 1 and got["payload"]["title"] == "PortCare: frissítés kész"
    subs = push.load()["subscriptions"]
    assert [s["endpoint"] for s in subs] == ["https://push.example.com/alive"]  # 410 removed
    # disabled event -> nothing sent
    push.set_events({"update_done": False})
    assert push.send("update_done", "x", "y") == 0


def test_api(api_client, monkeypatch):
    cfg = api_client.get("/api/notify/config").get_json()
    assert len(b64d(cfg["vapid_public_key"])) == 65 and cfg["devices"] == 0
    br = FakeBrowser()
    r = api_client.post("/api/notify/subscribe", json={"subscription": br.sub()})
    assert r.status_code == 200 and r.get_json()["this_device"] is True
    r = api_client.put("/api/notify/events", json={"events": {"updates_available": False}})
    assert r.get_json()["events"]["updates_available"] is False
    from app import push
    monkeypatch.setattr(push.requests, "post", lambda *a, **k: type("R", (), {"status_code": 201, "text": ""})())
    r = api_client.post("/api/notify/test", json={"delay": 0})
    assert r.get_json() == {"sent": 1, "devices": 1}
    r = api_client.post("/api/notify/unsubscribe", json={"endpoint": br.sub()["endpoint"]})
    assert r.get_json()["devices"] == 0


def test_static_pwa_files(api_client):
    r = api_client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.content_type
    assert r.headers["Cache-Control"] == "no-cache"
    r = api_client.get("/manifest.webmanifest")
    assert r.status_code == 200 and json.loads(r.data)["name"] == "PortCare"
    assert api_client.get("/icons/apple-touch-icon.png").status_code == 200


def test_public_url_and_contact(api_client):
    from app import config as cfg, push
    assert push._contact() == "mailto:portcare@users.noreply.github.com"
    assert push.app_url("#stacks") == "./#stacks"
    r = api_client.post("/api/settings", json={"public_url": "https://portcare.example.hu/"})
    assert r.status_code == 200
    assert push._contact() == "https://portcare.example.hu"
    assert push.app_url("#stacks") == "https://portcare.example.hu/#stacks"
    r = api_client.post("/api/settings", json={"notify_contact": "daniel@example.hu"})
    assert r.status_code == 200 and push._contact() == "mailto:daniel@example.hu"
    assert api_client.post("/api/settings", json={"notify_contact": "https://x.hu/path"}).status_code == 400
    assert api_client.post("/api/settings", json={"public_url": "javascript:alert(1)"}).status_code == 400
    cfgj = api_client.get("/api/notify/config").get_json()
    assert cfgj["public_url"] == "https://portcare.example.hu" and cfgj["contact"] == "mailto:daniel@example.hu"
