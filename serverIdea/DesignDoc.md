# ODDArchiver-server Design Document

## Overview

`ODDArchiver-server` is a service that collects data from multiple machines across a network and orchestrates burns to a locally attached optical disc drive (ODD). It depends on `ODDArchiver` as a library, calling its Python API directly rather than shelling out to the CLI.

The server does not solve the disc format, delta algorithm, or encryption problems -- those are ODDArchiver's concerns. The server solves: how to get the right plaintext data onto the archive machine reliably, securely, and automatically, then hand it to ODDArchiver at the right time.

**Design philosophy:** Like ODDArchiver, ODDArchiver-server is usable at any skill level. A minimal deployment is a single machine pointing at a local directory with no external services. A full enterprise homelab deployment adds restic, Vault, and observability integrations. Each tier is independently functional.

---

## Goals

- Collect data from multiple client machines onto the archive server
- Orchestrate ODDArchiver burn sessions: staging, scheduling, triggering
- Manage disc inventory (multiple discs, multiple clients, capacity tracking)
- Integrate with common homelab tooling without requiring it
- Expose a CLI; web UI is a future addition
- Integrate with observability stacks (OpenSearch, Prometheus, Grafana, Alertmanager)
- Idempotent and safe to restart at any point

## Non-Goals

- Replacing restic (data collection is delegated to existing tools)
- Being a general-purpose backup scheduler
- GUI (future addition)
- Windows client support (Linux/macOS clients via restic; Windows via restic natively)

---

## Project Relationship

```
oddarchiver             # standalone disc tool; no server awareness
    ↑ library dependency
oddarchiver-server      # server orchestration; calls ODDArchiver Python API
```

ODDArchiver-server imports ODDArchiver as a Python library:

```python
from oddarchiver import BurnSession, Disc, Manifest, Delta
```

It never shells out to the `oddarchiver` CLI. This means ODDArchiver must expose a clean Python API in addition to its CLI -- this is a requirement imposed on ODDArchiver by ODDArchiver-server.

---

## Architecture

```
oddarchiver-server/
├── __main__.py           # entry point
├── cli.py                # argparse, command dispatch
├── config.py             # config file loading, validation
├── inventory.py          # disc and client registry
├── scheduler.py          # trigger evaluation, burn queue
├── connectors/
│   ├── base.py           # abstract Connector interface
│   ├── local.py          # local directory connector (simplest)
│   ├── restic.py         # restic rest-server connector
│   └── rsync.py          # rsync/SSH pull connector (fallback)
├── secrets/
│   ├── base.py           # abstract SecretProvider interface
│   ├── env.py            # environment variable provider
│   ├── file.py           # plaintext or keyfile on disk
│   └── vault.py          # HashiCorp Vault provider (optional)
├── pipeline.py           # restore → stage → burn orchestration
├── notify.py             # observability integrations
└── api.py                # local HTTP API for webhook triggers
```

### External Dependencies

| Tool | Purpose | Required? |
|---|---|---|
| `oddarchiver` | Disc burn library | Yes |
| `restic` | Snapshot and restore (restic connector) | Only with restic connector |
| `restic/rest-server` | Receives client backups | Only with restic connector |
| `rsync` | Pull connector | Only with rsync connector |
| `hvac` | Vault secret provider | Only with vault extras |

```
pip install oddarchiver-server              # local + rsync connectors
pip install oddarchiver-server[restic]      # adds restic connector
pip install oddarchiver-server[vault]       # adds Vault secret provider
pip install oddarchiver-server[all]         # everything
```

---

## Complexity Gradient

### Level 0: Local Directory

No external tools. ODDArchiver-server watches a local directory and burns it on schedule. Useful for a single machine or when another tool (Syncthing, a cron rsync) is already delivering files.

```toml
[[client]]
name = "workstation"
connector = "local"
path = "/srv/oddarchiver/workstation"
schedule = "0 2 * * 0"   # weekly, Sunday 2am
```

