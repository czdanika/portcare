"""Flask app: REST API + static frontend for the Portainer update service.

Hardened: mandatory login (session cookie or Bearer), CSRF guard, shared job
gate for all mutating routes, input validation, encrypted secrets at rest,
TLS certificate pinning, non-blocking /api/stacks, /healthz.
"""
import os
import re
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface

from . import auth, checker, compose as compose_mod, crypto, dockerhub, history, policies, push, updater
from . import config as config_mod
from .config import get, load, set as cfg_set, save as cfg_save, ConfigError, env_locked
from .history import RunLogger
from .jobs import gate
from .portainer import Portainer, PortainerError
from .scheduler import scheduler

app = Flask(__name__, static_folder=None)
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
app.config.update(
    SESSION_COOKIE_NAME="pus_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    # set PUS_COOKIE_SECURE=true when the UI is served over HTTPS only
    SESSION_COOKIE_SECURE=os.environ.get("PUS_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)


def _init_secret_key():
    if not app.secret_key:
        app.secret_key = crypto.session_key()


class _LazyKeySessionInterface(SecureCookieSessionInterface):
    """Loads the signing key from DATA_DIR on first use (not at import time,
    so tests and PUS_DATA_DIR relocation keep working)."""

    def get_signing_serializer(self, app_):
        _init_secret_key()
        return super().get_signing_serializer(app_)


app.session_interface = _LazyKeySessionInterface()


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ------------------------------------------------------------------- auth
MUTATING = {"POST", "PUT", "DELETE"}
PUBLIC_API = {"/api/auth/status", "/api/auth/login", "/api/auth/setup"}


def _client_ip() -> str:
    return request.remote_addr or "?"


def _session_valid() -> bool:
    return bool(session.get("auth")) and session.get("pv") == auth.password_version()


def _bearer_valid() -> bool:
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return False
    ip = _client_ip()
    if auth.lockout_remaining(ip):
        return False
    ok = auth.check_password(h[7:].strip())
    if ok:
        auth.record_success(ip)
    else:
        auth.record_failure(ip)
    return ok


def _same_origin() -> bool:
    """CSRF origin check. Compares HOST names only (not the scheme): behind a
    TLS-terminating proxy (Cloudflare Tunnel, Tailscale serve, nginx) the
    browser's Origin is https://… while the app itself is reached over http.
    Accepted hosts: the Host header, X-Forwarded-Host and the configured
    public_url."""
    origin = request.headers.get("Origin", "")
    if not origin or origin == "null":
        return not origin  # no Origin: same-origin GET/CLI; "null": reject
    from urllib.parse import urlparse
    o_host = (urlparse(origin).netloc or "").lower()
    allowed = {(request.host or "").lower()}
    for h in (request.headers.get("X-Forwarded-Host", "") or "").split(","):
        if h.strip():
            allowed.add(h.strip().lower())
    pub = get("public_url", "") or ""
    if pub:
        allowed.add((urlparse(pub).netloc or "").lower())
    # default ports: https://x == x:443, http://x == x:80
    def norm(h):
        return h[:-4] if h.endswith(":443") else h[:-3] if h.endswith(":80") else h
    return norm(o_host) in {norm(h) for h in allowed if h}


@app.before_request
def _auth_gate():
    """Every /api/* route requires a logged-in session (or a Bearer password).

    - static UI files + /healthz stay public (the UI renders the login form);
    - mutating requests need a same-origin Origin header AND the custom
      X-PUS-Request header (CSRF: cross-site forms cannot set it, and the
      SameSite=Strict cookie is not sent cross-site anyway);
    - no password configured yet -> 401 setup_required (first-run screen).
    """
    _init_secret_key()
    path = request.path
    if not path.startswith("/api/"):
        return None
    # CSRF: browsers cannot attach the custom X-PUS-Request header to a
    # cross-site request without a CORS preflight, which this app never
    # grants - so the header alone proves same-origin. The Origin host check
    # is the extra layer for header-less clients (it can't see the public
    # host when a proxy rewrites the Host header, e.g. some tunnels).
    if request.method in MUTATING and request.headers.get("X-PUS-Request") != "1" \
            and not _same_origin():
        return _err("cross-site request rejected", 403)
    if path in PUBLIC_API:
        return None
    if not auth.configured():
        return jsonify({"error": "setup required - create an admin password",
                        "setup_required": True}), 401
    if _session_valid():
        if request.method in MUTATING and request.headers.get("X-PUS-Request") != "1":
            return _err("missing X-PUS-Request header (CSRF protection)", 403)
        return None
    if _bearer_valid():
        return None
    return jsonify({"error": "login required", "login_required": True}), 401


def _login_session(remember: bool):
    session.clear()
    session["auth"] = True
    session["pv"] = auth.password_version()
    session["t"] = int(time.time())
    session.permanent = bool(remember)
    app.permanent_session_lifetime = timedelta(days=int(get("session_days", 30)))


@app.route("/api/auth/status")
def api_auth_status():
    return jsonify({
        "authenticated": auth.configured() and _session_valid(),
        "setup_required": not auth.configured(),
        "password_from_env": auth.password_from_env(),
        "min_password_len": auth.MIN_PASSWORD_LEN,
    })


@app.route("/api/auth/setup", methods=["POST"])
def api_auth_setup():
    """First run only: create the admin password and log in."""
    if auth.configured():
        return _err("already configured", 409)
    body = request.get_json(silent=True) or {}
    try:
        auth.set_password(str(body.get("password") or ""))
    except config_mod.ConfigError as e:
        return _err(str(e))
    _login_session(remember=True)
    return jsonify({"ok": True})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    if not auth.configured():
        return jsonify({"error": "setup required", "setup_required": True}), 401
    ip = _client_ip()
    wait = auth.lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"too many failed attempts - try again in {wait}s",
                        "retry_after": wait}), 429
    body = request.get_json(silent=True) or {}
    if not auth.check_password(str(body.get("password") or "")):
        auth.record_failure(ip)
        time.sleep(0.4)  # flatten timing + slow down online guessing
        return _err("wrong password", 401)
    auth.record_success(ip)
    _login_session(remember=bool(body.get("remember", True)))
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/password", methods=["POST"])
def api_auth_password():
    body = request.get_json(silent=True) or {}
    if not auth.check_password(str(body.get("current") or "")):
        return _err("current password is wrong", 403)
    try:
        auth.set_password(str(body.get("new") or ""))
    except config_mod.ConfigError as e:
        return _err(str(e))
    _login_session(remember=True)  # keep THIS browser logged in
    return jsonify({"ok": True})


