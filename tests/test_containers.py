"""Standalone containers: detection, Portainer recreate, manual fallback + rollback,
and inclusion in update runs."""
import pytest

from app.portainer import PortainerError


class FakeDocker:
    def __init__(self, containers, stacks=(), recreate="ok"):
        self.containers = list(containers)
        self.stacks_list = list(stacks)
        self.recreate_mode = recreate
        self.calls = []
        self.endpoint_id = 3

    def stacks(self):
        return self.stacks_list

    def all_containers(self, eid=None):
        return self.containers

    def inspect_container(self, cid, eid=None):
        c = next(c for c in self.containers if c["Id"] == cid)
        return {"State": {"Running": c["State"] == "running", "Status": c["State"]},
                "Config": {"Image": c["Image"], "Hostname": cid[:12], "Env": ["A=1"]},
                "HostConfig": {"NetworkMode": "bridge", "Binds": ["/data:/data"]},
                "Mounts": [{"Type": "volume", "Name": "a" * 64, "Destination": "/cache"}],
                "NetworkSettings": {"Networks": {"bridge": {}, "extra": {"Aliases": ["x"]}}},
                "Name": "/" + c["Names"][0].lstrip("/")}

    def recreate_container(self, cid, eid=None, pull=True):
        self.calls.append(("recreate", cid))
        if self.recreate_mode == "missing":
            raise PortainerError("POST /api/docker/3/containers/x/recreate -> HTTP 404: not found")
        if self.recreate_mode == "denied":
            raise PortainerError("HTTP 500: pull access denied for foo")
        for c in self.containers:
            if c["Id"] == cid:
                c["Id"] = cid + "-new"

    def pull_image(self, image, eid=None):
        self.calls.append(("pull", image))

    def container_action(self, cid, action, eid=None, params=""):
        self.calls.append((action, cid, params))
        c = next((c for c in self.containers if c["Id"] == cid), None)
        if action == "rename" and c:
            c["Names"] = ["/" + params.split("=", 1)[1]]
        if action == "stop" and c:
            c["State"] = "exited"
        if action == "start" and c:
            c["State"] = "running"

    def create_container(self, name, body, eid=None):
        self.calls.append(("create", name, body))
        if self.recreate_mode == "create_fails":
            raise PortainerError("HTTP 500: bad config")
        self.containers.append({"Id": "new1", "Names": ["/" + name], "Image": body["Image"],
                                "State": "created", "Labels": {}})
        return "new1"

    def connect_network(self, net, cid, cfg, eid=None):
        self.calls.append(("connect", net, cid))

    def remove_container(self, cid, eid=None):
        self.calls.append(("remove", cid))
        self.containers = [c for c in self.containers if c["Id"] != cid]


def _ct(name, image="nginx:1.25", state="running", project=None, cid=None):
    labels = {"com.docker.compose.project": project} if project else {}
    return {"Id": cid or (name + "000000000000"), "Names": ["/" + name], "Image": image,
            "State": state, "Status": "Up", "Labels": labels}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr("app.containers.time.sleep", lambda s: None)


def test_standalone_detection(tmp_state):
    from app import containers as cm
    d = FakeDocker([_ct("homeassistant"), _ct("immich_server", project="immich"),
                    _ct("portainer", image="portainer/portainer-ce:2.21"),
                    _ct("other_compose", project="nas-app"), _ct("x", image="sha256:abc")],
                   stacks=[{"Name": "immich", "EndpointId": 3}])
    names = [c["name"] for c in cm.standalone(d, 3)]
    assert names == ["homeassistant", "other_compose"]


def test_update_via_portainer_recreate(tmp_state):
    from app import containers as cm
    d = FakeDocker([_ct("ha")])
    logs = []
    assert cm.update_container(d, 3, "ha", logs.append) is True
    assert d.calls[0][0] == "recreate"


def test_manual_fallback_keeps_volumes_and_networks(tmp_state):
    from app import containers as cm
    d = FakeDocker([_ct("ha")], recreate="missing")
    logs = []
    assert cm.update_container(d, 3, "ha", logs.append) is True
    create = next(c for c in d.calls if c[0] == "create")
    body = create[2]
    assert ("a" * 64 + ":/cache") in body["HostConfig"]["Binds"]      # anonymous volume kept
    assert "/data:/data" in body["HostConfig"]["Binds"]
    assert "Hostname" not in body                                     # default hostname dropped
    assert ("connect", "extra", "new1") in d.calls                     # 2nd network re-attached
    assert ("remove", "ha000000000000") in d.calls                     # old one removed
    assert [c["name"] for c in cm.standalone(d, 3)] == ["ha"]


def test_manual_fallback_rolls_back(tmp_state):
    from app import containers as cm
    d = FakeDocker([_ct("ha")], recreate="missing")
    d.recreate_mode = "missing"
    orig = d.create_container

    def boom(name, body, eid=None):
        raise PortainerError("HTTP 500: bad config")
    d.create_container = boom
    logs = []
    assert cm.update_container(d, 3, "ha", logs.append) is False
    c = d.containers[0]
    assert c["Names"] == ["/ha"] and c["State"] == "running"          # restored + restarted
    assert any("old container restored" in l for l in logs)


def test_pull_denied_gives_hint(tmp_state):
    from app import containers as cm
    d = FakeDocker([_ct("bot", image="binance-trading-bot:pr708")], recreate="denied")
    logs = []
    assert cm.update_container(d, 3, "bot", logs.append) is False
    assert any("[HINT]" in l for l in logs)


def test_policy_default_is_notify(tmp_state):
    from app import config as cfg, policies
    cfg.load(force=True)
    cfg.set("update_schedule_mode", "daily")
    assert policies.effective("@ha") == "notify"
    assert not policies.in_global_run("@ha") and not policies.in_manual_all("@ha")
    cfg.set("stack_policies", {"@ha": {"mode": "default"}})
    assert policies.effective("@ha") == "auto" and policies.in_global_run("@ha")


def test_run_includes_containers_with_updates(tmp_state, monkeypatch):
    import app.updater as up
    from app import checker
    d = FakeDocker([_ct("ha"), _ct("grafana")])
    checker._last_result = {"stacks": {"@ha": {"ha": {"update_available": True}},
                                       "@grafana": {"grafana": {"update_available": False}}}}
    got = up._containers_to_update(d, 3, lambda n: True, lambda m: None)
    assert got == ["ha"]
    assert up._containers_to_update(d, 3, lambda n: not n.startswith("@"), lambda m: None) == []