### Level 1: rsync Pull

Server SSHes into clients and rsyncs configured paths. Clients need only sshd. No restic, no agents.

```toml
[[client]]
name = "nas"
connector = "rsync"
host = "nas.lan"
user = "backup"
paths = ["/etc/", "/opt/configs/"]
schedule = "0 2 * * 0"
```

### Level 2: Restic (recommended for most users)

Clients push to restic rest-server. ODDArchiver-server restores snapshots to staging and burns. Most serious homelabs already run or could run restic.

```toml
[[client]]
name = "openstack-ctrl"
connector = "restic"
repo = "rest:https://archive-server:8000/openstack-ctrl"
schedule = "0 2 * * 0"
```

### Level 3: Restic + Vault

Vault supplies restic repo passwords and ODDArchiver keyfiles. Fully automated with no plaintext secrets in config files.

```toml
[[client]]
name = "openstack-ctrl"
connector = "restic"
repo = "rest:https://archive-server:8000/openstack-ctrl"
schedule = "0 2 * * 0"

[client.secrets]
provider = "vault"
restic_password_path = "secret/oddarchiver/openstack-ctrl/restic-password"
oddarchiver_keyfile_path = "secret/oddarchiver/keyfile"
```

### Level 4: Full Observability Stack

Adds Prometheus metrics, OpenSearch log shipping, Grafana dashboards, Alertmanager alerts, Ntfy/Gotify notifications.

---

## Data Collection: Why Restic Push

The confirmed architecture for multi-machine data collection is **restic push to restic rest-server**, for the following reasons:

**Restic is the de facto standard** for serious homelab backup collection. It handles encryption in transit, incremental chunk transfer, snapshot history, and cross-platform clients. Implementing a custom agent would duplicate this work worse.

**Restic push is the only supported model.** Restic does not support server-initiated pull. Clients run `restic backup` on schedule; the server receives.

**rest-server append-only mode** prevents a compromised client from deleting or modifying historical snapshots on the server.

**Plaintext lands on the server after restore.** ODDArchiver-server calls `restic restore` into a staging directory on the server's LUKS-encrypted filesystem. ODDArchiver then sees plaintext and can diff efficiently. Restic's encryption is a transport and at-rest mechanism for the repo; it is not the disc encryption.

**Why not Syncthing:** Syncthing is a sync tool, not a backup tool. It has no snapshot history, no coherent point-in-time state, and no append-only guarantees. It could deliver files but would not give ODDArchiver-server the ability to restore a known-good state from a specific point in time.

**Why not a custom agent:** More code, more maintenance, worse security, less adoption. Restic already exists and is trusted.

---

## restic rest-server Setup

The archive server runs `restic/rest-server` as a service:

```bash
rest-server \
  --path /srv/restic-repos \
  --tls \
  --tls-cert /etc/ssl/oddarchiver/server.crt \
  --tls-key /etc/ssl/oddarchiver/server.key \
  --append-only \
  --private-repos
```

`--append-only`: clients can create new snapshots but cannot delete or modify existing ones.

`--private-repos`: each client can only access its own subdirectory, enforced by HTTP basic auth.

Per-client credentials stored in `htpasswd` file. If using Vault, ODDArchiver-server generates and rotates these automatically.

Each client runs on schedule (cron or systemd timer):

```bash
restic -r rest:https://archive-server:8000/clientname \
  --password-file /etc/restic/password \
  backup /etc/vault /home/user/.ssh /opt/configs
```

---

## Secret Provider System

Secrets (restic passwords, ODDArchiver keyfile paths) are retrieved through a pluggable provider interface. ODDArchiver-server tries the configured provider; if unavailable, falls back in order.

```python
class SecretProvider:
    def get(self, key: str) -> str: ...
```

### Providers

**`env`**: reads from environment variable.
```toml
[secrets]
provider = "env"
```