def _client() -> Portainer:
    """Portainer client; raises ConfigError-free PortainerError on failure.
    Routes wanting a friendly 'not configured' message should check
    _configured() first."""
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)),
                       tls_ca_file=(get("tls_ca_file", "") or None))
    client.resolve_endpoint(socket.gethostname())
    return client


def _configured() -> bool:
    return bool(get("portainer_url") and get("portainer_api_key"))


def _client_or_error():
    """Returns (client, None) or (None, error_response)."""
    if not _configured():
        return None, _err("not configured - enter Portainer URL and API key in Settings", 400)
    try:
        return _client(), None
    except PortainerError as e:
        return None, _err(str(e), 502)


# ------------------------------------------------------------------- static UI
@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/sw.js")
def service_worker():
    """Service worker must come from the site root (scope "/") and must not
    be cached, otherwise app updates would not reach installed phones."""
    resp = send_from_directory(UI_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/<path:filename>")
def static_files(filename):
    resp = send_from_directory(UI_DIR, filename)
    if filename.endswith(".webmanifest"):
        resp.headers["Content-Type"] = "application/manifest+json"
    return resp


# ------------------------------------------------------------------- status
@app.route("/api/status")
def api_status():
    snap = checker.status_snapshot()
    snap["scheduler"] = {
        "busy": gate.is_busy(),
        "current": gate.current(),
        "interval_hours": get("update_interval_hours", 168),
        "schedule_mode": get("update_schedule_mode", "interval"),
        "schedule_time": get("update_schedule_time", "03:30"),
        "schedule_day": int(get("update_schedule_day", 0)),
        "schedule_desc": scheduler.schedule_desc(),
        "server_time_local": time.strftime("%a %d.%m. %H:%M"),
        "server_tz": time.strftime("%Z"),
        "next_check": scheduler.next_check,
        "next_full_update": scheduler.next_full_update,
        "consecutive_failures": scheduler.consecutive_failures,
        "last_tick": scheduler.last_tick,
        "now": time.time(),
    }
    snap["configured"] = _configured()
    snap["endpoint_configured"] = get("portainer_endpoint_id") is not None
    snap["auth_enabled"] = True
    snap["schedule_off"] = get("update_schedule_mode", "interval") == "off"
    snap["tls_mode"] = _tls_mode_now()
    snap["decrypt_error"] = config_mod.decrypt_error
    return jsonify(snap)


def _tls_mode_now() -> str:
    from .portainer import tls_mode
    url = get("portainer_url", "") or ""
    if url.startswith("http://"):
        return "http"
    return tls_mode(bool(get("tls_verify", False)), get("tls_ca_file", ""),
                    get("tls_fingerprint", ""))


@app.route("/api/tls/inspect", methods=["POST"])
def api_tls_inspect():
    """Fetch Portainer's certificate (without trusting it) so the user can
    compare + pin its SHA-256 fingerprint in Settings."""
    from .portainer import fetch_certificate
    body = request.get_json(silent=True) or {}
    url = (body.get("portainer_url") or get("portainer_url") or "").strip()
    if not url.startswith("https://"):
        return _err("pinning needs an https:// Portainer URL")
    try:
        return jsonify({"ok": True, **fetch_certificate(url)})
    except (OSError, ValueError) as e:
        return _err(f"could not read the certificate: {e}", 502)


@app.route("/api/status/ack", methods=["POST"])
def api_status_ack():
    """'Acknowledge' button: clears the red failed indicator."""
    return jsonify({"ok": True, "changed": history.acknowledge_failure()})


@app.route("/healthz")
def healthz():
    """Liveness + scheduler heartbeat. 503 if the scheduler tick is stale."""
    tick = scheduler.last_tick
    stale = tick is None or time.time() - tick > 120
    return jsonify({
        "ok": not stale,
        "last_tick": tick,
        "state": history.current_status().get("state"),
        "consecutive_failures": scheduler.consecutive_failures,
    }), (200 if not stale else 503)


@app.route("/api/test", methods=["GET", "POST"])
def api_test():
    """Settings 'Test connection' button: verify API + resolve endpoint.

    Accepts optional body {"url": "...", "api_key": "..."} to test the
    FIRST-RUN connection BEFORE saving anything to config.
    """
    body = request.get_json(silent=True) or {}
    url = body.get("portainer_url") or get("portainer_url")
    key = body.get("portainer_api_key") or get("portainer_api_key")
    if not url or not key or key == "***":
        return _err("not configured - Portainer URL and API key required", 400)
    fp = body["tls_fingerprint"] if "tls_fingerprint" in body else get("tls_fingerprint", "")
    try:
        fp = config_mod.validate("tls_fingerprint", fp)
    except config_mod.ConfigError as e:
        return _err(str(e))
    client = Portainer(url, key, tls_verify=bool(body.get("tls_verify", get("tls_verify", False))),
                       tls_ca_file=(get("tls_ca_file", "") or None), tls_fingerprint=fp)
    try:
        st = client.status()
        endpoint_id = client.resolve_endpoint(socket.gethostname())
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "version": st.get("Version") or st.get("version") or "?",
                    "endpoint_id": endpoint_id, "tls_mode": client.tls_mode})


