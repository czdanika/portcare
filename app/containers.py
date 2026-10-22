"""Standalone containers: everything Portainer sees that is NOT part of a
Portainer stack (plain `docker run` containers, compose projects started
outside Portainer, NAS-app containers, ...).

They are checked like stack services and can be updated by *recreating*
the container with a freshly pulled image:

1. Portainer's own "Recreate" API (POST /api/docker/{env}/containers/{id}/recreate,
   {"PullImage": true}) - the same as Portainer UI > Container > Recreate.
2. Fallback for older Portainer versions: a careful manual recreate through
   the Docker API (pull -> stop -> rename old -> create new with the same
   config, volumes and networks -> start -> remove old; any failure rolls
   back to the old container).

Policies use the key "@<container name>" (see policies.py). Standalone
containers default to "notify" - they are never recreated automatically
until you choose a rule for them in the UI.
"""
import json
import re
import time
from urllib.parse import quote

from .portainer import PortainerError

PREFIX = "@"
_PORTAINER_IMG = re.compile(r"(^|/)portainer/(portainer|portainer-ce|portainer-ee|agent)(:|@|$)")
_ANON_VOL = re.compile(r"^[0-9a-f]{64}$")


def key(name: str) -> str:
    return PREFIX + name


def is_key(name: str) -> bool:
    return str(name or "").startswith(PREFIX)


def _name(c: dict) -> str:
    return ((c.get("Names") or ["/?"])[0] or "/?").lstrip("/")


def standalone(client, eid, stacks=None) -> list:
    """Containers that are not managed by a Portainer stack on this endpoint.
    Portainer's own containers are left out (Portainer has its own updater)."""
    if stacks is None:
        stacks = [s for s in client.stacks() if s.get("EndpointId") == eid]
    stack_projects = {(s.get("Name") or "").lower() for s in stacks}
    out = []
    for c in client.all_containers(eid):
        labels = c.get("Labels") or {}
        project = (labels.get("com.docker.compose.project") or "").lower()
        if project and project in stack_projects:
            continue
        image = c.get("Image") or ""
        if not image or image.startswith("sha256:") or _PORTAINER_IMG.search(image):
            continue
        out.append({
            "id": c.get("Id"), "name": _name(c), "image": image,
            "image_id": c.get("ImageID") or "",
            "state": c.get("State", ""), "status": c.get("Status", ""),
            "project": project or None, "created": c.get("Created"),
        })
    return sorted(out, key=lambda x: x["name"].lower())


def find(client, eid, name) -> dict | None:
    return next((c for c in standalone(client, eid) if c["name"] == name), None)


# ------------------------------------------------------------------ update
def update_container(client, eid, name, log) -> bool:
    """Recreate with a freshly pulled image; records a real image change."""
    before = find(client, eid, name)
    ok = _update_container(client, eid, name, log)
    if ok and before:
        try:
            after = find(client, eid, name)
            if after and after.get("image_id") and after["image_id"] != before.get("image_id"):
                from . import changes
                log(f"[CHANGED] {name}: new image ({after['image']})")
                changes.record(key(name), {name: after["image"]}, log)
            else:
                log(f"[INFO] {name}: no new image (already up to date).")
        except Exception as e:  # noqa: BLE001 - informational only
            log(f"[WARN] {name}: could not compare images ({e}).")
    return ok


def _update_container(client, eid, name, log) -> bool:
    c = find(client, eid, name)
    if not c:
        log(f"[ERROR] container '{name}' not found (or it belongs to a stack).")
        return False
    log(f"Processing container: {name} ({c['image']})")
    try:
        client.recreate_container(c["id"], eid, pull=True)
        log(f"[INFO] {name}: recreated by Portainer (image pulled).")
        return _wait_running(client, eid, name, c["state"] == "running", log)
    except PortainerError as e:
        msg = str(e)
        if not re.search(r"HTTP (404|405)", msg):
            log(f"[ERROR] {name}: Portainer recreate failed: {msg[:300]}")
            _hint(name, msg, log)
            return False
        log(f"[INFO] {name}: this Portainer has no recreate API - manual recreate.")
    return _manual_recreate(client, eid, c, log)


def _hint(name, msg, log):
    m = msg.lower()
    if "pull access denied" in m or "repository does not exist" in m:
        log(f"[HINT] {name}: the image is not in any registry (built locally?) - "
            f"set this container's rule to 'Notify only' or 'Ignored'.")
    elif "toomanyrequests" in m:
        log(f"[HINT] {name}: registry rate limit - try again in a few minutes.")


