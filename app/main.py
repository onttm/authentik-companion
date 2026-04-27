"""authentik-companion — auto-provisions Authentik proxy apps for Traefik-protected subdomains.

For every HTTP router using the configured authentik middleware chain, this service:
  1. Creates an Authentik Proxy Provider (forward_single mode)
  2. Creates an Authentik Application linked to that provider
  3. Adds the provider to the embedded outpost
  4. Reads the container's authentik.access.group label and binds the named
     group(s) to the application as an access policy

Stale app handling (STALE_ACTION):

  flag (default): when a provisioned host disappears from Traefik, log a WARNING
    each poll with instructions for manual removal. Nothing is deleted automatically.

  remove: after STALE_THRESHOLD_DAYS of continuous absence, automatically delete
    the Authentik Application, Provider, and policy bindings, and remove the provider
    from the outpost. A grace period prevents accidental deletion during routine
    container restarts or maintenance.

Access group binding modes (AUTHENTIK_GROUP_MODE):

  hierarchical (default): label the minimum required group — higher-privilege tiers
    are automatically included. homelab-media → binds media + trusted + admin.

  flat (for Authentik pros only — you have been warned): bind only what you list.

Future: share Traefik discovery with cf-companion for a unified stack-companion.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
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
DOCKER_URL           = os.environ.get("DOCKER_URL", "")
LABEL_KEY            = os.environ.get("AUTHENTIK_LABEL_KEY", "authentik.access.group")
GROUP_MODE           = os.environ.get("AUTHENTIK_GROUP_MODE", "hierarchical").lower()
STALE_ACTION         = os.environ.get("STALE_ACTION", "flag").lower()
STALE_THRESHOLD_DAYS = int(os.environ.get("STALE_THRESHOLD_DAYS", "30"))

_TOKEN_FILE = os.environ.get("AUTHENTIK_TOKEN_FILE", "/run/secrets/authentik_token")
_TOKEN_ENV  = os.environ.get("AUTHENTIK_TOKEN", "")

# Tier order: index 0 = lowest privilege, index 3 = highest.
# In hierarchical mode, labelling an app with tier N automatically binds tiers N..3.
_TIER_ORDER: list[str] = [
    g for g in [
        os.environ.get("AUTHENTIK_GROUP_GUEST"),
        os.environ.get("AUTHENTIK_GROUP_MEDIA"),
        os.environ.get("AUTHENTIK_GROUP_TRUSTED"),
        os.environ.get("AUTHENTIK_GROUP_ADMIN"),
    ] if g
]

_STANDARD_GROUPS: list[str] = _TIER_ORDER[:]

_DOMAIN_RE = re.compile(r'^([^.]+)\.(.+)$')
_SLUG_RE   = re.compile(r'[^a-z0-9]+')


def _load_token() -> str:
    if _TOKEN_ENV:
        return _TOKEN_ENV
    try:
        return Path(_TOKEN_FILE).read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"Cannot read Authentik token from {_TOKEN_FILE}: {exc}") from exc


# ── state management ──────────────────────────────────────────────────────────

def _load_state() -> tuple[set, dict]:
    """Return (provisioned_hosts, stale_since_map).

    Migrates v1 state (plain list) to v2 format automatically.
    """
    if not STATE_FILE.exists():
        return set(), {}
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, list):
            log.info("Migrating state file from v1 to v2 format")
            return set(data), {}
        return set(data.get("provisioned", [])), data.get("stale_since", {})
    except Exception:
        log.warning("State file corrupt, starting fresh")
        return set(), {}


def _save_state(provisioned: set, stale_since: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "version": 2,
        "provisioned": sorted(provisioned),
        "stale_since": stale_since,
    }, indent=2))


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _resolve_groups(label_value: str) -> list[str]:
    requested = [g.strip() for g in label_value.split(",") if g.strip()]
    if GROUP_MODE != "hierarchical" or not _TIER_ORDER:
        return requested
    result: set[str] = set()
    for group in requested:
        if group in _TIER_ORDER:
            result.update(_TIER_ORDER[_TIER_ORDER.index(group):])
        else:
            result.add(group)
    return list(result)


# ── main loop ─────────────────────────────────────────────────────────────────

_REMOVE_PERMISSION_FIX = """\
docker exec authentik ak shell -c "
from authentik.core.models import User
from django.contrib.auth.models import Permission
user = User.objects.get(username='authentik-companion')
for app_label, codename in [
    ('authentik_core',            'delete_application'),
    ('authentik_providers_proxy', 'delete_proxyprovider'),
]:
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label=app_label, codename=codename)
    )