@app.route("/api/endpoints", methods=["GET", "POST"])
def api_endpoints():
    """Scan Portainer for available endpoints (Settings 'Scan' button).
    POST {"portainer_url", "portainer_api_key"} scans BEFORE saving
    credentials (kept out of the query string, so keys never hit logs)."""
    body = request.get_json(silent=True) or {}
    url = body.get("portainer_url") or request.args.get("url") or get("portainer_url")
    key = body.get("portainer_api_key") or request.args.get("key") or get("portainer_api_key")
    if not url or not key or key == "***":
        return _err("not configured - Portainer URL and API key required", 400)
    client = Portainer(url, key, tls_verify=bool(get("tls_verify", False)),
                          tls_ca_file=(get("tls_ca_file", "") or None))
    try:
        eps = client.endpoints()
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "endpoints": [
        {"id": e.get("Id"), "name": e.get("Name"), "type": e.get("Type"),
         "url": e.get("Snapshots", [{}])[0].get("DockerURL", "") if e.get("Snapshots") else ""}
        for e in eps
    ]})


@app.route("/api/portainer_mount_check")
def api_portainer_mount_check():
    """Settings 'Check compose mount' button: verify Portainer's compose dir
    is mounted into this container (at /host-portainer) and contains a
    docker-compose.yml."""
    from pathlib import Path
    compose_dir = "/host-portainer"
    d = Path(compose_dir)
    if not d.exists():
        return jsonify({"ok": False, "state": "not_mounted",
                        "message": f"'{compose_dir}' does not exist inside this "
                                   "container - the volume mount is missing. "
                                   "Add it to the stack (host path on the left, "
                                   "container path on the right of the ':') and "
                                   "redeploy."})
    yml = d / "docker-compose.yml"
    if not yml.exists():
        return jsonify({"ok": False, "state": "no_compose_file",
                        "message": f"'{compose_dir}' is mounted but contains no "
                                   "docker-compose.yml - check the mount points "
                                   "at Portainer's compose dir."})
    try:
        content = yml.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return jsonify({"ok": False, "state": "unreadable",
                        "message": f"compose file not readable: {e}"})
    image = ""
    import re as _re
    m = _re.search(r"image:\s*([^\s]+)", content)
    if m:
        image = m.group(1)
    return jsonify({"ok": True, "state": "ok", "path": compose_dir,
                    "image": image,
                    "message": f"Compose file found (image: {image})"
                               + " - Portainer self-update will work."})


