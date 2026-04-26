"""Docker API client — reads container labels via socket-proxy to correlate
Traefik Host() rules with authentik access-group labels.
File-provider routers have no container, so they return no label (open-to-all default)."""

import logging
import re
import requests

log = logging.getLogger(__name__)

_HOST_RULE_RE = re.compile(r'Host\(`([^`]+)`\)')


class DockerClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def get_host_access_groups(self, label_key: str) -> dict[str, str]:
        """Return {host: group_csv} by scanning running container labels.

        For each container with label_key set, extracts every Host() value from
        Traefik rule labels on the same container and maps it to the access group.
        Returns empty dict on error — provisioning continues without group binding.
        """
        try:
            resp = requests.get(f"{self.url}/containers/json", timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Docker API unavailable, skipping label read: %s", exc)
            return {}

        result: dict[str, str] = {}
        for container in resp.json():
            labels: dict = container.get("Labels") or {}
            access_group = labels.get(label_key)
            if not access_group:
                continue
            for lv in labels.values():
                for host in _HOST_RULE_RE.findall(lv):
                    result[host] = access_group
                    log.debug("Label map: %s → %r", host, access_group)

        return result
