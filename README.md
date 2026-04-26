# traefik-authentik-companion

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

By default (STALE_ACTION=flag), the companion only provisions — it never removes. Set STALE_ACTION=remove to enable automated pruning after a configurable grace period. See [Stale app handling](#stale-app-handling) for details.

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

## Service account and API token setup

authentik-companion runs as a dedicated **service account** with minimum required
permissions rather than a full admin user. This limits blast radius — if the token
is ever compromised, the attacker can only manage providers, applications, groups,
policy bindings, outposts, and flows. They cannot touch users, passwords, or any
other part of Authentik.

Setup is done entirely via `ak shell` — Authentik's built-in Django management
shell running directly inside the container with database access. **No pre-existing
API token is required.** Anyone with `docker exec` access to the host can run it.

### Step 1 — Create the service account and token

```bash
docker exec authentik ak shell -c "
from authentik.core.models import User, UserTypes, Token, TokenIntents
from django.contrib.auth.models import Permission

# Create a non-superuser service account (cannot log in via UI)
user = User(
    username='authentik-companion',
    name='Authentik Companion',
    type=UserTypes.SERVICE_ACCOUNT,
    is_active=True,
)
user.set_unusable_password()
user.save()

# Grant minimum required permissions — nothing more
for app_label, codename in [
    ('authentik_flows',           'view_flow'),
    ('authentik_outposts',        'view_outpost'),
    ('authentik_outposts',        'change_outpost'),
    ('authentik_providers_proxy', 'add_proxyprovider'),
    ('authentik_providers_proxy', 'view_proxyprovider'),
    ('authentik_providers_proxy', 'delete_proxyprovider'),  # required for STALE_ACTION=remove
    ('authentik_core',            'add_application'),
    ('authentik_core',            'view_application'),
    ('authentik_core',            'delete_application'),    # required for STALE_ACTION=remove
    ('authentik_core',            'add_group'),
    ('authentik_core',            'view_group'),
    ('authentik_policies',        'add_policybinding'),
    ('authentik_policies',        'view_policybinding'),
]:
    try:
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    except Permission.DoesNotExist:
        print('WARNING: permission not found:', app_label + '.' + codename)

# Issue a non-expiring API token
token = Token.objects.create(
    identifier='authentik-companion',
    user=user,
    intent=TokenIntents.INTENT_API,
    description='Service account token for authentik-companion stack automation',
    expiring=False,
)
print(token.key)
" 2>&1 | tail -1
```

Copy the printed token key. If you miss it, retrieve it from the Authentik UI under
**Admin → Directory → Tokens** — the key is visible there at any time.

> [!CAUTION]
> **DO NOT create or edit this token through the Authentik UI.**
> A confirmed bug in Authentik (tested on 2025.12.1, likely broader) causes the
> expiration date field to be ignored on both create and save — tokens revert to
> ~30 minutes regardless of what you set. This will silently break authentik-companion.
> Use `ak shell` for all token management. Report upstream: https://github.com/goauthentik/authentik/issues

To update any token field (e.g. description), use the shell:

```bash
docker exec authentik ak shell -c "
from authentik.core.models import Token
t = Token.objects.get(identifier='authentik-companion')
t.description = 'updated description'
t.save()
print('saved, expiring=', t.expiring)
" 2>&1 | tail -2
```

If the user or token already exists from a previous attempt, delete them first:

```bash
docker exec authentik ak shell -c "
from authentik.core.models import Token, User
Token.objects.filter(identifier='authentik-companion').delete()
User.objects.filter(username='authentik-companion').delete()
print('deleted')
" 2>&1 | tail -1
```

### Existing install: enable STALE_ACTION=remove

If you installed before v4 and want to switch to `STALE_ACTION=remove`, grant the two additional delete permissions with one `ak shell` command, then restart the companion:

```bash
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
" 2>&1 | tail -1
```

Then set `STALE_ACTION=remove` in your `.env` and restart the container. On startup, authentik-companion will verify the delete permissions are present and log an error with this same command if they're missing — so you'll always know exactly what to run.

### Step 2 — Store as a Docker secret

```bash
echo -n "your-token-here" | sudo tee /path/to/docker/secrets/authentik_token > /dev/null
sudo chmod 600 /path/to/docker/secrets/authentik_token
```

## Security

### Evaluation

Five concerns were identified during design and reviewed before deployment:

**1. API token blast radius**
The companion requires write access to Authentik's API. If the token is compromised,
the attacker inherits whatever permissions that token carries.

**2. Automation removes human review from security decisions**
Every SSO provisioning decision is made automatically rather than by a human reviewing
an Authentik UI form. This was a deliberate design objection raised in the community
(see brokenscripts/authentik_traefik).

**3. Socket-proxy exposes stack topology**
Reading Docker container labels requires socket-proxy access. A compromised container
could enumerate all running containers, labels, and network configuration.

**4. Stale application accumulation**
Provision-only design means Authentik Applications persist after their services are
removed. A reused slug could inherit stale policy bindings.

**5. Label trust**
A container controls its own `authentik.access.group` label. A malicious image could
set it to empty, making itself open to all authenticated users. It cannot use labels
to escalate beyond the default open-to-all-authenticated behavior.

---

### Mitigations applied

**Concern 1 — Token blast radius: mitigated**

Rather than running under an admin user token, authentik-companion uses a dedicated
`service_account` user (`authentik-companion`, `is_superuser=False`, unusable password)
with exactly 13 Django model permissions:

| Permission | Purpose |
|---|---|
| `authentik_flows.view_flow` | Resolve auth/invalidation flow UUIDs on startup |
| `authentik_outposts.view_outpost` | Find the embedded outpost |
| `authentik_outposts.change_outpost` | Add/remove providers from outpost |
| `authentik_providers_proxy.add_proxyprovider` | Create proxy providers |
| `authentik_providers_proxy.view_proxyprovider` | Check if provider exists |
| `authentik_providers_proxy.delete_proxyprovider` | Remove stale providers (`STALE_ACTION=remove`) |
| `authentik_core.add_application` | Create applications |
| `authentik_core.view_application` | Check if application exists |
| `authentik_core.delete_application` | Remove stale applications (`STALE_ACTION=remove`) |
| `authentik_core.add_group` | Create access groups |
| `authentik_core.view_group` | Check if group exists |
| `authentik_policies.add_policybinding` | Bind groups to applications |
| `authentik_policies.view_policybinding` | Check existing bindings |

A compromised token cannot: create or modify users, reset passwords, access user data,
create admin backdoors, or reach any other part of Authentik. The delete permissions are
scoped only to providers and applications — the objects the companion itself creates.
The service account cannot log in via the Authentik UI (`set_unusable_password`).

Setup uses `ak shell` (Django management shell with direct DB access) — no pre-existing
API token is required to bootstrap the service account. This avoids a chicken-and-egg
dependency on akadmin.

**Concern 2 — Human review: accepted by design**

The label on the compose file IS the human decision. A developer choosing `chain-authentik`
and setting `authentik.access.group` has made an explicit access control choice. The
companion executes that decision; it does not make it. This is the same trust model as
cf-companion — the human writes the Traefik rule, the tool acts on it.

**Concern 3 — Socket-proxy topology exposure: partially mitigated**

The companion reads only `GET /containers/json` via socket-proxy. Exposure is bounded
by whatever the shared socket-proxy allowlist permits. Pending action: audit the
socket-proxy allowlist and confirm `CONTAINERS=1` read access is the only path granted.

**Concern 4 — Stale apps: accepted, documented**

Provision-only is an intentional safety choice — automatic deletion of Authentik
Applications is too destructive to automate without human confirmation. Users are
responsible for removing stale apps via the Authentik UI when services are decommissioned.

**Concern 5 — Label trust: accepted by design**

The worst a malicious label can do is remove a restriction that wouldn't have existed
without the label (since no label = open to all authenticated users by default). It
cannot grant access beyond what is already the baseline. The attack surface is
self-limiting.

---

### Re-evaluation after mitigations

| Concern | Before | After |
|---|---|---|
| Token blast radius | Full Authentik admin access | 11 scoped permissions, no user management |
| Human review | Same | Same — label is the human decision |
| Stack topology | Bounded by socket-proxy | Bounded by socket-proxy (audit pending) |
| Stale apps | Accumulates | flag/remove modes with grace period — see stale app docs |
| Label trust | Self-limiting | Self-limiting — no change needed |

**Overall posture:** appropriate for a homelab. Not appropriate for a multi-tenant or
production environment where Authentik doesn't support scoped API tokens (as of 2025.12.1),
meaning no further blast-radius reduction is possible without upstream Authentik changes.

---

## Stale app handling

When a provisioned subdomain disappears from Traefik (service removed, compose file deleted, container renamed), authentik-companion detects it as stale and takes action based on `STALE_ACTION`.

### How the stale timer works

**The timer only runs while authentik-companion is running.** If your server goes offline, the stale clock stops — it does not accumulate during outages. When the server comes back:

- Services that restart normally → detected as active immediately, stale marker never set
- Services that don't come back → stale clock starts from the *restart moment*, not from when they disappeared

This means a 4-month server outage followed by a normal restart is completely safe. The 30-day grace period begins counting only from the point authentik-companion is actually running and observing Traefik.

### STALE_ACTION modes

**`flag` (default):** Log a WARNING every poll cycle with instructions for manual removal in the Authentik UI. Nothing is ever deleted automatically. Use this if you prefer to stay in control of what gets removed.

**`remove`:** After `STALE_THRESHOLD_DAYS` of continuous absence, automatically delete the Authentik Application, Provider, and policy bindings, and remove the provider from the outpost. The companion will re-provision the app automatically if the service comes back — it treats it as a new host and creates a fresh Provider and Application with the same slug.

### Choosing a threshold

The default of 30 days is deliberately conservative. Consider what you're protecting against:

| Scenario | Minimum safe threshold |
|---|---|
| Container restart / brief maintenance | minutes to hours (any reasonable value) |
| Planned weekly maintenance window | 7+ days |
| Extended server outage, vacation, repairs | 30+ days |

A lower threshold means stale apps are cleaned up faster. A higher threshold means more tolerance for unplanned downtime before auto-removal kicks in. If in doubt, use `flag` mode and clean up manually.

> [!NOTE]
> If `STALE_ACTION=remove` deletes an app and the service later comes back, the companion automatically re-provisions it. No manual intervention needed — it sees the returning host as new and creates a fresh Provider, Application, and policy bindings from the Docker label. Authentik group memberships for users are never touched.

---

## Deployrr / docker-compose usage

See the [deployrr-tools community app](https://github.com/onttm/deployrr-tools/tree/main/community-apps/authentik-companion) for the ready-to-use `compose.yml` and `manifest.json`. The container, service account, and volume paths all use the short name `authentik-companion` for convenience.

## Future: unified stack-companion

Both authentik-companion and cf-companion watch the same Traefik router list for the same event: a new protected subdomain. The planned convergence path:

1. **Phase 1 (now):** run independently, same poll cadence, complementary actions
2. **Phase 2:** shared Traefik discovery module / library
3. **Phase 3:** single `stack-companion` container — one poll, pluggable providers for Cloudflare DNS and Authentik SSO in one pass