@app.route("/api/history")
def api_history():
    return jsonify({"history": history.history(),
                    "prune_stats_30d": history.prune_stats()})


@app.route("/api/runs/current")
def api_runs_current():
    """Live progress of the active run (steps + log tail)."""
    cur = gate.current()
    if not cur.get("active"):
        return jsonify({"active": False})
    tail = []
    p = history.run_log_path(cur["run_id"]) if cur.get("run_id") else None
    if p:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-15:]
        except OSError:
            pass
    return jsonify({"active": True, "run_id": cur["run_id"],
                    "holder": cur["holder"], "since": cur["since"],
                    "steps": cur["steps"], "log_tail": tail})


@app.route("/api/runs/<run_id>/log")
def api_run_log(run_id):
    p = history.run_log_path(run_id)
    if not p:
        return _err("log not found", 404)
    return app.response_class(p.read_text(encoding="utf-8", errors="replace"),
                              mimetype="text/plain")


# ------------------------------------------------------------------- stacks
# tiny TTL caches so the 15s UI polling doesn't hammer Portainer with a
# fresh TCP+TLS session on every tick (data is informational, staleness OK)
_TTL_STACKS = 10      # seconds - stack list redeploys show up within 10s
_TTL_INVENTORY = 120  # seconds - dashboard card only
_stacks_cache: dict = {}    # endpoint_id -> (ts, payload)
_inventory_cache: dict = {}  # endpoint_id -> (ts, payload)


def _cache_get(cache: dict, key, ttl: float):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key, payload):
    cache[key] = (time.time(), payload)
    return payload


@app.route("/api/stacks")
def api_stacks():
    """Cache-only read: NEVER triggers registry calls in the request thread."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    check = checker.get_cached()  # returns snapshot immediately, refreshes in bg
    cached = _stacks_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_STACKS:
        stacks = cached[1]
    else:
        stacks = []
        for s in client.stacks():
            if s.get("EndpointId") != eid:
                continue
            stacks.append({"Id": s["Id"], "Name": s.get("Name"),
                           "EndpointId": eid, "Status": s.get("Status")})
        _stacks_cache[eid] = (time.time(), stacks)
    out = []
    for s in stacks:
        name = s["Name"]
        svc_check = (check.get("stacks") or {}).get(name, {})
        nxt = (scheduler.stack_next.get(name) or {}).get("next")
        out.append({
            "id": s["Id"], "name": name, "status": s.get("Status"),
            "services": svc_check,
            "updates_available": sum(1 for v in svc_check.values()
                                     if v.get("update_available")),
            "policy": policies.get_policy(name),
            "effective": policies.effective(name),
            "next_run": nxt if policies.effective(name) == "own" else
                        (scheduler.next_full_update if policies.effective(name) == "auto" else None),
        })
    return jsonify({"endpoint_id": eid,
                    "checked_at": check.get("checked_at"),
                    "check_running": bool(check.get("running")),
                    "stacks": out})


# ------------------------------------------------------------- push notify
def _push_public(data: dict, endpoint: str = "") -> dict:
    subs = data.get("subscriptions") or []
    return {"events": data.get("events", {}), "devices": len(subs),
            "this_device": bool(endpoint and any(s.get("endpoint") == endpoint for s in subs)),
            "last_send": data.get("last_send")}


@app.route("/api/notify/config")
def api_notify_config():
    return jsonify({**_push_public(push.load(), request.args.get("endpoint", "")),
                    "vapid_public_key": push.vapid_public_key(),
                    "public_url": get("public_url", ""), "contact": push._contact()})


@app.route("/api/notify/events", methods=["PUT", "POST"])
def api_notify_events():
    body = request.get_json(silent=True) or {}
    return jsonify(_push_public(push.set_events(body.get("events") or {})))


@app.route("/api/notify/subscribe", methods=["POST"])
def api_notify_subscribe():
    body = request.get_json(silent=True) or {}
    sub = body.get("subscription") or {}
    try:
        data = push.subscribe(sub, request.headers.get("User-Agent", ""))
    except ValueError as e:
        return _err(str(e))
    return jsonify(_push_public(data, sub.get("endpoint", "")))


@app.route("/api/notify/unsubscribe", methods=["POST"])
def api_notify_unsubscribe():
    body = request.get_json(silent=True) or {}
    return jsonify(_push_public(push.unsubscribe(str(body.get("endpoint") or ""))))


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Test push. `delay` (s, max 30) lets you close the app before it arrives."""
    body = request.get_json(silent=True) or {}
    try:
        delay = max(0, min(30, int(body.get("delay", 0))))
    except (TypeError, ValueError):
        delay = 0
    devices = len(push.load().get("subscriptions") or [])
    title, text = "PortCare – próbaértesítés", "Ha ezt látod, az értesítések működnek."

    def _later():
        time.sleep(delay)
        push._safe_send("test", title, text, url=push.app_url("#settings"), force=True)
    if delay:
        threading.Thread(target=_later, daemon=True).start()
        return jsonify({"scheduled": True, "delay": delay, "devices": devices})
    return jsonify({"sent": push.send("test", title, text, url=push.app_url("#settings"), force=True),
                    "devices": devices})


