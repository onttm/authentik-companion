"""authentik-companion — auto-provisions Authentik proxy apps for Traefik-protected subdomains.

For every HTTP router using the configured authentik middleware chain, this service:
  1. Creates an Authentik Proxy Provider (forward_single mode)
  2. Creates an Authentik Application linked to that provider
  3. Adds the provider to the embedded outpost
  4. Reads the container's authentik.access.group label and binds the named
     group(s) to the application as an access policy

Host filtering (TRAEFIK_INCLUDED_HOST / TRAEFIK_EXCLUDED_HOST):

  Regex allow/deny lists applied after the middleware match, so a host can stay
  behind authentik while its Authentik app is managed by hand. Excluding a host
  that was already provisioned releases it from state — nothing is deleted.

Reconciliation (REFRESH_ENTRIES):

  off (default): a host is provisioned once and never revisited. Changing its
    authentik.access.group label has no effect until state is cleared.

  on: every poll re-checks known hosts — binds newly-labelled groups, unbinds
    groups this companion previously bound that are no longer labelled, repairs
    missing outpost membership, and re-provisions apps deleted out from under it.

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

DRY_RUN=true logs every create/delete/bind it would perform and writes nothing —
to Authentik or to the state file.

Future: share Traefik discovery with cf-companion for a unified stack-companion.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from authentik import AuthentikClient
from docker import DockerClient
from naming import (
    canonical_group, canonical_name, canonical_provider_name, family_for,
    load_domains, load_overrides, load_sections,
)
from traefik import TraefikClient

STATE_VERSION = 3


# ── configuration helpers ─────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    """Read config from NAME_FILE (Docker secret) when set, else NAME.

    The _FILE convention is honoured for every variable, matching cf-companion,
    so any setting can be delivered as a secret rather than an env var.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read {name}_FILE={path}: {exc}") from exc
    return os.environ.get(name, default)


def _env_required(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} is required (set {name} or {name}_FILE)")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    value = _env(name).strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "enable", "enabled")


def _env_list(prefix: str, limit: int = 100) -> list[str]:
    """Collect PREFIX, PREFIX1, PREFIX2, ... — cf-companion's indexed convention."""
    values = []
    base = _env(prefix)
    if base:
        values.append(base)
    for i in range(1, limit + 1):
        value = _env(f"{prefix}{i}")
        if not value:
            break
        values.append(value)
    return values


def _compile(patterns: list[str], label: str) -> list[re.Pattern]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise RuntimeError(f"{label} contains an invalid regex {pattern!r}: {exc}") from exc
    return compiled


# ── logging ───────────────────────────────────────────────────────────────────

_LOG_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
)