print('done')
" 2>&1 | tail -1"""


def _check_remove_permissions(ak: AuthentikClient) -> None:
    """Warn if the service account lacks delete permissions required by STALE_ACTION=remove."""
    log.info("Checking delete permissions for STALE_ACTION=remove...")
    can_del_app, can_del_provider = ak.check_delete_permissions()
    missing = []
    if not can_del_app:
        missing.append("authentik_core.delete_application")
    if not can_del_provider:
        missing.append("authentik_providers_proxy.delete_proxyprovider")
    if not missing:
        log.info("  Delete permissions OK")
        return
    log.error("STALE_ACTION=remove is set but the service account is missing permissions: %s", ", ".join(missing))
    log.error("Stale apps will NOT be removed until this is fixed. Run:")
    log.error(_REMOVE_PERMISSION_FIX)
    log.error("Then restart authentik-companion.")


def run() -> None:
    token   = _load_token()
    traefik = TraefikClient(TRAEFIK_URL)
    ak      = AuthentikClient(AUTHENTIK_URL, token)
    docker  = DockerClient(DOCKER_URL) if DOCKER_URL else None

    log.info("Starting authentik-companion")
    log.info("  Traefik:      %s", TRAEFIK_URL)
    log.info("  Authentik:    %s", AUTHENTIK_URL)
    log.info("  Outpost:      %s", AUTHENTIK_OUTPOST)
    log.info("  Middleware:   %s", AUTHENTIK_MIDDLEWARE)
    log.info("  Interval:     %ds", POLL_INTERVAL)
    log.info("  Docker:       %s", DOCKER_URL or "disabled")
    log.info("  Label key:    %s", LABEL_KEY)
    log.info("  Stale action: %s%s", STALE_ACTION,
             f" (threshold: {STALE_THRESHOLD_DAYS}d)" if STALE_ACTION == "remove" else "")

    if GROUP_MODE == "hierarchical":
        log.info("  Group mode:   hierarchical — label minimum tier, higher tiers auto-included")
        if _TIER_ORDER:
            log.info("  Tier order:   %s", " → ".join(_TIER_ORDER))
    else:
        log.warning("  Group mode:   flat — FOR AUTHENTIK PROS ONLY. Higher tiers NOT auto-included.")

    log.info("Resolving flows and outpost on startup...")
    auth_flow = inval_flow = None
    wait = 10
    while auth_flow is None:
        try:
            auth_flow  = ak.get_flow_uuid(AUTH_FLOW_SLUG)
            inval_flow = ak.get_flow_uuid(INVAL_FLOW_SLUG)
        except Exception as exc:
            log.warning("Authentik not ready (%s) — retrying in %ds...", exc, wait)
            time.sleep(wait)
            wait = min(wait * 2, 60)
    log.info("  auth_flow=%s  invalidation_flow=%s", auth_flow[:8], inval_flow[:8])

    if STALE_ACTION == "remove":
        _check_remove_permissions(ak)

    if _STANDARD_GROUPS:
        log.info("Ensuring standard groups exist in Authentik...")
        for name in _STANDARD_GROUPS:
            ak.find_or_create_group(name)
            log.info("  Group ready: %r", name)

    provisioned, stale_since = _load_state()
    log.info("Loaded %d provisioned host(s), %d stale", len(provisioned), len(stale_since))

    while True:
        try:
            _poll(traefik, ak, docker, auth_flow, inval_flow, provisioned, stale_since)
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
    stale_since: dict,
) -> None:
    host_groups: dict[str, str] = docker.get_host_access_groups(LABEL_KEY) if docker else {}

    hosts   = traefik.get_protected_hosts(AUTHENTIK_MIDDLEWARE)
    active  = {e["host"] for e in hosts}
    new     = [e for e in hosts if e["host"] not in provisioned]

    log.info("Poll: %d protected router(s), %d new", len(hosts), len(new))

    # ── stale detection ───────────────────────────────────────────────────────
    _check_stale(ak, provisioned, active, stale_since)
    _save_state(provisioned, stale_since)

    # ── provision new hosts ───────────────────────────────────────────────────
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
        provider_is_new = provider_pk is None
        if provider_is_new:
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
            # Pre-existing provider may be linked to an app whose slug differs
            # from what we'd derive from the hostname (e.g. qbit vs qbittorrent).
            linked_slug = ak.get_provider_application_slug(provider_pk)
            if linked_slug:
                app_slug = linked_slug

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
        outpost = ak.get_outpost(AUTHENTIK_OUTPOST)
        ak.add_provider_to_outpost(outpost, provider_pk)
        log.info("  Added provider %d to outpost", provider_pk)

        # ── access-group binding ──────────────────────────────────────────────
        access_label = host_groups.get(host, "")
        if access_label:
            groups_to_bind = _resolve_groups(access_label)
            for group_name in groups_to_bind:
                group_uuid = ak.find_or_create_group(group_name)
                ak.bind_group_to_application(app_uuid, group_uuid)
            log.info("  Access groups bound: %s", ", ".join(groups_to_bind))
        else:
            log.info("  No access-group label — open to all authenticated users")

        stale_since.pop(host, None)
        provisioned.add(host)
        _save_state(provisioned, stale_since)
        log.info("  Done: %s", host)


