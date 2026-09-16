# proxmox2mqtt

Bridge that polls the Proxmox VE API (and optionally a Proxmox Backup Server)
with read-only API tokens and publishes node, guest, physical disk and storage
metrics to MQTT in the Home Assistant discovery format. Any MQTT consumer can
use the same retained topics. Public open-source project, MIT licensed.

## Tech Stack

- Python 3.13 (Docker image `python:3.13-slim`), a single script, no framework.
- `requests`: PVE and PBS REST APIs (API token header, TLS verification optional).
- `paho-mqtt` 2.x: the code uses `mqtt.CallbackAPIVersion.VERSION2`, so 1.x
  does not work.
- Both packages are installed unpinned by the `Dockerfile` (no requirements
  file): a rebuild takes the latest releases.
- Docker Compose, one service.

## Directory Structure

```
proxmox2mqtt/
├── proxmox2mqtt.py - the whole bridge: config, collectors, SMART/ZFS parsers, discovery, loop
├── Dockerfile - python:3.13-slim with requests and paho-mqtt
├── docker-compose.yml - service proxmox2mqtt on the external mqtt_net network
├── .env.example - configuration template (copy to .env, gitignored)
├── README.md - user overview: PVE/PBS token setup, variables, MQTT topics
├── LICENSE - MIT
├── scripts/
│   └── doc_check.py - documentation staleness check
└── .githooks/
    └── pre-commit - runs the check before every commit
```

## Setup & Commands

PVE token: create it read-only (`PVEAuditor` on `/`) as shown in `README.md`.
PBS token (optional, for last backup and verify state) on the backup server,
same commands as in `README.md`:

```bash
proxmox-backup-manager user create monitoring@pbs
proxmox-backup-manager user generate-token monitoring@pbs mqtt
proxmox-backup-manager acl update /datastore/<datastore> DatastoreAudit --auth-id monitoring@pbs
proxmox-backup-manager acl update /datastore/<datastore> DatastoreAudit --auth-id 'monitoring@pbs!mqtt'
```

Bridge:

```bash
cp .env.example .env                  # then fill in hosts, node, tokens, broker
docker network create mqtt_net        # only if the external network does not exist yet
docker compose config --services      # validates the compose file: prints proxmox2mqtt
docker compose up -d --build          # build and start (also after every code change)
docker logs -f proxmox2mqtt           # bridge log
python3 -m py_compile proxmox2mqtt.py # syntax check, touches nothing
python3 scripts/doc_check.py          # documentation staleness check
```

- A healthy start logs `[mqtt] <host>:<port> as <user>; backups=on|off`,
  `[discovery] N guests + node`, `[pve] reachable -> online`, then
  `[discovery] N disks` and `[discovery] N storages`.
- There is no test suite and no API mock: verification means running against a
  real node and watching the topics below. The bridge only sends GET requests.
- The compose file expects an existing external network `mqtt_net` shared with
  the broker; `MQTT_HOST` is the broker's name on that network. The container
  also needs outbound HTTPS to the PVE and PBS hosts.

## Coding Conventions

- One script, top to bottom: configuration, availability helpers, sensor
  tables, parsers, collectors (`collect_*`), discovery (`publish_*_discovery`),
  state publishers (`publish_*`), `main()`. Functions in `snake_case`, module
  constants in `UPPER_CASE`.
- Sensor tables are the single source of truth for entities: `GUEST_SENSORS`,
  `NODE_SENSORS`, `DISK_SENSORS` (+ `DISK_SENSORS_NVME` / `DISK_SENSORS_ATA`,
  the latter built from `ATA_ATTRS`), `STORAGE_SENSORS` (+
  `STORAGE_SENSORS_ZFS`). A new metric is one entry there plus the line that
  fills it in the collector; discovery follows from the table.
- Topic layout, one level per kind, never repeating a name inside a branch:
  `<BASE>/node/<key>`, `<BASE>/guest/<vmid>/<key>`, `<BASE>/disk/<dev>/<key>`,
  `<BASE>/storage/<id>/<key>`, plus `<BASE>/availability`. Keep it: consumers
  bind to these paths.
- API calls go through `pve_get` (base URL, token session, `raise_for_status`).
  Only GET requests: the tokens are read-only on purpose and the bridge must
  never need more.
- A collector failure must not hide the rest: SMART, ZFS and PBS errors are
  logged with a `[smart]`/`[zfs]`/`[pbs]`/`[disks]` tag and the loop goes on;
  only an unreachable PVE marks the bridge `offline`.