**`file`**: reads from a file path. Suitable for keyfiles on LUKS volumes or Apricorn USB.
```toml
[secrets]
provider = "file"
base_path = "/mnt/apricorn/oddarchiver-secrets/"
```

**`vault`**: reads from HashiCorp Vault KV store. Requires `oddarchiver-server[vault]`.
```toml
[secrets]
provider = "vault"
addr = "https://vault.lan:8200"
auth = "approle"
role_id_file = "/etc/oddarchiver/vault-role-id"
secret_id_file = "/etc/oddarchiver/vault-secret-id"
fallback = "file"
fallback_base_path = "/mnt/apricorn/oddarchiver-secrets/"
```

**Vault is additive, not required.** If Vault is unreachable and a fallback is configured, ODDArchiver-server uses the fallback silently and logs a WARN. If no fallback is configured and Vault is unreachable, the burn is skipped and logged as ERROR -- failing loudly rather than silently.

Vault holds:
- restic repo password per client (`secret/oddarchiver/<clientname>/restic-password`)
- ODDArchiver keyfile content (`secret/oddarchiver/keyfile`) -- server writes this to a temp file on the LUKS volume, passes path to ODDArchiver, shreds temp file after burn

From ODDArchiver's perspective it always receives a keyfile path or passphrase. It has no awareness of Vault.

---

## Burn Pipeline

```
1. ACQUIRE LOCK
   - Per-client advisory lock (file lock)
   - Prevents concurrent burns for the same client
   - Separate lock per disc device (one burn at a time per ODD)

2. COLLECT SECRETS
   - Retrieve restic password (if restic connector)
   - Retrieve ODDArchiver keyfile (if encryption enabled)
   - On failure: log ERROR, release lock, skip burn

3. RESTORE TO STAGING
   For restic connector:
     restic restore latest \
       --repo <repo> \
       --password-file <tmpfile> \
       --target /srv/oddarchiver/staging/<clientname>/
   For local connector:
     rsync -a <source>/ /srv/oddarchiver/staging/<clientname>/
   For rsync connector:
     rsync -az <user>@<host>:<paths> /srv/oddarchiver/staging/<clientname>/

   Staging directory is on LUKS-encrypted filesystem
   Plaintext exists here temporarily; shredded after burn (step 6)

4. CALL ODDARCHIVER
   from oddarchiver import BurnSession
   session = BurnSession(
       source="/srv/oddarchiver/staging/<clientname>/",
       device=disc.device,
       keyfile=keyfile_path,   # None if no encryption
       label=disc.label,
   )
   result = session.sync()

5. RECORD RESULT
   - Update inventory: disc capacity, last burn timestamp, session index
   - Log result at INFO or ERROR
   - Emit metrics (if Prometheus enabled)
   - Send notifications (if configured)

6. CLEANUP
   - Shred staging: shred -u /srv/oddarchiver/staging/<clientname>/*
   - Remove temp secret files
   - Release locks
   - Runs in finally block -- executes even on crash or Ctrl+C
```

---

## Scheduler and Triggers

All triggers funnel into the same burn pipeline.

### Trigger Types

**Scheduled (cron expression per client)**
```toml
[[client]]
schedule = "0 2 * * 0"
```

**Manual (CLI)**
```bash
oddarchiver-server burn <clientname> [--device /dev/sr0]
```

**Threshold**
```toml
[[client]]
burn_threshold_mb = 50   # burn when staged changes exceed 50MB
```

**Webhook**
```
POST http://localhost:8765/trigger/<clientname>
Authorization: Bearer <token>
```
Allows Shuffle SOAR, Ansible, or any HTTP client to trigger a burn. Useful for event-driven archival: trigger a burn immediately after secrets rotation rather than waiting for the weekly schedule.

### Scheduler Implementation