# ---------------------------------------------------- standalone containers
_TTL_CONTAINERS = 10
_containers_cache: dict = {}


@app.route("/api/containers")
def api_containers():
    """Standalone containers (not in a Portainer stack) + check results."""
    from . import containers as cmod
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    cached = _containers_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_CONTAINERS:
        items = cached[1]
    else:
        try:
            items = cmod.standalone(client, eid)
        except PortainerError as err:
            return _err(str(err), 502)
        _containers_cache[eid] = (time.time(), items)
    check = checker.get_cached()
    out = []
    for c in items:
        k = cmod.key(c["name"])
        svc = (check.get("stacks") or {}).get(k, {})
        eff = policies.effective(k)
        out.append({**c, "key": k, "services": svc,
                    "updates_available": sum(1 for v in svc.values() if v.get("update_available")),
                    "policy": policies.get_policy(k), "effective": eff,
                    "next_run": (scheduler.stack_next.get(k) or {}).get("next") if eff == "own"
                    else (scheduler.next_full_update if eff == "auto" else None)})
    return jsonify({"endpoint_id": eid, "checked_at": check.get("checked_at"), "containers": out})


@app.route("/api/containers/update", methods=["POST"])
def api_container_update():
    """Update ONE standalone container now (pull + recreate)."""
    from . import containers as cmod
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "")
    if not policies.valid_name(name):
        return _err("valid container name required")
    if not gate.try_acquire("redeploy"):
        return _err("another job is running - see /api/runs/current", 409)
    runlog = None
    try:
        client, e = _client_or_error()
        if e:
            return e
        before = cmod.find(client, client.endpoint_id, name)
        runlog = RunLogger("redeploy", "manual")
        gate.set_runlog(runlog)
        ok = cmod.update_container(client, client.endpoint_id, name, runlog.log)
        runlog.finish(ok)
        _containers_cache.clear()
        if ok:
            checker.recheck_after_update([before["image"]] if before else None,
                                          names=["@" + name])
        return jsonify({"ok": ok, "run_id": runlog.run_id})
    except PortainerError as e:
        if runlog:
            runlog.finish(False)
        return _err(str(e), 502)
    finally:
        gate.release()


@app.route("/api/stacks/policy", methods=["POST"])
def api_stack_policy():
    """Set one stack's update policy (web UI only).
    Body: {"name": "immich", "mode": "default|notify|schedule|ignore",
           "kind": "daily|weekly", "time": "04:00", "day": 6}"""
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "")
    if not policies.valid_name(name):
        return _err("valid stack name required")
    try:
        pol = policies.normalize(body)
    except policies.PolicyError as e:
        return _err(str(e))
    current = dict(get("stack_policies", {}) or {})
    if pol["mode"] == "default" and not name.startswith("@"):
        current.pop(name, None)
    else:
        current[name] = pol
    try:
        cfg_set("stack_policies", current)
    except config_mod.ConfigError as e:
        return _err(str(e))
    cfg_save()
    scheduler._sync_stack_schedules()
    _stacks_cache.clear()
    return jsonify({"ok": True, "name": name, "policy": policies.get_policy(name),
                    "effective": policies.effective(name),
                    "next_run": (scheduler.stack_next.get(name) or {}).get("next")})


