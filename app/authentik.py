"""Authentik API client — provisions and optionally cleans up proxy providers,
applications, outpost membership, groups, and application access policy bindings."""

import logging
import requests

log = logging.getLogger(__name__)


class AuthentikClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
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

    def _post(self, path: str, data: dict) -> dict:
        resp = self._s.post(f"{self.url}{path}", json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict) -> dict:
        resp = self._s.patch(f"{self.url}{path}", json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        resp = self._s.delete(f"{self.url}{path}", timeout=10)
        resp.raise_for_status()

    # ── permission probing ───────────────────────────────────────────────────

    def check_delete_permissions(self) -> tuple[bool, bool]:
        """Return (can_delete_application, can_delete_provider).

        Probes with DELETE on a nonexistent resource — 404 = permitted (object
        missing), 403 = no permission. No side effects.
        """
        def probe(path: str) -> bool:
            return self._s.delete(f"{self.url}{path}", timeout=10).status_code != 403

        return (
            probe("/api/v3/core/applications/__permission-check__/"),
            probe("/api/v3/providers/proxy/999999999/"),
        )

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

    def find_or_create_group(self, name: str) -> str:
        data = self._get("/api/v3/core/groups/", {"search": name})
        for g in data.get("results", []):
            if g["name"] == name:
                return g["pk"]
        result = self._post("/api/v3/core/groups/", {"name": name})
        log.info("Created group %r (pk=%s)", name, result["pk"][:8])
        return result["pk"]

    # ── provider management ──────────────────────────────────────────────────

    def find_provider(self, external_host: str) -> int | None:
        data = self._get("/api/v3/providers/proxy/", {"search": external_host})
        for p in data.get("results", []):
            if p.get("external_host") == external_host:
                return p["pk"]
        return None

    def create_provider(
        self,
        name: str,
        external_host: str,
        auth_flow: str,
        invalidation_flow: str,
        cookie_domain: str,
    ) -> int:
        result = self._post("/api/v3/providers/proxy/", {
            "name": name,
            "authorization_flow": auth_flow,
            "invalidation_flow": invalidation_flow,
            "external_host": external_host,
            "mode": "forward_single",
            "cookie_domain": cookie_domain,
        })
        return result["pk"]

    def delete_provider(self, provider_pk: int) -> None:
        self._delete(f"/api/v3/providers/proxy/{provider_pk}/")

    # ── application management ───────────────────────────────────────────────

    def find_application(self, slug: str) -> str | None:
        data = self._get("/api/v3/core/applications/", {"search": slug})
        for a in data.get("results", []):
            if a.get("slug") == slug:
                return a["pk"]
        return None

    def get_application(self, slug: str) -> dict | None:
        data = self._get("/api/v3/core/applications/", {"search": slug})
        for a in data.get("results", []):
            if a.get("slug") == slug:
                return a
        return None

    def create_application(
        self, name: str, slug: str, provider_pk: int, launch_url: str
    ) -> str:
        result = self._post("/api/v3/core/applications/", {
            "name": name,
            "slug": slug,
            "provider": provider_pk,
            "meta_launch_url": launch_url,
            "policy_engine_mode": "any",  # OR logic: member of ANY bound group gets access
        })
        return result["pk"]

    def delete_application(self, slug: str) -> None:
        # Policy bindings cascade-delete with the application automatically.
        self._delete(f"/api/v3/core/applications/{slug}/")

    # ── outpost management ───────────────────────────────────────────────────

    def add_provider_to_outpost(self, outpost: dict, provider_pk: int) -> None:
        current = list(outpost.get("providers") or [])
        if provider_pk in current:
            return
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": current + [provider_pk],
        })

    def remove_provider_from_outpost(self, outpost: dict, provider_pk: int) -> None:
        current = list(outpost.get("providers") or [])
        if provider_pk not in current:
            return
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": [p for p in current if p != provider_pk],
        })

    # ── access policy bindings ───────────────────────────────────────────────

    def bind_group_to_application(self, app_uuid: str, group_uuid: str) -> None:
        # No bindings = any authenticated user can access (Authentik default).
        # One or more bindings = only members of a bound group can access (OR logic).
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