Single-process scheduler using APScheduler. Burn pipeline runs in a thread pool with one worker per ODD device. No separate scheduler daemon -- ODDArchiver-server is one process, one systemd service.

---

## Disc Inventory

```toml
# ~/.config/oddarchiver-server/inventory.toml (managed by server)

[[disc]]
label = "ARCHIVE-01"
device = "/dev/sr0"
sessions = 4
used_gb = 6.1
capacity_gb = 23.0
percent_full = 26
last_burn = "2026-04-13T02:00:00Z"
clients = ["workstation", "nas", "openstack-ctrl"]
status = "active"   # "active" | "full" | "retired"

[[disc]]
label = "ARCHIVE-00"
sessions = 153
used_gb = 22.8
capacity_gb = 23.0
percent_full = 99
last_burn = "2025-11-02T02:00:00Z"
clients = ["workstation"]
status = "retired"
```

When a disc reaches capacity threshold (default 95%), it is marked `full` and the admin is alerted. The next `init` against a new disc picks up where the old one left off -- clients are not reset, delta chains continue correctly across physical discs.

**Multi-disc per client**: a client's archive can span multiple physical discs. `oddarchiver restore` accepts a list of disc mount points and reconstructs across them in session order.

---

## CLI Interface

```
oddarchiver-server start                        # start server daemon
oddarchiver-server stop                         # stop daemon

oddarchiver-server register <clientname>        # add a new client
oddarchiver-server unregister <clientname>      # remove client (does not erase disc data)

oddarchiver-server burn <clientname>            # manual trigger
oddarchiver-server burn --all                   # burn all clients with pending changes

oddarchiver-server status                       # disc inventory, client health, pending burns
oddarchiver-server history [--client NAME]      # session log across all clients
oddarchiver-server verify [--client NAME]       # run oddarchiver verify for client's disc(s)

oddarchiver-server disc init --label LABEL      # initialize a new disc
oddarchiver-server disc retire                  # mark current disc retired, prompt for new one
oddarchiver-server disc status                  # capacity, session count, % full
```

---

## Idempotency

All ODDArchiver idempotency guarantees are inherited via the library call. Additionally:

| Operation | Idempotent? | Mechanism |
|---|---|---|
| `burn` triggered twice simultaneously | Yes | Per-client + per-device file locks |
| Server restart mid-burn | Yes | Pipeline lock released in finally; next run re-stages |
| `register` existing client | Yes | No-op if client already exists |
| Vault unavailable on trigger | Yes | Skipped with ERROR log; next schedule retries |
| Staging dir exists from prior crash | Yes | Deleted and rebuilt at pipeline start |
| restic snapshot not yet available | Yes | Checks snapshot exists before restore; skips if not |

---

## Ctrl+C / Restart Safety

ODDArchiver-server installs SIGTERM and SIGINT handlers. On signal:

1. No new burn pipelines are started
2. In-flight pipelines are allowed to complete their current step
3. If mid-burn: ODDArchiver's Ctrl+C safety applies (see ODDArchiver design doc)
4. Staging directories are shredded in finally blocks
5. Locks are released

On forced kill (`SIGKILL`): next start detects orphaned staging directories and lock files, logs SUSPECT, cleans up, resumes normally.

---

## Logging

Default: `/var/log/oddarchiver-server/server.log` (system) or `~/logs/oddarchiver-server.log` (user).

```
2026-04-18T02:00:00Z INFO    [scheduler] Trigger fired: client=openstack-ctrl type=scheduled
2026-04-18T02:00:01Z INFO    [secrets] Restic password retrieved from Vault
2026-04-18T02:00:02Z INFO    [restic] Restoring snapshot abc123 to /srv/oddarchiver/staging/openstack-ctrl/
2026-04-18T02:01:15Z INFO    [restic] Restore complete: 2.1GB in 73s
2026-04-18T02:01:15Z INFO    [pipeline] Calling oddarchiver sync
2026-04-18T02:03:44Z INFO    [pipeline] Burn complete: session=5 written=4.2MB
2026-04-18T02:03:44Z INFO    [inventory] Disc ARCHIVE-01: 28% full (6.5GB / 23.0GB)
2026-04-18T02:03:45Z INFO    [cleanup] Staging shredded
2026-04-18T02:03:45Z INFO    [notify] Prometheus metrics updated
```

