from app import links


def test_links_from_labels():
    assert links.from_labels({"org.opencontainers.image.source": "https://github.com/immich-app/immich"}) \
        == "https://github.com/immich-app/immich/releases"
    assert links.from_labels({"org.opencontainers.image.source": "https://github.com/a/b.git"}) \
        == "https://github.com/a/b/releases"
    assert links.from_labels({}) is None


def test_links_guess():
    assert links.guess("nginx:1.27") == "https://hub.docker.com/_/nginx"
    assert links.guess("valkey/valkey:8") == "https://hub.docker.com/r/valkey/valkey/tags"
    assert links.guess("ghcr.io/czdanika/portcare:latest") == "https://github.com/czdanika/portcare/releases"
    assert links.guess("lscr.io/linuxserver/sonarr:latest") == "https://github.com/linuxserver/docker-sonarr/releases"
    assert links.guess("linuxserver/radarr") == "https://github.com/linuxserver/docker-radarr/releases"
    assert links.guess("192.168.0.5:5000/x:1") is None


def test_changes_diff_and_record(tmp_path, monkeypatch):
    from app import changes
    monkeypatch.setattr(changes, "FILE", tmp_path / "last_updated.json")
    before = {"server": ("sha256:a", "immich:r"), "redis": ("sha256:r", "valkey:8")}
    after = {"server": ("sha256:b", "immich:r"), "redis": ("sha256:r", "valkey:8")}
    d = changes.diff(before, after)
    assert d == {"server": "immich:r"}

    class Owner:
        def __init__(self):
            self.changes = []

        def log(self, m):
            pass
    o = Owner()
    changes.record("immich", d, o.log)
    assert o.changes == [{"name": "immich", "services": ["server"]}]
    assert changes.load()["immich"]["services"] == ["server"]
    changes.record("x", {}, o.log)      # nothing changed -> nothing recorded
    assert "x" not in changes.load()
