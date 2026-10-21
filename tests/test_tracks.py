"""Version-track matching: no variant switches, no prereleases, precision kept."""
import pytest


@pytest.fixture()
def chk(tmp_state, monkeypatch):
    from app import dockerhub
    dockerhub.clear_cache()
    monkeypatch.setattr(dockerhub, "remote_digest", lambda *a, **k: "sha256:abc")

    def run(image, tags):
        monkeypatch.setattr(dockerhub, "newest_semver_tags", lambda r, repo: tags)
        return dockerhub._check_update_uncached(*dockerhub.parse_image(image), None)
    return run


def test_variant_is_kept(chk):
    r = chk("valkey/valkey:8-bookworm", ["9.2-trixie", "9-trixie", "8.1-bookworm", "8-bookworm", "latest"])
    assert r["update_available"] is False  # no 9-bookworm -> nothing on this track


def test_variant_upgrade_same_track(chk):
    r = chk("valkey/valkey:8-bookworm", ["9-bookworm", "9.2-trixie", "8-bookworm"])
    assert r["update_available"] and r["latest_tag"] == "9-bookworm" and r["upgrade"] == "major"


def test_prerelease_never_suggested(chk):
    r = chk("python:3.12-slim", ["3.16-rc-slim-trixie", "3.16-rc-slim", "3.14-slim", "3.12-slim", "3.14"])
    assert r["latest_tag"] == "3.14-slim" and r["upgrade"] == "minor"


def test_precision_kept_and_same_major(chk):
    r = chk("mongo:6.0", ["9.0.4", "9.0", "8.0", "6.0", "6.0.19", "7.0"])
    assert r["latest_tag"] == "9.0" and r["upgrade"] == "major"
    assert r["latest_same_major"] is None
    r = chk("nginx:1.25.3", ["1.27.2", "1.25.5", "2.0.0-beta1", "1.27"])
    assert r["latest_tag"] == "1.27.2" and r["upgrade"] == "minor"


def test_variants_are_not_prereleases():
    from app.dockerhub import _is_prerelease, _track
    for v in ("bookworm", "bullseye", "alpine", "alpine3.24", "apache", "debian"):
        assert not _is_prerelease(v), v
    for v in ("rc1", "rc", "beta.2", "alpha", "b3", "a1", "dev", "rc-slim-trixie"):
        assert _is_prerelease(v), v
    assert _track("8-bookworm") == ((8,), 1, "bookworm", False)


def test_bookworm_never_becomes_alpine(chk):
    r = chk("docker.io/valkey/valkey:8-bookworm", ["9-alpine3.24", "9-trixie", "9.2-alpine", "8-bookworm"])
    assert r["update_available"] is False