### Log Levels

| Level | When |
|---|---|
| `INFO` | Normal operation milestones |
| `WARN` | Vault unreachable but fallback available; disc >80% full; restic snapshot older than expected |
| `ERROR` | Vault unreachable with no fallback; burn failed; disc full; restic restore failed |
| `SUSPECT` | Orphaned staging dir on startup; client not seen for >2x its schedule interval |

### Disc Capacity Alerts

Same 80%/95% thresholds as ODDArchiver. Alert fires once when threshold crossed; clears when new disc initialized. Always visible in `oddarchiver-server status`.

---

## Observability Integrations

All optional and additive. The server works without any of them.

### Prometheus

Exposes `/metrics` on `localhost:9876`:

```
oddarchiver_disc_percent_full{label="ARCHIVE-01"} 28.0
oddarchiver_disc_sessions_total{label="ARCHIVE-01"} 5
oddarchiver_burn_duration_seconds{client="openstack-ctrl"} 149.0
oddarchiver_burn_success_total{client="openstack-ctrl"} 12
oddarchiver_burn_failure_total{client="openstack-ctrl"} 0
oddarchiver_client_last_burn_timestamp{client="openstack-ctrl"} 1.745e9
oddarchiver_client_healthy{client="openstack-ctrl"} 1
```

### Grafana

Dashboard provisioning JSON in `contrib/grafana/oddarchiver-dashboard.json`.

### Alertmanager

Sample rules in `contrib/alertmanager/oddarchiver-rules.yml`:

```yaml
- alert: OddArchiverDiscNearFull
  expr: oddarchiver_disc_percent_full > 80
  for: 1h
  annotations:
    summary: "BD-R disc {{ $labels.label }} is {{ $value }}% full"

- alert: OddArchiverClientMissed
  expr: time() - oddarchiver_client_last_burn_timestamp > 1209600
  annotations:
    summary: "Client {{ $labels.client }} has not burned in over 2 weeks"

- alert: OddArchiverBurnFailed
  expr: increase(oddarchiver_burn_failure_total[1h]) > 0
  annotations:
    summary: "Burn failure for client {{ $labels.client }}"
```

### OpenSearch

JSON log format via `log_format = "json"`. Index template in `contrib/opensearch/oddarchiver-index-template.json`. Filebeat or Fluent Bit picks up from log file.

### Ntfy / Gotify

```toml
[notify.ntfy]
enabled = true
url = "https://ntfy.lan/oddarchiver"
events = ["burn_complete", "burn_failed", "disc_near_full", "disc_full"]

[notify.gotify]
enabled = true
url = "https://gotify.lan"
token = "..."
events = ["burn_failed", "disc_full"]
```

### Shuffle SOAR

Webhook endpoint for event-driven triggers:

```
POST http://archive-server:8765/trigger/<clientname>
Authorization: Bearer <token>
```

Example: secrets rotation event in Shuffle → HTTP node → trigger immediate ODDArchiver burn for the affected client.

---

## Configuration

`~/.config/oddarchiver-server/config.toml`:

