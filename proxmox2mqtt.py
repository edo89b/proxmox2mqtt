#!/usr/bin/env python3
"""Publish Proxmox VE guest and node metrics to MQTT (Home Assistant discovery).

Polls the PVE API with an API token and exposes, per VM/CT: run state, CPU,
memory, disk, network throughput and uptime; plus node-level CPU, memory, disk,
load and uptime. Network counters are cumulative, so throughput is derived from
the delta between polls.

If a Proxmox Backup Server is configured (PBS_*), it also reports the last backup
time and verification state per guest.
"""
import datetime
import json
import os
import time

import requests
import paho.mqtt.client as mqtt

PVE_HOST = os.environ.get("PVE_HOST", "127.0.0.1:8006")
PVE_NODE = os.environ.get("PVE_NODE", "pve")
PVE_TOKEN = os.environ.get("PVE_TOKEN", "")          # user@realm!tokenid=secret
VERIFY_SSL = os.environ.get("PVE_VERIFY_SSL", "false").lower() == "true"

# Optional: Proxmox Backup Server, for last-backup info
PBS_HOST = os.environ.get("PBS_HOST", "")            # host:port, empty disables
PBS_TOKEN = os.environ.get("PBS_TOKEN", "")          # user@realm!tokenid:secret
PBS_DATASTORE = os.environ.get("PBS_DATASTORE", "")

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "proxmox2mqtt")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").rstrip("/")
PREFIX = os.environ.get("STATE_PREFIX", f"proxmox/{PVE_NODE}").rstrip("/")
DEVICE_PREFIX = os.environ.get("DEVICE_PREFIX", f"pve_{PVE_NODE}")
POLL = int(os.environ.get("POLL_INTERVAL", "15"))

AVAIL = f"{PREFIX}/availability"

if not VERIFY_SSL:
    requests.packages.urllib3.disable_warnings()

pve = requests.Session()
pve.headers["Authorization"] = f"PVEAPIToken={PVE_TOKEN}"
pve.verify = VERIFY_SSL

pbs = None
if PBS_HOST and PBS_TOKEN and PBS_DATASTORE:
    pbs = requests.Session()
    pbs.headers["Authorization"] = f"PBSAPIToken={PBS_TOKEN}"
    pbs.verify = VERIFY_SSL


def log(*a):
    print(*a, flush=True)


def pve_get(path):
    r = pve.get(f"https://{PVE_HOST}/api2/json{path}", timeout=15)
    r.raise_for_status()
    return r.json()["data"]


# key -> (label, unit, device_class, icon)
GUEST_SENSORS = {
    "cpu":      ("CPU", "%", None, "mdi:cpu-64-bit"),
    "mem":      ("Memory", "%", None, "mdi:memory"),
    "mem_used": ("Memory used", "MiB", None, "mdi:memory"),
    "disk":     ("Disk", "%", None, "mdi:harddisk"),
    "net_in":   ("Net in", "KiB/s", None, "mdi:download-network"),
    "net_out":  ("Net out", "KiB/s", None, "mdi:upload-network"),
    "uptime":   ("Uptime", "s", "duration", None),
}
NODE_SENSORS = {
    "cpu":      ("CPU", "%", None, "mdi:cpu-64-bit"),
    "mem":      ("Memory", "%", None, "mdi:memory"),
    "mem_used": ("Memory used", "MiB", None, "mdi:memory"),
    "disk":     ("Root FS", "%", None, "mdi:harddisk"),
    "load1":    ("Load 1m", None, None, "mdi:gauge"),
    "uptime":   ("Uptime", "s", "duration", None),
    "guests_running": ("Guests running", None, None, "mdi:server"),
}


def device(dev_id, name, model):
    return {"identifiers": [dev_id], "name": name, "manufacturer": "Proxmox", "model": model}


def sensor_config(dev, dev_id, key, label, unit=None, dclass=None, icon=None, component="sensor"):
    obj = f"{dev_id}_{key}"
    payload = {
        "name": label,
        "state_topic": f"{PREFIX}/{dev_id}/{key}",
        "unique_id": obj,
        "object_id": obj,
        "device": dev,
        "availability_topic": AVAIL,
    }
    if component == "sensor" and dclass != "timestamp":
        payload["state_class"] = "measurement"
    if unit:
        payload["unit_of_measurement"] = unit
    if dclass:
        payload["device_class"] = dclass
    if icon:
        payload["icon"] = icon
    return f"{DISCOVERY_PREFIX}/{component}/{dev_id}/{key}/config", json.dumps(payload)


def publish_discovery(client, guests):
    for g in guests:
        dev_id = f"{DEVICE_PREFIX}_{g['vmid']}"
        dev = device(dev_id, f"{g['name']} ({g['vmid']})", g["kind"].upper())
        # run state
        topic, payload = sensor_config(dev, dev_id, "status", "Running",
                                        dclass="running", component="binary_sensor")
        payload = json.loads(payload)
        payload.update({"payload_on": "running", "payload_off": "stopped"})
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
        for key, (label, unit, dclass, icon) in GUEST_SENSORS.items():
            client.publish(*sensor_config(dev, dev_id, key, label, unit, dclass, icon), qos=1, retain=True)
        if pbs:
            client.publish(*sensor_config(dev, dev_id, "last_backup", "Last backup",
                                          dclass="timestamp", icon="mdi:backup-restore"), qos=1, retain=True)
            client.publish(*sensor_config(dev, dev_id, "backup_verify", "Backup verify",
                                          icon="mdi:check-decagram"), qos=1, retain=True)
    node_dev = device(DEVICE_PREFIX, f"Proxmox {PVE_NODE}", "PVE node")
    for key, (label, unit, dclass, icon) in NODE_SENSORS.items():
        client.publish(*sensor_config(node_dev, DEVICE_PREFIX, key, label, unit, dclass, icon), qos=1, retain=True)


