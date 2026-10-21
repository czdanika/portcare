"""Per-stack update policies: validation, API, scheduler integration."""
import importlib
import time

import pytest


class TestNormalize:
    def test_modes(self):
        from app.policies import PolicyError, normalize
        assert normalize({"mode": "notify"}) == {"mode": "notify"}
        assert normalize({"mode": "schedule", "kind": "weekly", "time": "04:15", "day": 6}) == \
            {"mode": "schedule", "kind": "weekly", "time": "04:15", "day": 6}
        assert normalize({"mode": "schedule"}) == {"mode": "schedule", "kind": "daily", "time": "03:30"}
        for bad in ({"mode": "x"}, {"mode": "schedule", "time": "25:00"},
                    {"mode": "schedule", "kind": "weekly", "day": 9}):
            with pytest.raises(PolicyError):
                normalize(bad)

    def test_default_entries_dropped(self):
        from app.policies import normalize_all
        assert normalize_all({"a": {"mode": "default"}, "b": {"mode": "ignore"}}) == {"b": {"mode": "ignore"}}

    def test_next_due_weekly(self):
        from app.policies import next_due
        base = time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))  # Wed 12:00
        nxt = time.localtime(next_due({"kind": "weekly", "day": 6, "time": "04:00"}, base))
        assert (nxt.tm_wday, nxt.tm_hour, nxt.tm_min) == (6, 4, 0)


class TestEffective:
    def test_default_follows_global(self, tmp_state, monkeypatch):
        from app import config as cfg, policies
        cfg.load(force=True)
        assert policies.effective("web") == "notify"          # global off (default)
        cfg.set("update_schedule_mode", "weekly")
        assert policies.effective("web") == "auto"
        cfg.set("stack_policies", {"db": {"mode": "notify"}, "imm": {"mode": "schedule"},
                                   "old": {"mode": "ignore"}})
        assert policies.effective("db") == "notify"
        assert policies.effective("imm") == "own"
        assert policies.in_global_run("web") and not policies.in_global_run("imm")
        assert policies.in_manual_all("imm") and not policies.in_manual_all("db")
        assert policies.ignored("old")


class TestApi:
    def test_set_policy(self, api_client):
        r = api_client.post("/api/stacks/policy", json={"name": "immich", "mode": "schedule",
                                                        "kind": "weekly", "day": 6, "time": "04:00"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["effective"] == "own" and body["next_run"]
        from app import config as cfg
        cfg.load(force=True)
        assert cfg.get("stack_policies")["immich"]["day"] == 6
        # back to default removes the entry
        r = api_client.post("/api/stacks/policy", json={"name": "immich", "mode": "default"})
        assert r.status_code == 200
        assert "immich" not in cfg.load(force=True)["stack_policies"]

    def test_bad_input(self, api_client):
        assert api_client.post("/api/stacks/policy", json={"name": "../x", "mode": "ignore"}).status_code == 400
        assert api_client.post("/api/stacks/policy", json={"name": "a", "mode": "nope"}).status_code == 400


class TestScheduler:
    def test_due_stack_runs_targeted(self, tmp_state, monkeypatch):
        from app import config as cfg
        cfg.load(force=True)
        cfg.set("stack_policies", {"immich": {"mode": "schedule", "kind": "daily", "time": "04:00"}})
        import app.scheduler as sch
        importlib.reload(sch)
        calls = []

        def fake_run(runlog, stack_filter=None, targeted=False):
            calls.append(([n for n in ("immich", "web") if stack_filter(n)], targeted))
            return True
        monkeypatch.setattr(sch.updater, "run_full_update", fake_run)
        s = sch.Scheduler()
        s._sync_stack_schedules(time.time())
        assert "immich" in s.stack_next
        s.stack_next["immich"]["next"] = time.time() - 1   # make it due
        s._run_due_stacks(time.time())
        assert calls == [(["immich"], True)]
        assert s.stack_next["immich"]["next"] > time.time()  # rescheduled

    def test_global_run_skips_non_default(self, tmp_state, monkeypatch):
        from app import config as cfg
        cfg.load(force=True)
        cfg.set("stack_policies", {"db": {"mode": "notify"}})
        import app.scheduler as sch
        importlib.reload(sch)
        seen = []
        monkeypatch.setattr(sch.updater, "run_full_update",
                            lambda runlog, stack_filter=None, targeted=False:
                            seen.append([n for n in ("db", "web") if stack_filter(n)]) or True)
        s = sch.Scheduler()
        s._run_update("scheduled")
        assert seen == [["web"]]
