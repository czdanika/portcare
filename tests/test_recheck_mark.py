from app import checker


def test_mark_updated_clears_build_flags_only():
    checker._last_result = {"checked_at": 1, "stacks": {
        "immich": {"server": {"mode": "digest", "update_available": True, "lag_days": 2},
                   "valkey": {"mode": "semver", "update_available": True}},
        "@nginx": {"nginx": {"mode": "digest", "update_available": True}},
        "other": {"x": {"mode": "digest", "update_available": True}}}}
    checker.mark_updated(["immich", "@nginx"])
    st = checker._last_result["stacks"]
    assert st["immich"]["server"]["update_available"] is False
    assert st["immich"]["valkey"]["update_available"] is True   # tag upgrade stays
    assert st["@nginx"]["nginx"]["update_available"] is False
    assert st["other"]["x"]["update_available"] is True
    assert checker._last_result["checked_at"] == 1


def test_mark_updated_noop():
    checker._last_result = {}
    checker.mark_updated(["a"])
    assert checker._last_result == {}
    checker._last_result = {}
