# traefik-authentik-companion

Watches the Traefik API and automatically provisions Authentik Proxy Provider + Application + Outpost membership for every subdomain protected by `chain-authentik`. Reads `authentik.access.group` Docker labels to bind per-app access policies automatically.

## Inspiration and credit

This project is directly inspired by **[docker-traefik-cloudflare-companion](https://github.com/tiredofit/docker-traefik-cloudflare-companion)** by [@tiredofit](https://github.com/tiredofit). That project pioneered the pattern of watching the Traefik API for new routers and automatically acting on them — in its case creating Cloudflare DNS records. authentik-companion applies the same pattern to Authentik SSO provisioning.

If you run a Traefik + Cloudflare stack, cf-companion handles your DNS. authentik-companion handles your SSO. They are independent but designed to run side by side, polling the same Traefik source on the same cadence.

## How it works

1. Polls `GET /api/http/routers` on the Traefik API every `POLL_INTERVAL` seconds
2. Filters for routers whose middleware list contains `AUTHENTIK_MIDDLEWARE` (default: `chain-authentik`), then applies the optional `TRAEFIK_INCLUDED_HOST` / `TRAEFIK_EXCLUDED_HOST` regex filters
3. For each new `Host()` found:
   - Creates a **Proxy Provider** (`forward_single` mode, scoped cookie domain)
   - Creates an **Application** linked to the provider
   - Adds the provider to the configured **Outpost** (defaults to embedded outpost)
   - Reads the container's `authentik.access.group` label and binds the named group(s) as an access policy
4. With `REFRESH_ENTRIES=true`, re-checks known hosts every poll so a changed label actually takes effect
5. Persists provisioned hosts to `/data/provisioned.json` across restarts

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
| `AUTHENTIK_AUTH_FLOW` | `default-authentication-flow` | **Authentication** flow slug — runs when the user is not logged in (login page, Plex SSO, etc.) |
| `AUTHENTIK_AUTHZ_FLOW` | `default-provider-authorization-implicit-consent` | **Authorization** flow slug — runs after login to grant access to an application (consent). Must be an implicit-consent flow, NOT the authentication flow. |
| `AUTHENTIK_INVALIDATION_FLOW` | `default-provider-invalidation-flow` | Invalidation flow slug — runs on logout |
| `POLL_INTERVAL` | `60` | Seconds between Traefik polls |
| `LOG_LEVEL` | `INFO` | Python log level |
| `LOG_TYPE` | `CONSOLE` | `CONSOLE`, `FILE`, or `BOTH` — see [Logging](#logging) |
| `LOG_PATH` | `/logs/` | Directory for the log file (`FILE`/`BOTH` only) |
| `LOG_FILE` | `authentik-companion.log` | Log filename (`FILE`/`BOTH` only) |
| `LOG_FILE_MAX_MB` | `10` | Rotate the log file at this size |
| `LOG_FILE_RETAIN` | `5` | Rotated files to keep |
| `STATE_FILE` | `/data/provisioned.json` | Persistent state path |
| `NAMING_OVERRIDES_FILE` | `/data/naming-overrides.json` | Naming/section config — see [Naming standard](#naming-standard-and-toolsnormalize-namespy) |
| `HEARTBEAT_FILE` | `/data/heartbeat` | Touched after each successful poll; the Docker healthcheck reads it |
| `DRY_RUN` | `false` | Log every change that would be made and write nothing — see [Dry run](#dry-run) |
| `REFRESH_ENTRIES` | `false` | Re-check known hosts every poll and apply label changes — see [Reconciliation](#reconciliation-refresh_entries) |
| `TRAEFIK_INCLUDED_HOST` | `.*` | Regex allow-list; also `TRAEFIK_INCLUDED_HOST1`, `...2` — see [Host filtering](#host-filtering) |
| `TRAEFIK_EXCLUDED_HOST` | — | Regex deny-list; also `TRAEFIK_EXCLUDED_HOST1`, `...2`. Wins over the allow-list |
| `STALE_ACTION` | `flag` | What to do with provisioned hosts that disappear from Traefik: `flag` (log warning only) or `remove` (auto-delete after threshold) |
| `STALE_THRESHOLD_DAYS` | `30` | Days a host must be absent before auto-removal (only used when `STALE_ACTION=remove`) |

> [!TIP]
> **Every** variable in this table also accepts a `_FILE` suffix pointing at a file whose
> contents are the value — `AUTHENTIK_URL_FILE`, `STALE_ACTION_FILE`, and so on. This matches
> cf-companion's convention and lets any setting be delivered as a Docker secret, not just the
> API token.

## Dry run

`DRY_RUN=true` runs the full poll loop against your live Authentik and Traefik and changes
nothing — no API writes, no state file, no heartbeat. Every create, bind, unbind, and delete
is logged as `DRY-RUN would ...` instead.

```
DRY-RUN would create provider 'Sonarr Proxy Provider' → https://sonarr.example.com (cookie_domain=example.com)
DRY-RUN would create application 'Sonarr' (slug=sonarr) → provider pk=-1
DRY-RUN would add provider pk=-1 to outpost 'authentik Embedded Outpost'
DRY-RUN would bind group 00000000 to application 00000000
```

Because nothing is persisted, every cycle reports the same pending work rather than falling
silent after the first pass. Use it before enabling `STALE_ACTION=remove` — that is the only
setting that deletes things, and a dry run shows you exactly which applications it has in its
sights.

## Host filtering

`TRAEFIK_EXCLUDED_HOST` and `TRAEFIK_INCLUDED_HOST` are regex lists applied after the
middleware match. Both accept a bare name and an indexed series, following cf-companion:

```yaml
environment:
  TRAEFIK_EXCLUDED_HOST:  '^plex\.'
  TRAEFIK_EXCLUDED_HOST1: '^vault\.'
```

Patterns are **unanchored** (`re.search`), so `example\.com` matches every subdomain of it.
Anchor with `^...$` when you want an exact host. Exclusion always beats inclusion.

Use this when a host should stay behind `chain-authentik` but its Authentik application is
managed by hand — previously the only way to opt out was to strip the middleware, which also
removed the authentication.

> [!NOTE]
> Excluding a host that was already provisioned **releases** it: the companion drops it from
> its state file and logs that it is no longer managed. Its Authentik application, provider,
> and bindings are left exactly as they are. Without this, an excluded host would look like it
> had vanished from Traefik and would be flagged stale — or deleted, under `STALE_ACTION=remove`.

## Reconciliation (`REFRESH_ENTRIES`)

By default a host is provisioned once and never looked at again, so **changing its
`authentik.access.group` label does nothing** until you clear the state file. Set
`REFRESH_ENTRIES=true` and every poll re-checks known hosts:

- binds groups the label now names
- unbinds groups the companion previously bound that the label no longer names
- re-adds a provider that was removed from the outpost
- re-provisions an application that was deleted out from under it

### What it will not do

Reconciliation is deliberately conservative, because every mistake here is an access-control
mistake:

| Situation | Behaviour | Why |
|---|---|---|
| Label removed entirely | Bindings left alone | No label means "I'm not declaring access rules", not "revoke everything". A container recreated without its label must not silently widen the app to all authenticated users. |
| Binding added by hand in the UI | Left alone | The companion only unbinds groups it recorded binding itself. |
| First poll after upgrading from v4 | Adopts whatever is bound, prunes nothing | Pre-v5 state has no record of which bindings were the companion's, so it claims them all rather than guessing. Pruning starts from the next label change. |
| Docker API unreachable | Whole cycle skipped | Every label would read as empty, which would provision new apps as open-to-all and strip existing bindings. |

### Required permission

Unbinding needs `authentik_policies.delete_policybinding`, which installs created before v5 do
not have. Without it the companion still **adds** bindings, but logs an error for every group it
should have removed — an app demoted from `homelab-media` to `homelab-admin` would keep the wider
access. It checks at startup and prints the exact command to fix it. See
[Existing install: enable REFRESH_ENTRIES](#existing-install-enable-refresh_entries).

## Application slugs and multi-domain stacks

The Authentik application slug comes from the leftmost label of the host: `sonarr.example.com`
→ `sonarr`. That breaks when one stack serves more than one apex, because `admin.example.com`
and `admin.example.org` both want the slug `admin` — and the provider name `Admin Proxy
Provider`, which Authentik requires to be unique.

Before v5 the second host would silently adopt the first host's application and leave its own
provider orphaned, or fail outright with `provider with this name already exists`. v5 detects
both collisions and falls back to the full host:

```
Provider name 'Admin Proxy Provider' is taken — using 'admin.example.org Proxy Provider'
Slug 'admin' is taken by another host — using 'admin-example-org' for admin.example.org
Created application slug=admin-example-org
```

The application's display name is qualified too (`Admin (example.org)`), so the Authentik app
list doesn't show two identical entries.

Short slugs stay the default, so **no migration is needed** and existing applications keep the
names you know. The fallback only fires on an actual collision. If both the short and the
qualified slug are taken by other hosts, the companion skips that host and says so rather than
hijacking someone else's application.

## Naming standard and `tools/normalize-names.py`

Application and provider names drift. Some get created by the companion, some by hand, some
by an older version with a different rule — you end up with `Adminer` next to `bazarr`, and
`Ha`, `Wg`, `Db`, `Ddnsupdater` where real product names belong.

`tools/normalize-names.py` audits every application and proxy provider against one standard,
prints what should change, and can apply it. **It is read-only unless you pass `--apply`.**

```bash
# report only
python3 tools/normalize-names.py --url http://authentik:9000 --token-file /path/to/secret

# show the exact API calls it would make
python3 tools/normalize-names.py --token-file /path/to/secret --verbose

# prove one app before touching the rest
python3 tools/normalize-names.py --token-file /path/to/secret --only wud --apply

# rename for real
python3 tools/normalize-names.py --token-file /path/to/secret --apply
```

Standard library only — no `pip install`, no Claude, no AI service, no network beyond your
own Authentik. Exit codes are `0` = compliant, `1` = drift found, `2` = error, so it works as
a cron or CI check.

### The standard

Defined once in [`app/naming.py`](app/naming.py) and imported by **both** the tool and the
companion, so newly provisioned applications are born compliant instead of appearing as drift
on the next run.

The rule has two halves, and which applies depends on who reads the string:

- **Identifiers** — application slugs, override keys, hostnames; anything a machine matches on
  — are always **lowercase**. They are code, not prose. The tool validates this and reports
  any slug that breaks it.
- **Display names** — what a human reads in the portal — follow the **upstream project's own
  spelling**: qBittorrent, UniFi, WireGuard, Home Assistant. A house style imposed on top
  would spell every one of those differently from the project itself.

A display name resolves as: an **override** for the slug, used verbatim → **acronym
expansion** for known initialisms (`db` → `DB`) → **Title Case**, dashes and underscores as
word breaks (`win-control` → `Win Control`).

### Portal sections

Authentik has exactly one grouping dimension — `Application.group`, a flat string. The portal
groups on it and sorts the labels with `localeCompare`, so two levels are encoded into that
one string:

```
Acme · Media          Bazarr, Jellyfin, Radarr, Sonarr
Acme · Monitoring     Netdata, Prometheus, Uptime Kuma
Lab · Admin           Database, Portainer
```

Sorting that alphabetically yields **domain first, function second** — as close to a hierarchy
as the field allows. The `domains` block maps an apex to its family, so a host added tomorrow
lands in the right family with no per-app configuration; `sections` maps a slug to its
function.

> [!IMPORTANT]
> Applications with **no** group sort **above** every named section, not below. A partial
> rollout puts unassigned apps at the very top of the portal. Assign them all — the tool emits
> a `no-section` warning listing any that are missing.

### Provider names are family-qualified

Two applications may share a display name — Authentik only requires slugs to be unique — but
it **does** enforce uniqueness on provider names. Aliasing one service under two domains
(`docker.example.org` and `portainer.example.com` both showing "Portainer") therefore collides
on the provider.

Provider names always carry the family: `Acme Portainer Proxy Provider` versus
`Lab Portainer Proxy Provider`. Qualifying only the pairs that collide *today* would make a
provider's correct name depend on the whole application set, so adding one host tomorrow would
silently make an existing, untouched provider wrong.

### Overrides are yours to edit

Copy `tools/naming-overrides.example.json` to `tools/naming-overrides.json` and edit. That copy
is gitignored — it describes your stack, not the tool. It holds `domains`, `sections`, and
`overrides`, and supports a `_comment` key so you can annotate freely.

To have the **companion** use the same configuration for newly created applications, put the
file where the container can read it — the default is `/data/naming-overrides.json`, inside
the appdata directory you already mount — or point `NAMING_OVERRIDES_FILE` at it.

> [!NOTE]
> The companion reads this file at startup. After changing it, restart the container for new
> applications to pick up the change; existing ones are corrected with the tool.

### What it will not touch

| Thing | Behaviour | Why |
|---|---|---|
| Application **slugs** | Reported, never changed | The slug is in the Authentik URL and in the companion's state file. Renaming one is a deliberate act, not a cleanup. |
| Applications with no proxy provider | Reported, never changed | Not companion-managed — typically hand-made apps like a Plex OAuth application. |
| Collision-qualified provider names | Left alone | `admin.example.org Proxy Provider` is the companion's own tie-breaker, not drift. |
| `external_host`, cookie domains, bindings, outposts | Never read for writing | Renaming is cosmetic; nothing about routing or access changes. |

It also warns when two applications would normalise onto the same name, before you apply.

> [!NOTE]
> `--apply` needs `authentik_core.change_application` and
> `authentik_providers_proxy.change_proxyprovider`, which the companion's service account
> deliberately does **not** have — renaming is not part of its job. The tool prints the exact
> `ak shell` command to grant them, or you can run it with an admin token and grant nothing.

> [!TIP]
> A scoped token sees a truncated `/core/applications/` list because of Authentik's
> object-level filtering — on one 53-app install the endpoint returned 24. The tool enumerates
> through proxy providers instead and tells you when the counts disagree, so the report is
> complete rather than quietly partial.

## Logging

By default the companion logs to stdout only, for `docker logs` / Dozzle / Loki. For stacks
without a log viewer, `LOG_TYPE` mirrors cf-companion:

| `LOG_TYPE` | Behaviour |
|---|---|
| `CONSOLE` (default) | stdout only |
| `FILE` | `LOG_PATH`/`LOG_FILE` only |
| `BOTH` | stdout and file |

```yaml
environment:
  LOG_TYPE: BOTH
  LOG_PATH: /logs/
  LOG_FILE: authentik-companion.log
volumes:
  - ./logs/authentik-companion:/logs
```

Rotation is handled in-process (`LOG_FILE_MAX_MB`, `LOG_FILE_RETAIN`) — there is no logrotate
in this image, and a 60-second poll loop writing an unbounded file would eventually fill the
volume. If the log directory cannot be written the companion logs an error and continues on
console only; it never exits over a logging problem.

> [!IMPORTANT]
> `LOG_TYPE=BOTH` works with no mount at all — `/logs` exists inside the image. If you do
> bind-mount a log directory and have opted into [running unprivileged](#running-unprivileged),
> chown it to match that uid, or logging falls back to console with an error.

## Health

The image ships a `HEALTHCHECK`. `main.py` touches `HEARTBEAT_FILE` after every successful
poll and the healthcheck fails if it is older than three poll intervals (minimum 90s).

This matters because the poll loop catches every exception and never exits: without a
heartbeat, a companion whose API token was revoked looks exactly like a healthy one —
running, quiet, and doing nothing. Dry runs are always reported healthy, since they
deliberately write nothing.

### Authentication flow vs. authorization flow

These are two distinct Authentik flow types that serve different purposes:

**`AUTHENTIK_AUTH_FLOW`** (authentication flow) — runs when a user is **not yet logged in**. Handles credential collection: username/password form, Plex SSO, MFA, etc. The Authentik login page is rendered by this flow. Default: `default-authentication-flow`.

**`AUTHENTIK_AUTHZ_FLOW`** (authorization flow) — runs when a user **is already logged in** and requests access to an application for the first time. Handles consent: "do you allow this app to see your profile?" In a homelab the implicit-consent flow skips the prompt and grants access automatically. Default: `default-provider-authorization-implicit-consent`.

> [!CAUTION]
> Setting `AUTHENTIK_AUTHZ_FLOW` to an authentication flow (the login flow) is a silent misconfiguration that causes every already-authenticated user to be sent back through the login flow on every access. Symptoms: users who completed login are immediately redirected back to the login page; Plex-federated users who have no local password hit a `404 /if/flow/.../undefined` loop. The companion logs both UUIDs on startup — verify that `auth_flow` and `authz_flow` resolve to different flows.

On startup the companion logs both resolved UUIDs so you can verify:
```
auth_flow=32ea77bc  authz_flow=ec63c754  invalidation_flow=f7bae89b
```
If `auth_flow` and `authz_flow` are the same UUID, `AUTHENTIK_AUTHZ_FLOW` is misconfigured.

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

> [!NOTE]
> Authentik 2025.10+ enforces permissions through its RBAC system (group → role → permissions).
> Direct `user_permissions` are not checked by the API — on 2026.8.0 the attribute is not even
> populated. The script below creates the required RBAC group and role automatically.

> [!CAUTION]
> **If you followed this guide before v5, your copy of this script is broken on current
> Authentik.** It assigned `role.group` to a backing `django.contrib.auth.Group`; the `Role`
> model has no `group` field as of 2026.8.0 and the script raises `AttributeError`. The version
> below is verified against **2026.8.0** and uses permission strings, which `Role.assign_perms()`
> accepts directly.

```bash
docker exec authentik ak shell -c "
from authentik.core.models import Group as AKGroup, Token, TokenIntents, User, UserTypes
from authentik.rbac.models import Role

PERMS = [
    'authentik_flows.view_flow',
    'authentik_outposts.view_outpost',
    'authentik_outposts.change_outpost',
    'authentik_providers_proxy.add_proxyprovider',
    'authentik_providers_proxy.view_proxyprovider',
    'authentik_providers_proxy.delete_proxyprovider',
    'authentik_core.add_application',
    'authentik_core.view_application',
    'authentik_core.delete_application',
    'authentik_core.add_group',
    'authentik_core.view_group',
    'authentik_policies.add_policybinding',
    'authentik_policies.view_policybinding',
    'authentik_policies.delete_policybinding',
]

# Non-superuser service account — cannot log in via the UI
user, created = User.objects.get_or_create(
    username='authentik-companion',
    defaults={'name': 'Authentik Companion', 'type': UserTypes.SERVICE_ACCOUNT, 'is_active': True},
)
if created:
    user.set_unusable_password()
    user.save()

# RBAC group + role — how the Authentik API actually enforces permissions
group, _ = AKGroup.objects.get_or_create(name='authentik-companion')
role, _ = Role.objects.get_or_create(name='authentik-companion')
role.assign_perms(PERMS)
group.roles.add(role)
user.ak_groups.add(group)

# Non-expiring API token
Token.objects.filter(identifier='authentik-companion').delete()
token = Token.objects.create(
    identifier='authentik-companion',
    user=user,
    intent=TokenIntents.INTENT_API,
    description='Service account token for authentik-companion stack automation',
    expiring=False,
)
granted = sorted(
    f'{p.permission.content_type.app_label}.{p.permission.codename}'
    for p in role.rolemodelpermission_set.all()
)
print('MISSING:' + ','.join(p for p in PERMS if p not in granted))
print(token.key)
" 2>&1 | tail -2
```

The command prints two lines. `MISSING:` must be empty — anything listed there is a permission
name this Authentik version does not recognise, and the matching feature will not work. The
second line is your token.

To audit the permissions of an existing install at any time:

```bash
docker exec authentik ak shell -c "
from authentik.rbac.models import Role
role = Role.objects.get(name='authentik-companion')
for p in sorted(role.rolemodelpermission_set.all(),
                key=lambda x: str(x.permission.codename)):
    print(f'{p.permission.content_type.app_label}.{p.permission.codename}')
" 2>&1 | grep authentik_
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

If you installed before v4 and want to switch to `STALE_ACTION=remove`, grant the two additional delete permissions via the RBAC role, then restart the companion:

```bash
docker exec authentik ak shell -c "
from authentik.rbac.models import Role
Role.objects.get(name='authentik-companion').assign_perms([
    'authentik_core.delete_application',
    'authentik_providers_proxy.delete_proxyprovider',
])
print('done')
" 2>&1 | tail -1
```

Then set `STALE_ACTION=remove` in your `.env` and restart the container. On startup, authentik-companion will verify the delete permissions are present and log an error with this same command if they're missing — so you'll always know exactly what to run.

### Existing install: enable REFRESH_ENTRIES

Reconciliation needs one permission that no pre-v5 install has. Grant it, then restart:

```bash
docker exec authentik ak shell -c "
from authentik.rbac.models import Role
Role.objects.get(name='authentik-companion').assign_perms([
    'authentik_policies.delete_policybinding',
])
print('done')
" 2>&1 | tail -1
```

Without it, `REFRESH_ENTRIES=true` still adds bindings but cannot remove them, and logs an
error naming every group it failed to revoke. The companion checks this at startup.

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
with exactly 14 Django model permissions:

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
| `authentik_policies.delete_policybinding` | Revoke a group when a label narrows (`REFRESH_ENTRIES`) |

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

**Concern 3 — Socket-proxy topology exposure: mitigated**

The companion reads only `GET /containers/json` via socket-proxy. Socket-proxy allowlist
audited against the companion's actual Docker API usage:

| Permission | Required by companion | Notes |
|---|---|---|
| `CONTAINERS=1` | ✓ yes | `GET /containers/json` — read container labels |
| `ALLOW_START/STOP/RESTARTS` | ✗ no | Portainer only |
| `POST=1` | ✗ no | Portainer only — companion never POSTs to containers |
| `IMAGES/NETWORKS/SERVICES/TASKS/VOLUMES` | ✗ no | Portainer only |
| `SECRETS=0` | blocked | companion reads its token via `/run/secrets/` container mount, not Docker API |
| `AUTH=0` | blocked | correctly disabled |
| `EXEC=0` | blocked | correctly disabled |

The companion shares the `socket_proxy` network with Portainer so it technically has
*access* to the broader permission set. This is a code-trust boundary: the companion's
code only ever calls `GET /containers/json`. Since you control the image build, this is
acceptable for a homelab deployment.

**Concern 4 — Stale apps: mitigated, opt-in**

The default (`STALE_ACTION=flag`) is still provision-only: stale applications are reported
every poll and removed by a human. `STALE_ACTION=remove` automates it behind a grace period
(`STALE_THRESHOLD_DAYS`, default 30), and requires two delete permissions that are checked at
startup.

Slug reuse — a decommissioned app inheriting stale policy bindings — is addressed in v5: the
companion records the slug it actually used rather than re-deriving it, and before deleting
anything it confirms the application's provider points at the host being removed. If it does
not, it refuses and tells you to remove it by hand.

**Concern 5 — Label trust: accepted by design**

The worst a malicious label can do is remove a restriction that wouldn't have existed
without the label (since no label = open to all authenticated users by default). It
cannot grant access beyond what is already the baseline. The attack surface is
self-limiting.

---

### Re-evaluation after mitigations

| Concern | Before | After |
|---|---|---|
| Token blast radius | Full Authentik admin access | 14 scoped permissions, no user management |
| Human review | Same | Same — label is the human decision |
| Stack topology | Bounded by socket-proxy | Audited — only `CONTAINERS=1` read needed; all write paths blocked |
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

## Upgrading to v5

v5 is backwards compatible. Nothing needs migrating and no application is renamed.

1. **Pull and restart.** The state file migrates from v1/v2 to v3 in place on first start, and
   the new `hosts` records are backfilled from Authentik as each host is next seen. Existing
   applications are matched by their provider's `external_host`, so hand-renamed applications
   (`qbit` → `qbittorrent`) are recognised and left alone.
2. **Optional — grant the unbind permission** if you want `REFRESH_ENTRIES`. See
   [Existing install: enable REFRESH_ENTRIES](#existing-install-enable-refresh_entries).
3. **Optional — turn on the new behaviour.** All four new features default to off:
   `REFRESH_ENTRIES=false`, `DRY_RUN=false`, no host filters, `LOG_TYPE=CONSOLE`.

One change takes effect with no opt-in: the image now has a **healthcheck**, so the container
reports `healthy`/`unhealthy` where it previously reported nothing. Anything watching container
health (deunhealth, monitoring dashboards) will start seeing it.

## Running unprivileged

The image still runs as root by default, and that is deliberate. The Authentik API token
arrives as a Docker secret, and compose bind-mounts that file with its **host** ownership —
normally `root:root 0600`. An image that switched to a non-root user by default would fail at
startup on every existing and new install with `Permission denied: /run/secrets/authentik_token`.

A `companion` user (uid/gid 1000) is built into the image, so opting in is two host commands
and one compose line:

```bash
sudo chown 1000:1000 /path/to/docker/secrets/authentik_token   # keep it 0600
sudo chown -R 1000:1000 /path/to/appdata/traefik-authentik-companion/data
```

```yaml
services:
  traefik-authentik-companion:
    user: "1000:1000"
```

Arguably better than the default: the token becomes readable only by the uid that actually
needs it, instead of by root. If you get it wrong the failure is loud and specific — the
companion checks that its state directory is writable at startup and names the problem.

### Verification

The decision logic has an offline test suite — no Authentik, Traefik, or Docker required:

```bash
python3 -m unittest discover -s tests -v
```

v5 was additionally exercised end-to-end against a throwaway **Authentik 2026.8.0** server
(server + worker + postgres + redis) covering: dry run, first provisioning with the scoped
service account, the multi-apex collision, label changes in both directions under
`REFRESH_ENTRIES`, host exclusion, stale removal, file logging, the healthcheck, and the
v2 → v3 upgrade path against pre-existing applications.

## Deployrr / docker-compose usage

See the [deployrr-tools community app](https://github.com/onttm/deployrr-tools/tree/main/community-apps/authentik-companion) for the ready-to-use `compose.yml` and `manifest.json`. The container, service account, and volume paths all use the short name `authentik-companion` for convenience.

## Future: unified stack-companion

Both authentik-companion and cf-companion watch the same Traefik router list for the same event: a new protected subdomain. The planned convergence path:

1. **Phase 1 (now):** run independently, same poll cadence, complementary actions
2. **Phase 2:** shared Traefik discovery module / library
3. **Phase 3:** single `stack-companion` container — one poll, pluggable providers for Cloudflare DNS and Authentik SSO in one pass