def _wait_running(client, eid, name, must_run, log, timeout=90) -> bool:
    if not must_run:
        log(f"[OK] {name}: recreated (was stopped, left stopped).")
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = find(client, eid, name)
        if c and c["state"] == "running":
            try:
                insp = client.inspect_container(c["id"], eid)
                health = ((insp.get("State") or {}).get("Health") or {}).get("Status")
            except PortainerError:
                health = None
            if health in (None, "healthy"):
                log(f"[OK] {name}: running{' (healthy)' if health else ''}.")
                return True
        time.sleep(3)
    log(f"[ERROR] {name}: not running after {timeout}s.")
    return False


def _manual_recreate(client, eid, c, log) -> bool:
    name, old_id = c["name"], c["id"]
    try:
        info = client.inspect_container(old_id, eid)
    except PortainerError as e:
        log(f"[ERROR] {name}: inspect failed: {e}")
        return False
    cfg = dict(info.get("Config") or {})
    host = dict(info.get("HostConfig") or {})
    image = cfg.get("Image") or c["image"]
    was_running = bool((info.get("State") or {}).get("Running"))

    # 1) pull first - if this fails nothing has been touched yet
    try:
        client.pull_image(image, eid)
        log(f"[INFO] {name}: pulled {image}")
    except PortainerError as e:
        log(f"[ERROR] {name}: pull failed: {str(e)[:300]}")
        _hint(name, str(e), log)
        return False

    # keep anonymous volumes (image VOLUMEs) - otherwise their data is lost
    binds = list(host.get("Binds") or [])
    bound = {b.split(":")[1] for b in binds if b.count(":") >= 1}
    for m in info.get("Mounts") or []:
        if m.get("Type") == "volume" and _ANON_VOL.match(m.get("Name") or "") \
                and m.get("Destination") not in bound:
            binds.append(f"{m['Name']}:{m['Destination']}")
    host["Binds"] = binds or None

    if (cfg.get("Hostname") or "") == (old_id or "")[:12]:
        cfg.pop("Hostname", None)  # default hostname = old container id
    nets = ((info.get("NetworkSettings") or {}).get("Networks") or {})
    mode = host.get("NetworkMode") or ""
    shared = mode in ("host", "none") or mode.startswith("container:")
    first = None if shared else (mode if mode in nets else next(iter(nets), None))

    def ep(n):
        e = nets.get(n) or {}
        return {k: v for k, v in {"Aliases": [a for a in (e.get("Aliases") or [])
                                              if a != (old_id or "")[:12]] or None,
                                  "IPAMConfig": e.get("IPAMConfig"),
                                  "Links": e.get("Links")}.items() if v}

    body = dict(cfg)
    body["Image"] = image
    body["HostConfig"] = host
    if first:
        body["NetworkingConfig"] = {"EndpointsConfig": {first: ep(first)}}

    renamed = new_id = None
    try:
        if was_running:
            client.container_action(old_id, "stop", eid, params="t=30")
        client.container_action(old_id, "rename", eid, params="name=" + quote(name + "-portcare-old"))
        renamed = True
        new_id = client.create_container(name, body, eid)
        for n in nets:
            if n != first and not shared:
                client.connect_network(n, new_id, eid, endpoint_config=ep(n))
        if was_running:
            client.container_action(new_id, "start", eid)
    except PortainerError as e:
        log(f"[ERROR] {name}: recreate failed ({str(e)[:300]}) - rolling back.")
        try:
            if new_id:
                client.remove_container(new_id, eid)
            if renamed:
                client.container_action(old_id, "rename", eid, params="name=" + quote(name))
            if was_running:
                client.container_action(old_id, "start", eid)
            log(f"[INFO] {name}: old container restored.")
        except PortainerError as e2:
            log(f"[ERROR] {name}: rollback failed too: {e2} - check it in Portainer!")
        return False

    ok = _wait_running(client, eid, name, was_running, log)
    if not ok:
        log(f"[INFO] {name}: new container did not come up - rolling back.")
        try:
            client.remove_container(new_id, eid)
            client.container_action(old_id, "rename", eid, params="name=" + quote(name))
            client.container_action(old_id, "start", eid)
            log(f"[INFO] {name}: old container restored.")
        except PortainerError as e2:
            log(f"[ERROR] {name}: rollback failed: {e2} - check it in Portainer!")
        return False
    if ok:
        try:
            client.remove_container(old_id, eid)
            log(f"[OK] {name}: old container removed.")
        except PortainerError as e:
            log(f"[WARN] {name}: could not remove the old container ({e}); "
                f"it is stopped as '{name}-portcare-old'.")
    return ok