- Availability goes through `publish_avail`, and `on_connect` re-asserts it
  after every reconnection (the retained LWT `offline` would stick otherwise).
- Everything is published retained; discovery and availability QoS 1, state
  QoS 0. The bridge subscribes to nothing.
- Logging is `print` through `log()`; never print tokens or the MQTT password.
- Comments, docstrings and log messages in English (public repository).

## Git Workflow and operating rules

- Single branch `main`, pushed to `origin` on GitHub. No CI.
- Commit messages in English, imperative subject (e.g. `Report physical disks
  and storages, and restructure the topic layout`).
- Commits, pushes and releases are done by the maintainer only; nothing is
  pushed without an explicit decision.
- Public repository: never commit `.env`, tokens, IP addresses, hostnames,
  node or guest names. `.env.example` holds placeholders only.
- Changing the topic layout or discovery ids breaks every consumer: say so in
  the commit message and in the README.
- Deploying a change means `docker compose up -d --build`; the image does not
  follow the repository by itself.
- Documentation-code coherence: a change that makes a sentence of the
  documentation false fixes it in the same commit. When the staleness check
  fails, fix the document, do not silence the check. The check catches broken
  references (names that no longer exist), not descriptions that became false.
- Staleness check: `python3 scripts/doc_check.py` (manual run). The versioned
  hook `.githooks/pre-commit` runs it on every commit once enabled, once per
  clone: `git config core.hooksPath .githooks`. Markers for legitimate
  exceptions: `<!-- doc-check:ignore -->` (line), `-start`/`-end` (block),
  `-file` (whole document).

## Key Files & Directories

- `proxmox2mqtt.py`: all runtime code.
- `.env.example`: configuration template; the real `.env` is gitignored.
- `docker-compose.yml`: restart policy `unless-stopped`, no ports, no volumes.
- Tests: none. Documentation: this file and `README.md`.

## Related documents

- [README.md](README.md): user overview, PVE/PBS token setup, every variable,
  MQTT topic layout.

## Integrations (census)

Before adding or changing a call to PVE or PBS or a published topic, check
this census; every new call or topic is added here in the same commit, before
the code.

| # | API | Endpoint (GET) | Every | Used for |
|---|---|---|---|---|
| 1 | PVE | `/nodes/<node>/qemu`, `/nodes/<node>/lxc` | `POLL_INTERVAL` | guests: status, CPU, memory, disk, net counters, uptime |
| 2 | PVE | `/nodes/<node>/status` | `POLL_INTERVAL` | node metrics |
| 3 | PBS | `/admin/datastore/<datastore>/snapshots` | `POLL_INTERVAL` | newest snapshot and verify state per numeric `backup-id` |
| 4 | PVE | `/nodes/<node>/disks/list` | `DISK_POLL_INTERVAL` | disk inventory, health, wearout |
| 5 | PVE | `/nodes/<node>/disks/smart?disk=<dev>` | `DISK_POLL_INTERVAL` | SMART: text block (NVMe) or attribute list (ATA) |
| 6 | PVE | `/nodes/<node>/storage`, `/storage` | `DISK_POLL_INTERVAL` | storage status and configuration |
| 7 | PVE | `/nodes/<node>/disks/zfs`, `/nodes/<node>/disks/zfs/<pool>` | `DISK_POLL_INTERVAL` | pool health, layout, members, last scrub |

Why PBS directly: the backup listing through PVE storage content is filtered
by `VM.Backup`, which `PVEAuditor` lacks, so a read-only PVE token sees no
backups at all.

MQTT, all under `<BASE>` = `STATE_PREFIX`, default `proxmox2mqtt/<NODE_ID>`:

| # | Topic | Content |
|---|---|---|
| 1 | `<BASE>/availability` | `online` while PVE answers, else `offline`; also the LWT |
| 2 | `<BASE>/node/<key>` | `NODE_SENSORS` keys |
| 3 | `<BASE>/guest/<vmid>/<key>` | `status` (`running`/`stopped`), `GUEST_SENSORS` keys, `last_backup` (ISO, UTC) and `backup_verify` with PBS |
| 4 | `<BASE>/disk/<dev>/<key>` | `DISK_SENSORS` plus the NVMe or ATA set; `storage` is the owning storage or `-` |
| 5 | `<BASE>/storage/<id>/<key>` | `STORAGE_SENSORS`, the ZFS set for pool-backed storages, `disks` (comma list, no entity) |
| 6 | `<DISCOVERY_PREFIX>/<component>/<device id>/<key>/config` | device ids `<NODE_ID>_node`, `<NODE_ID>_guest_<vmid>`, `<NODE_ID>_disk_<dev>`, `<NODE_ID>_storage_<id>` (dashes become underscores) |

