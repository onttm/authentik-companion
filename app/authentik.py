"""Authentik API client — provisions and optionally cleans up proxy providers,
applications, outpost membership, groups, and application access policy bindings.

Every mutating method honours ``dry_run``: it logs the change it *would* make and
returns a synthetic value, so a full poll cycle can be exercised against a live
Authentik without touching it.
"""

import logging
import requests

log = logging.getLogger(__name__)

# Synthetic values returned by create_* in dry-run mode. Chosen so downstream
# logging (which slices [:8]) and dict lookups behave normally.
DRY_PROVIDER_PK = -1
DRY_APPLICATION_UUID = "00000000-dry-run-application"
DRY_GROUP_UUID = "00000000-dry-run-group"


class AuthentikClient:
    def __init__(self, url: str, token: str, dry_run: bool = False):
        self.url = url.rstrip("/")
        self.dry_run = dry_run
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    # ── low-level helpers ────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self._s.get(f"{self.url}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(self, path: str, params: dict = None) -> list[dict]:
        """Fetch every page of a list endpoint.

        Authentik caps page_size server-side; a single large request is not a
        substitute for following pagination once a stack outgrows one page.
        """
        params = dict(params or {})
        params.setdefault("page_size", 100)
        page, out = 1, []
        while True:
            params["page"] = page
            data = self._get(path, params)
            out.extend(data.get("results", []))
            total = (data.get("pagination") or {}).get("total_pages") or 1
            if page >= total:
                return out
            page += 1

    def _post(self, path: str, data: dict) -> dict:
        resp = self._s.post(f"{self.url}{path}", json=data, timeout=10)
        if not resp.ok:
            log.error("POST %s → %d: %s", path, resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict) -> dict:
        resp = self._s.patch(f"{self.url}{path}", json=data, timeout=10)
        if not resp.ok:
            log.error("PATCH %s → %d: %s", path, resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        resp = self._s.delete(f"{self.url}{path}", timeout=10)
        if resp.status_code == 404:
            return  # already gone — deleting an absent object is a success
        if not resp.ok:
            log.error("DELETE %s → %d: %s", path, resp.status_code, resp.text[:500])
        resp.raise_for_status()

    # ── permission probing ───────────────────────────────────────────────────

    def check_delete_permissions(self) -> dict[str, str]:
        """Return {'application'|'provider'|'policybinding': 'denied'|'unverified'}.

        Probes with DELETE on a nonexistent resource. A 403 is conclusive: the
        permission is missing. Anything else is NOT proof it is granted — verified
        on 2026.8.0, an account without delete_policybinding still gets 404 for a
        nonexistent binding, because the object lookup runs before the permission
        check. So the probe can only ever rule a permission *out*.

        The authoritative check is the ak shell audit in the README; this exists to
        catch the common misconfiguration cheaply and without side effects.
        """
        def probe(path: str) -> str:
            return ("denied"
                    if self._s.delete(f"{self.url}{path}", timeout=10).status_code == 403
                    else "unverified")

        return {
            "application":   probe("/api/v3/core/applications/__permission-check__/"),
            "provider":      probe("/api/v3/providers/proxy/999999999/"),
            "policybinding": probe("/api/v3/policies/bindings/"
                                   "00000000-0000-0000-0000-000000000000/"),
        }

    # ── startup discovery ────────────────────────────────────────────────────

    def get_flow_uuid(self, slug: str) -> str:
        data = self._get("/api/v3/flows/instances/", {"slug": slug})
        results = data.get("results", [])
        if not results:
            raise RuntimeError(f"Flow not found: {slug!r}")
        return results[0]["pk"]

    def get_outpost(self, name: str) -> dict:
        data = self._get("/api/v3/outposts/instances/", {"search": name})
        for outpost in data.get("results", []):
            if outpost["name"] == name:
                return outpost
        raise RuntimeError(f"Outpost not found: {name!r}")

    # ── group management ─────────────────────────────────────────────────────

    def group_index(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (pk_by_name, name_by_pk) for every group, fetched once per poll."""
        pk_by_name, name_by_pk = {}, {}
        for g in self._get_paginated("/api/v3/core/groups/"):
            pk_by_name[g["name"]] = g["pk"]
            name_by_pk[g["pk"]] = g["name"]
        return pk_by_name, name_by_pk

    def find_or_create_group(self, name: str) -> str:
        data = self._get("/api/v3/core/groups/", {"search": name})
        for g in data.get("results", []):
            if g["name"] == name:
                return g["pk"]
        if self.dry_run:
            log.info("  DRY-RUN would create group %r", name)
            return DRY_GROUP_UUID
        result = self._post("/api/v3/core/groups/", {"name": name})
        log.info("Created group %r (pk=%s)", name, result["pk"][:8])
        return result["pk"]

    # ── provider management ──────────────────────────────────────────────────

    def provider_index(self) -> tuple[dict[str, int], set[str]]:
        """Return ({external_host: pk}, {name}) for every proxy provider.

        Names come back too because Authentik enforces uniqueness on them: two
        hosts sharing a subdomain across different apexes would otherwise derive
        the same provider name and the second create would fail with a 400.
        """
        providers = self._get_paginated("/api/v3/providers/proxy/")
        by_host = {p["external_host"]: p["pk"] for p in providers if p.get("external_host")}
        names = {p["name"] for p in providers if p.get("name")}
        return by_host, names

    def get_provider(self, provider_pk: int) -> dict | None:
        resp = self._s.get(f"{self.url}/api/v3/providers/proxy/{provider_pk}/", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_provider_application_slug(self, provider_pk: int) -> str | None:
        """Return the slug of the application already linked to this provider, or None."""
        provider = self.get_provider(provider_pk)
        return provider.get("assigned_application_slug") if provider else None

    def find_provider(self, external_host: str) -> int | None:
        # Authentik's ?search= matches names, not URLs — fetch all and filter client-side.
        by_host, _ = self.provider_index()
        return by_host.get(external_host)

    def create_provider(
        self,
        name: str,
        external_host: str,
        auth_flow: str,
        authz_flow: str,
        invalidation_flow: str,
        cookie_domain: str,
    ) -> int:
        if self.dry_run:
            log.info("  DRY-RUN would create provider %r → %s (cookie_domain=%s)",
                     name, external_host, cookie_domain)
            return DRY_PROVIDER_PK
        result = self._post("/api/v3/providers/proxy/", {
            "name": name,
            "authentication_flow": auth_flow,
            "authorization_flow": authz_flow,
            "invalidation_flow": invalidation_flow,
            "external_host": external_host,
            "mode": "forward_single",
            "cookie_domain": cookie_domain,
        })
        return result["pk"]

    def delete_provider(self, provider_pk: int) -> None:
        if self.dry_run:
            log.info("  DRY-RUN would delete provider pk=%s", provider_pk)
            return
        self._delete(f"/api/v3/providers/proxy/{provider_pk}/")

    # ── application management ───────────────────────────────────────────────

    def find_application(self, slug: str) -> str | None:
        app = self.get_application(slug)
        return app["pk"] if app else None

    def get_application(self, slug: str) -> dict | None:
        # Authentik guardian filters the list endpoint — use direct slug retrieve instead.
        resp = self._s.get(f"{self.url}/api/v3/core/applications/{slug}/", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def create_application(
        self, name: str, slug: str, provider_pk: int, launch_url: str, group: str = ""
    ) -> str:
        if self.dry_run:
            log.info("  DRY-RUN would create application %r (slug=%s) → provider pk=%s%s",
                     name, slug, provider_pk, f" in section {group!r}" if group else "")
            return DRY_APPLICATION_UUID
        result = self._post("/api/v3/core/applications/", {
            "name": name,
            "slug": slug,
            "provider": provider_pk,
            "meta_launch_url": launch_url,
            "group": group,  # portal section heading; "" means ungrouped
            "policy_engine_mode": "any",  # OR logic: member of ANY bound group gets access
        })
        return result["pk"]

    def delete_application(self, slug: str) -> None:
        if self.dry_run:
            log.info("  DRY-RUN would delete application %s", slug)
            return
        # Policy bindings cascade-delete with the application automatically.
        self._delete(f"/api/v3/core/applications/{slug}/")

    # ── outpost management ───────────────────────────────────────────────────

    def add_provider_to_outpost(self, outpost: dict, provider_pk: int) -> None:
        """Add a provider, keeping the caller's outpost dict in sync.

        The dict is updated in place on success so a single fetch can serve a whole
        poll cycle — without that, a second add would PATCH a stale provider list
        and silently drop the first.
        """
        current = list(outpost.get("providers") or [])
        if provider_pk in current:
            return
        if self.dry_run:
            log.info("  DRY-RUN would add provider pk=%s to outpost %r",
                     provider_pk, outpost["name"])
            return
        updated = current + [provider_pk]
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": updated,
        })
        outpost["providers"] = updated

    def remove_provider_from_outpost(self, outpost: dict, provider_pk: int) -> None:
        current = list(outpost.get("providers") or [])
        if provider_pk not in current:
            return
        if self.dry_run:
            log.info("  DRY-RUN would remove provider pk=%s from outpost %r",
                     provider_pk, outpost["name"])
            return
        updated = [p for p in current if p != provider_pk]
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": updated,
        })
        outpost["providers"] = updated

    # ── access policy bindings ───────────────────────────────────────────────

    def list_application_bindings(self, app_uuid: str) -> list[dict]:
        """Return every policy binding targeting this application."""
        return self._get_paginated("/api/v3/policies/bindings/", {"target": app_uuid})

    def bind_group_to_application(self, app_uuid: str, group_uuid: str) -> None:
        # No bindings = any authenticated user can access (Authentik default).
        # One or more bindings = only members of a bound group can access (OR logic).
        if self.dry_run:
            log.info("  DRY-RUN would bind group %s to application %s",
                     group_uuid[:8], app_uuid[:8])
            return
        data = self._get("/api/v3/policies/bindings/", {"target": app_uuid})
        for binding in data.get("results", []):
            if binding.get("group") == group_uuid:
                return  # already bound
        self._post("/api/v3/policies/bindings/", {
            "target": app_uuid,
            "group": group_uuid,
            "enabled": True,
            "order": 0,
            "negate": False,
            "timeout": 30,
        })
        log.info("  Bound group %s to application %s", group_uuid[:8], app_uuid[:8])

    def unbind_group_from_application(self, app_uuid: str, group_uuid: str) -> bool:
        """Remove the binding for one group. Returns True if a binding was removed.

        Requires authentik_policies.delete_policybinding, which older installs do
        not grant — see check_delete_permissions().
        """
        for binding in self.list_application_bindings(app_uuid):
            if binding.get("group") != group_uuid:
                continue
            if self.dry_run:
                log.info("  DRY-RUN would unbind group %s from application %s",
                         group_uuid[:8], app_uuid[:8])
                return True
            self._delete(f"/api/v3/policies/bindings/{binding['pk']}/")
            log.info("  Unbound group %s from application %s",
                     group_uuid[:8], app_uuid[:8])
            return True
        return False
