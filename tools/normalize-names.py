#!/usr/bin/env python3
"""Audit Authentik application and provider names against the naming standard.

Reports drift, suggests the compliant name, and can apply the fix. Read-only by
default — nothing is changed unless you pass --apply.

Standard library only, so it runs on the Docker host with no pip install. It does
not need Claude, an AI service, or any network access beyond your Authentik.

    # report only (safe, the default)
    ./normalize-names.py --url http://localhost:9000 --token-file ~/token

    # see what --apply would do, in detail
    ./normalize-names.py --token-file ~/token --verbose

    # actually rename
    ./normalize-names.py --token-file ~/token --apply

Exit codes: 0 = everything compliant, 1 = drift found, 2 = error. Suitable for cron.

THE STANDARD lives in app/naming.py and is shared with the companion itself, so
newly provisioned apps are born compliant. Brand spellings (qBittorrent, UniFi,
Home Assistant) come from a JSON overrides file you own and edit — see
naming-overrides.example.json.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from naming import (  # noqa: E402
    canonical_group, canonical_name, canonical_provider_name, family_for,
    is_identifier, load_domains, load_overrides, load_sections,
)

DEFAULT_OVERRIDES = Path(__file__).resolve().parent / "naming-overrides.json"


# ── Authentik REST access (stdlib only) ───────────────────────────────────────

class Authentik:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"detail": body.decode(errors="replace")[:300]}
        except urllib.error.URLError as exc:
            raise SystemExit(f"error: cannot reach Authentik at {self.url}: {exc.reason}")

    def get_all(self, path: str) -> list[dict]:
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            status, data = self._request("GET", f"{path}{sep}page={page}&page_size=100")
            if status == 403:
                raise SystemExit(
                    f"error: token lacks read access to {path} (HTTP 403).\n"
                    "       The companion service account needs view_application and "
                    "view_proxyprovider."
                )
            if status != 200:
                raise SystemExit(f"error: GET {path} returned HTTP {status}: {data}")
            out.extend(data.get("results", []))
            total = (data.get("pagination") or {}).get("total_pages") or 1
            if page >= total:
                return out
            page += 1

    def get_one(self, path: str) -> dict | None:
        status, data = self._request("GET", path)
        return data if status == 200 else None

    def applications_via_providers(self, providers: list[dict]) -> list[dict]:
        """Enumerate applications through their providers, not the list endpoint.

        Authentik's object-level permission filtering silently truncates
        /core/applications/ for a non-admin token — a scoped service account sees
        a fraction of what exists. Retrieving each application directly by slug is
        not filtered, and every application the companion manages necessarily has
        a proxy provider pointing at it.
        """
        apps, seen = [], set()
        for provider in providers:
            slug = provider.get("assigned_application_slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            app = self.get_one(f"/api/v3/core/applications/{urllib.parse.quote(slug)}/")
            if app:
                apps.append(app)
        return apps

    def patch(self, path: str, payload: dict) -> tuple[bool, str]:
        status, data = self._request("PATCH", path, payload)
        if status in (200, 201):
            return True, ""
        if status == 403:
            return False, "HTTP 403 — token lacks change permission"
        return False, f"HTTP {status}: {json.dumps(data)[:200]}"


# ── audit ─────────────────────────────────────────────────────────────────────

def host_of(external_host: str) -> str:
    return (external_host or "").split("://", 1)[-1].rstrip("/")


def derived_slug(host: str) -> str:
    label = host.split(".")[0] if host else ""
    return "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")


def audit(apps: list[dict], providers: list[dict], overrides: dict,
          domains: dict | None = None, sections: dict | None = None) -> dict:
    by_pk = {p["pk"]: p for p in providers}
    rows, notes = [], []

    # First pass: what should each application be called, and what bare provider
    # name does that imply. Two apps may share a display name (Authentik only
    # requires unique slugs) but provider names MUST be unique, so any bare name
    # wanted by more than one app gets family-qualified in the second pass.
    plan = []
    for app in sorted(apps, key=lambda a: a["slug"]):
        provider = by_pk.get(app.get("provider")) if app.get("provider") else None
        host = host_of(provider.get("external_host")) if provider else ""
        domain = host.split(".", 1)[1] if "." in host else ""
        plan.append({
            "app": app, "provider": provider, "host": host, "domain": domain,
            "want_app": canonical_name(app["slug"], overrides, domain, domains),
            "family": family_for(domain, domains),
        })

    # Provider names are ALWAYS family-qualified where a family exists — not just
    # where two apps happen to collide today. Conditional qualification would make
    # a provider's correct name depend on the whole application set, so adding one
    # host tomorrow would silently make an existing, untouched provider "wrong".

    for item in plan:
        app, provider = item["app"], item["provider"]
        slug, host, domain = app["slug"], item["host"], item["domain"]

        if provider is None:
            notes.append({
                "slug": slug, "kind": "no-provider",
                "detail": "application has no proxy provider — not managed by the companion, "
                          "skipped entirely",
            })
            continue

        want_app = item["want_app"]
        want_prov = canonical_provider_name(want_app, item["family"])

        if app["name"] != want_app:
            rows.append({"kind": "application", "slug": slug, "path":
                         f"/api/v3/core/applications/{urllib.parse.quote(slug)}/",
                         "current": app["name"], "suggested": want_app,
                         "field": "name", "host": host})

        collision_form = canonical_provider_name(host)  # v5's cross-apex tie-breaker
        if provider["name"] not in (want_prov, collision_form):
            rows.append({"kind": "provider", "slug": slug, "path":
                         f"/api/v3/providers/proxy/{provider['pk']}/",
                         "current": provider["name"], "suggested": want_prov,
                         "field": "name", "host": host,
                         # A partial PATCH still runs the full serializer, which
                         # rejects a proxy provider whose mode it cannot see:
                         # "Internal host cannot be empty when forward auth is
                         # disabled." Resend the fields that validation reads.
                         "extra": {"mode": provider.get("mode"),
                                   "external_host": provider.get("external_host"),
                                   "internal_host": provider.get("internal_host") or ""}})

        want_group = canonical_group(slug, domain, domains, sections)
        if want_group and (app.get("group") or "") != want_group:
            rows.append({"kind": "group", "slug": slug, "path":
                         f"/api/v3/core/applications/{urllib.parse.quote(slug)}/",
                         "current": app.get("group") or "(none)", "suggested": want_group,
                         "field": "group", "host": host})

        if not is_identifier(slug):
            notes.append({
                "slug": slug, "kind": "slug-not-lowercase",
                "detail": f"slug {slug!r} is not a lowercase identifier — rename it in "
                          "Authentik if you want it to match the standard",
            })

        if host and slug != derived_slug(host):
            notes.append({
                "slug": slug, "kind": "slug-differs",
                "detail": f"slug {slug!r} does not match host {host} "
                          f"(derived {derived_slug(host)!r}) — intentional rename? "
                          "left alone",
            })

    ungrouped = sorted(
        i["app"]["slug"] for i in plan
        if i["provider"] and not canonical_group(
            i["app"]["slug"], i["domain"], domains, sections)
    )
    if ungrouped:
        notes.append({"slug": ", ".join(ungrouped), "kind": "no-section",
                      "detail": f"{len(ungrouped)} application(s) have no portal section; "
                                "ungrouped entries sort ABOVE every named section"})

    wanted = {}
    for r in rows:
        if r["kind"] == "application":
            wanted.setdefault(r["suggested"], []).append(r["slug"])
    for name, slugs in sorted(wanted.items()):
        if len(slugs) > 1:
            notes.append({"slug": ", ".join(slugs), "kind": "name-collision",
                          "detail": f"would all be named {name!r}; provider names are "
                                    "family-qualified so they stay unique"})

    return {"changes": rows, "notes": notes}


# ── output ────────────────────────────────────────────────────────────────────

def render(result: dict, total_apps: int, verbose: bool) -> None:
    changes, notes = result["changes"], result["notes"]

    if changes:
        width_slug = max(len(r["slug"]) for r in changes)
        width_cur = max(len(r["current"]) for r in changes)
        print(f"\nDrift ({len(changes)} change(s) suggested):\n")
        print(f"  {'KIND':<12} {'SLUG':<{width_slug}}  {'CURRENT':<{width_cur}}  SUGGESTED")
        print(f"  {'-'*12} {'-'*width_slug}  {'-'*width_cur}  {'-'*20}")
        for r in changes:
            print(f"  {r['kind']:<12} {r['slug']:<{width_slug}}  "
                  f"{r['current']:<{width_cur}}  → {r['suggested']}")
    else:
        print("\nAll application and provider names match the standard.")

    if notes:
        print(f"\nNotes ({len(notes)}), reported only — nothing here is ever changed:\n")
        for n in notes:
            print(f"  [{n['kind']}] {n['slug']}")
            print(f"      {n['detail']}")

    if verbose and changes:
        print("\nAPI calls --apply would make:\n")
        for r in changes:
            print(f"  PATCH {r['path']}  {json.dumps({r['field']: r['suggested']})}")

    print(f"\n{total_apps} application(s) examined, {len(changes)} change(s) suggested, "
          f"{len(notes)} note(s).")


GRANT_HINT = """
To let the companion's service account rename things, grant the two change
permissions (renaming is not part of its normal job, so it does not have them):

  docker exec authentik ak shell -c "
  from authentik.rbac.models import Role
  Role.objects.get(name='authentik-companion').assign_perms([
      'authentik_core.change_application',
      'authentik_providers_proxy.change_proxyprovider',
  ])
  print('done')
  "