def collect_guests():
    out = []
    for kind, path in (("vm", "qemu"), ("ct", "lxc")):
        for g in pve_get(f"/nodes/{PVE_NODE}/{path}"):
            g["kind"] = kind
            out.append(g)
    return out


def collect_backups():
    """{vmid: (last_backup_time, verify_state)} from PBS, or {} if not configured."""
    if not pbs:
        return {}
    r = pbs.get(f"https://{PBS_HOST}/api2/json/admin/datastore/{PBS_DATASTORE}/snapshots", timeout=30)
    r.raise_for_status()
    out = {}
    for s in r.json()["data"]:
        try:
            vmid = int(s["backup-id"])
        except (KeyError, ValueError):
            continue
        t = s.get("backup-time", 0)
        if vmid not in out or t > out[vmid][0]:
            out[vmid] = (t, (s.get("verification") or {}).get("state", "none"))
    return out


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="proxmox2mqtt", clean_session=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL, "offline", qos=1, retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    log(f"[mqtt] {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER}; backups={'on' if pbs else 'off'}")

    prev = {}
    known = set()
    online = None

    while True:
        try:
            guests = collect_guests()
            node = pve_get(f"/nodes/{PVE_NODE}/status")
        except Exception as e:
            if online is not False:
                client.publish(AVAIL, "offline", qos=1, retain=True)
                online = False
                log(f"[pve] unreachable: {e}")
            time.sleep(POLL)
            continue

        try:
            backups = collect_backups()
        except Exception as e:
            backups = {}
            log(f"[pbs] {e}")

        ids = frozenset(g["vmid"] for g in guests)
        if ids != known:
            publish_discovery(client, guests)
            known = ids
            log(f"[discovery] {len(guests)} guests + node")

        if online is not True:
            client.publish(AVAIL, "online", qos=1, retain=True)
            online = True

        now = time.time()
        for g in guests:
            vmid = g["vmid"]
            base = f"{PREFIX}/{DEVICE_PREFIX}_{vmid}"
            client.publish(f"{base}/status", g.get("status", "unknown"), qos=0, retain=True)
            client.publish(f"{base}/cpu", round(g.get("cpu", 0) * 100, 1), qos=0, retain=True)
            maxmem, mem = g.get("maxmem") or 0, g.get("mem") or 0
            client.publish(f"{base}/mem", round(mem / maxmem * 100, 1) if maxmem else 0, qos=0, retain=True)
            client.publish(f"{base}/mem_used", round(mem / 1048576), qos=0, retain=True)
            maxdisk, disk = g.get("maxdisk") or 0, g.get("disk") or 0
            client.publish(f"{base}/disk", round(disk / maxdisk * 100, 1) if maxdisk else 0, qos=0, retain=True)
            client.publish(f"{base}/uptime", g.get("uptime", 0), qos=0, retain=True)

            netin, netout = g.get("netin", 0), g.get("netout", 0)
            if vmid in prev:
                dt = now - prev[vmid][0]
                if dt > 0:
                    client.publish(f"{base}/net_in", round(max(0, netin - prev[vmid][1]) / dt / 1024, 1), qos=0, retain=True)
                    client.publish(f"{base}/net_out", round(max(0, netout - prev[vmid][2]) / dt / 1024, 1), qos=0, retain=True)
            prev[vmid] = (now, netin, netout)

            if vmid in backups:
                t, state = backups[vmid]
                iso = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()
                client.publish(f"{base}/last_backup", iso, qos=0, retain=True)
                client.publish(f"{base}/backup_verify", state, qos=0, retain=True)

        nb = f"{PREFIX}/{DEVICE_PREFIX}"
        client.publish(f"{nb}/cpu", round(node.get("cpu", 0) * 100, 1), qos=0, retain=True)
        mem = node.get("memory", {})
        if mem.get("total"):
            client.publish(f"{nb}/mem", round(mem["used"] / mem["total"] * 100, 1), qos=0, retain=True)
            client.publish(f"{nb}/mem_used", round(mem["used"] / 1048576), qos=0, retain=True)
        rootfs = node.get("rootfs", {})
        if rootfs.get("total"):
            client.publish(f"{nb}/disk", round(rootfs["used"] / rootfs["total"] * 100, 1), qos=0, retain=True)
        client.publish(f"{nb}/load1", node.get("loadavg", ["0"])[0], qos=0, retain=True)
        client.publish(f"{nb}/uptime", node.get("uptime", 0), qos=0, retain=True)
        client.publish(f"{nb}/guests_running", sum(1 for g in guests if g.get("status") == "running"), qos=0, retain=True)

        time.sleep(POLL)


if __name__ == "__main__":
    main()
