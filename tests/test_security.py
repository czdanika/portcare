"""Security hardening: mandatory login, CSRF, encrypted secrets, TLS pinning,
schedule 'off' mode and update notifications."""
import datetime
import importlib
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml


# ------------------------------------------------------------------- login
@pytest.fixture()
def anon(tmp_state):
    from app.main import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.environ_base["HTTP_X_PUS_REQUEST"] = "1"
        yield c


class TestLogin:
    def test_setup_required_without_password(self, anon):
        r = anon.get("/api/status")
        assert r.status_code == 401 and r.get_json()["setup_required"] is True
        assert anon.get("/api/auth/status").get_json()["setup_required"] is True

    def test_setup_then_logged_in(self, anon):
        assert anon.post("/api/auth/setup", json={"password": "short"}).status_code == 400
        r = anon.post("/api/auth/setup", json={"password": "a-long-password"})
        assert r.status_code == 200
        assert anon.get("/api/status").status_code == 200
        # second setup is refused
        assert anon.post("/api/auth/setup", json={"password": "another-pass"}).status_code == 409
        # the hash (never the password) is on disk
        from app.config import CONFIG_FILE
        text = CONFIG_FILE.read_text()
        assert "a-long-password" not in text and "auth_password_hash" in text

    def test_login_logout(self, anon):
        anon.post("/api/auth/setup", json={"password": "a-long-password"})
        anon.post("/api/auth/logout")
        assert anon.get("/api/status").status_code == 401
        assert anon.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert anon.post("/api/auth/login", json={"password": "a-long-password"}).status_code == 200
        assert anon.get("/api/status").status_code == 200

    def test_rate_limit(self, anon, monkeypatch):
        monkeypatch.setattr("app.main.time.sleep", lambda s: None)
        anon.post("/api/auth/setup", json={"password": "a-long-password"})
        anon.post("/api/auth/logout")
        for _ in range(5):
            anon.post("/api/auth/login", json={"password": "wrong"})
        r = anon.post("/api/auth/login", json={"password": "a-long-password"})
        assert r.status_code == 429  # locked out even with the right password

    def test_csrf_header_required(self, anon):
        anon.post("/api/auth/setup", json={"password": "a-long-password"})
        del anon.environ_base["HTTP_X_PUS_REQUEST"]
        assert anon.post("/api/update").status_code == 403
        assert anon.post("/api/update", headers={"Origin": "https://evil.example",
                                                 "X-PUS-Request": "1"}).status_code == 403

    def test_password_change_invalidates_other_sessions(self, tmp_state):
        from app.main import app
        app.config["TESTING"] = True
        a, b = app.test_client(), app.test_client()
        for c in (a, b):
            c.environ_base["HTTP_X_PUS_REQUEST"] = "1"
        a.post("/api/auth/setup", json={"password": "first-password"})
        b.post("/api/auth/login", json={"password": "first-password"})
        assert b.get("/api/status").status_code == 200
        r = a.post("/api/auth/password", json={"current": "first-password",
                                               "new": "second-password"})
        assert r.status_code == 200
        assert a.get("/api/status").status_code == 200   # this browser stays in
        assert b.get("/api/status").status_code == 401   # the other one is out

    def test_env_password_is_initial_then_ui_owned(self, api_client, monkeypatch):
        # env seeds the first password; afterwards the UI can change it and
        # the (unchanged) env var no longer applies
        r = api_client.post("/api/auth/password", json={
            "current": "test-password-123", "new": "something-else"})
        assert r.status_code == 200
        from app import auth, config as cfg
        cfg.load(force=True)
        assert auth.check_password("something-else")
        assert not auth.check_password("test-password-123")

    def test_settings_never_expose_secrets(self, api_client):
        body = api_client.get("/api/settings").get_json()
        assert "auth_token" not in body and "auth_password_hash" not in body
        assert body["password_from_env"] is False


