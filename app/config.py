"""Configuration loading/persistence with validation.

config/config.yaml holds credentials + settings (gitignored).
config/config.example.yaml is the committed template.
All settings passed via the UI are validated/coerced before being applied,
and writes are atomic (tmp + replace).
"""
import os
import re
import threading
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
# PUS_DATA_DIR lets tests (and exotic deployments) relocate all state files
# without touching the code - must be set BEFORE the app modules load.
# PUS_CONFIG_DIR relocates the config file the same way (tests must not see
# the developer's live config.yaml with real credentials in it).
_DATA_DIR_OVERRIDE = os.environ.get("PUS_DATA_DIR", "")
CONFIG_DIR = Path(os.environ.get("PUS_CONFIG_DIR", "")) \
    if os.environ.get("PUS_CONFIG_DIR", "") else BASE_DIR / "config"
DATA_DIR = Path(_DATA_DIR_OVERRIDE) if _DATA_DIR_OVERRIDE else BASE_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
UI_DIR = BASE_DIR / "ui"

# Container deployments set configuration via env vars (PUS_<KEY>) -
# they take precedence over config.yaml. See docker-compose.yml.
ENV_PREFIX = "PUS_"

DEFAULTS = {
    "portainer_url": "",
    "portainer_api_key": "",
    "portainer_endpoint_id": None,   # None = auto-detect via hostname
    "update_interval_hours": 168,    # weekly - only used in "interval" mode
    "update_schedule_mode": "interval",  # "interval" | "daily" | "weekly" | "off"
    "check_interval_hours": 12,      # registry check cadence (independent of updates)
    "update_schedule_time": "03:30",     # HH:MM, server-local time (TZ env)
    "update_schedule_day": 0,            # 0=Monday .. 6=Sunday (weekly mode)
    "tls_verify": False,             # True = verify Portainer TLS cert
    "tls_ca_file": "",               # path to a CA bundle/cert (inside this
                                      # container) to verify a self-signed
                                      # Portainer cert against
    "tls_fingerprint": "",           # SHA-256 of Portainer's cert: pinning
                                      # (works with self-signed certs + IPs)
    "max_parallel_deploys": 3,
    "deploy_wait_time": 300,         # seconds to wait for containers to become ready
    "keep_backups": 5,
    "backup_dir": str(DATA_DIR / "backups"),
    "check_cache_minutes": 30,       # docker hub result cache TTL
    "listen_host": "127.0.0.1",      # safe default: localhost only
    "listen_port": 8090,
    "auth_token": "",                # admin password from env (PUS_AUTH_TOKEN /
                                      # PUS_ADMIN_PASSWORD) - never persisted
    "auth_password_hash": "",        # admin password set in the UI (pbkdf2)
    "session_days": 30,              # login session lifetime
    "notify_webhook": "",            # optional POST target for notifications
    "notify_on_updates": False,      # also notify when new updates are found
    "include_portainer": False,      # true = also update Portainer itself
                                      # (compose pull/up in /host-portainer)
    "repairs": {
        "enabled": True,
        # optional user-defined repair rules (see config.example.yaml);
        # the built-in stale host-gateway repair needs no config
        "rules": [],
    },
}

# validation table: key -> (type, min, max) - value ranges for ints
INT_RANGES = {
    "update_interval_hours": (1, 8760),
    "check_interval_hours": (1, 168),
    "session_days": (1, 365),
    "update_schedule_day": (0, 6),
    "max_parallel_deploys": (1, 10),
    "deploy_wait_time": (30, 3600),
    "keep_backups": (1, 100),
    "check_cache_minutes": (5, 720),
    "listen_port": (1, 65535),
    "portainer_endpoint_id": (None, None),
}

STR_ENUMS = {
    "update_schedule_mode": ("interval", "daily", "weekly", "off"),
}


class ConfigError(ValueError):
    pass


def validate(key: str, value):
    """Coerce + validate one setting. Raises ConfigError on garbage."""
    if key in STR_ENUMS:
        v = str(value or "").strip().lower()
        if v not in STR_ENUMS[key]:
            raise ConfigError(f"{key} must be one of: {', '.join(STR_ENUMS[key])}")
        return v
    if key == "update_schedule_time":
        v = str(value or "").strip()
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ConfigError("update_schedule_time must be HH:MM (24h), e.g. 03:30")
        return v
    if key in INT_RANGES:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be an integer (got {value!r})")
        lo, hi = INT_RANGES[key]
        if lo is not None and v < lo:
            raise ConfigError(f"{key} must be >= {lo}")
        if hi is not None and v > hi:
            raise ConfigError(f"{key} must be <= {hi}")
        return v
    if key in ("tls_verify", "notify_on_updates"):
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true/false")
        return value
    if key == "tls_fingerprint":
        v = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
        if v and len(v) != 64:
            raise ConfigError("tls_fingerprint must be a SHA-256 fingerprint "
                              "(64 hex characters)")
        return v
    if key in ("portainer_url", "notify_webhook"):
        v = str(value or "").strip()
        if v and not re.match(r"^https?://", v):
            raise ConfigError(f"{key} must start with http:// or https://")
        return v
    if key in ("portainer_api_key", "auth_token", "tls_ca_file",
               "auth_password_hash"):
        return str(value or "").strip()
    if key == "include_portainer":
        if not isinstance(value, bool):
            raise ConfigError("include_portainer must be true/false")
        return value
    if key == "repairs_enabled":
        if not isinstance(value, bool):
            raise ConfigError("repairs_enabled must be true/false")
        return value
    raise ConfigError(f"unknown setting: {key}")


