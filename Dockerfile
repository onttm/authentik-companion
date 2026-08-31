# Base image is tracked by tag, not digest, so rebuilds pick up Debian security
# patches. Pinning a digest here would freeze known CVEs in place until someone
# remembered to bump it.
FROM python:3.12-slim

# A `companion` user (uid/gid 1000) exists so the container CAN run unprivileged,
# but the image does not switch to it by default. The Authentik API token is
# delivered as a Docker secret, and compose bind-mounts that file with its host
# ownership — typically root:root 0600. Defaulting to non-root would make every
# existing and new install fail at startup with "Permission denied" on the token.
#
# To run unprivileged (recommended), chown the secret and the data directory to
# 1000:1000 on the host, then set in compose:
#     user: "1000:1000"
# See "Running unprivileged" in the README.
RUN groupadd --gid 1000 companion \
 && useradd --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin companion

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# /logs is only used when LOG_TYPE=FILE or BOTH; it exists so the default path
# works without a bind mount, and is owned by the unprivileged runtime user.
RUN mkdir -p /data /logs && chown -R companion:companion /data /logs /app

# Deliberately no `USER` — see the note above. Set `user: "1000:1000"` in compose
# to run as the unprivileged `companion` user once the host files are chowned.

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["python", "-u", "main.py"]
