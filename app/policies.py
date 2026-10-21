"""Per-stack update policies (set in the web UI, stored in config.yaml).

Every stack has one mode:

  default   follows the global schedule (Settings > Schedule)            [default]
  notify    never updated automatically - only checked and flagged
  schedule  own schedule: daily / weekly at HH:MM (server time, TZ env)
  ignore    skipped completely - no registry check, no automatic update

Manual "Update all" redeploys the stacks in `default` + `schedule` mode;
`notify` / `ignore` stacks are only updated with their own Update button.
Policies are keyed by stack NAME (stack IDs change when a stack is recreated).
"""
import re
import time
from datetime import datetime, timedelta

MODES = ("default", "notify", "schedule", "ignore")
KINDS = ("daily", "weekly")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class PolicyError(ValueError):
    pass


def normalize(policy) -> dict:
    """Validate + canonicalize one policy dict. Raises PolicyError."""
    if not isinstance(policy, dict):
        raise PolicyError("policy must be an object")
    mode = str(policy.get("mode") or "default").strip().lower()
    if mode not in MODES:
        raise PolicyError(f"mode must be one of: {', '.join(MODES)}")
    out = {"mode": mode}
    if mode == "schedule":
        kind = str(policy.get("kind") or "daily").strip().lower()
        if kind not in KINDS:
            raise PolicyError("kind must be daily or weekly")
        t = str(policy.get("time") or "03:30").strip()
        if not _TIME_RE.match(t):
            raise PolicyError("time must be HH:MM (24h)")
        out.update(kind=kind, time=t)
        if kind == "weekly":
            try:
                day = int(policy.get("day", 0))
            except (TypeError, ValueError):
                raise PolicyError("day must be 0..6")
            if not 0 <= day <= 6:
                raise PolicyError("day must be 0..6")
            out["day"] = day
    return out


def normalize_all(value) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise PolicyError("stack_policies must be a mapping")
    out = {}
    for name, pol in value.items():
        name = str(name)
        if not _NAME_RE.match(name):
            raise PolicyError(f"invalid stack name: {name!r}")
        p = normalize(pol)
        if p["mode"] != "default":  # default = no entry
            out[name] = p
    return out


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(str(name or "")))


# ------------------------------------------------------------------ queries
def _all() -> dict:
    from .config import get
    return get("stack_policies", {}) or {}


def get_policy(name: str) -> dict:
    return dict(_all().get(name) or {"mode": "default"})


def global_auto() -> bool:
    from .config import get
    return get("update_schedule_mode", "off") != "off"


def effective(name: str) -> str:
    """'auto' (global schedule) | 'own' | 'notify' | 'ignore'."""
    m = get_policy(name)["mode"]
    if m == "default":
        return "auto" if global_auto() else "notify"
    return {"schedule": "own"}.get(m, m)


def ignored(name: str) -> bool:
    return get_policy(name)["mode"] == "ignore"


def in_global_run(name: str) -> bool:
    """Scheduled global run: only stacks that follow the global schedule."""
    return get_policy(name)["mode"] == "default"


def in_manual_all(name: str) -> bool:
    """Manual 'Update all': default + own-schedule stacks."""
    return get_policy(name)["mode"] in ("default", "schedule")


def own_schedules() -> dict:
    return {n: p for n, p in _all().items() if p.get("mode") == "schedule"}


def spec(policy: dict) -> str:
    return f"{policy.get('kind')}:{policy.get('day', '')}:{policy.get('time')}"


def next_due(policy: dict, now_ts: float | None = None) -> float:
    """Next run epoch for an own (daily/weekly) schedule, server-local time."""
    now_ts = time.time() if now_ts is None else now_ts
    hh, mm = (int(x) for x in policy.get("time", "03:30").split(":", 1))
    now = datetime.fromtimestamp(now_ts)
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    if policy.get("kind") == "weekly":
        target = int(policy.get("day", 0)) % 7
        while cand.weekday() != target:
            cand += timedelta(days=1)
    return time.mktime(cand.timetuple())
