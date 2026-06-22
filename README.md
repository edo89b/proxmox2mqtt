# proxmox2mqtt

Small bridge that polls the Proxmox VE API and publishes guest and node metrics
to MQTT using the Home Assistant discovery format. Works with Home Assistant,
OpenHAB or anything that reads MQTT.

Per VM/CT it reports run state, CPU, memory, disk, network throughput and uptime.
For the node: CPU, memory, root filesystem usage, 1-minute load, uptime and the
number of running guests. Network throughput is computed from the delta of the
cumulative counters between polls.

## Setup

1. On Proxmox create a read-only API token:

   ```
   pveum user add monitoring@pve
   pveum acl modify / --users monitoring@pve --roles PVEAuditor
   pveum user token add monitoring@pve mqtt --privsep 0
   ```

2. Copy `.env.example` to `.env` and fill in the PVE host/node/token and your
   MQTT broker.

3. `docker compose up -d --build`

## Configuration

| Variable | Description |
|----------|-------------|
| `PVE_HOST` | host:port of the PVE API (e.g. `10.0.0.10:8006`) |
| `PVE_NODE` | node name |
| `PVE_TOKEN` | `user@realm!tokenid=secret` |
| `PVE_VERIFY_SSL` | verify the API certificate (default `false`) |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASS` | broker connection |
| `STATE_PREFIX` | base topic, default `proxmox/<node>` |
| `POLL_INTERVAL` | seconds between polls (default 15) |

## Notes

- Disk usage for QEMU guests is only reported when the guest agent exposes it;
  otherwise it stays at 0. Containers report it directly.
- The token only needs `PVEAuditor` (read-only).