Or run this tool with an admin token instead, and grant nothing.
"""


# ── entry point ───────────────────────────────────────────────────────────────

def read_token(args) -> str:
    if args.token:
        return args.token.strip()
    if args.token_file:
        try:
            return Path(args.token_file).read_text().strip()
        except OSError as exc:
            raise SystemExit(
                f"error: cannot read token file {args.token_file}: {exc}\n"
                "       Docker secrets are usually root-owned 0600 — try sudo."
            )
    env = os.environ.get("AUTHENTIK_TOKEN", "").strip()
    if env:
        return env
    raise SystemExit(
        "error: no API token. Pass --token-file, --token, or set AUTHENTIK_TOKEN."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and normalise Authentik application/provider names.",
        epilog="Read-only unless --apply is given. See --help output above for the standard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=os.environ.get("AUTHENTIK_URL", "http://localhost:9000"),
                        help="Authentik base URL (env: AUTHENTIK_URL)")
    parser.add_argument("--token-file", help="file containing the API token (e.g. a Docker secret)")
    parser.add_argument("--token", help="API token inline (avoid — it lands in shell history)")
    parser.add_argument("--overrides", default=str(DEFAULT_OVERRIDES),
                        help=f"brand-name overrides JSON (default: {DEFAULT_OVERRIDES})")
    parser.add_argument("--only", action="append", metavar="SLUG",
                        help="limit to these application slugs (repeatable). Use to prove "
                             "a change on one app before running the whole set.")
    parser.add_argument("--apply", action="store_true",
                        help="actually rename. Without this, nothing is changed.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="also print the exact API calls --apply would make")
    args = parser.parse_args()

    overrides = load_overrides(args.overrides)
    domains = load_domains(args.overrides)
    sections = load_sections(args.overrides)
    if not overrides and not args.json:
        print(f"note: no overrides loaded from {args.overrides} — brand spellings like "
              f"'qBittorrent' will be suggested as 'Qbittorrent'.\n"
              f"      Copy naming-overrides.example.json next to it to fix that.",
              file=sys.stderr)

    ak = Authentik(args.url, read_token(args))
    providers = ak.get_all("/api/v3/providers/proxy/")
    apps = ak.applications_via_providers(providers)

    # Cross-check against the list endpoint purely to warn about the truncation:
    # if it returns fewer, a scoped token is being filtered and a report built on
    # it would have quietly missed applications.
    listed = ak.get_all("/api/v3/core/applications/")
    if len(listed) < len(apps) and not args.json:
        print(f"note: /core/applications/ returned only {len(listed)} of {len(apps)} apps for "
              f"this token (object-level filtering).\n"
              f"      Enumerated via proxy providers instead — the report is complete.",
              file=sys.stderr)

    # Whatever the list endpoint did surface that has no proxy provider is worth a
    # note: those are hand-made applications the companion does not manage.
    known = {a["slug"] for a in apps}
    apps += [a for a in listed if a["slug"] not in known]

    result = audit(apps, providers, overrides, domains, sections)

    if args.only:
        wanted = {s.strip().lower() for s in args.only}
        result["changes"] = [c for c in result["changes"] if c["slug"].lower() in wanted]
        result["notes"] = [n for n in result["notes"] if n["slug"].lower() in wanted]
        missing = wanted - {c["slug"].lower() for c in result["changes"]}
        if missing and not args.json:
            print(f"note: no pending changes for {', '.join(sorted(missing))}", file=sys.stderr)

    if args.json:
        print(json.dumps({**result, "examined": len(apps), "applied": args.apply}, indent=2))
    else:
        render(result, len(apps), args.verbose)

    if not result["changes"]:
        return 0

    if not args.apply:
        if not args.json:
            print("\nNothing was changed. Re-run with --apply to rename.")
        return 1

    print("\nApplying...\n")
    failed, denied = 0, False
    for r in result["changes"]:
        payload = {r["field"]: r["suggested"]}
        payload.update({k: v for k, v in (r.get("extra") or {}).items() if v is not None})
        ok, err = ak.patch(r["path"], payload)
        if ok:
            print(f"  renamed {r['kind']:<12} {r['current']!r} → {r['suggested']!r}")
        else:
            failed += 1
            denied = denied or "403" in err
            print(f"  FAILED  {r['kind']:<12} {r['current']!r}: {err}")
    if failed:
        print(f"\n{failed} rename(s) failed.")
        if denied:
            print(GRANT_HINT)
        return 1
    print(f"\n{len(result['changes'])} rename(s) applied.")
    print("Application slugs and external hosts were not touched, so the companion's "
          "state file and every Traefik route remain valid.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