# ---------------------------------------------------------- encryption
class TestEncryption:
    def test_api_key_encrypted_on_disk(self, tmp_state):
        from app import config as cfg
        cfg.load(force=True)
        cfg.set("portainer_api_key", "ptr_supersecret")
        cfg.set("notify_webhook", "https://ha.example/api/webhook/abc")
        cfg.save()
        raw = yaml.safe_load(cfg.CONFIG_FILE.read_text())
        assert raw["portainer_api_key"].startswith("enc:v1:")
        assert raw["notify_webhook"].startswith("enc:v1:")
        assert "ptr_supersecret" not in cfg.CONFIG_FILE.read_text()
        assert cfg.load(force=True)["portainer_api_key"] == "ptr_supersecret"
        assert (cfg.CONFIG_FILE.stat().st_mode & 0o777) == 0o600

    def test_plaintext_is_migrated(self, tmp_state):
        from app import config as cfg
        cfg.CONFIG_FILE.write_text(yaml.safe_dump({"portainer_api_key": "ptr_plain"}))
        assert cfg.load(force=True)["portainer_api_key"] == "ptr_plain"
        assert "ptr_plain" not in cfg.CONFIG_FILE.read_text()

    def test_legacy_auth_token_becomes_hash(self, tmp_state):
        from app import config as cfg
        cfg.CONFIG_FILE.write_text(yaml.safe_dump({"auth_token": "legacy-secret"}))
        cfg.load(force=True)
        text = cfg.CONFIG_FILE.read_text()
        assert "legacy-secret" not in text and "auth_password_hash" in text
        from app import auth
        assert auth.check_password("legacy-secret")

    def test_env_values_never_written(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_PORTAINER_API_KEY", "ptr_from_env")
        monkeypatch.setenv("PUS_AUTH_TOKEN", "env-password")
        cfg.load(force=True)
        cfg.set("max_parallel_deploys", 4)
        cfg.save()
        text = cfg.CONFIG_FILE.read_text()
        assert "ptr_from_env" not in text and "env-password" not in text
        assert cfg.from_env_default("portainer_api_key")
        assert not cfg.env_locked("portainer_api_key")

    def test_ui_value_wins_over_env_after_save(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_MAX_PARALLEL_DEPLOYS", "2")
        assert cfg.load(force=True)["max_parallel_deploys"] == 2   # env = initial default
        cfg.set("max_parallel_deploys", 5)
        cfg.save()
        assert cfg.load(force=True)["max_parallel_deploys"] == 5   # UI owns it now

    def test_wrong_key_reports_error(self, tmp_state, monkeypatch):
        from app import config as cfg, crypto
        cfg.load(force=True)
        cfg.set("portainer_api_key", "ptr_x")
        cfg.save()
        crypto.reset_cache()
        monkeypatch.setenv("PUS_SECRET_KEY", "a-different-key")
        loaded = cfg.load(force=True)
        assert loaded["portainer_api_key"] == ""
        assert "cannot be decrypted" in cfg.decrypt_error


# ------------------------------------------------------------ scheduler
class TestScheduleOff:
    def test_off_mode(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_UPDATE_SCHEDULE_MODE", "off")
        cfg.load(force=True)
        import app.scheduler as sch
        importlib.reload(sch)
        assert sch._next_full_from(1_700_000_000) is None
        s = sch.Scheduler()
        s._bump_full()
        assert s.next_full_update is None
        assert "off" in sch._schedule_desc()

    def test_validate_off(self):
        from app.config import validate
        assert validate("update_schedule_mode", "OFF") == "off"

    def test_update_notification_dedup(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_NOTIFY_WEBHOOK", "http://hook.local/x")
        monkeypatch.setenv("PUS_NOTIFY_ON_UPDATES", "true")
        cfg.load(force=True)
        import app.scheduler as sch
        importlib.reload(sch)
        sent = []
        monkeypatch.setattr("requests.post", lambda url, **kw: sent.append(kw["json"]))
        res = {"stacks": {"web": {"nginx": {"update_available": True, "image": "nginx:1.25",
                                            "latest_tag": "1.27"}}}}
        s = sch.Scheduler()
        s._notify_updates(res)
        s._notify_updates(res)  # same set -> no second message
        assert len(sent) == 1 and sent[0]["count"] == 1
        assert sent[0]["updates"][0]["latest"] == "1.27"


# ---------------------------------------------------------------- TLS
@pytest.fixture(scope="module")
def https_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(crt), "-days", "2",
                    "-subj", "/CN=portainer"], check=True, capture_output=True)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"Version": "2.21.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"https://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class TestTls:
    def test_fetch_and_pin(self, tmp_state, https_server):
        from app.portainer import Portainer, PortainerError, fetch_certificate
        info = fetch_certificate(https_server)
        assert len(info["fingerprint"]) == 64 and info["self_signed"]
        ok = Portainer(https_server, "k", tls_fingerprint=info["fingerprint"])
        assert ok.status()["Version"] == "2.21.0"
        assert ok.tls_mode == "pinned"
        bad = Portainer(https_server, "k", tls_fingerprint="0" * 64)
        with pytest.raises(PortainerError, match="pinned fingerprint"):
            bad.status()

    def test_verify_true_without_ca_really_verifies(self, tmp_state, https_server):
        # upstream bug: verify=None silently disabled verification
        from app.portainer import Portainer, PortainerError
        p = Portainer(https_server, "k", tls_verify=True, tls_fingerprint="")
        assert p.session.verify is True
        with pytest.raises(PortainerError):
            p.status()

    def test_fingerprint_validation(self):
        from app.config import ConfigError, validate
        fp = "AB:" * 31 + "AB"
        assert validate("tls_fingerprint", fp) == "ab" * 32
        assert validate("tls_fingerprint", "") == ""
        with pytest.raises(ConfigError):
            validate("tls_fingerprint", "abcd")

    def test_inspect_endpoint(self, api_client, https_server):
        r = api_client.post("/api/tls/inspect", json={"portainer_url": https_server})
        assert r.status_code == 200 and len(r.get_json()["fingerprint"]) == 64


class TestPasswordRecovery:
    def test_reset_flag_replaces_saved_password(self, tmp_state, monkeypatch):
        from app import auth, config as cfg
        cfg.load(force=True)
        auth.set_password("forgotten-one")
        monkeypatch.setenv("PUS_AUTH_TOKEN", "new-secret-123")
        cfg.load(force=True)
        assert auth.check_password("forgotten-one")          # env alone does not override
        monkeypatch.setenv("PUS_RESET_PASSWORD", "true")
        cfg.load(force=True)
        assert auth.check_password("new-secret-123")
        assert auth.check_password("  new-secret-123\n")     # pasted whitespace ignored
        assert not auth.check_password("forgotten-one")


class TestProxyOrigin:
    def test_https_origin_behind_tls_proxy(self, anon):
        # Cloudflare Tunnel: browser on https, app reached over http, Host kept
        r = anon.post("/api/auth/setup", json={"password": "a-long-password"},
                      headers={"Origin": "https://portcare.example.com",
                               "Host": "portcare.example.com"})
        assert r.status_code == 200

    def test_forwarded_host_accepted(self, anon):
        r = anon.post("/api/auth/setup", json={"password": "a-long-password"},
                      headers={"Origin": "https://portcare.example.com",
                               "X-Forwarded-Host": "portcare.example.com"})
        assert r.status_code == 200

    def test_public_url_accepted_and_foreign_rejected(self, anon):
        from app import config as cfg
        cfg.load(force=True)
        cfg.set("public_url", "https://portcare.example.com")
        r = anon.post("/api/auth/setup", json={"password": "a-long-password"},
                      headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = anon.post("/api/auth/setup", json={"password": "a-long-password"},
                      headers={"Origin": "https://portcare.example.com"})
        assert r.status_code == 200
