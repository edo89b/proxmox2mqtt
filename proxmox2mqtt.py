#!/usr/bin/env python3
"""Publish Proxmox VE guest and node metrics to MQTT (Home Assistant discovery).

Polls the PVE API with an API token and exposes, per VM/CT: run state, CPU,
memory, disk, network throughput and uptime; plus node-level CPU, memory, disk,
load and uptime. Network counters are cumulative, so throughput is derived from
the delta between polls.

Physical disks are polled on their own, slower schedule (DISK_POLL_INTERVAL):
SMART data is read through the PVE API, which returns a free-form text block for
NVMe drives and a structured attribute list for ATA ones, so the two are parsed
separately and normalised onto shared keys where they overlap.

If a Proxmox Backup Server is configured (PBS_*), it also reports the last backup
time and verification state per guest.
"""
import datetime
import json
import os
import re
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
MQTT_CONNECT_WAIT = 300  # seconds waited for the first connection before going on
MQTT_USER = os.environ.get("MQTT_USER", "proxmox2mqtt")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").rstrip("/")
# Name the node carries in topics and entity ids. Defaults to the PVE node name,
# but is separate from it: PVE_NODE must match the API, this one is cosmetic.
NODE_ID = os.environ.get("NODE_ID", PVE_NODE)
BASE = os.environ.get("STATE_PREFIX", f"proxmox2mqtt/{NODE_ID}").rstrip("/")

# Topic layout, one level per kind of thing, no name repeated inside a branch:
#   <BASE>/availability
#   <BASE>/node/<key>
#   <BASE>/guest/<vmid>/<key>
#   <BASE>/disk/<dev>/<key>
#   <BASE>/storage/<id>/<key>
TOPIC_NODE = f"{BASE}/node"
TOPIC_GUEST = f"{BASE}/guest"
TOPIC_DISK = f"{BASE}/disk"
TOPIC_STORAGE = f"{BASE}/storage"
POLL = int(os.environ.get("POLL_INTERVAL", "15"))
# Disks change slowly and smartctl spins up sleeping drives: poll them rarely.
DISK_POLL = int(os.environ.get("DISK_POLL_INTERVAL", "600"))

AVAIL = f"{BASE}/availability"

# Last availability we asserted on the broker. Kept at module level so on_connect()
# can re-assert it after a reconnection (see publish_avail below).
_avail = {"state": None}


def publish_avail(client, state):
    """Publish the retained availability, only when it actually changes.

    Returns True if the value changed, so the caller can log the transition.

    The last asserted value is remembered because the LWT is retained: when the
    connection drops (broker restart, network blip) the broker publishes a
    retained "offline". paho reconnects on its own, but without this bookkeeping
    nothing would ever overwrite that retained "offline" — the bridge would keep
    streaming guest and node metrics while every consumer saw it as down.
    """
    if _avail["state"] == state:
        return False
    _avail["state"] = state
    client.publish(AVAIL, state, qos=1, retain=True)
    return True


def on_connect(client, userdata, flags, rc, properties=None):
    """Re-assert the current availability on every successful (re)connection."""
    if _avail["state"] is not None:
        client.publish(AVAIL, _avail["state"], qos=1, retain=True)
        log(f"[mqtt] (re)connected -> re-asserted {_avail['state']}")

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


def pve_get(path, params=None, timeout=15):
    r = pve.get(f"https://{PVE_HOST}/api2/json{path}", params=params, timeout=timeout)
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