```toml
[server]
log_file = "~/logs/oddarchiver-server.log"
log_format = "text"          # "text" | "json"
staging_base = "/srv/oddarchiver/staging"
inventory_file = "~/.config/oddarchiver-server/inventory.toml"
api_port = 8765
api_token = "..."            # generate: openssl rand -hex 32

[disc]
device = "/dev/sr0"
capacity_warn_pct = 80
capacity_error_pct = 95

[secrets]
provider = "file"            # "env" | "file" | "vault"
base_path = "/mnt/apricorn/oddarchiver-secrets/"
# fallback = "file"
# fallback_base_path = "..."

# [secrets.vault]
# addr = "https://vault.lan:8200"
# auth = "approle"
# role_id_file = "/etc/oddarchiver/vault-role-id"
# secret_id_file = "/etc/oddarchiver/vault-secret-id"

[oddarchiver]
encrypt = "keyfile"          # "none" | "passphrase" | "keyfile"
# keyfile_secret_key = "oddarchiver-keyfile"

[restic]
rest_server_url = "https://archive-server:8000"
htpasswd_file = "/etc/restic/htpasswd"
tls_cert = "/etc/ssl/oddarchiver/server.crt"
tls_key = "/etc/ssl/oddarchiver/server.key"

# [notify.ntfy]
# enabled = true
# url = "https://ntfy.lan/oddarchiver"
# events = ["burn_failed", "disc_full"]

# [metrics]
# enabled = true
# port = 9876

[[client]]
name = "workstation"
connector = "restic"
repo = "rest:https://archive-server:8000/workstation"
schedule = "0 2 * * 0"

[[client]]
name = "nas"
connector = "rsync"
host = "nas.lan"
user = "backup"
paths = ["/etc/", "/opt/configs/"]
schedule = "0 3 * * 0"

[[client]]
name = "openstack-ctrl"
connector = "restic"
repo = "rest:https://archive-server:8000/openstack-ctrl"
schedule = "0 4 * * 0"
```

---

## Systemd Service

`/etc/systemd/system/oddarchiver-server.service`:

```ini
[Unit]
Description=ODDArchiver archival server
After=network.target
RequiresMountsFor=/srv/oddarchiver

[Service]
Type=simple
User=oddarchiver
ExecStart=/usr/bin/oddarchiver-server start
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## Security Considerations

**Staging on LUKS**: plaintext from restic restore lands here temporarily. Shredded immediately after burn.

**rest-server TLS + append-only**: clients push over TLS; append-only prevents client compromise from destroying backup history.

**Webhook token**: local API should be bound to localhost or management VLAN only.

**Vault AppRole**: preferred over token auth for automation. Secret ID can be short-lived and rotated. ODDArchiver-server can fetch a new Secret ID from a wrapped token on startup.

**Least privilege**: `oddarchiver` system user has no sudo. ODD access via `cdrom`/`optical` group. Restic repo credentials scoped per client.

---

## What to Archive

See the ODDArchiver design document for full Tier 1/2/3 data classification.

### Per-Client Sizing

**25GB BD-R:**

| Client type | Typical Tier 1+2 size | Approx sessions before full |
|---|---|---|
| Workstation (keys, configs, kdbx) | 1–3GB | 30–100+ weekly sessions |
| NAS (configs, database dumps) | 2–8GB | 10–50 sessions |
| OpenStack controller | 3–10GB | 8–30 sessions |
| Monitoring server (rules, dashboards) | 1–4GB | 20–80 sessions |

**100GB BDXL:** Suitable for Tier 2 database dumps alongside Tier 1. A single disc per client holds years of weekly delta sessions before reaching capacity.

---

## Future Considerations

- **Web UI**: status dashboard, manual burn trigger, disc inventory, client health. Flask or FastAPI backend. CLI remains fully functional.
- **Multi-drive redundancy**: `--mirror /dev/sr1` burns identical sessions to two ODDs simultaneously. Store one on-site, one off-site.
- **Cross-disc restore**: `oddarchiver-server restore <clientname> --discs /mnt/disc0 /mnt/disc1` reconstructs full history across multiple physical discs.
- **Restic snapshot policy integration**: trigger burns on snapshots matching a policy (weekly only, not daily) rather than a separate schedule.
- **Vault credential rotation**: automatically rotate restic htpasswd credentials, updating both rest-server and Vault.