"""authentik-companion — auto-provisions Authentik proxy apps for Traefik-protected subdomains.

For every HTTP router using the configured authentik middleware chain, this service:
  1. Creates an Authentik Proxy Provider (forward_single mode)
  2. Creates an Authentik Application linked to that provider
  3. Adds the provider to the embedded outpost
  4. Reads the container's authentik.access.group label and binds the named
     group(s) to the application as an access policy

No label on a container = any authenticated user can reach the app (open default).
Label present = only members of the specified group(s) can reach the app.

File-provider routers (app-*.yml, no container) get no group binding by default.

Covers both file-provider and Docker-label Traefik routers via the Traefik API.

Future: share Traefik discovery with cf-companion for a unified stack-companion.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

from authentik import AuthentikClient
from docker import DockerClient
from traefik import TraefikClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("authentik-companion")

# ── configuration ─────────────────────────────────────────────────────────────

TRAEFIK_URL          = os.environ["TRAEFIK_URL"]
AUTHENTIK_URL        = os.environ["AUTHENTIK_URL"]
AUTHENTIK_OUTPOST    = os.environ.get("AUTHENTIK_OUTPOST_NAME", "authentik Embedded Outpost")
AUTHENTIK_MIDDLEWARE = os.environ.get("AUTHENTIK_MIDDLEWARE", "chain-authentik")
AUTH_FLOW_SLUG       = os.environ.get("AUTHENTIK_AUTH_FLOW", "default-authentication-flow")
INVAL_FLOW_SLUG      = os.environ.get("AUTHENTIK_INVALIDATION_FLOW", "default-provider-invalidation-flow")
POLL_INTERVAL        = int(os.environ.get("POLL_INTERVAL", "60"))
STATE_FILE           = Path(os.environ.get("STATE_FILE", "/data/provisioned.json"))
DOCKER_URL           = os.environ.get("DOCKER_URL", "")          # optional: tcp://socket-proxy:2375
LABEL_KEY            = os.environ.get("AUTHENTIK_LABEL_KEY", "authentik.access.group")

_TOKEN_FILE = os.environ.get("AUTHENTIK_TOKEN_FILE", "/run/secrets/authentik_token")
_TOKEN_ENV  = os.environ.get("AUTHENTIK_TOKEN", "")

# Standard group names — pulled from .env so homelabbers can rename freely.
# authentik-companion creates any of these that don't exist yet in Authentik.
# See .env for descriptions of each tier's intended purpose.
_STANDARD_GROUPS: list[str] = [
    g for g in [
        os.environ.get("AUTHENTIK_GROUP_ADMIN"),
        os.environ.get("AUTHENTIK_GROUP_TRUSTED"),
        os.environ.get("AUTHENTIK_GROUP_MEDIA"),
        os.environ.get("AUTHENTIK_GROUP_GUEST"),
    ] if g
]

_DOMAIN_RE = re.compile(r'^([^.]+)\.(.+)$')
_SLUG_RE   = re.compile(r'[^a-z0-9]+')


def _load_token() -> str:
    if _TOKEN_ENV:
        return _TOKEN_ENV
    try:
        return Path(_TOKEN_FILE).read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"Cannot read Authentik token from {_TOKEN_FILE}: {exc}") from exc


def _load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            log.warning("State file corrupt, starting fresh")
    return set()


def _save_state(provisioned: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(provisioned), indent=2))


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


# ── main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    token   = _load_token()
    traefik = TraefikClient(TRAEFIK_URL)
    ak      = AuthentikClient(AUTHENTIK_URL, token)
    docker  = DockerClient(DOCKER_URL) if DOCKER_URL else None

    log.info("Starting authentik-companion")
    log.info("  Traefik:    %s", TRAEFIK_URL)
    log.info("  Authentik:  %s", AUTHENTIK_URL)
    log.info("  Outpost:    %s", AUTHENTIK_OUTPOST)
    log.info("  Middleware: %s", AUTHENTIK_MIDDLEWARE)
    log.info("  Interval:   %ds", POLL_INTERVAL)
    log.info("  Docker:     %s", DOCKER_URL or "disabled (no access-group label reading)")
    log.info("  Label key:  %s", LABEL_KEY)

    log.info("Resolving flows and outpost on startup...")
    auth_flow  = ak.get_flow_uuid(AUTH_FLOW_SLUG)
    inval_flow = ak.get_flow_uuid(INVAL_FLOW_SLUG)
    log.info("  auth_flow=%s  invalidation_flow=%s", auth_flow[:8], inval_flow[:8])

    if _STANDARD_GROUPS:
        log.info("Ensuring standard groups exist in Authentik...")
        for name in _STANDARD_GROUPS:
            ak.find_or_create_group(name)
            log.info("  Group ready: %r", name)

    provisioned = _load_state()
    log.info("Loaded %d previously provisioned host(s)", len(provisioned))

    while True:
        try:
            _poll(traefik, ak, docker, auth_flow, inval_flow, provisioned)
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc)

        time.sleep(POLL_INTERVAL)


def _poll(
    traefik: TraefikClient,
    ak: AuthentikClient,
    docker: DockerClient | None,
    auth_flow: str,
    inval_flow: str,
    provisioned: set,
) -> None:
    # Refresh label map every poll so new containers are picked up immediately
    host_groups: dict[str, str] = docker.get_host_access_groups(LABEL_KEY) if docker else {}

    hosts = traefik.get_protected_hosts(AUTHENTIK_MIDDLEWARE)
    new   = [e for e in hosts if e["host"] not in provisioned]
    log.info("Poll: %d protected router(s), %d new", len(hosts), len(new))

    if not new:
        return

    outpost = ak.get_outpost(AUTHENTIK_OUTPOST)

    for entry in new:
        host = entry["host"]
        m = _DOMAIN_RE.match(host)
        if not m:
            log.warning("Cannot parse host %r — skipping", host)
            continue

        subdomain, domain = m.group(1), m.group(2)
        external_url = f"https://{host}"
        app_slug     = _slug(subdomain)
        app_name     = subdomain.replace("-", " ").title()

        log.info("Provisioning %s (slug=%s)", host, app_slug)

        # ── provider ──────────────────────────────────────────────────────────
        provider_pk = ak.find_provider(external_url)
        if provider_pk is None:
            provider_pk = ak.create_provider(
                name=f"{app_name} Proxy Provider",
                external_host=external_url,
                auth_flow=auth_flow,
                invalidation_flow=inval_flow,
                cookie_domain=domain,
            )
            log.info("  Created provider pk=%d", provider_pk)
        else:
            log.info("  Provider pk=%d already exists", provider_pk)

        # ── application ───────────────────────────────────────────────────────
        app_uuid = ak.find_application(app_slug)
        if app_uuid is None:
            app_uuid = ak.create_application(
                name=app_name,
                slug=app_slug,
                provider_pk=provider_pk,
                launch_url=external_url,
            )
            log.info("  Created application slug=%s uuid=%s", app_slug, app_uuid[:8])
        else:
            log.info("  Application slug=%s already exists", app_slug)

        # ── outpost ───────────────────────────────────────────────────────────
        outpost = ak.get_outpost(AUTHENTIK_OUTPOST)  # refresh before patching
        ak.add_provider_to_outpost(outpost, provider_pk)
        log.info("  Added provider %d to outpost", provider_pk)

        # ── access-group binding ──────────────────────────────────────────────
        access_label = host_groups.get(host, "")
        if access_label:
            groups = [g.strip() for g in access_label.split(",") if g.strip()]
            for group_name in groups:
                group_uuid = ak.find_or_create_group(group_name)
                ak.bind_group_to_application(app_uuid, group_uuid)
            log.info("  Access restricted to: %s", ", ".join(groups))
        else:
            log.info("  No access-group label — open to all authenticated users")

        provisioned.add(host)
        _save_state(provisioned)
        log.info("  Done: %s", host)


if __name__ == "__main__":
    run()