# Shared by every disk, whatever the bus.
DISK_SENSORS = {
    "health":          ("Health", None, None, "mdi:heart-pulse"),
    "temperature":     ("Temperature", "\u00b0C", "temperature", None),
    "power_on_hours":  ("Power on hours", "h", None, "mdi:clock-outline"),
    "power_on_years":  ("Power on years", "y", None, "mdi:calendar-clock"),
    "power_cycles":    ("Power cycles", None, None, "mdi:restart"),
    "size_gb":         ("Size", "GB", None, "mdi:harddisk"),
    "wearout":         ("Wearout left", "%", None, "mdi:battery-50"),
    "used":            ("Used by", None, None, "mdi:information-outline"),
    "storage":         ("Storage", None, None, "mdi:database"),
    "wwn":             ("WWN", None, None, "mdi:identifier"),
    "model":           ("Model", None, None, "mdi:harddisk"),
    "serial":          ("Serial", None, None, "mdi:identifier"),
}
# NVMe health log (0x02).
DISK_SENSORS_NVME = {
    "critical_warning":  ("Critical warning", None, None, "mdi:alert"),
    "available_spare":   ("Available spare", "%", None, "mdi:shield-check"),
    "spare_threshold":   ("Spare threshold", "%", None, "mdi:shield-alert"),
    "percentage_used":   ("Percentage used", "%", None, "mdi:gauge"),
    "data_read_tb":      ("Data read", "TB", None, "mdi:download"),
    "data_written_tb":   ("Data written", "TB", None, "mdi:upload"),
    "unsafe_shutdowns":  ("Unsafe shutdowns", None, None, "mdi:power-plug-off"),
    "media_errors":      ("Media errors", None, None, "mdi:alert-circle"),
    "error_log_entries": ("Error log entries", None, None, "mdi:format-list-numbered"),
    "warn_temp_time":    ("Warning temp time", "min", None, "mdi:thermometer-alert"),
    "crit_temp_time":    ("Critical temp time", "min", None, "mdi:thermometer-alert"),
}
# ATA SMART attributes worth surfacing, by attribute name.
ATA_ATTRS = {
    "Reallocated_Sector_Ct":   ("reallocated_sectors", "Reallocated sectors", "mdi:alert-circle"),
    "Current_Pending_Sector":  ("pending_sectors", "Pending sectors", "mdi:alert-circle"),
    "Offline_Uncorrectable":   ("offline_uncorrectable", "Offline uncorrectable", "mdi:alert-circle"),
    "Reallocated_Event_Count": ("reallocated_events", "Reallocated events", "mdi:alert-circle"),
    "UDMA_CRC_Error_Count":    ("udma_crc_errors", "UDMA CRC errors", "mdi:lan-disconnect"),
    "Raw_Read_Error_Rate":     ("raw_read_error_rate", "Raw read error rate", "mdi:book-open-variant"),
    "Seek_Error_Rate":         ("seek_error_rate", "Seek error rate", "mdi:magnify"),
    "Spin_Retry_Count":        ("spin_retry_count", "Spin retry count", "mdi:rotate-right"),
    "Start_Stop_Count":        ("start_stop_count", "Start/stop count", "mdi:power"),
    "Load_Cycle_Count":        ("load_cycle_count", "Load cycle count", "mdi:autorenew"),
    "Power-Off_Retract_Count": ("power_off_retract", "Power-off retract count", "mdi:power-plug-off"),
    "Helium_Level":            ("helium_level", "Helium level", "mdi:balloon"),
    "Throughput_Performance":  ("throughput_performance", "Throughput performance", "mdi:speedometer"),
    "Seek_Time_Performance":   ("seek_time_performance", "Seek time performance", "mdi:timer-outline"),
    "Spin_Up_Time":            ("spin_up_time", "Spin up time", "mdi:rotate-right"),
}
DISK_SENSORS_ATA = dict(
    [(k, (lab, None, None, icon)) for k, lab, icon in
     ((v[0], v[1], v[2]) for v in ATA_ATTRS.values())]
    + [("temp_min", ("Temperature min", "\u00b0C", "temperature", None)),
       ("temp_max", ("Temperature max", "\u00b0C", "temperature", None)),
       ("failing_now", ("Attributes failing", None, None, "mdi:alert-decagram"))]
)

# Sensors whose value is text, not a number. Home Assistant refuses a non-numeric
# state on an entity declared with state_class "measurement" ("Value error while
# updating state of sensor..."), so these must never get one. Everything else is a
# number, with or without a unit (SMART counters, load, guests running...).
TEXT_KEYS = {
    "health", "used", "storage", "wwn", "model", "serial",
    "type", "active", "content", "server",
    "zfs_pool", "zfs_state", "zfs_layout", "zfs_scrub_info",
    "backup_verify",
}

HOURS_PER_YEAR = 8766  # 365.25 days, so leap years do not skew the age


