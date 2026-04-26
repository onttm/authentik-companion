"""Authentik API client — provisions proxy providers, applications, outpost membership,
groups, and application access policy bindings."""

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
        """Return the UUID of a group, creating it if it doesn't exist."""
        data = self._get("/api/v3/core/groups/", {"search": name})
        for g in data.get("results", []):
            if g["name"] == name:
                return g["pk"]
        result = self._post("/api/v3/core/groups/", {"name": name})
        log.info("Created group %r (pk=%s)", name, result["pk"][:8])
        return result["pk"]

    # ── per-host provisioning ────────────────────────────────────────────────

    def find_provider(self, external_host: str) -> int | None:
        """Return pk of an existing proxy provider for external_host, or None."""
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

    def find_application(self, slug: str) -> str | None:
        """Return the UUID of an existing application by slug, or None."""
        data = self._get("/api/v3/core/applications/", {"search": slug})
        for a in data.get("results", []):
            if a.get("slug") == slug:
                return a["pk"]
        return None

    def create_application(
        self, name: str, slug: str, provider_pk: int, launch_url: str
    ) -> str:
        """Create an application and return its UUID."""
        result = self._post("/api/v3/core/applications/", {
            "name": name,
            "slug": slug,
            "provider": provider_pk,
            "meta_launch_url": launch_url,
            "policy_engine_mode": "any",  # OR logic: user needs to be in ANY bound group
        })
        return result["pk"]

    def add_provider_to_outpost(self, outpost: dict, provider_pk: int) -> None:
        current = list(outpost.get("providers") or [])
        if provider_pk in current:
            return
        self._patch(f"/api/v3/outposts/instances/{outpost['pk']}/", {
            "name": outpost["name"],
            "type": outpost["type"],
            "providers": current + [provider_pk],
        })

    # ── access policy bindings ───────────────────────────────────────────────

    def bind_group_to_application(self, app_uuid: str, group_uuid: str) -> None:
        """Bind a group to an application as an access policy.

        When one or more groups are bound, only members of those groups can
        access the application (policy_engine_mode=any → OR logic across bindings).
        No bindings = any authenticated user can access (Authentik default).
        """
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
