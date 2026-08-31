"""The naming standard — shared by the companion and tools/normalize-names.py.

One rule, one place. If the companion derived names differently from the audit
tool, every newly provisioned app would immediately be reported as drift.

THE RULE has two halves, and which half applies depends on who reads the string:

  Identifiers — application slugs, override keys, hostnames; anything a machine
    matches on — are ALWAYS lowercase. They are code, not prose.

  Display names — the label a human reads in the Authentik portal — follow the
    UPSTREAM PROJECT'S OWN SPELLING: qBittorrent, UniFi, WireGuard, Home
    Assistant, Sonarr. Not a house style imposed on top of them; a lowercase
    portal would spell every one of those differently from the project itself,
    which reads as a mistake rather than as a style.

So, in precedence order, a display name is:

  1. The override for the slug, used VERBATIM. This is where upstream spelling
     lives, and nothing downstream is allowed to re-case it.
  2. Acronym expansion for whole words that are known initialisms (db → DB).
  3. Title case, dashes and underscores as word separators — the fallback for
     things with no upstream brand at all (logs, metrics, ops, win-control).

A DOMAIN MAP then says what family a host belongs to, keyed on its apex, and
drives two separate things:

  group   the Authentik portal section the application appears under. This is
          the one that actually organises the dashboard, and it costs nothing
          per-app — a host added tomorrow lands in the right section without
          anyone remembering to configure it.
  prefix  an optional prefix on the display name itself. Usually you want the
          group OR the prefix, not both: inside an "Acta" section, an app named
          "Acta Database" reads as "Acta / Acta Database". A name that already
          begins with the prefix is left alone rather than doubled.

With no overrides file present, rule 3 alone reproduces exactly what the
companion did before this module existed, so behaviour is unchanged until
someone opts in by writing the file.
"""

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_OVERRIDES_FILE = os.environ.get("NAMING_OVERRIDES_FILE", "/data/naming-overrides.json")

# Whole words that should be upper-cased rather than title-cased. Only genuine
# initialisms belong here; anything needing real words is an override.
ACRONYMS = {"db", "dns", "ddns", "api", "vpn", "ui", "id", "ip", "ssh", "sql", "nas", "cpu", "gpu"}

# An identifier is lowercase alphanumerics and dashes. Anything else is a
# violation of the first half of the rule.
IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def is_identifier(value: str) -> bool:
    """True if `value` obeys the lowercase identifier rule."""
    return bool(IDENTIFIER_RE.fullmatch(value or ""))


def load_overrides(path: str | Path | None = None) -> dict[str, str]:
    """Load {slug: display name}. A missing file is normal and not an error.

    Keys are identifiers, so they are lower-cased on the way in and a warning is
    logged for any that were not already — silently accepting 'Sonarr' as a key
    would mean the override never matches the slug it was written for.
    """
    path = Path(path or DEFAULT_OVERRIDES_FILE)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Cannot read naming overrides %s: %s — using defaults", path, exc)
        return {}

    # Accept either a bare mapping or {"overrides": {...}} so the file can carry
    # comments/metadata alongside the data.
    mapping = raw.get("overrides", raw) if isinstance(raw, dict) else {}
    if not isinstance(mapping, dict):
        log.warning("Naming overrides %s is not an object — using defaults", path)
        return {}

    out = {}
    for key, value in mapping.items():
        key, value = str(key).strip(), str(value).strip()
        if not value:
            continue
        if not is_identifier(key.lower()):
            log.warning("Naming override key %r is not a valid slug — ignoring", key)
            continue
        if key != key.lower():
            log.warning("Naming override key %r should be lowercase — using %r",
                        key, key.lower())
        out[key.lower()] = value
    return out


