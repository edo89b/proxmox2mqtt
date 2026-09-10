# proxmox2mqtt

Small bridge that polls the Proxmox VE API and publishes node, guest, physical
disk and storage metrics to MQTT using the Home Assistant discovery format.
Works with Home Assistant, OpenHAB or anything that reads MQTT. Optionally it
also reads a Proxmox Backup Server for the last backup of each guest.

Per VM/CT it reports run state, CPU, memory, disk, network throughput and uptime,
plus the last backup time and its verification state when a PBS is configured.
For the node: CPU, memory, root filesystem usage, 1-minute load, uptime and the
number of running guests. Network throughput is computed from the delta of the
cumulative counters between polls.

On a slower schedule (`DISK_POLL_INTERVAL`) it also reports:

- every physical disk: model, serial, size, WWN, what it is used by and which
  storage it belongs to, health, wearout and SMART data read through the PVE
  API (temperature, power-on hours and cycles, plus the NVMe health log or a
  selection of ATA attributes);
- every storage: type, active state, total/used/available space, content and
  server; for ZFS-backed storages also the pool state, layout, size,
  fragmentation, dedup ratio, data errors, member disks and last scrub.

## Setup

1. On Proxmox VE create a read-only API token:

   ```
   pveum user add monitoring@pve
   pveum acl modify / --users monitoring@pve --roles PVEAuditor
   pveum user token add monitoring@pve mqtt --privsep 0
   ```

2. Optional, for backup info: on the Proxmox Backup Server create a token with
   `DatastoreAudit` on the datastore. A PBS token gets nothing from its user
   alone, so the ACL is granted to the user and to the token itself:

   ```
   proxmox-backup-manager user create monitoring@pbs
   proxmox-backup-manager user generate-token monitoring@pbs mqtt
   proxmox-backup-manager acl update /datastore/<datastore> DatastoreAudit --auth-id monitoring@pbs
   proxmox-backup-manager acl update /datastore/<datastore> DatastoreAudit --auth-id 'monitoring@pbs!mqtt'
   ```

3. Copy `.env.example` to `.env` and fill in the PVE host/node/token, the
   optional PBS settings and your MQTT broker (see the notes below about
   `.env.example`).

4. `docker compose up -d --build`

The provided `docker-compose.yml` attaches the container to an existing
external network `mqtt_net` shared with the broker (`docker network create
mqtt_net` if it does not exist yet); `MQTT_HOST` is the broker's name on that
network. The container also needs outbound HTTPS to the PVE and PBS hosts.

## Configuration

| # | Variable | Default | Description |
|---|---|---|---|
| 1 | `PVE_HOST` | `127.0.0.1:8006` | `host:port` of the PVE API (e.g. `10.0.0.10:8006`) |
| 2 | `PVE_NODE` | `pve` | node name as known to the API |
| 3 | `PVE_TOKEN` | empty | `user@realm!tokenid=secret` (equals sign before the secret) |
| 4 | `PVE_VERIFY_SSL` | `false` | `true` verifies the TLS certificates of PVE and PBS alike |
| 5 | `PBS_HOST` | empty | `host:port` of the PBS API; backup info is on only when `PBS_HOST`, `PBS_TOKEN` and `PBS_DATASTORE` are all set |
| 6 | `PBS_TOKEN` | empty | `user@realm!tokenid:secret` (colon before the secret) |
| 7 | `PBS_DATASTORE` | empty | datastore whose snapshots are read |
| 8 | `MQTT_HOST` / `MQTT_PORT` | `mqtt` / `1883` | broker |
| 9 | `MQTT_USER` / `MQTT_PASS` | `proxmox2mqtt` / empty | broker credentials; an empty user connects anonymously |
| 10 | `DISCOVERY_PREFIX` | `homeassistant` | root of the discovery topics |
| 11 | `NODE_ID` | value of `PVE_NODE` | node name in topics and entity ids; cosmetic, while `PVE_NODE` must match the API |
| 12 | `STATE_PREFIX` | `proxmox2mqtt/<NODE_ID>` | base topic, `<BASE>` below |
| 13 | `POLL_INTERVAL` | `15` | seconds between guest, node and PBS polls |
| 14 | `DISK_POLL_INTERVAL` | `600` | seconds between disk, SMART and storage polls; SMART reads can wake sleeping drives, so keep it long |

## MQTT topics

Everything is published retained under `<BASE>` (`STATE_PREFIX`, default
`proxmox2mqtt/<NODE_ID>`), one level per kind of object:

| # | Topic | Content |
|---|---|---|
| 1 | `<BASE>/availability` | `online` while the PVE API answers, `offline` otherwise; also the LWT |
| 2 | `<BASE>/node/<key>` | `cpu`, `mem`, `mem_used`, `disk` (root filesystem), `load1`, `uptime`, `guests_running` |
| 3 | `<BASE>/guest/<vmid>/<key>` | `status` (PVE run state, e.g. `running` / `stopped`), `cpu`, `mem`, `mem_used`, `disk`, `net_in`, `net_out`, `uptime`; with PBS also `last_backup` (ISO 8601, UTC) and `backup_verify` (`none` if never verified) |
| 4 | `<BASE>/disk/<dev>/<key>` | `<dev>` is the device name (e.g. `sda`, `nvme0n1`); inventory, health and SMART keys; `storage` is the owning storage or `-` |
| 5 | `<BASE>/storage/<id>/<key>` | capacity and state keys, the `zfs_*` keys for ZFS-backed storages, `disks` (comma-separated member disks) |
| 6 | `<DISCOVERY_PREFIX>/<component>/<device id>/<key>/config` | Home Assistant discovery |

Units: percent for `cpu`, `mem` and `disk`, MiB for `mem_used`, KiB/s for
`net_in`/`net_out`, seconds for `uptime`. The key lists per kind are the sensor
tables at the top of `proxmox2mqtt.py`: `NODE_SENSORS`, `GUEST_SENSORS`,
`DISK_SENSORS` with `DISK_SENSORS_NVME` or `DISK_SENSORS_ATA`, and
`STORAGE_SENSORS` with `STORAGE_SENSORS_ZFS`. A disk or storage key the API
does not provide for that device is not published.

Discovery creates one device per node (`<NODE_ID>_node`), guest
(`<NODE_ID>_guest_<vmid>`), disk (`<NODE_ID>_disk_<dev>`) and storage
(`<NODE_ID>_storage_<id>`, dashes turned into underscores). The guest run state
is a `binary_sensor`, everything else a `sensor`.

## Notes

- Disk usage for QEMU guests is only reported when the guest agent exposes it;
  otherwise it stays at 0. Containers report it directly.
- Both tokens stay read-only: `PVEAuditor` on PVE, `DatastoreAudit` on the PBS
  datastore. The bridge only sends GET requests.
- `.env.example` still carries settings from the first release:
  `STATE_PREFIX=proxmox/pve` overrides the default base topic (delete the line
  to get `proxmox2mqtt/<NODE_ID>`), `DEVICE_PREFIX` is no longer read, and
  `NODE_ID` / `DISK_POLL_INTERVAL` are not listed (their defaults apply).
- The topic layout and the discovery ids changed when disk and storage support
  was added: update existing consumers and remove the old retained discovery
  configs by hand. The bridge never cleans up: a deleted guest, disk or storage
  keeps its retained state and discovery topics until you clear them (empty
  retained message on each topic).

## License

MIT — see [LICENSE](LICENSE).
