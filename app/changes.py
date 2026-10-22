"""What did an update actually change?

A redeploy re-pulls every image of a stack, but only services whose image
really changed get a new container image. We compare the containers' image
IDs before and after, and remember the last real change per stack /
standalone container ("@name") in data/last_updated.json:

  {"immich": {"ts": 1729..., "services": ["immich-server"], "images": {...}}}

The current run's RunLogger also collects them (runlog.changes) so the
history and the push can say "updated: immich (server), nextcloud".
"""
import json
import threading
import time

from .config import DATA_DIR

FILE = DATA_DIR / "last_updated.json"
_lock = threading.Lock()


def load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def project_image_ids(client, project, eid) -> dict:
    """service -> (image ID, image ref) of the project's containers."""
    out = {}
    try:
        for c in client.containers_by_project(project, eid):
            svc = (c.get("Labels") or {}).get("com.docker.compose.service") or \
                ((c.get("Names") or ["?"])[0] or "?").lstrip("/")
            out[svc] = (c.get("ImageID") or "", c.get("Image") or "")
    except Exception:  # noqa: BLE001 - informational only
        return {}
    return out


def diff(before: dict, after: dict) -> dict:
    """Services whose image ID changed (or that are new): service -> image ref."""
    return {svc: ref for svc, (iid, ref) in after.items()
            if iid and before.get(svc, ("", ""))[0] != iid}


def record(name: str, services: dict, log=None) -> None:
    """Remember a real change and attach it to the current run (if any)."""
    if not services:
        return
    entry = {"ts": time.time(), "services": sorted(services),
             "images": dict(services)}
    with _lock:
        data = load()
        data[name] = entry
        try:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(FILE)
        except OSError:
            pass
        owner = getattr(log, "__self__", None) if callable(log) else None
        if owner is not None and isinstance(getattr(owner, "changes", None), list):
            owner.changes[:] = [c for c in owner.changes if c.get("name") != name] + \
                [{"name": name, "services": sorted(services)}]