def load_domains(path: str | Path | None = None) -> dict[str, dict]:
    """Load {apex domain: {"prefix": str, "group": str}} from the overrides file.

    One map drives both the optional name prefix and the Authentik portal section,
    because they answer the same question — what family does this host belong to.
    """
    path = Path(path or DEFAULT_OVERRIDES_FILE)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}
    mapping = raw.get("domains") if isinstance(raw, dict) else None
    if not isinstance(mapping, dict):
        return {}
    out = {}
    for apex, cfg in mapping.items():
        if not isinstance(cfg, dict):
            continue
        out[str(apex).strip().lower().lstrip(".")] = {
            "prefix": str(cfg.get("prefix", "")).strip(),
            "group": str(cfg.get("group", "")).strip(),
        }
    return out


def domain_config(domain: str, domains: dict[str, dict] | None) -> dict:
    """Return the config for the apex this domain belongs to (subdomains match)."""
    domain = (domain or "").strip().lower().rstrip(".")
    for apex, cfg in (domains or {}).items():
        if domain == apex or domain.endswith(f".{apex}"):
            return cfg
    return {}


SECTION_SEPARATOR = " \u00b7 "  # middle dot; verified to sort predictably under localeCompare


def load_sections(path: str | Path | None = None) -> dict[str, str]:
    """Load {slug: function} — the second level of the portal section label."""
    path = Path(path or DEFAULT_OVERRIDES_FILE)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}
    mapping = raw.get("sections") if isinstance(raw, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip()
            for k, v in mapping.items() if str(v).strip()}


def canonical_group(
    slug: str,
    domain: str | None = None,
    domains: dict[str, dict] | None = None,
    sections: dict[str, str] | None = None,
) -> str:
    """The Authentik portal section label ('' = ungrouped).

    Authentik has exactly one grouping dimension: `Application.group`, a flat
    string. The portal groups on it and sorts the labels with localeCompare, so
    two levels are encoded into the one string — "Plexy \u00b7 Media". Sorting that
    alphabetically yields domain-first, function-second, which is as close to a
    hierarchy as the field allows.

    Ungrouped applications sort ABOVE every named section, so leaving apps
    unassigned floats them to the top of the portal rather than to the bottom.
    """
    family = domain_config(domain, domains).get("group", "")
    section = (sections or {}).get((slug or "").strip().lower(), "")
    if family and section:
        return f"{family}{SECTION_SEPARATOR}{section}"
    return family or section


def prefix_for(domain: str, domains: dict[str, dict] | None) -> str:
    """Return the name prefix configured for this domain, if any."""
    return domain_config(domain, domains).get("prefix", "")


def canonical_name(
    slug: str,
    overrides: dict[str, str] | None = None,
    domain: str | None = None,
    domains: dict[str, dict] | None = None,
) -> str:
    """Return the display name this slug should have under the standard.

    An override is returned exactly as written — it carries the upstream project's
    own spelling, and re-casing it would defeat the entire point. The domain
    prefix, if any, is applied on top.
    """
    key = (slug or "").strip().lower()
    overrides = overrides or {}
    if key in overrides:
        name = overrides[key]
    else:
        words = [w for w in key.replace("_", "-").split("-") if w]
        if not words:
            return slug
        name = " ".join(w.upper() if w in ACRONYMS else w.capitalize() for w in words)

    prefix = prefix_for(domain, domains)
    if prefix and not name.lower().startswith(f"{prefix.lower()} "):
        name = f"{prefix} {name}"
    return name


def family_for(domain: str, domains: dict[str, dict] | None) -> str:
    """The family label for a domain — the first half of the portal section."""
    return domain_config(domain, domains).get("group", "")


def canonical_provider_name(app_name: str, family: str = "") -> str:
    """Provider names track their application's name, but stay FAMILY-QUALIFIED.

    Application names are user-facing and get their context from the portal
    section heading, so two apps may legitimately both be called "Portainer" —
    Authentik only requires slugs to be unique. Provider names are admin-facing
    and Authentik DOES enforce uniqueness on them, so they carry the family
    explicitly: "Acta Portainer Proxy Provider" versus "Portainer Proxy Provider".
    Without this, aliasing one service under two domains collides on create.
    """
    base = app_name
    if family and not app_name.lower().startswith(f"{family.lower()} "):
        base = f"{family} {app_name}"
    return f"{base} Proxy Provider"
