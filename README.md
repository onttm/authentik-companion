# authentik-companion

Watches the Traefik API and automatically provisions Authentik Proxy Provider + Application + Outpost membership for every subdomain protected by `chain-authentik`. Reads `authentik.access.group` Docker labels to bind per-app access policies automatically.

## Inspiration and credit

This project is directly inspired by **[docker-traefik-cloudflare-companion](https://github.com/tiredofit/docker-traefik-cloudflare-companion)** by [@tiredofit](https://github.com/tiredofit). That project pioneered the pattern of watching the Traefik API for new routers and automatically acting on them — in its case creating Cloudflare DNS records. authentik-companion applies the same pattern to Authentik SSO provisioning.

If you run a Traefik + Cloudflare stack, cf-companion handles your DNS. authentik-companion handles your SSO. They are independent but designed to run side by side, polling the same Traefik source on the same cadence.

## How it works

1. Polls `GET /api/http/routers` on the Traefik API every `POLL_INTERVAL` seconds
2. Filters for routers whose middleware list contains `AUTHENTIK_MIDDLEWARE` (default: `chain-authentik`)
3. For each new `Host()` found:
   - Creates a **Proxy Provider** (`forward_single` mode, scoped cookie domain)
   - Creates an **Application** linked to the provider
   - Adds the provider to the configured **Outpost** (defaults to embedded outpost)
   - Reads the container's `authentik.access.group` label and binds the named group(s) as an access policy
4. Persists provisioned hosts to `/data/provisioned.json` across restarts

Covers both **file-provider** rules (`app-*.yml`) and **Docker-label** routers — Traefik merges all sources into a single API response.

Provision-only: existing apps are never deleted. Remove stale apps manually in the Authentik UI.

## Access group labels

Add a label to any compose service to restrict which Authentik group can access it:

```yaml
labels:
  - "authentik.access.group=homelab-media"
```

No label = open to all authenticated Authentik users.

### Group binding modes

**`hierarchical` (default, recommended)**

Label the minimum group that should have access. The companion automatically includes all higher-privilege tiers so you can never accidentally lock out your admin account.

```
Label: homelab-media  →  binds: homelab-media + homelab-trusted + homelab-admin
Label: homelab-admin  →  binds: homelab-admin only
```

Tier order is defined by `AUTHENTIK_GROUP_*` env vars (guest → media → trusted → admin).

**`flat` — for Authentik pros only. You have been warned.**

Binds only what you explicitly list. No inference, no safety net. If you label an app `homelab-media` and forget to add `homelab-admin`, your admin account cannot reach it. Comma-separate for multiple groups: `homelab-media,homelab-trusted`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TRAEFIK_URL` | *(required)* | Traefik API base URL, e.g. `http://traefik:8080` |
| `AUTHENTIK_URL` | *(required)* | Authentik base URL, e.g. `http://authentik:9000` |
| `AUTHENTIK_TOKEN_FILE` | `/run/secrets/authentik_token` | Path to API token file (Docker secret) |
| `AUTHENTIK_TOKEN` | — | Token value directly (overrides file) |
| `AUTHENTIK_OUTPOST_NAME` | `authentik Embedded Outpost` | Outpost to add providers to |
| `AUTHENTIK_MIDDLEWARE` | `chain-authentik` | Middleware substring to match |
| `AUTHENTIK_GROUP_MODE` | `hierarchical` | `hierarchical` or `flat` — see above |
| `AUTHENTIK_GROUP_GUEST` | — | Name of your guest tier group |
| `AUTHENTIK_GROUP_MEDIA` | — | Name of your media tier group |
| `AUTHENTIK_GROUP_TRUSTED` | — | Name of your trusted tier group |
| `AUTHENTIK_GROUP_ADMIN` | — | Name of your admin tier group |
| `AUTHENTIK_LABEL_KEY` | `authentik.access.group` | Docker label key to read |
| `DOCKER_URL` | — | Socket-proxy URL for label reading, e.g. `tcp://socket-proxy:2375` |
| `AUTHENTIK_AUTH_FLOW` | `default-authentication-flow` | Auth flow slug |
| `AUTHENTIK_INVALIDATION_FLOW` | `default-provider-invalidation-flow` | Invalidation flow slug |
| `POLL_INTERVAL` | `60` | Seconds between Traefik polls |
| `LOG_LEVEL` | `INFO` | Python log level |
| `STATE_FILE` | `/data/provisioned.json` | Persistent state path |

## Authentik API token

Create a token in Authentik → **Admin Interface → Directory → Tokens → Create**. The user needs full admin access.

Store it as a Docker secret:

```bash
echo -n "your-token-here" | sudo tee /home/sysadmin/docker/secrets/authentik_token > /dev/null
sudo chmod 600 /home/sysadmin/docker/secrets/authentik_token
```

## Deployrr / docker-compose usage

See the [deployrr-tools community app](https://github.com/onttm/deployrr-tools/tree/main/community-apps/authentik-companion) for the ready-to-use `compose.yml` and `manifest.json`.

## Future: unified stack-companion

Both authentik-companion and cf-companion watch the same Traefik router list for the same event: a new protected subdomain. The planned convergence path:

1. **Phase 1 (now):** run independently, same poll cadence, complementary actions
2. **Phase 2:** shared Traefik discovery module / library
3. **Phase 3:** single `stack-companion` container — one poll, pluggable providers for Cloudflare DNS and Authentik SSO in one pass