def _num(text):
    """First number in a smartctl value, ignoring thousands separators and units."""
    m = re.search(r"-?[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def parse_nvme(text):
    """NVMe health log: 'Field: value' lines, values carrying units and commas."""
    raw = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        raw[k.strip()] = v.strip()

    def g(field):
        return _num(raw.get(field))

    out = {
        "critical_warning":  raw.get("Critical Warning"),
        "temperature":       g("Temperature"),
        "available_spare":   g("Available Spare"),
        "spare_threshold":   g("Available Spare Threshold"),
        "percentage_used":   g("Percentage Used"),
        "power_on_hours":    g("Power On Hours"),
        "power_cycles":      g("Power Cycles"),
        "unsafe_shutdowns":  g("Unsafe Shutdowns"),
        "media_errors":      g("Media and Data Integrity Errors"),
        "error_log_entries": g("Error Information Log Entries"),
        "warn_temp_time":    g("Warning  Comp. Temperature Time"),
        "crit_temp_time":    g("Critical Comp. Temperature Time"),
    }
    # "Data Units Read: 86,726,177 [44.4 TB]" -> take the human-readable TB figure.
    for field, key in (("Data Units Read", "data_read_tb"), ("Data Units Written", "data_written_tb")):
        m = re.search(r"\[([\d.]+)\s*([KMGTP]B)\]", raw.get(field, ""))
        if m:
            val, unit = float(m.group(1)), m.group(2)
            out[key] = round(val * {"KB": 1e-9, "MB": 1e-6, "GB": 1e-3, "TB": 1, "PB": 1e3}[unit], 2)
    return out


def parse_ata(attributes):
    """ATA SMART: one row per attribute, the useful figure being the raw value."""
    by_name = {a.get("name"): a for a in attributes or []}
    out = {}
    for name, (key, _label, _icon) in ATA_ATTRS.items():
        if name in by_name:
            out[key] = _num(by_name[name].get("raw"))
    if "Power_On_Hours" in by_name:
        out["power_on_hours"] = _num(by_name["Power_On_Hours"].get("raw"))
    if "Power_Cycle_Count" in by_name:
        out["power_cycles"] = _num(by_name["Power_Cycle_Count"].get("raw"))
    temp = by_name.get("Temperature_Celsius") or by_name.get("Airflow_Temperature_Cel")
    if temp:
        out["temperature"] = _num(temp.get("raw"))
        # Raw often reads "43 (Min/Max 18/48)".
        m = re.search(r"Min/Max\s+(-?\d+)/(-?\d+)", temp.get("raw", ""))
        if m:
            out["temp_min"], out["temp_max"] = int(m.group(1)), int(m.group(2))
    # smartctl marks a failing attribute with anything other than "-".
    out["failing_now"] = sum(1 for a in attributes or [] if str(a.get("fail", "-")).strip() not in ("-", ""))
    return out


def disk_id(devpath):
    """/dev/nvme0n1 -> nvme0n1, usable in a topic and a discovery object_id."""
    return devpath.rsplit("/", 1)[-1]


def collect_disks():
    """Inventory plus SMART for every physical disk. A disk that fails to report
    SMART is still published with its inventory, so it does not vanish."""
    out = []
    for d in pve_get(f"/nodes/{PVE_NODE}/disks/list"):
        dev = d.get("devpath")
        if not dev:
            continue
        size = d.get("size") or 0
        info = {
            "devpath": dev,
            "id": disk_id(dev),
            "kind": d.get("type", "unknown"),
            "model": d.get("model"),
            "serial": d.get("serial"),
            "size_gb": round(size / 1e9, 1) if size else None,
            "health": d.get("health"),
            "used": d.get("used"),
            "wwn": d.get("wwn"),
            # kept only to match the disk against ZFS leaf devices, not published
            "by_id_link": d.get("by_id_link"),
            "wearout": d.get("wearout") if isinstance(d.get("wearout"), (int, float)) else None,
        }
        try:
            smart = pve_get(f"/nodes/{PVE_NODE}/disks/smart", params={"disk": dev}, timeout=45)
            if smart.get("type") == "text":
                info.update(parse_nvme(smart.get("text")))
            else:
                info.update(parse_ata(smart.get("attributes")))
            if smart.get("health"):
                info["health"] = smart["health"]
        except Exception as e:
            log(f"[smart] {dev}: {e}")
        hours = info.get("power_on_hours")
        if hours:
            info["power_on_years"] = round(hours / HOURS_PER_YEAR, 2)
        out.append(info)
    return out


# Every storage, whatever the backend.
STORAGE_SENSORS = {
    "type":      ("Type", None, None, "mdi:database"),
    "active":    ("Active", None, None, "mdi:check-network"),
    "total_gb":  ("Total", "GB", None, "mdi:database"),
    "used_gb":   ("Used", "GB", None, "mdi:database-check"),
    "avail_gb":  ("Available", "GB", None, "mdi:database-plus"),
    "used_pct":  ("Used", "%", None, "mdi:gauge"),
    "content":   ("Content", None, None, "mdi:file-tree"),
    "server":    ("Server", None, None, "mdi:server-network"),
}
# Only for zfspool storages, read from the underlying pool.
STORAGE_SENSORS_ZFS = {
    "zfs_pool":       ("ZFS pool", None, None, "mdi:database"),
    "zfs_state":      ("ZFS state", None, None, "mdi:heart-pulse"),
    "zfs_size_gb":    ("ZFS size", "GB", None, "mdi:database"),
    "zfs_alloc_gb":   ("ZFS allocated", "GB", None, "mdi:database-check"),
    "zfs_free_gb":    ("ZFS free", "GB", None, "mdi:database-plus"),
    "zfs_frag":       ("ZFS fragmentation", "%", None, "mdi:puzzle"),
    "zfs_dedup":      ("ZFS dedup ratio", None, None, "mdi:content-duplicate"),
    "zfs_errors":     ("ZFS errors", None, None, "mdi:alert-circle"),
    "zfs_layout":     ("ZFS layout", None, None, "mdi:vector-arrange-below"),
    "zfs_devices":    ("ZFS devices", None, None, "mdi:harddisk"),
    "zfs_degraded":   ("ZFS devices not online", None, None, "mdi:alert-decagram"),
    "zfs_last_scrub": ("ZFS last scrub", None, "timestamp", "mdi:broom"),
    "zfs_scrub_info": ("ZFS scrub result", None, None, "mdi:broom"),
}

_SCRUB_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_scan(scan):
    """zpool's scan line: '... with 0 errors on Sun Aug  9 00:45:50 2026'."""
    m = re.search(r"on\s+\w{3}\s+(\w{3})\s+(\d+)\s+(\d+):(\d+):(\d+)\s+(\d{4})", scan or "")
    if not m:
        return None
    mon, day, hh, mm, ss, year = m.groups()
    if mon not in _SCRUB_MONTHS:
        return None
    return datetime.datetime(int(year), _SCRUB_MONTHS[mon], int(day),
                             int(hh), int(mm), int(ss)).astimezone().isoformat()


def zfs_leaves(node, out=None, depth=0):
    """Flatten a pool tree to (leaf device, state), skipping vdev containers."""
    out = [] if out is None else out
    for c in node or []:
        kids = c.get("children")
        if kids:
            zfs_leaves(kids, out, depth + 1)
        elif depth:  # depth 0 is the pool itself when it has no children
            out.append((c.get("name", ""), c.get("state", "")))
    return out


def zfs_layout(children):
    """raidz1 / mirror / stripe, read from the vdev level under the pool."""
    pool = (children or [{}])[0]
    vdevs = [c.get("name", "") for c in pool.get("children") or []]
    kinds = {re.sub(r"-\d+$", "", v) for v in vdevs if not v.startswith("/dev")}
    if kinds:
        return "+".join(sorted(kinds))
    return "stripe" if len(vdevs) > 1 else "single"


def match_disk(leaf, disks):
    """Map a ZFS leaf device back to the physical disk it lives on."""
    name = leaf.rsplit("/", 1)[-1]
    for d in disks:
        wwn = (d.get("wwn") or "").replace("eui.", "")
        if wwn and wwn in name:
            return d
        base = d["devpath"].rsplit("/", 1)[-1]
        if re.fullmatch(re.escape(base) + r"p?\d*", name):
            return d
        byid = (d.get("by_id_link") or "").rsplit("/", 1)[-1]
        if byid and name.startswith(byid):
            return d
    return None


def collect_storages(disks):
    """Storages with their capacity, plus ZFS pool health and member disks.

    Also returns {disk id: storage id} so each disk can say where it belongs."""
    status = pve_get(f"/nodes/{PVE_NODE}/storage")
    cfg = {c["storage"]: c for c in pve_get("/storage")}
    pools = {p["name"]: p for p in pve_get(f"/nodes/{PVE_NODE}/disks/zfs")}

    out, disk_of = [], {}
    for st in status:
        sid = st.get("storage")
        c = cfg.get(sid, {})
        total, used, avail = st.get("total") or 0, st.get("used") or 0, st.get("avail") or 0
        info = {
            "id": sid,
            "type": st.get("type"),
            "active": "ON" if st.get("active") else "OFF",
            "total_gb": round(total / 1e9, 1) if total else 0,
            "used_gb": round(used / 1e9, 1) if used else 0,
            "avail_gb": round(avail / 1e9, 1) if avail else 0,
            "used_pct": round(used / total * 100, 1) if total else 0,
            # Sorted: the API returns the items in a different order at every poll,
            # so an unsorted value would look like a state change every time.
            "content": ",".join(sorted((st.get("content") or "").split(","))),
            "server": c.get("server"),
            "disks": [],
        }
        # zfspool storages carry "pool" (possibly a dataset: rpool/data -> rpool)
        pool_name = (c.get("pool") or "").split("/")[0]
        p = pools.get(pool_name)
        if p:
            info["zfs_pool"] = p["name"]
            info["zfs_state"] = p.get("health")
            info["zfs_size_gb"] = round((p.get("size") or 0) / 1e9, 1)
            info["zfs_alloc_gb"] = round((p.get("alloc") or 0) / 1e9, 1)
            info["zfs_free_gb"] = round((p.get("free") or 0) / 1e9, 1)
            info["zfs_frag"] = p.get("frag")
            info["zfs_dedup"] = p.get("dedup")
            try:
                detail = pve_get(f"/nodes/{PVE_NODE}/disks/zfs/{pool_name}")
                info["zfs_errors"] = detail.get("errors")
                info["zfs_layout"] = zfs_layout(detail.get("children"))
                leaves = zfs_leaves(detail.get("children"))
                info["zfs_devices"] = len(leaves)
                info["zfs_degraded"] = sum(1 for _n, state in leaves if state != "ONLINE")
                scrub = parse_scan(detail.get("scan"))
                if scrub:
                    info["zfs_last_scrub"] = scrub
                info["zfs_scrub_info"] = detail.get("scan")
                for leaf, _state in leaves:
                    d = match_disk(leaf, disks)
                    if d:
                        info["disks"].append(d["id"])
                        disk_of[d["id"]] = sid
            except Exception as e:
                log(f"[zfs] {pool_name}: {e}")
        out.append(info)
    return out, disk_of


def publish_storage_discovery(client, storages):
    for s in storages:
        dev_id = f"{NODE_ID}_storage_{s['id']}".replace("-", "_")
        base = f"{TOPIC_STORAGE}/{s['id']}"
        dev = device(dev_id, f"Storage {s['id']}", (s.get("type") or "storage").upper())
        keys = dict(STORAGE_SENSORS)
        if "zfs_pool" in s:
            keys.update(STORAGE_SENSORS_ZFS)
        for key, (label, unit, dclass, icon) in keys.items():
            client.publish(*sensor_config(dev, dev_id, key, label, unit, dclass, icon,
                                          topic_base=base), qos=1, retain=True)


def publish_storages(client, storages):
    for s in storages:
        base = f"{TOPIC_STORAGE}/{s['id']}"
        for key, value in s.items():
            if key == "id" or value is None:
                continue
            if key == "disks":
                value = ",".join(value)
            client.publish(f"{base}/{key}", value, qos=0, retain=True)


def publish_disk_discovery(client, disks):
    for d in disks:
        dev_id = f"{NODE_ID}_disk_{d['id']}"
        base = f"{TOPIC_DISK}/{d['id']}"
        dev = device(dev_id, f"Disk {d['id']}", d["kind"].upper())
        extra = DISK_SENSORS_NVME if d["kind"] == "nvme" else DISK_SENSORS_ATA
        for key, (label, unit, dclass, icon) in list(DISK_SENSORS.items()) + list(extra.items()):
            client.publish(*sensor_config(dev, dev_id, key, label, unit, dclass, icon,
                                          topic_base=base), qos=1, retain=True)


def publish_disks(client, disks):
    for d in disks:
        base = f"{TOPIC_DISK}/{d['id']}"
        for key, value in d.items():
            if key in ("devpath", "id", "kind", "by_id_link") or value is None:
                continue
            client.publish(f"{base}/{key}", value, qos=0, retain=True)


def device(dev_id, name, model):
    return {"identifiers": [dev_id], "name": name, "manufacturer": "Proxmox", "model": model}


def sensor_config(dev, dev_id, key, label, unit=None, dclass=None, icon=None,
                  component="sensor", topic_base=None):
    obj = f"{dev_id}_{key}"
    payload = {
        "name": label,
        "state_topic": f"{topic_base}/{key}",
        "unique_id": obj,
        "object_id": obj,
        "device": dev,
        "availability_topic": AVAIL,
    }
    if component == "sensor" and dclass != "timestamp" and key not in TEXT_KEYS:
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
        dev_id = f"{NODE_ID}_guest_{g['vmid']}"
        base = f"{TOPIC_GUEST}/{g['vmid']}"
        dev = device(dev_id, f"{g['name']} ({g['vmid']})", g["kind"].upper())
        # run state
        topic, payload = sensor_config(dev, dev_id, "status", "Running",
                                        dclass="running", component="binary_sensor",
                                        topic_base=base)
        payload = json.loads(payload)
        payload.update({"payload_on": "running", "payload_off": "stopped"})
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
        for key, (label, unit, dclass, icon) in GUEST_SENSORS.items():
            client.publish(*sensor_config(dev, dev_id, key, label, unit, dclass, icon,
                                          topic_base=base), qos=1, retain=True)
        if pbs:
            client.publish(*sensor_config(dev, dev_id, "last_backup", "Last backup",
                                          dclass="timestamp", icon="mdi:backup-restore",
                                          topic_base=base), qos=1, retain=True)
            client.publish(*sensor_config(dev, dev_id, "backup_verify", "Backup verify",
                                          icon="mdi:check-decagram", topic_base=base), qos=1, retain=True)
    node_dev = device(f"{NODE_ID}_node", f"Proxmox {NODE_ID}", "PVE node")
    for key, (label, unit, dclass, icon) in NODE_SENSORS.items():
        client.publish(*sensor_config(node_dev, f"{NODE_ID}_node", key, label, unit, dclass, icon,
                                      topic_base=TOPIC_NODE), qos=1, retain=True)


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
    client.on_connect = on_connect
    # connect_async + retry: a broker that is not up (or not yet resolvable) at start
    # used to kill the process, and Docker restarted it in a loop until the broker came
    # back. Now paho keeps retrying, including the very first connection.
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    # Wait for the first connection before publishing: QoS 0 messages sent while
    # disconnected would be dropped silently.
    for _ in range(MQTT_CONNECT_WAIT):
        if client.is_connected():
            break
        time.sleep(1)
    log(f"[mqtt] {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER}; backups={'on' if pbs else 'off'}")

    prev = {}
    known = set()
    disks_known = set()
    storages_known = set()
    next_disk_poll = 0.0

    while True:
        try:
            guests = collect_guests()
            node = pve_get(f"/nodes/{PVE_NODE}/status")
        except Exception as e:
            if publish_avail(client, "offline"):
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

        if publish_avail(client, "online"):
            log("[pve] reachable -> online")

        now = time.time()
        for g in guests:
            vmid = g["vmid"]
            base = f"{TOPIC_GUEST}/{vmid}"
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

        nb = TOPIC_NODE
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

        if time.time() >= next_disk_poll:
            next_disk_poll = time.time() + DISK_POLL
            try:
                disks = collect_disks()
                storages, disk_of = collect_storages(disks)
                for d in disks:
                    d["storage"] = disk_of.get(d["id"], "-")
                ids = frozenset(d["id"] for d in disks)
                if ids != disks_known:
                    publish_disk_discovery(client, disks)
                    disks_known = ids
                    log(f"[discovery] {len(disks)} disks")
                sids = frozenset(s["id"] for s in storages)
                if sids != storages_known:
                    publish_storage_discovery(client, storages)
                    storages_known = sids
                    log(f"[discovery] {len(storages)} storages")
                publish_disks(client, disks)
                publish_storages(client, storages)
            except Exception as e:
                log(f"[disks] {e}")

        time.sleep(POLL)


if __name__ == "__main__":
    main()
