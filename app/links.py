"""'What's new?' links for an image: the project's release notes / changelog.

Best source: the OCI labels of the local image
(org.opencontainers.image.source / .url) - set by most projects and exact
(e.g. ghcr.io/immich-app/immich-server -> github.com/immich-app/immich).
Fallback: a guess from the registry path.
"""
import re

from . import dockerhub

_GH = re.compile(r"^https?://(www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)
_GL = re.compile(r"^https?://(www\.)?gitlab\.com/([^\s#?]+?)(\.git)?/?$", re.I)


def _clean(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url


def from_labels(labels: dict | None) -> str | None:
    """Release-notes URL from OCI labels, or None."""
    labels = labels or {}
    for key in ("org.opencontainers.image.source", "org.label-schema.vcs-url",
                "org.opencontainers.image.url", "org.label-schema.url"):
        url = _clean(labels.get(key) or "")
        m = _GH.match(url)
        if m:
            repo = re.sub(r"\.git$", "", m.group(3))
            return f"https://github.com/{m.group(2)}/{repo}/releases"
        m = _GL.match(url)
        if m:
            return f"https://gitlab.com/{m.group(2)}/-/releases"
    for key in ("org.opencontainers.image.source", "org.opencontainers.image.url"):
        url = _clean(labels.get(key) or "")
        if url.startswith("https://"):
            return url
    return None


def guess(image: str) -> str | None:
    """Best-effort link from the image reference alone."""
    reg, repo, _tag = dockerhub.parse_image((image or "").split("@")[0])
    if not repo:
        return None
    parts = repo.split("/")
    if reg in ("ghcr.io",) and len(parts) >= 2:
        return f"https://github.com/{parts[0]}/{parts[1]}/releases"
    if reg == "lscr.io" or (reg == "docker.io" and parts[0] == "linuxserver"):
        if len(parts) >= 2 and parts[0] == "linuxserver":
            return f"https://github.com/linuxserver/docker-{parts[-1]}/releases"
    if reg == "docker.io":
        if parts[0] == "library":
            return f"https://hub.docker.com/_/{parts[1]}"
        return f"https://hub.docker.com/r/{repo}/tags"
    if reg == "quay.io":
        return f"https://quay.io/repository/{repo}?tab=tags"
    if reg == "registry.gitlab.com":
        return f"https://gitlab.com/{repo}/-/releases"
    return None


def release_url(image: str, labels: dict | None = None) -> str | None:
    return from_labels(labels) or guess(image)