def _check_stale(
    ak: AuthentikClient,
    provisioned: set,
    active_hosts: set,
    stale_since: dict,
) -> None:
    now = datetime.now(timezone.utc)

    for host in list(provisioned):
        if host in active_hosts:
            if host in stale_since:
                del stale_since[host]
                log.info("Host %s is active again — stale marker cleared", host)
            continue

        if host not in stale_since:
            stale_since[host] = now.isoformat()
            log.warning("Stale: %s disappeared from Traefik", host)

        absent_since = datetime.fromisoformat(stale_since[host])
        days_absent  = (now - absent_since).days

        if STALE_ACTION == "remove" and days_absent >= STALE_THRESHOLD_DAYS:
            log.warning("Stale: %s absent %dd — removing from Authentik", host, days_absent)
            _remove_stale_app(ak, host, provisioned, stale_since)
        elif STALE_ACTION == "remove":
            days_left = STALE_THRESHOLD_DAYS - days_absent
            log.warning(
                "Stale: %s absent %dd — auto-remove in %dd "
                "(Authentik UI → Applications → %s → Delete to remove now)",
                host, days_absent, days_left, _slug(host.split(".")[0]),
            )
        else:
            log.warning(
                "Stale: %s absent %dd — remove manually: "
                "Authentik UI → Applications → %s → Delete",
                host, days_absent, _slug(host.split(".")[0]),
            )
            log.warning(
                "  Set STALE_ACTION=remove + STALE_THRESHOLD_DAYS=%d to auto-remove",
                STALE_THRESHOLD_DAYS,
            )


def _remove_stale_app(
    ak: AuthentikClient,
    host: str,
    provisioned: set,
    stale_since: dict,
) -> None:
    """Delete the Authentik Application, Provider, and outpost membership for a stale host."""
    subdomain = host.split(".")[0]
    app_slug  = _slug(subdomain)

    try:
        app = ak.get_application(app_slug)
        if app is None:
            log.info("  Application %s not found in Authentik — already gone", app_slug)
        else:
            provider_pk = app.get("provider")

            # Remove from outpost before deleting provider (ordering matters)
            if provider_pk:
                try:
                    outpost = ak.get_outpost(AUTHENTIK_OUTPOST)
                    ak.remove_provider_from_outpost(outpost, provider_pk)
                    log.info("  Removed provider %d from outpost", provider_pk)
                except Exception as exc:
                    log.warning("  Could not update outpost: %s", exc)

            ak.delete_application(app_slug)
            log.info("  Deleted application %s", app_slug)

            # Provider is not cascade-deleted with the application — delete separately
            if provider_pk:
                try:
                    ak.delete_provider(provider_pk)
                    log.info("  Deleted provider pk=%d", provider_pk)
                except Exception as exc:
                    log.warning("  Could not delete provider: %s", exc)

        provisioned.discard(host)
        stale_since.pop(host, None)
        log.info("  Stale app %s fully removed", host)

    except Exception as exc:
        log.error("  Failed to remove stale app %s: %s", host, exc)


if __name__ == "__main__":
    run()
