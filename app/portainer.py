"""Portainer API client (HTTP only - no docker CLI required).

The Docker API is reached through Portainer's docker proxy
(/api/endpoints/<id>/docker/...) so the app works even without
local docker CLI access.
"""
import json
from urllib.parse import quote

import requests
import urllib3
from requests.adapters import HTTPAdapter

# Self-signed Portainer certs are explicitly supported (tls_verify=false) -
# suppress the per-request warning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class PortainerError(RuntimeError):
    pass


class _FingerprintAdapter(HTTPAdapter):
    """Certificate pinning: the TLS handshake only succeeds when the server
    certificate's SHA-256 matches. Works with self-signed certs and bare IP
    addresses (no CA / hostname check needed) - the right fit for a
    Portainer instance on the LAN."""

    def __init__(self, fingerprint, **kw):
        self._fp = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kw):
        kw["assert_fingerprint"] = self._fp
        return super().init_poolmanager(*args, **kw)


_UNSET = object()


def fetch_certificate(url: str, timeout: float = 8.0) -> dict:
    """Read the server certificate WITHOUT validating it (trust-on-first-use
    helper for the Settings 'Pin certificate' button)."""
    import hashlib
    import socket
    import ssl
    from urllib.parse import urlparse

    from cryptography import x509

    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("an https:// URL is required")
    port = u.port or 443
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((u.hostname, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=u.hostname) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("the server did not present a certificate")
    cert = x509.load_der_x509_certificate(der)
    fp = hashlib.sha256(der).hexdigest()
    try:
        not_after = cert.not_valid_after_utc.isoformat()
    except AttributeError:  # cryptography < 42
        not_after = cert.not_valid_after.isoformat()
    return {
        "fingerprint": fp,
        "fingerprint_pretty": ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper(),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "self_signed": cert.subject == cert.issuer,
        "not_after": not_after,
    }


def tls_mode(tls_verify, tls_ca_file, tls_fingerprint) -> str:
    """'pinned' | 'ca' | 'system' | 'off' - for the UI and the logs."""
    if tls_fingerprint:
        return "pinned"
    if tls_verify:
        return "ca" if tls_ca_file else "system"
    return "off"


class Portainer:
    def __init__(self, base_url, api_key, endpoint_id=None, tls_verify=False,
                 tls_ca_file=None, tls_fingerprint=_UNSET):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        if tls_fingerprint is _UNSET:
            from .config import get
            tls_fingerprint = get("tls_fingerprint", "") or ""
        # TLS modes (see tls_mode()):
        #  - pinned: tls_fingerprint set -> cert must match that SHA-256
        #  - ca:     tls_verify + tls_ca_file -> verify against that CA bundle
        #  - system: tls_verify only -> verify against the system CA store
        #  - off:    accept any certificate (Portainer's self-signed default)
        # NOTE: requests treats verify=None as "no verification" - so the
        # verify=True case must be explicit (upstream passed None here).
        if tls_fingerprint:
            self.session.verify = False
            self.session.mount("https://", _FingerprintAdapter(tls_fingerprint))
        elif tls_verify:
            self.session.verify = tls_ca_file or True
        else:
            self.session.verify = False
        self.tls_mode = tls_mode(tls_verify, tls_ca_file, tls_fingerprint)
        self.endpoint_id = endpoint_id

    # ------------------------------------------------------------------ HTTP
    def _req(self, method, path, **kw):
        timeout = kw.pop("timeout", 30)
        # pass verify explicitly: requests lets REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE
        # env vars override session.verify when the per-request value is None
        kw.setdefault("verify", self.session.verify)
        try:
            r = self.session.request(method, self.base + path, timeout=timeout, **kw)
        except requests.RequestException as e:
            msg = str(e)
            if "fingerprints did not match" in msg.lower():
                msg = (f"{method} {path} failed: Portainer's TLS certificate "
                       f"does NOT match the pinned fingerprint. If Portainer's "
                       f"certificate was renewed on purpose, pin it again in "
                       f"Settings - otherwise something is intercepting the "
                       f"connection.")
            elif isinstance(e, requests.exceptions.SSLError) or "SSLError" in msg \
                    or "CERTIFICATE_VERIFY_FAILED" in msg:
                msg = (f"{method} {path} failed: TLS certificate verification "
                       f"failed ({msg[:200]}). This happens when Portainer serves "
                       f"a self-signed certificate. Use 'Pin certificate' in "
                       f"Settings (recommended), or disable TLS verification.")
            raise PortainerError(msg) from e
        if r.status_code >= 400:
            raise PortainerError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    # ---------------------------------------------------------------- system
    def status(self):
        return self._req("GET", "/api/status").json()

    def endpoints(self):
        return self._req("GET", "/api/endpoints").json()

    def resolve_endpoint(self, hostname: str) -> int:
        """Same strategy as the bash script: exact match -> partial -> single local."""
        if self.endpoint_id is not None:
            return self.endpoint_id
        eps = self.endpoints()
        h = (hostname or "").lower()
        for e in eps:
            if (e.get("Name") or "").lower() == h:
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        for e in eps:
            if h and h in (e.get("Name") or "").lower():
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        local = [e for e in eps if e.get("Type") == 1]
        if len(local) == 1:
            self.endpoint_id = local[0]["Id"]
            return self.endpoint_id
        listing = "; ".join(f"ID {e.get('Id')} '{e.get('Name')}' type={e.get('Type')}" for e in eps)
        raise PortainerError(f"Could not resolve endpoint for host '{hostname}'. Set portainer_endpoint_id. Endpoints: {listing}")

    # ---------------------------------------------------------------- stacks
    def stacks(self):
        return self._req("GET", "/api/stacks").json()

    def stack_file(self, stack_id, eid):
        r = self._req("GET", f"/api/stacks/{stack_id}/file?endpointId={eid}")
        return r.json().get("StackFileContent") or ""

    def update_stack(self, stack_id, eid, compose_content, env, prune=True, pull=True):
        payload = {
            "StackFileContent": compose_content,
            "Env": env or [],
            "Prune": prune,
            "PullImage": pull,
        }
        self._req("PUT", f"/api/stacks/{stack_id}?endpointId={eid}",
                  json=payload, timeout=300)

    def redeploy_git_stack(self, stack_id, eid, env=None, prune=True, pull=False):
        """Redeploy a stack created from a git repository. The regular
        update_stack() (PUT /stacks/{id} with StackFileContent) DETACHES git
        stacks from their repository and redeploys the stale local clone -
        new commits would never arrive. This endpoint re-clones first."""
        payload = {
            "Env": env or [],
            "Prune": prune,
            # pull=False: compose rebuilds build: services anyway (>=2.26);
            # a registry pull on build-only services errors out
            "PullImage": pull,
        }
        self._req("PUT", f"/api/stacks/{stack_id}/git/redeploy?endpointId={eid}",
                  json=payload, timeout=600)

    # ---------------------------------------------------------- docker proxy
    def _docker(self, method, path, eid=None, **kw):
        eid = eid or self.endpoint_id
        return self._req(method, f"/api/endpoints/{eid}/docker{path}", **kw)

    def docker_info(self, eid=None):
        return self._docker("GET", "/info", eid).json()

    def containers_by_project(self, project, eid=None, all_=True):
        filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
        return self._docker(
            "GET", f"/containers/json?all={1 if all_ else 0}&filters={filters}", eid
        ).json()

    def all_containers(self, eid=None):
        return self._docker("GET", "/containers/json?all=1", eid).json()

    def inspect_container(self, cid, eid=None):
        return self._docker("GET", f"/containers/{cid}/json", eid).json()

    def image_inspect(self, image_ref, eid=None):
        ref = quote(image_ref, safe=":@")
        return self._docker("GET", f"/images/{ref}/json", eid).json()

    # ------------------------------------------------- standalone containers
    def recreate_container(self, cid, eid=None, pull=True):
        """Portainer's own Recreate (UI: Container > Recreate > Pull image).
        404/405 on Portainer versions without this API -> caller falls back."""
        eid = eid or self.endpoint_id
        return self._req("POST", f"/api/docker/{eid}/containers/{cid}/recreate",
                         json={"PullImage": bool(pull)}, timeout=600)

    def pull_image(self, image_ref, eid=None):
        ref = image_ref.split("@")[0]
        repo, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "latest")
        r = self._docker("POST", f"/images/create?fromImage={quote(repo, safe='')}&tag={quote(tag or 'latest', safe='')}",
                         eid, timeout=900)
        # the pull streams JSON lines; an error is reported INSIDE the stream
        for line in (r.text or "").splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise PortainerError(f"pull {image_ref}: {msg['error']}")
        return True

    def container_action(self, cid, action, eid=None, params=""):
        q = f"?{params}" if params else ""
        r = self._docker("POST", f"/containers/{cid}/{action}{q}", eid, timeout=120)
        return r

    def create_container(self, name, body, eid=None):
        r = self._docker("POST", f"/containers/create?name={quote(name, safe='')}", eid,
                         json=body, timeout=60)
        return r.json().get("Id")

    def connect_network(self, net, cid, endpoint_config, eid=None):
        return self._docker("POST", f"/networks/{quote(net, safe='')}/connect", eid,
                            json={"Container": cid, "EndpointConfig": endpoint_config or {}})

    def remove_container(self, cid, eid=None):
        return self._docker("DELETE", f"/containers/{cid}?force=1&v=0", eid)

    # ---------------------------------------------------------- prune/backup
    def prune_images(self, eid=None):
        """Portainer admin-only: prune ALL unused images (dangling=false)."""
        filters = quote('{"dangling": ["false"]}', safe="")
        return self._docker("POST", f"/images/prune?filters={filters}", eid).json()

    def prune_build_cache(self, eid=None):
        """Portainer admin-only: prune ALL build cache (all=true)."""
        return self._docker("POST", "/build/prune?all=true", eid).json()

    def images(self, eid=None):
        return self._docker("GET", "/images/json?all=1", eid).json()

    def networks(self, eid=None):
        return self._docker("GET", "/networks", eid).json()

    def network_inspect(self, name_or_id, eid=None):
        return self._docker("GET",
                            f"/networks/{quote(name_or_id, safe='')}",
                            eid).json()

    def connect_network(self, network_id, container_id, eid=None):
        return self._docker("POST", f"/networks/{network_id}/connect", eid,
                            json={"Container": container_id})

    def restart_container(self, cid, eid=None, timeout_s=30):
        return self._docker("POST", f"/containers/{cid}/restart?t={timeout_s}", eid)

    def exec_in_container(self, cid, cmd, eid=None, timeout_s=30):
        """Run a command inside a container via the Portainer docker-proxy
        exec flow. Returns (exit_code, stdout_text). Handles the 8-byte
        frame-header multiplexing of the non-TTY stream."""
        create = self._docker("POST", f"/containers/{cid}/exec", eid,
                              json={"AttachStdout": True, "AttachStderr": True,
                                    "Cmd": cmd if isinstance(cmd, list) else [cmd]})
        exec_id = create.json().get("Id")
        if not exec_id:
            raise PortainerError("exec create returned no Id")
        r = self._docker("POST", f"/exec/{exec_id}/start", eid,
                         json={"Detach": False, "Tty": False}, timeout=timeout_s)
        # demultiplex the docker stream (non-TTY): 8-byte frames
        # [stream_type(1), 0,0,0, size(4 BE)]
        stdout = bytearray()
        buf = r.content
        i = 0
        while i + 8 <= len(buf):
            size = int.from_bytes(buf[i + 4:i + 8], "big")
            stdout.extend(buf[i + 8:i + 8 + size])
            i += 8 + size
        try:
            insp = self._docker("GET", f"/exec/{exec_id}/json", eid).json()
            code = insp.get("ExitCode", 0)
        except PortainerError:
            code = 0
        return code, stdout.decode("utf-8", errors="replace").strip()

    def backup(self, password=None):
        """Download a Portainer datastore backup (tar.gz attachment).
        Uses the app's session key as auth (admin required)."""
        payload = {"password": password} if password else {}
        r = self._req("POST", "/api/backup", json=payload, timeout=300)
        return r.content