def _setup_logging() -> None:
    """Configure console and/or rotating file logging (cf-companion's LOG_TYPE).

    Console-only by default, so `docker logs` behaves exactly as before. Set
    LOG_TYPE=FILE or BOTH to also write LOG_PATH/LOG_FILE for stacks without a
    log viewer. Rotation is handled in-process by RotatingFileHandler — there is
    no logrotate in this image, and an unbounded log file on a 60s poll loop
    would eventually fill the volume.

    A file destination that cannot be written never takes the service down: it
    falls back to console and says why.
    """
    log_type = (_env("LOG_TYPE", "CONSOLE") or "CONSOLE").upper()
    level = getattr(logging, (_env("LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    def add_console() -> None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_LOG_FORMAT)
        root.addHandler(handler)

    if log_type in ("CONSOLE", "BOTH"):
        add_console()

    if log_type not in ("FILE", "BOTH"):
        if not root.handlers:
            add_console()
            logging.getLogger("authentik-companion").warning(
                "LOG_TYPE=%s is not one of CONSOLE/FILE/BOTH — logging to console", log_type)
        return

    path = Path(_env("LOG_PATH", "/logs/")) / _env("LOG_FILE", "authentik-companion.log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=int(_env("LOG_FILE_MAX_MB", "10")) * 1024 * 1024,
            backupCount=int(_env("LOG_FILE_RETAIN", "5")),
            encoding="utf-8",
        )
        handler.setFormatter(_LOG_FORMAT)
        root.addHandler(handler)
    except OSError as exc:
        if not root.handlers:
            add_console()
        logging.getLogger("authentik-companion").error(
            "Cannot write log file %s: %s — falling back to console only. If the log "
            "directory is bind-mounted, chown it to the uid this container runs as.",
            path, exc)


_setup_logging()
log = logging.getLogger("authentik-companion")


# ── configuration ─────────────────────────────────────────────────────────────

TRAEFIK_URL          = _env_required("TRAEFIK_URL")
AUTHENTIK_URL        = _env_required("AUTHENTIK_URL")
AUTHENTIK_OUTPOST    = _env("AUTHENTIK_OUTPOST_NAME", "authentik Embedded Outpost")
AUTHENTIK_MIDDLEWARE = _env("AUTHENTIK_MIDDLEWARE", "chain-authentik")
AUTH_FLOW_SLUG       = _env("AUTHENTIK_AUTH_FLOW", "default-authentication-flow")
AUTHZ_FLOW_SLUG      = _env("AUTHENTIK_AUTHZ_FLOW", "default-provider-authorization-implicit-consent")
INVAL_FLOW_SLUG      = _env("AUTHENTIK_INVALIDATION_FLOW", "default-provider-invalidation-flow")
POLL_INTERVAL        = int(_env("POLL_INTERVAL", "60"))
STATE_FILE           = Path(_env("STATE_FILE", "/data/provisioned.json"))
HEARTBEAT_FILE       = Path(_env("HEARTBEAT_FILE", "/data/heartbeat"))
DOCKER_URL           = _env("DOCKER_URL")
LABEL_KEY            = _env("AUTHENTIK_LABEL_KEY", "authentik.access.group")
GROUP_MODE           = _env("AUTHENTIK_GROUP_MODE", "hierarchical").lower()
STALE_ACTION         = _env("STALE_ACTION", "flag").lower()
STALE_THRESHOLD_DAYS = int(_env("STALE_THRESHOLD_DAYS", "30"))
DRY_RUN              = _env_flag("DRY_RUN", False)
REFRESH_ENTRIES      = _env_flag("REFRESH_ENTRIES", False)
NAMING_OVERRIDES     = load_overrides(_env("NAMING_OVERRIDES_FILE") or None)
NAMING_DOMAINS       = load_domains(_env("NAMING_OVERRIDES_FILE") or None)
NAMING_SECTIONS      = load_sections(_env("NAMING_OVERRIDES_FILE") or None)
LOG_TYPE             = (_env("LOG_TYPE", "CONSOLE") or "CONSOLE").upper()
LOG_PATH             = _env("LOG_PATH", "/logs/")
LOG_FILE             = _env("LOG_FILE", "authentik-companion.log")

INCLUDED_HOSTS = _compile(_env_list("TRAEFIK_INCLUDED_HOST") or [".*"], "TRAEFIK_INCLUDED_HOST")
EXCLUDED_HOSTS = _compile(_env_list("TRAEFIK_EXCLUDED_HOST"), "TRAEFIK_EXCLUDED_HOST")

_TOKEN_FILE = os.environ.get("AUTHENTIK_TOKEN_FILE", "/run/secrets/authentik_token")
_TOKEN_ENV  = os.environ.get("AUTHENTIK_TOKEN", "")

# Tier order: index 0 = lowest privilege, index 3 = highest.
# In hierarchical mode, labelling an app with tier N automatically binds tiers N..3.
_TIER_ORDER: list[str] = [
    g for g in [
        _env("AUTHENTIK_GROUP_GUEST"),
        _env("AUTHENTIK_GROUP_MEDIA"),
        _env("AUTHENTIK_GROUP_TRUSTED"),
        _env("AUTHENTIK_GROUP_ADMIN"),
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

def _load_state() -> tuple[set, dict, dict]:
    """Return (provisioned_hosts, stale_since_map, host_records).

    host_records is {host: {"slug": str, "groups": [str]}} — the slug this
    companion actually used, and the groups it actually bound. Migrates v1
    (plain list) and v2 (no records) state automatically; records for older
    entries are backfilled lazily on first reconcile.
    """
    if not STATE_FILE.exists():
        return set(), {}, {}
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, list):
            log.info("Migrating state file from v1 to v%d format", STATE_VERSION)
            return set(data), {}, {}
        version = data.get("version", 2)
        if version < STATE_VERSION:
            log.info("Migrating state file from v%d to v%d format", version, STATE_VERSION)
        return (
            set(data.get("provisioned", [])),
            data.get("stale_since", {}),
            data.get("hosts", {}),
        )
    except Exception:
        log.warning("State file corrupt, starting fresh")
        return set(), {}, {}


def _save_state(provisioned: set, stale_since: dict, records: dict) -> None:
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "version": STATE_VERSION,
        "provisioned": sorted(provisioned),
        "stale_since": stale_since,
        "hosts": records,
    }, indent=2))


def _beat() -> None:
    """Touch the heartbeat file so the Docker healthcheck can see a live poll loop."""
    if DRY_RUN:
        return
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError as exc:
        log.warning("Cannot write heartbeat file %s: %s", HEARTBEAT_FILE, exc)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _resolve_groups(label_value: str) -> list[str]:
    requested = [g.strip() for g in label_value.split(",") if g.strip()]
    if GROUP_MODE != "hierarchical" or not _TIER_ORDER:
        return sorted(set(requested))
    result: set[str] = set()
    for group in requested:
        if group in _TIER_ORDER:
            result.update(_TIER_ORDER[_TIER_ORDER.index(group):])
        else:
            result.add(group)
    return sorted(result)


def _host_allowed(host: str) -> bool:
    """Apply TRAEFIK_EXCLUDED_HOST then TRAEFIK_INCLUDED_HOST (exclude wins).

    Patterns are unanchored (re.search), so `example\\.com` matches every
    subdomain of it. Anchor with ^...$ when you need an exact host.
    """
    if any(p.search(host) for p in EXCLUDED_HOSTS):
        return False
    return any(p.search(host) for p in INCLUDED_HOSTS)


# ── main loop ─────────────────────────────────────────────────────────────────

_REMOVE_PERMISSION_FIX = """\
docker exec authentik ak shell -c "
from authentik.rbac.models import Role
Role.objects.get(name='authentik-companion').assign_perms([
    'authentik_core.delete_application',
    'authentik_providers_proxy.delete_proxyprovider',
])
print('done')
" 2>&1 | tail -1"""

_UNBIND_PERMISSION_FIX = """\
docker exec authentik ak shell -c "
from authentik.rbac.models import Role
Role.objects.get(name='authentik-companion').assign_perms([
    'authentik_policies.delete_policybinding',
])
print('done')
" 2>&1 | tail -1"""


def _check_permissions(ak: AuthentikClient) -> bool:
    """Verify the optional permissions the enabled features need.

    Returns whether group unbinding is available. Missing permissions never abort
    startup — the affected feature degrades and says so, loudly, every poll.
    """
    perms = ak.check_delete_permissions()

    if STALE_ACTION == "remove":
        missing = []
        if perms["application"] == "denied":
            missing.append("authentik_core.delete_application")
        if perms["provider"] == "denied":
            missing.append("authentik_providers_proxy.delete_proxyprovider")
        if missing:
            log.error("STALE_ACTION=remove is set but the service account is missing "
                      "permissions: %s", ", ".join(missing))
            log.error("Stale apps will NOT be removed until this is fixed. Run:")
            log.error(_REMOVE_PERMISSION_FIX)
            log.error("Then restart authentik-companion.")
        else:
            log.info("  Delete permissions: not denied (see README to audit for certain)")

    # The probe can only rule a permission out, so assume unbinding is available
    # unless it was explicitly denied. A real 403 at unbind time is caught and
    # reported per-group by _reconcile.
    can_unbind = perms["policybinding"] != "denied"
    if REFRESH_ENTRIES and not can_unbind:
        log.error("REFRESH_ENTRIES is set but the service account is missing "
                  "authentik_policies.delete_policybinding.")
        log.error("Group bindings will be ADDED but never REMOVED — an app demoted to a "
                  "narrower group keeps its old, wider access. Run:")
        log.error(_UNBIND_PERMISSION_FIX)
        log.error("Then restart authentik-companion.")
    elif REFRESH_ENTRIES:
        log.info("  Unbind permission: not denied — if unbinds fail at runtime, grant "
                 "authentik_policies.delete_policybinding (see README)")

    return can_unbind


def run() -> None:
    token   = _load_token()
    traefik = TraefikClient(TRAEFIK_URL)
    ak      = AuthentikClient(AUTHENTIK_URL, token, dry_run=DRY_RUN)
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
    log.info("  Refresh:      %s", "on — known hosts reconciled every poll"
             if REFRESH_ENTRIES else "off — hosts provisioned once, never revisited")
    log.info("  Logging:      %s%s", LOG_TYPE,
             f" → {Path(LOG_PATH) / LOG_FILE}" if LOG_TYPE in ("FILE", "BOTH") else "")
    if EXCLUDED_HOSTS:
        log.info("  Excluded:     %s", ", ".join(p.pattern for p in EXCLUDED_HOSTS))
    if [p.pattern for p in INCLUDED_HOSTS] != [".*"]:
        log.info("  Included:     %s", ", ".join(p.pattern for p in INCLUDED_HOSTS))

    if DRY_RUN:
        log.warning("  DRY_RUN:      ON — no changes will be made to Authentik or state")

    if GROUP_MODE == "hierarchical":
        log.info("  Group mode:   hierarchical — label minimum tier, higher tiers auto-included")
        if _TIER_ORDER:
            log.info("  Tier order:   %s", " → ".join(_TIER_ORDER))
    else:
        log.warning("  Group mode:   flat — FOR AUTHENTIK PROS ONLY. Higher tiers NOT auto-included.")

    _check_state_writable()

    log.info("Resolving flows and outpost on startup...")
    auth_flow = authz_flow = inval_flow = None
    wait = 10
    while auth_flow is None:
        try:
            auth_flow  = ak.get_flow_uuid(AUTH_FLOW_SLUG)
            authz_flow = ak.get_flow_uuid(AUTHZ_FLOW_SLUG)
            inval_flow = ak.get_flow_uuid(INVAL_FLOW_SLUG)
        except Exception as exc:
            log.warning("Authentik not ready (%s) — retrying in %ds...", exc, wait)
            time.sleep(wait)
            wait = min(wait * 2, 60)
    log.info("  auth_flow=%s  authz_flow=%s  invalidation_flow=%s",
             auth_flow[:8], authz_flow[:8], inval_flow[:8])

    can_unbind = _check_permissions(ak)

    if _STANDARD_GROUPS:
        log.info("Ensuring standard groups exist in Authentik...")
        for name in _STANDARD_GROUPS:
            ak.find_or_create_group(name)
            log.info("  Group ready: %r", name)

    provisioned, stale_since, records = _load_state()
    log.info("Loaded %d provisioned host(s), %d stale", len(provisioned), len(stale_since))

    flows = (auth_flow, authz_flow, inval_flow)

    while True:
        try:
            _poll(traefik, ak, docker, flows, provisioned, stale_since, records, can_unbind)
            _beat()
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc)

        if DRY_RUN:
            # Nothing was persisted, so reload rather than carrying forward the
            # in-memory pretence that this cycle's work happened. Every cycle then
            # reports the same pending changes instead of going quiet after one.
            provisioned, stale_since, records = _load_state()

        time.sleep(POLL_INTERVAL)


