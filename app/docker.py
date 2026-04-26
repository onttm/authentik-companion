"""Docker API client — reads container labels via socket-proxy.

Used to correlate a Traefik Host() rule with the authentik access-group
label declared on the same container. File-provider routers have no
container and return no label (open-to-all-authenticated default applies).
"""

import logging
import re
import requests

log = logging.getLogger(__name__)

_HOST_RULE_RE = re.compile(r'Host\(`([^`]+)`\)')


class DockerClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def get_host_access_groups(self, label_key: str) -> dict[str, str]:
        """Return {host: group_csv} by correlating Traefik Host() labels with label_key.

        Scans all running containers. For each container that has label_key set,
        finds every Traefik rule label on the same container and extracts Host()
        values — mapping each host to the access group(s) declared for that container.

        Returns empty dict on Docker API error (non-fatal; provisioning continues
        without access-group binding).
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