## Known issues and operating traps

1. **PBS token without its own ACL.** A PBS token gets nothing from its user
   alone: grant `DatastoreAudit` to the token itself (last command above),
   otherwise the snapshot listing is refused or empty and no `last_backup`
   arrives.
2. **Token formats differ.** `PVE_TOKEN` is `user@realm!tokenid=secret` (equals
   sign), `PBS_TOKEN` is `user@realm!tokenid:secret` (colon).
3. **`.env.example` is behind the code.** It sets `STATE_PREFIX=proxmox/pve`
   (remove it to get the default `proxmox2mqtt/<NODE_ID>`), still lists
   `DEVICE_PREFIX`, which nothing reads, and lacks `NODE_ID` and
   `DISK_POLL_INTERVAL`.
4. **No cleanup.** A deleted guest, disk or storage keeps its retained state
   and discovery topics; remove them by hand (empty retained message on each
   config topic). Discovery is resent only when the set of ids changes, so a
   renamed guest keeps its old device name until the next restart.
5. **Disk poll blocks the loop.** SMART calls (45 s timeout each) run inside
   the main loop: during a disk poll, guest and node values are not refreshed.
   SMART reads can also wake sleeping drives; keep `DISK_POLL_INTERVAL` long.
6. **PBS scope.** Snapshots are listed in full at every poll, from the root
   namespace only, and matched by numeric `backup-id` only: a VM and a CT with
   the same id, or two clusters sharing a datastore, collide.
7. **Scrub time zone.** `zfs_last_scrub` parses the node's local-time text in
   the container's time zone (the compose file sets none, so UTC): with a node
   in another zone the timestamp is shifted by the offset.
8. **Start-up and login.** `connect_async()` retries by itself (1-60 s), first
   connection included, and the process waits up to `MQTT_CONNECT_WAIT` (300 s)
   before going on, so a broker that is down at start no longer kills it. The
   `[mqtt]` start line only means the TCP connection opened; `on_connect`
   ignores the reason code, so rejected credentials are silent here.
9. **Network rates.** `net_in`/`net_out` appear from the second poll on and
   read 0 for one poll after a counter reset.

## Environment Variables and secrets

All variables live in `.env` (gitignored, loaded by `env_file`); the reference
with placeholders is `.env.example`.

| # | Variable | Default | Meaning |
|---|---|---|---|
| 1 | `PVE_HOST` | `127.0.0.1:8006` | PVE API `host:port` |
| 2 | `PVE_NODE` | `pve` | node name as known to the API |
| 3 | `PVE_TOKEN` | empty | `user@realm!tokenid=secret`, read-only (`PVEAuditor`) |
| 4 | `PVE_VERIFY_SSL` | `false` | `true` verifies TLS certificates, for PVE and PBS alike |
| 5 | `PBS_HOST` / `PBS_TOKEN` / `PBS_DATASTORE` | empty | all three set enables backup info |
| 6 | `MQTT_HOST` / `MQTT_PORT` | `mqtt` / `1883` | broker |
| 7 | `MQTT_USER` / `MQTT_PASS` | `proxmox2mqtt` / empty | broker credentials; an empty user means anonymous |
| 8 | `DISCOVERY_PREFIX` | `homeassistant` | discovery root |
| 9 | `NODE_ID` | `PVE_NODE` | node name in topics and entity ids, cosmetic |
| 10 | `STATE_PREFIX` | `proxmox2mqtt/<NODE_ID>` | state root (`<BASE>`) |
| 11 | `POLL_INTERVAL` | `15` | seconds between guest/node polls |
| 12 | `DISK_POLL_INTERVAL` | `600` | seconds between disk, SMART and storage polls |

- Secrets: `PVE_TOKEN`, `PBS_TOKEN`, `MQTT_PASS`. Only in `.env`, never in the
  repository, in logs or in chat. Both API tokens stay read-only
  (`PVEAuditor`, `DatastoreAudit`).
- The MQTT client id is fixed (`proxmox2mqtt`): one instance per broker.
- Give the bridge its own broker user: it only publishes, under `<BASE>/` and
  the discovery topics of its device ids, and subscribes to nothing.