def _check_state_writable() -> None:
    """Fail loudly at startup rather than obscurely on the first write.

    The container runs unprivileged, so a /data bind mount owned by another uid
    is the most likely first-run problem.
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        probe = STATE_FILE.parent / ".write-test"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        log.error("State directory %s is not writable: %s", STATE_FILE.parent, exc)
        log.error("If you set `user:` in compose, chown the mounted directory to match "
                  "that uid on the host.")
        raise


def _poll(
    traefik: TraefikClient,
    ak: AuthentikClient,
    docker: DockerClient | None,
    flows: tuple[str, str, str],
    provisioned: set,
    stale_since: dict,
    records: dict,
    can_unbind: bool,
) -> None:
    # None = Docker configured but unreachable; {} = configured and nothing labelled.
    host_groups: dict[str, str] | None = (
        docker.get_host_access_groups(LABEL_KEY) if docker else {}
    )

    discovered = traefik.get_protected_hosts(AUTHENTIK_MIDDLEWARE)
    hosts      = [e for e in discovered if _host_allowed(e["host"])]
    skipped    = len(discovered) - len(hosts)
    active     = {e["host"] for e in hosts}
    new        = [e for e in hosts if e["host"] not in provisioned]

    log.info("Poll: %d protected router(s)%s, %d new", len(hosts),
             f" ({skipped} filtered out)" if skipped else "", len(new))

    # ── release hosts that config now excludes ────────────────────────────────
    _release_filtered(provisioned, stale_since, records)

    # ── stale detection ───────────────────────────────────────────────────────
    # Independent of labels, so it still runs when the Docker API is down.
    _check_stale(ak, provisioned, active, stale_since, records)
    _save_state(provisioned, stale_since, records)

    if host_groups is None:
        log.error("Docker label source unavailable — skipping provisioning and group "
                  "reconciliation this cycle. Acting now would provision apps as "
                  "open-to-all-authenticated and could strip existing bindings.")
        return

    if not new and not REFRESH_ENTRIES:
        return

    # One fetch per poll, shared by reconcile and provisioning.
    providers, provider_names = ak.provider_index()
    outpost = ak.get_outpost(AUTHENTIK_OUTPOST)

    # ── reconcile already-provisioned hosts ───────────────────────────────────
    if REFRESH_ENTRIES:
        known = [e for e in hosts if e["host"] in provisioned]
        if known:
            pk_by_name, name_by_pk = ak.group_index()
            for entry in known:
                try:
                    _reconcile(ak, entry["host"], host_groups, records, provisioned,
                               providers, outpost, pk_by_name, name_by_pk, can_unbind)
                except Exception as exc:
                    log.error("Reconcile of %s failed: %s", entry["host"], exc)
            _save_state(provisioned, stale_since, records)

    # ── provision new hosts ───────────────────────────────────────────────────
    claimed: set[str] = set()
    for entry in new:
        host = entry["host"]
        m = _DOMAIN_RE.match(host)
        if not m:
            log.warning("Cannot parse host %r — skipping", host)
            continue

        # One unprovisionable host must not abort the rest of the batch.
        try:
            _provision(ak, host, m.group(1), m.group(2), flows, host_groups,
                       providers, provider_names, outpost, claimed,
                       provisioned, stale_since, records)
        except Exception as exc:
            log.error("Provisioning %s failed: %s", host, exc)


def _provision(
    ak: AuthentikClient,
    host: str,
    subdomain: str,
    domain: str,
    flows: tuple[str, str, str],
    host_groups: dict,
    providers: dict,
    provider_names: set,
    outpost: dict,
    claimed: set,
    provisioned: set,
    stale_since: dict,
    records: dict,
) -> None:
    """Create the provider, application, outpost membership and bindings for one host."""
    external_url = f"https://{host}"
    # Same rule tools/normalize-names.py audits against, so new apps are born
    # compliant instead of showing up as drift on the next run.
    app_name     = canonical_name(_slug(subdomain), NAMING_OVERRIDES, domain, NAMING_DOMAINS)

    log.info("Provisioning %s", host)

    # ── provider ──────────────────────────────────────────────────────────────
    provider_pk     = providers.get(external_url)
    provider_is_new = provider_pk is None
    if provider_is_new:
        provider_name = _choose_provider_name(app_name, host, provider_names,
                                              family_for(domain, NAMING_DOMAINS))
        provider_pk = ak.create_provider(
            name=provider_name,
            external_host=external_url,
            auth_flow=flows[0],
            authz_flow=flows[1],
            invalidation_flow=flows[2],
            cookie_domain=domain,
        )
        providers[external_url] = provider_pk
        provider_names.add(provider_name)
        log.info("  Created provider pk=%s (%s)", provider_pk, provider_name)
    else:
        log.info("  Provider pk=%s already exists", provider_pk)

    # ── application slug ──────────────────────────────────────────────────────
    # A provider we just created cannot already be linked to an application,
    # and in dry-run mode its pk is synthetic — don't look it up.
    app_slug = _choose_slug(ak, host, external_url,
                            None if provider_is_new else provider_pk, claimed)
    if app_slug is None:
        return

    # ── application ───────────────────────────────────────────────────────────
    app_uuid = ak.find_application(app_slug)
    if app_uuid is None:
        # A qualified slug means another apex already owns the plain name; say which
        # domain this one is, so the Authentik app list doesn't show two "Admin"s.
        display_name = app_name if app_slug == _slug(subdomain) else f"{app_name} ({domain})"
        app_uuid = ak.create_application(
            name=display_name,
            slug=app_slug,
            provider_pk=provider_pk,
            launch_url=external_url,
            group=canonical_group(app_slug, domain, NAMING_DOMAINS, NAMING_SECTIONS),
        )
        log.info("  Created application slug=%s uuid=%s", app_slug, app_uuid[:8])
    else:
        log.info("  Application slug=%s already exists", app_slug)

    # ── outpost ───────────────────────────────────────────────────────────────
    ak.add_provider_to_outpost(outpost, provider_pk)
    log.info("  Added provider %s to outpost", provider_pk)

    # ── access-group binding ──────────────────────────────────────────────────
    access_label = host_groups.get(host, "")
    groups_to_bind = _resolve_groups(access_label) if access_label else []
    for group_name in groups_to_bind:
        ak.bind_group_to_application(app_uuid, ak.find_or_create_group(group_name))
    if groups_to_bind:
        log.info("  Access groups bound: %s", ", ".join(groups_to_bind))
    else:
        log.info("  No access-group label — open to all authenticated users")

    stale_since.pop(host, None)
    provisioned.add(host)
    records[host] = {"slug": app_slug, "groups": groups_to_bind}
    _save_state(provisioned, stale_since, records)
    log.info("  Done: %s", host)


# ── naming ────────────────────────────────────────────────────────────────────

def _choose_provider_name(app_name: str, host: str, existing: set[str],
                          family: str = "") -> str:
    """Pick a provider name Authentik will accept.

    Provider names are unique server-side, and the readable form is derived from
    the subdomain alone — so admin.example.com and admin.example.org both want
    "Admin Proxy Provider" and the second create fails with a 400. Fall back to
    the full host, which is unique by construction.
    """
    preferred = canonical_provider_name(app_name, family)
    if preferred not in existing:
        return preferred
    qualified = canonical_provider_name(host)
    log.warning("  Provider name %r is taken — using %r", preferred, qualified)
    return qualified


# ── slug selection ────────────────────────────────────────────────────────────

def _choose_slug(
    ak: AuthentikClient,
    host: str,
    external_url: str,
    provider_pk: int,
    claimed: set[str] | None = None,
) -> str | None:
    """Pick the Authentik application slug for a host.

    The short leftmost label (`sonarr`) is preferred, because that is what every
    install before v5 produced and what reads well in Authentik. It is only safe
    when nothing else owns it: two apexes can carry the same subdomain
    (admin.example.com and admin.example.org), and the pre-v5 code would silently
    hand the second one the first one's application while orphaning its provider.
    On collision the slug falls back to the full host (`admin-example-org`).

    `claimed` holds slugs handed out earlier in this same poll cycle. Without it
    two colliding hosts in one batch would both be offered the short slug in
    dry-run mode, where neither application actually gets created.

    Returns None when both candidates are taken by other hosts — better to skip
    and complain than to hijack someone else's application.
    """
    claimed = claimed if claimed is not None else set()

    # An existing provider may already be linked to a hand-renamed application
    # (e.g. qbit vs qbittorrent) — that link always wins.
    if provider_pk is not None:
        linked = ak.get_provider_application_slug(provider_pk)
        if linked:
            claimed.add(linked)
            return linked

    short = _slug(host.split(".")[0])
    if short not in claimed and _slug_available(ak, short, external_url):
        claimed.add(short)
        return short

    qualified = _slug(host)
    if qualified not in claimed and _slug_available(ak, qualified, external_url):
        claimed.add(qualified)
        log.warning("  Slug %r is taken by another host — using %r for %s",
                    short, qualified, host)
        return qualified

    log.error("  Both %r and %r are taken by other applications — skipping %s. "
              "Rename the conflicting application in Authentik, or exclude this "
              "host with TRAEFIK_EXCLUDED_HOST.", short, qualified, host)
    return None


def _slug_available(ak: AuthentikClient, slug: str, external_url: str) -> bool:
    """True if the slug is free, or already belongs to this host."""
    app = ak.get_application(slug)
    if app is None:
        return True
    linked_pk = app.get("provider")
    if not linked_pk:
        return False  # an application with no provider is not ours to claim
    provider = ak.get_provider(linked_pk)
    return bool(provider and provider.get("external_host") == external_url)


# ── reconciliation ────────────────────────────────────────────────────────────

def _reconcile(
    ak: AuthentikClient,
    host: str,
    host_groups: dict,
    records: dict,
    provisioned: set,
    providers: dict,
    outpost: dict,
    pk_by_name: dict,
    name_by_pk: dict,
    can_unbind: bool,
) -> None:
    """Bring one already-provisioned host back in line with its Docker label."""
    external_url = f"https://{host}"
    record       = records.get(host, {})
    provider_pk  = providers.get(external_url)

    app_slug = record.get("slug")
    if not app_slug:
        # Backfill for state written before v5: trust the provider link, and fall
        # back to the legacy short slug only if it really points at this host.
        app_slug = ak.get_provider_application_slug(provider_pk) if provider_pk else None
        if not app_slug:
            legacy = _slug(host.split(".")[0])
            if _slug_available(ak, legacy, external_url) and ak.get_application(legacy):
                app_slug = legacy
        if not app_slug:
            log.warning("Reconcile: cannot identify the Authentik application for %s "
                        "— re-provisioning on next poll", host)
            provisioned.discard(host)
            records.pop(host, None)
            return
        record["slug"] = app_slug
        records[host] = record

    app = ak.get_application(app_slug)
    if app is None:
        log.warning("Reconcile: application %s for %s no longer exists "
                    "— re-provisioning on next poll", app_slug, host)
        provisioned.discard(host)
        records.pop(host, None)
        return

    app_uuid = app["pk"]

    # ── outpost membership ────────────────────────────────────────────────────
    if provider_pk and provider_pk not in (outpost.get("providers") or []):
        ak.add_provider_to_outpost(outpost, provider_pk)
        log.info("Reconcile %s: re-added provider %s to outpost", host, provider_pk)

    # ── access groups ─────────────────────────────────────────────────────────
    bound_pks   = {b["group"] for b in ak.list_application_bindings(app_uuid) if b.get("group")}
    bound_names = {name_by_pk[pk] for pk in bound_pks if pk in name_by_pk}

    access_label = host_groups.get(host, "")
    if not access_label:
        # No label means "I am not declaring access rules", not "revoke everything".
        # Pruning here would silently widen an app to all authenticated users the
        # moment a container is recreated without its label. To open an app up,
        # remove its bindings in the Authentik UI.
        record["groups"] = sorted(bound_names)
        records[host] = record
        return

    desired = set(_resolve_groups(access_label))

    # A record written before v5 has no "groups" key — adopt whatever is bound now
    # rather than treating unknown bindings as ours to delete.
    adopting = "groups" not in record
    previously = set(record.get("groups") or [])

    for name in sorted(desired - bound_names):
        ak.bind_group_to_application(app_uuid, ak.find_or_create_group(name))
        log.info("Reconcile %s: bound %s", host, name)

    # Only ever unbind groups this companion recorded binding itself. Bindings
    # added by hand in the Authentik UI are left alone.
    stale_groups = set() if adopting else (previously - desired) & bound_names
    for name in sorted(stale_groups):
        if not can_unbind:
            log.error("Reconcile %s: %s should be unbound but delete_policybinding "
                      "is not granted — access NOT revoked", host, name)
            continue
        try:
            if ak.unbind_group_from_application(app_uuid, pk_by_name[name]):
                log.info("Reconcile %s: unbound %s", host, name)
        except Exception as exc:
            # The startup probe can only rule the permission out, never confirm it,
            # so a 403 can still surface here. Report it per group and keep going
            # rather than failing the whole host.
            log.error("Reconcile %s: could not unbind %s (%s) — access NOT revoked. "
                      "Grant authentik_policies.delete_policybinding; see README.",
                      host, name, exc)

    # Claim only what the label asks for — never the bindings that were already
    # there. Folding those in would make hand-made bindings (an "authentik Admins"
    # or a "friends" group added in the UI) look like ours, and the next time the
    # label narrowed they would be pruned along with it.
    record["groups"] = sorted(desired)
    if adopting:
        adopted_elsewhere = sorted(bound_names - desired)
        log.info("Reconcile %s: now managing %s%s", host,
                 ", ".join(sorted(desired)) or "(none)",
                 f"; leaving {', '.join(adopted_elsewhere)} alone" if adopted_elsewhere else "")

    records[host] = record


def _release_filtered(provisioned: set, stale_since: dict, records: dict) -> None:
    """Drop hosts that config now excludes, without touching Authentik.

    Without this an excluded host looks like it vanished from Traefik and would
    be flagged — or, under STALE_ACTION=remove, eventually deleted.
    """
    for host in list(provisioned):
        if _host_allowed(host):
            continue
        log.warning("Host %s is no longer managed (matches TRAEFIK_EXCLUDED_HOST or falls "
                    "outside TRAEFIK_INCLUDED_HOST) — releasing from state. Its Authentik "
                    "application and provider are left untouched.", host)
        provisioned.discard(host)
        stale_since.pop(host, None)
        records.pop(host, None)


def _check_stale(
    ak: AuthentikClient,
    provisioned: set,
    active_hosts: set,
    stale_since: dict,
    records: dict,
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
        # Cheap best guess, good enough for the "remove it yourself" hints below.
        # The authoritative lookup costs a full provider fetch, so it only runs at
        # the moment of deletion rather than every poll for every flagged host.
        app_slug     = records.get(host, {}).get("slug") or _slug(host.split(".")[0])

        if STALE_ACTION == "remove" and days_absent >= STALE_THRESHOLD_DAYS:
            log.warning("Stale: %s absent %dd — removing from Authentik", host, days_absent)
            _remove_stale_app(ak, host, _resolve_stale_slug(ak, host, records),
                              provisioned, stale_since, records)
        elif STALE_ACTION == "remove":
            days_left = STALE_THRESHOLD_DAYS - days_absent
            log.warning(
                "Stale: %s absent %dd — auto-remove in %dd "
                "(Authentik UI → Applications → %s → Delete to remove now)",
                host, days_absent, days_left, app_slug,
            )
        else:
            log.warning(
                "Stale: %s absent %dd — remove manually: "
                "Authentik UI → Applications → %s → Delete",
                host, days_absent, app_slug,
            )
            log.warning(
                "  Set STALE_ACTION=remove + STALE_THRESHOLD_DAYS=%d to auto-remove",
                STALE_THRESHOLD_DAYS,
            )


def _resolve_stale_slug(ak: AuthentikClient, host: str, records: dict) -> str | None:
    """Work out which Authentik application belongs to a host that has gone stale.

    Deriving the slug from the hostname is a guess, and it is wrong for any
    application renamed by hand (qbit.example.com → "qbittorrent"). The guess then
    finds nothing, the caller concludes the app was already deleted, and the real
    application and provider are orphaned while state says they were cleaned up.

    So: trust what we recorded, then the provider's external_host — which is the
    authoritative link — and only guess as a last resort.
    """
    recorded = records.get(host, {}).get("slug")
    if recorded:
        return recorded

    external_url = f"https://{host}"
    try:
        provider_pk = ak.find_provider(external_url)
        if provider_pk is not None:
            linked = ak.get_provider_application_slug(provider_pk)
            if linked:
                log.info("  Resolved %s to application %s via its provider", host, linked)
                return linked
    except Exception as exc:
        log.warning("  Could not resolve %s via its provider: %s", host, exc)

    derived = _slug(host.split(".")[0])
    if ak.get_application(derived):
        return derived

    # Nothing matched. Returning None keeps the host flagged rather than letting
    # the caller "successfully" delete nothing and forget about it.
    log.warning("  Cannot identify the Authentik application for stale host %s "
                "(no recorded slug, no provider for %s, no application %r). "
                "Leaving it flagged — remove it by hand if it really is stale.",
                host, external_url, derived)
    return None


def _remove_stale_app(
    ak: AuthentikClient,
    host: str,
    app_slug: str | None,
    provisioned: set,
    stale_since: dict,
    records: dict,
) -> None:
    """Delete the Authentik Application, Provider, and outpost membership for a stale host."""
    external_url = f"https://{host}"

    if app_slug is None:
        # Identity unknown — never drop the host from state on a guess that missed.
        return

    try:
        app = ak.get_application(app_slug)
        if app is None:
            log.info("  Application %s not found in Authentik — already gone", app_slug)
        else:
            provider_pk = app.get("provider")

            # Never delete an application that belongs to a different host. Slug
            # collisions across apexes make this a real possibility for state
            # written before v5, where the slug was re-derived rather than recorded.
            provider = ak.get_provider(provider_pk) if provider_pk else None
            if provider and provider.get("external_host") != external_url:
                log.error("  Application %s points at %s, not %s — refusing to delete. "
                          "Remove it by hand if it really is stale.",
                          app_slug, provider.get("external_host"), external_url)
                return

            # Remove from outpost before deleting provider (ordering matters)
            if provider_pk:
                try:
                    outpost = ak.get_outpost(AUTHENTIK_OUTPOST)
                    ak.remove_provider_from_outpost(outpost, provider_pk)
                    log.info("  Removed provider %s from outpost", provider_pk)
                except Exception as exc:
                    log.warning("  Could not update outpost: %s", exc)

            ak.delete_application(app_slug)
            log.info("  Deleted application %s", app_slug)

            # Provider is not cascade-deleted with the application — delete separately
            if provider_pk:
                try:
                    ak.delete_provider(provider_pk)
                    log.info("  Deleted provider pk=%s", provider_pk)
                except Exception as exc:
                    log.warning("  Could not delete provider: %s", exc)

        provisioned.discard(host)
        stale_since.pop(host, None)
        records.pop(host, None)
        log.info("  Stale app %s fully removed", host)

    except Exception as exc:
        log.error("  Failed to remove stale app %s: %s", host, exc)


if __name__ == "__main__":
    run()