_lock = threading.Lock()
_cfg = None
_env_keys: list = []       # keys whose value came from PUS_* env (never persisted)
_needs_migration = False    # plaintext secrets / legacy auth_token found on disk
decrypt_error = ""          # surfaced in the UI when a secret can't be decrypted


def load(force: bool = False) -> dict:
    cfg = _load(force)
    global _needs_migration
    if _needs_migration:
        _needs_migration = False
        try:
            save()
        except OSError:
            pass
    return cfg


def _load(force: bool = False) -> dict:
    global _cfg, _env_keys, _needs_migration, decrypt_error
    from . import crypto
    with _lock:
        if _cfg is not None and not force:
            return _cfg
        env_keys = []
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            cfg.update({k: v for k, v in user.items()
                        if k not in ("repairs", "repairs_enabled")})
            cfg["repairs"] = {**DEFAULTS["repairs"], **(user.get("repairs") or {})}
        # secrets: decrypt (enc:v1:...) or schedule a migration for plaintext
        decrypt_error = ""
        for sk in crypto.SECRET_KEYS:
            val = cfg.get(sk)
            if not val:
                continue
            if crypto.is_encrypted(val):
                try:
                    cfg[sk] = crypto.decrypt(val)
                except crypto.DecryptError as e:
                    cfg[sk] = ""
                    decrypt_error = str(e)
            else:
                _needs_migration = True
        # legacy: a plaintext auth_token in config.yaml becomes a password hash
        if cfg.get("auth_token") and not os.environ.get("PUS_AUTH_TOKEN") \
                and not os.environ.get("PUS_ADMIN_PASSWORD"):
            from werkzeug.security import generate_password_hash
            if not cfg.get("auth_password_hash"):
                cfg["auth_password_hash"] = generate_password_hash(str(cfg["auth_token"]))
            cfg["auth_token"] = ""
            _needs_migration = True
        # env overrides (container deployments): PUS_PORTAINER_URL, PUS_LISTEN_HOST, ...
        # YAML coercion ONLY for keys whose type is int/bool/enum - string
        # settings (auth_token, api_key, urls, ...) must be taken LITERALLY:
        # yaml.safe_load("no") -> False (silently disabled auth!), keys with
        # '#' were truncated as comments, numeric-looking keys became ints.
        for key in list(cfg.keys()):
            if key == "repairs":
                continue
            env_val = os.environ.get(ENV_PREFIX + key.upper())
            if key == "auth_token" and not env_val:
                env_val = os.environ.get(ENV_PREFIX + "ADMIN_PASSWORD")
            if env_val is None or env_val == "":
                continue
            env_keys.append(key)
            if key in STR_ENUMS:
                # verbatim: yaml.safe_load("off") would become False
                cfg[key] = str(env_val).strip().lower()
            elif key in INT_RANGES or key == "update_schedule_time":
                try:
                    cfg[key] = yaml.safe_load(env_val)  # coerces ints/HH:MM-safe
                except yaml.YAMLError:
                    cfg[key] = env_val
            elif key in ("tls_verify", "include_portainer", "notify_on_updates"):
                v = str(env_val).strip().lower()
                cfg[key] = v in ("true", "1", "yes", "on")
            else:
                cfg[key] = env_val  # verbatim string
        # repairs_enabled: flat UI/API alias for cfg["repairs"]["enabled"]
        # (the nested dict is config.yaml-only; the UI toggles the flat key)
        env_rep = os.environ.get(ENV_PREFIX + "REPAIRS_ENABLED")
        if env_rep not in (None, ""):
            cfg["repairs"]["enabled"] = str(env_rep).strip().lower() in (
                "true", "1", "yes", "on")
        elif "repairs_enabled" in user_vars(CONFIG_FILE):
            cfg["repairs"]["enabled"] = bool(user_vars(CONFIG_FILE)["repairs_enabled"])
        # expose the flat alias so /api/settings GET + UI checkbox stay in sync
        cfg["repairs_enabled"] = bool(cfg["repairs"].get("enabled", True))
        _env_keys = env_keys
        _cfg = cfg
        return cfg


def env_locked(key: str) -> bool:
    """True if the value comes from a PUS_* env var (UI can't change it)."""
    return key in _env_keys


def user_vars(path: Path) -> dict:
    """Raw user-supplied keys from config.yaml (no defaults merged)."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def save() -> None:
    """Atomic write: tmp file + replace.

    - values that came from env vars are NOT written (the env stays the
      source of truth and secrets passed via env never land on disk);
    - secrets are encrypted (enc:v1:...);
    - the file is created with 0600 permissions.
    """
    from . import crypto
    with _lock:
        if _cfg is None:
            return
        on_disk = user_vars(CONFIG_FILE)
        out = {}
        for k, v in _cfg.items():
            if k == "auth_token":
                continue  # env-only, never persisted
            if k in _env_keys:
                if k in on_disk:
                    out[k] = on_disk[k]  # keep whatever the file had
                continue
            if k in crypto.SECRET_KEYS and v:
                v = crypto.encrypt(v)
            out[k] = v
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
        tmp.replace(CONFIG_FILE)


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:
    """Validate then set (does NOT save; call save() explicitly)."""
    load()
    if key == "repairs_enabled":
        if not isinstance(value, bool):
            raise ConfigError("repairs_enabled must be true/false")
        with _lock:
            _cfg["repairs_enabled"] = value
            _cfg["repairs"] = {**_cfg.get("repairs", {}), "enabled": value}
        return
    validated = validate(key, value)
    with _lock:
        _cfg[key] = validated