@app.route("/api/stacks/<int:stack_id>")
def api_stack_detail(stack_id):
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    try:
        stacks = client.stacks()
    except PortainerError as err:
        return _err(str(err), 502)
    stack = next((s for s in stacks if s["Id"] == stack_id), None)
    if not stack:
        return _err("stack not found", 404)
    try:
        content = client.stack_file(stack_id, eid)
    except PortainerError as e:
        return _err(str(e), 502)
    images = compose_mod.services_images(content)
    check = checker.get_cached().get("stacks", {}).get(stack.get("Name"), {})
    return jsonify({
        "id": stack_id, "name": stack.get("Name"),
        "compose": content, "images": images,
        "checks": check,
        "policy": policies.get_policy(stack.get("Name") or ""),
        "effective": policies.effective(stack.get("Name") or ""),
        "next_run": (scheduler.stack_next.get(stack.get("Name") or "") or {}).get("next"),
    })


@app.route("/api/stacks/<int:stack_id>/image", methods=["POST"])
def api_stack_set_image(stack_id):
    """Pin a service to a specific version (or 'latest').

    Body: {"service": "...", "image": "nginx:1.27.3"}
    Edits the compose file via the Portainer API and redeploys the stack.
    """
    body = request.get_json(silent=True) or {}
    service, image = body.get("service"), (body.get("image") or "").strip()
    if not service or not re.match(r"^[A-Za-z0-9_./:@-]+$", image):
        return _err("service and valid image required")
    if not gate.try_acquire("pin-version"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        client = _client()
        eid = client.endpoint_id
        stacks = client.stacks()
        stack = next((s for s in stacks if s["Id"] == stack_id), None)
        if not stack:
            return _err("stack not found", 404)
        content = client.stack_file(stack_id, eid)
        new_content = compose_mod.set_image(content, service, image)
        client.update_stack(stack_id, eid, new_content, stack.get("Env") or [])
        return jsonify({"ok": True, "service": service, "image": image,
                        "redeployed": True})
    except (ValueError, PortainerError) as e:
        return _err(str(e), 500)
    finally:
        gate.release()


@app.route("/api/stacks/<int:stack_id>/redeploy", methods=["POST"])
def api_stack_redeploy(stack_id):
    if not gate.try_acquire("redeploy"):
        return _err("another job is running - see /api/runs/current", 409)
    runlog = None
    try:
        client = _client()
        eid = client.endpoint_id
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
        if not stack:
            return _err("stack not found", 404)
        runlog = RunLogger("redeploy", "manual")
        gate.set_runlog(runlog)
        ok = updater.redeploy_single_stack(client, stack, eid, runlog.log)
        runlog.finish(ok)
        if ok:
            try:
                imgs = list(compose_mod.services_images(client.stack_file(stack_id, eid)).values())
            except Exception:  # noqa: BLE001
                imgs = None
            checker.recheck_after_update(imgs, names=[stack.get("Name")])
        return jsonify({"ok": ok, "run_id": runlog.run_id})
    except (PortainerError, KeyError) as e:
        if runlog:
            runlog.finish(False)
        return _err(str(e), 500)
    finally:
        gate.release()


# ------------------------------------------------------------------ versions
@app.route("/api/stacks/<int:stack_id>/rollback_info")
def api_rollback_info(stack_id):
    """Diff for the rollback dialog: what 'Restore last healthy' would
    change on this stack (current images vs the stored pre-update snapshot)."""
    client, e = _client_or_error()
    if e:
        return e
    try:
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
    except PortainerError as ex:
        return _err(str(ex), 502)
    if not stack:
        return _err("stack not found", 404)
    name = stack.get("Name") or f"stack-{stack_id}"
    from . import snapshots as snap_mod
    snap = snap_mod.get_snapshot(name)
    if not snap:
        return jsonify({"ok": False, "has_snapshot": False,
                        "message": "No pre-update snapshot for this stack yet - "
                                   "one is captured on the next update run."})
    try:
        content = client.stack_file(stack_id, client.endpoint_id)
        current = updater._resolved_images(content, _stack_env_of(stack))
    except (PortainerError, RuntimeError) as ex:
        return _err(str(ex), 502)
    rows = snap_mod.diff_snapshot(name, current)
    return jsonify({"ok": True, "has_snapshot": True,
                    "snapshot_ts": snap.get("ts"),
                    "snapshot_run_id": snap.get("run_id"),
                    "changes": rows,
                    "unchanged": len((snap.get("images") or {}) or {}) - len(rows)})


def _stack_env_of(stack) -> list:
    return stack.get("Env") or []


def _run_rollback_worker(runlog, stack_name, target_images):
    try:
        ok = updater.rollback_stack(runlog, stack_name, target_images)
        runlog.finish(ok)
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            runlog.log("[ERROR] rollback crashed:\n" + err)
        except Exception:  # noqa: BLE001
            pass
        runlog.finish(False)
    finally:
        gate.release()
        from .scheduler import scheduler as sched
        sched._bump_full()


@app.route("/api/stacks/<int:stack_id>/rollback", methods=["POST"])
def api_stack_rollback(stack_id):
    """'Restore last healthy': async rollback of ONE stack to its stored
    pre-update image snapshot. Gate acquired first, RunLogger second."""
    body = request.get_json(silent=True) or {}
    target = body.get("images") or {}
    if not isinstance(target, dict) or not target:
        return _err("images map required (service -> image)")
    for svc, img in target.items():
        if not re.match(r"^[A-Za-z0-9_./:@-]+$", svc) or \
                not re.match(r"^[A-Za-z0-9_./:@${}-]+$", str(img)):
            return _err("invalid service/image entry")
    if not gate.try_acquire("rollback"):
        return _err("another job is running - see /api/runs/current", 409)
    runlog = None
    try:
        client, e = _client_or_error()
        if e:
            gate.release()
            return e
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
        if not stack:
            gate.release()
            return _err("stack not found", 404)
        runlog = RunLogger("rollback", "manual")
        gate.set_runlog(runlog)
        runlog.step("rollback_started", True)
        threading.Thread(
            target=_run_rollback_worker,
            args=(runlog, stack.get("Name", ""), dict(target)),
            daemon=True).start()
        return jsonify({"ok": True, "run_id": runlog.run_id})
    except PortainerError as e:
        if runlog:
            runlog.finish(False)
            gate.release()
        return _err(str(e), 502)


@app.route("/api/versions")
def api_versions():
    """Known tags for an image ref: /api/versions?image=nginx"""
    image = request.args.get("image", "")
    if not image or len(image) > 300:
        return _err("image query param required")
    return jsonify(dockerhub.available_versions(image))


# ------------------------------------------------------------------- actions
@app.route("/api/check", methods=["POST"])
def api_check():
    """Fire-and-forget update check. The gate is held for the DURATION of
    the check (released in the worker thread), so a full update can't start
    mid-check."""
    if not gate.try_acquire("check"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        try:
            client = _client()
        except PortainerError as e:
            return _err(str(e), 502)
        force = bool((request.get_json(silent=True) or {}).get("force"))
        started = threading.Thread(
            target=_check_worker, args=(client, client.endpoint_id, force),
            daemon=True)
        started.start()
        return jsonify({"ok": True, "started": True})
    except Exception as e:  # noqa: BLE001 - spawn failure must release the gate
        gate.release()
        return _err(f"could not start check: {e}", 500)


def _check_worker(client, eid, force):
    """Runs OUTSIDE the request context - releases the gate when done."""
    try:
        checker.run_check(client, eid, force=force)  # notifies via the checker hook
    except Exception as e:  # noqa: BLE001 - defensive: check must never crash silently
        import sys
        print(f"[check] worker crashed: {e}", file=sys.stderr, flush=True)
    finally:
        gate.release()


@app.route("/api/update", methods=["POST"])
def api_update():
    run_id = scheduler.trigger_update_async("manual")
    if not run_id:
        return _err("another job is running - see /api/runs/current", 409)
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/self_update", methods=["POST"])
def api_self_update():
    """'Update PUS' button: deliberately redeploy this app's own stack.

    Runs in a thread - the redeploy replaces this container mid-request,
    so the response may never reach the browser (expected!). The run is
    recorded in history BEFORE the container dies."""
    if not gate.try_acquire("self_update"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        runlog = RunLogger("self_update", "manual")
        gate.set_runlog(runlog)
        runlog.step("self_update_started", True)

        def _worker():
            try:
                updater.self_update_run(runlog)
            except Exception:  # noqa: BLE001 - the process may die mid-request
                pass

        threading.Thread(target=_worker, daemon=True).start()
        # give the redeploy a moment to fire; if the container survives
        # (nothing to do / error), the worker has finished already
        threading.Event().wait(2.0)
        return jsonify({"ok": True, "run_id": runlog.run_id,
                        "note": "redeploying - container will restart"})
    finally:
        gate.release()


@app.route("/api/inventory")
def api_inventory():
    """Docker inventory for the dashboard: unused images (reclaimable),
    unused networks, totals. Read-only, TTL-cached (120s)."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    cached = _inventory_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_INVENTORY:
        return jsonify(cached[1])
    try:
        imgs = client.images(eid)
        cs = client.all_containers(eid)
        nets = client.networks(eid)
    except PortainerError as err:
        return _err(str(err), 502)
    used_ids = {c.get("ImageID") for c in cs if c.get("ImageID")}
    unused = [i for i in imgs if i.get("Id") not in used_ids]
    unused_mb = sum((i.get("Size") or 0) for i in unused) // (1024 * 1024)
    containers_per_net: dict = {}
    for c in cs:
        for nid in ((c.get("NetworkSettings", {}) or {}).get("Networks") or {}):
            containers_per_net[nid] = containers_per_net.get(nid, 0) + 1
    unused_nets = [n for n in nets if containers_per_net.get(n.get("Id"), 0) == 0]
    payload = {
        "images_total": len(imgs),
        "images_unused": len(unused),
        "images_unused_mb": unused_mb,
        "networks_total": len(nets),
        "networks_unused": len(unused_nets),
        "containers_total": len(cs),
    }
    _inventory_cache[eid] = (time.time(), payload)
    return jsonify(payload)


@app.route("/api/prune", methods=["POST"])
def api_prune():
    client, e = _client_or_error()
    if e:
        return e
    if not gate.try_acquire("prune"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        runlog = RunLogger("prune", "manual")
        gate.set_runlog(runlog)
        updater.cleanup_docker(client, client.endpoint_id, runlog.log)
        runlog.step("docker_prune", True)
        runlog.finish(True)
        return jsonify({"ok": True, "run_id": runlog.run_id})
    finally:
        gate.release()


# ------------------------------------------------------------------ settings
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = dict(load())
    # never expose the secrets themselves
    cfg["portainer_api_key"] = "***" if cfg.get("portainer_api_key") else ""
    cfg["notify_webhook"] = "***" if cfg.get("notify_webhook") else ""
    cfg.pop("auth_token", None)
    cfg.pop("auth_password_hash", None)
    cfg["password_from_env"] = auth.password_from_env()
    cfg["env_locked"] = sorted(k for k in cfg if env_locked(k))
    cfg["tls_mode"] = _tls_mode_now()
    cfg["decrypt_error"] = config_mod.decrypt_error
    cfg["secrets_encrypted"] = True
    # flat alias mirroring cfg["repairs"]["enabled"] (UI checkbox reads this)
    cfg["repairs_enabled"] = bool(cfg.get("repairs", {}).get("enabled", True))
    return jsonify(cfg)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    allowed = {"portainer_url", "portainer_api_key", "portainer_endpoint_id",
               "update_interval_hours", "update_schedule_mode",
               "update_schedule_time", "update_schedule_day",
               "tls_verify", "max_parallel_deploys", "tls_ca_file",
               "deploy_wait_time", "keep_backups",
               "check_cache_minutes", "listen_port",
               "notify_webhook", "include_portainer",
               "repairs_enabled", "check_interval_hours", "tls_fingerprint",
               "notify_on_updates", "session_days", "public_url", "notify_contact"}
    # two-pass: validate EVERYTHING first, then apply - a bad key must never
    # leave earlier keys mutated in memory (memory/disk divergence)
    from .config import validate
    validated = {}
    for k, v in body.items():
        if k not in allowed:
            return _err(f"unknown setting: {k}")
        if k in ("portainer_api_key", "notify_webhook") and v == "***":
            continue
        if env_locked(k):
            if v == get(k):
                continue  # unchanged value echoed back by the form
            return _err(f"{k} is set via environment variable (PUS_{k.upper()}) "
                        "- change it in the stack")
        try:
            validated[k] = validate(k, v)  # raises ConfigError on garbage
        except config_mod.ConfigError as e:
            return _err(str(e))
    for k, v in validated.items():
        cfg_set(k, v)
    cfg_save()
    return jsonify({"ok": True, "applied": sorted(validated)})


# ----------------------------------------------------------------------- main
def main():
    load()
    _init_secret_key()
    from .history import mark_interrupted
    mark_interrupted()  # convert stale 'running' markers from a crash
    scheduler.start()
    host = get("listen_host", "127.0.0.1")
    port = int(get("listen_port", 8090))
    # production WSGI server (waitress): graceful threads, no dev-server warning.
    # ARCHITECTURAL INVARIANT: this app MUST run as a single process with N
    # threads. The JobGate (app/jobs.py), checker caches and the state files
    # (status.json/history.jsonl) are per-process - running under multiple
    # workers (e.g. gunicorn -w 4) would race concurrent full-updates and
    # corrupt state. Threads>1 is fine; workers>1 is not.
    from waitress import serve
    print(f"[update-service] listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()