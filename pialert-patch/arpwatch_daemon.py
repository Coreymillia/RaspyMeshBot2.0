#!/usr/bin/env python3
"""arpwatch_daemon.py v2 — Enhanced passive ARP monitor for Pi.Alert

Writes /tmp/arp_status.json with a full network health snapshot:
  iface, gateway_ip, gateway_mac_current, gateway_mac_expected
  status          — "ok" | "warning" | "anomaly"
  last_arp_ts     — timestamp of the most recent ARP packet seen
  arp_rate        — packets per minute (rolling 60 s window)
  duplicate_arp_count — lifetime count of IP→MAC collisions since boot
  gateway_mac_changes — lifetime count of gateway MAC changes since boot
  last_anomaly / last_anomaly_ts
  top_talkers     — [{ip, name, count}, …]  top 5 by packet volume
  last_events     — [{ip, name, mac, type, ts}, …]  rolling buffer of last 5 ARPs
  alerts          — [{type, ip, old_mac, new_mac, time}, …]  last 10 anomalies

Alert types:
  gateway_mac_changed  — gateway IP answered with a new MAC
  arp_spoof            — same IP flipped between two MACs within SPOOF_WINDOW_S
  mac_change           — any other device changed its MAC for a known IP

Status thresholds:
  anomaly  — gateway MAC changed OR current ≠ expected OR rate > ANOMALY_RATE (2000 pkt/min)
  warning  — rate > WARN_RATE (500 pkt/min) OR any duplicate_arp_count > 0
  ok       — everything else

Reset: write /tmp/arpwatch_reset_flag (any user, no signal permission needed).
       Daemon picks it up within WRITE_INTERVAL seconds and resets all counters.

Note: Pi.Alert's own ARP scanner generates significant traffic (~50-200 pkt/min bursts).
      Thresholds are set well above that to avoid false positives.

Usage:
  sudo python3 arpwatch_daemon.py [--iface eth0] [--gateway 192.168.0.1]
      [--db /opt/pialert/db/pialert.db] [--out /tmp/arp_status.json]

Dependencies:  pip3 install scapy   (or: apt install python3-scapy)
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

try:
    from scapy.all import ARP, sniff
except ImportError:
    sys.exit("scapy not installed.  Run: sudo apt install python3-scapy")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MAX_ALERTS      = 10    # anomaly alerts kept in JSON
MAX_EVENTS      = 5     # last raw ARP events kept
MAX_TALKERS     = 5     # top talkers shown
RATE_WINDOW_S   = 60    # rolling window for ARP rate (seconds = packets/min)
SPOOF_WINDOW_S  = 120   # back-and-forth MAC flip within this window → arp_spoof
TALKER_PRUNE    = 200   # prune talker dict when it exceeds this key count
WARN_RATE       = 500   # pkt/min → warning  (Pi.Alert's own scanner easily hits 100+)
ANOMALY_RATE    = 2000  # pkt/min → anomaly  (real ARP flood is much higher than normal scan)
WRITE_INTERVAL  = 5     # seconds between background status writes

# ---------------------------------------------------------------------------
# Shared state (all protected by _lock)
# ---------------------------------------------------------------------------
_lock       = threading.Lock()
_alerts     = []                       # newest-first anomaly list
_ip_table   = {}                       # ip → {mac, prev_mac, changed_at}
_talkers    = {}                       # ip → lifetime packet count
_events     = deque(maxlen=MAX_EVENTS) # rolling last-N raw ARP events
_rate_win   = deque()                  # monotonic ts of pkts in last RATE_WINDOW_S
_stats = {
    "iface":                "eth0",
    "gateway_ip":           "",
    "gateway_mac_current":  "",
    "gateway_mac_expected": "",
    "status":               "ok",
    "status_reason":        "starting up",
    "last_arp_ts":          "",
    "arp_rate":             0.0,
    "duplicate_arp_count":  0,
    "gateway_mac_changes":  0,
    "last_anomaly":         "none",
    "last_anomaly_ts":      "",
}

_write_flag  = threading.Event()   # signals writer thread
_alert_file  = "/tmp/arp_status.json"
_reset_flag  = "/tmp/arpwatch_reset_flag"  # any user can touch this to trigger a reset
_db_path     = ""                          # set in main() from --db arg

# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def _get_default_gateway():
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "default" and "via" in parts:
                return parts[parts.index("via") + 1]
    except Exception:
        pass
    return None


def _query_expected_mac(gateway_ip, db_path):
    """Return the MAC Pi.Alert has on record for gateway_ip, or None."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        cur = conn.cursor()
        cur.execute(
            "SELECT dev_MAC FROM Devices "
            "WHERE dev_LastIP=? AND dev_Archived=0 LIMIT 1",
            (gateway_ip,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0].lower().replace("-", ":") if row else None
    except Exception as exc:
        print(f"[arpwatch] DB lookup error: {exc}", flush=True)
        return None


def _query_device_name(ip):
    """Return the friendly device name for an IP from Pi.Alert DB, or empty string."""
    if not _db_path or not os.path.exists(_db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True, timeout=3)
        cur = conn.cursor()
        cur.execute(
            "SELECT dev_Name FROM Devices "
            "WHERE dev_LastIP=? AND dev_Archived=0 LIMIT 1",
            (ip,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[0] not in ("(unknown)", "unknown", ""):
            return row[0]
        return ""
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# Atomic file write (called WITHOUT _lock held)
# ---------------------------------------------------------------------------

def _atomic_write(payload_str):
    dir_ = os.path.dirname(os.path.abspath(_alert_file))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(payload_str)
        os.chmod(tmp, 0o644)
        os.replace(tmp, _alert_file)
    except Exception as exc:
        print(f"[arpwatch] Write error: {exc}", flush=True)
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Writer thread — builds JSON snapshot every WRITE_INTERVAL s or on demand
# ---------------------------------------------------------------------------

def _apply_reset():
    """Reset ARP baseline — called from writer thread when flag file is found."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[arpwatch] {now_str}  Baseline reset (flag file)", flush=True)
    with _lock:
        cur = _stats["gateway_mac_current"]
        if cur:
            _stats["gateway_mac_expected"] = cur
        _stats["gateway_mac_changes"]  = 0
        _stats["duplicate_arp_count"]  = 0
        _stats["last_anomaly"]         = "none"
        _stats["last_anomaly_ts"]      = ""
        _alerts.clear()
        _ip_table.clear()
    _write_flag.set()
    print(f"[arpwatch] Reset complete. EXP MAC: {cur or '(unknown)'}", flush=True)


def _writer_thread():
    while True:
        _write_flag.wait(timeout=WRITE_INTERVAL)
        _write_flag.clear()

        # Check for reset flag written by index.php (flag file avoids signal permission issues)
        if os.path.exists(_reset_flag):
            try:
                os.unlink(_reset_flag)
            except Exception:
                pass
            _apply_reset()
            continue  # rebuild JSON immediately after reset

        now_mono = time.monotonic()
        with _lock:
            # Prune rate window
            while _rate_win and (now_mono - _rate_win[0]) > RATE_WINDOW_S:
                _rate_win.popleft()
            rate = float(len(_rate_win))  # packets in last 60 s = pkt/min

            # Compute status + reason
            cur     = _stats["gateway_mac_current"]
            exp     = _stats["gateway_mac_expected"]
            changes = _stats["gateway_mac_changes"]
            dupes   = _stats["duplicate_arp_count"]
            if changes > 0:
                status = "anomaly"
                reason = f"GW MAC changed {changes}x"
            elif cur and exp and cur != exp:
                status = "anomaly"
                reason = f"GW MAC mismatch"
            elif rate > ANOMALY_RATE:
                status = "anomaly"
                reason = f"ARP flood {rate:.0f}/min"
            elif rate > WARN_RATE:
                status = "warning"
                reason = f"high rate {rate:.0f}/min"
            elif dupes > 0:
                status = "warning"
                reason = f"{dupes} duplicate ARPs"
            else:
                status = "ok"
                reason = "normal"
            _stats["status"]        = status
            _stats["status_reason"] = reason
            _stats["arp_rate"]      = round(rate, 1)

            # Build top-talkers list and snapshot events
            top = sorted(_talkers.items(), key=lambda x: x[1], reverse=True)
            talkers_snap = [(ip, cnt) for ip, cnt in top[:MAX_TALKERS]]
            events_snap  = list(_events)

            payload = dict(_stats)
            payload["alerts"] = list(_alerts)

        # Enrich with device names (DB I/O outside the lock)
        all_ips = set(ip for ip, _ in talkers_snap) | set(e["ip"] for e in events_snap)
        names   = {ip: _query_device_name(ip) for ip in all_ips}

        talkers_out = [{"ip": ip, "name": names.get(ip, ""), "count": cnt}
                       for ip, cnt in talkers_snap]
        events_out  = [{"ip": e["ip"], "name": names.get(e["ip"], ""),
                        "mac": e["mac"], "type": e["type"], "ts": e["ts"]}
                       for e in events_snap]

        payload["top_talkers"] = talkers_out
        payload["last_events"] = events_out

        _atomic_write(json.dumps(payload))

# ---------------------------------------------------------------------------
# Anomaly recorder (call with _lock held)
# ---------------------------------------------------------------------------

def _add_alert(alert_type, ip, old_mac, new_mac):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Deduplicate: skip if same type+ip+new_mac already in recent alerts
    for a in _alerts[:5]:
        if a["type"] == alert_type and a["ip"] == ip and a["new_mac"] == new_mac:
            return
    _alerts.insert(0, {
        "type":    alert_type,
        "ip":      ip,
        "old_mac": old_mac,
        "new_mac": new_mac,
        "time":    now_str,
    })
    del _alerts[MAX_ALERTS:]
    _stats["last_anomaly"]    = alert_type
    _stats["last_anomaly_ts"] = now_str
    print(
        f"[arpwatch] {now_str}  {alert_type:<22}  "
        f"ip={ip:<15}  {old_mac} -> {new_mac}",
        flush=True,
    )

# ---------------------------------------------------------------------------
# Packet handler
# ---------------------------------------------------------------------------

def _process_packet(pkt, gateway_ip):
    if not pkt.haslayer(ARP):
        return
    arp = pkt[ARP]
    if arp.op not in (1, 2):
        return

    src_ip  = arp.psrc
    src_mac = arp.hwsrc.lower()
    op_str  = "reply" if arp.op == 2 else "request"

    if not src_ip or src_ip == "0.0.0.0" or not src_mac:
        return

    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts_short = now_str[11:]   # HH:MM:SS
    anomaly  = False

    with _lock:
        # ARP rate window
        _rate_win.append(time.monotonic())

        # Rolling last-events buffer (every packet, not just anomalies)
        _events.appendleft({
            "ip":   src_ip,
            "mac":  src_mac,
            "type": op_str,
            "ts":   ts_short,
        })

        # Per-IP packet counter (talker tracking)
        _talkers[src_ip] = _talkers.get(src_ip, 0) + 1
        if len(_talkers) > TALKER_PRUNE:
            # Drop bottom half by count to keep memory bounded
            cutoff = sorted(_talkers.values())[len(_talkers) // 2]
            for k in [k for k, v in list(_talkers.items()) if v <= cutoff]:
                del _talkers[k]

        # Update last-seen timestamp
        _stats["last_arp_ts"] = now_str

        # Seed gateway MAC if this is the first packet from the gateway
        if src_ip == gateway_ip and not _stats["gateway_mac_current"]:
            _stats["gateway_mac_current"] = src_mac
            if not _stats["gateway_mac_expected"]:
                _stats["gateway_mac_expected"] = src_mac
                print(f"[arpwatch] Learned gateway MAC: {src_mac}", flush=True)

        # MAC-change detection
        entry = _ip_table.get(src_ip)
        if entry is None:
            _ip_table[src_ip] = {"mac": src_mac, "prev_mac": None, "changed_at": 0.0}
        elif entry["mac"] != src_mac:
            old_mac    = entry["mac"]
            prev_mac   = entry["prev_mac"]
            changed_at = entry["changed_at"]
            now_mono   = time.monotonic()

            _ip_table[src_ip] = {
                "mac":        src_mac,
                "prev_mac":   old_mac,
                "changed_at": now_mono,
            }
            _stats["duplicate_arp_count"] += 1

            if prev_mac == src_mac and changed_at > 0 and (now_mono - changed_at) < SPOOF_WINDOW_S:
                atype = "arp_spoof"
            elif src_ip == gateway_ip:
                atype = "gateway_mac_changed"
                _stats["gateway_mac_changes"] += 1
                _stats["gateway_mac_current"]  = src_mac
            else:
                atype = "mac_change"

            _add_alert(atype, src_ip, old_mac, src_mac)
            anomaly = True

    # Signal writer — immediately on anomaly, otherwise let the timer fire
    if anomaly:
        _write_flag.set()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Enhanced passive ARP monitor for Pi.Alert v2"
    )
    parser.add_argument("--iface",   default="eth0",
                        help="Network interface to sniff (default: eth0)")
    parser.add_argument("--gateway", default=None,
                        help="Gateway IP — auto-detected if omitted")
    parser.add_argument("--db",      default="/opt/pialert/db/pialert.db",
                        help="Pi.Alert SQLite DB path for expected gateway MAC")
    parser.add_argument("--out",     default="/tmp/arp_status.json",
                        help="Output JSON path (default: /tmp/arp_status.json)")
    args = parser.parse_args()

    global _alert_file, _db_path
    _alert_file = args.out
    _db_path    = args.db

    gateway_ip = args.gateway or _get_default_gateway()
    if gateway_ip:
        print(f"[arpwatch] Gateway:   {gateway_ip}", flush=True)
    else:
        print("[arpwatch] Warning: no gateway detected — GATEWAY_MAC detection off",
              flush=True)
        gateway_ip = ""

    with _lock:
        _stats["iface"]      = args.iface
        _stats["gateway_ip"] = gateway_ip

    # Seed expected MAC from Pi.Alert DB
    if gateway_ip:
        exp = _query_expected_mac(gateway_ip, args.db)
        if exp:
            with _lock:
                _stats["gateway_mac_expected"] = exp
            print(f"[arpwatch] Expected MAC (DB):  {exp}", flush=True)
        else:
            print("[arpwatch] Gateway MAC not in DB — will learn from first ARP",
                  flush=True)

    print(f"[arpwatch] Sniffing on {args.iface}  →  {_alert_file}", flush=True)

    # Write PID file so index.php can signal us
    _pid_file = "/tmp/arpwatch_daemon.pid"
    try:
        with open(_pid_file, "w") as _pf:
            _pf.write(str(os.getpid()) + "\n")
    except Exception as _e:
        print(f"[arpwatch] PID file write failed: {_e}", flush=True)

    # Write initial empty state
    _write_flag.set()
    t = threading.Thread(target=_writer_thread, daemon=True)
    t.start()

    def _sigterm(sig, frame):
        print("[arpwatch] SIGTERM — exiting.", flush=True)
        try:
            os.unlink(_pid_file)
        except Exception:
            pass
        sys.exit(0)

    def _sigusr1(sig, frame):
        """Reset via SIGUSR1 — writes the flag file so the writer thread handles it cleanly."""
        try:
            open(_reset_flag, "w").close()
        except Exception:
            pass
        _write_flag.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGUSR1, _sigusr1)

    sniff(
        iface=args.iface,
        filter="arp",
        prn=lambda pkt: _process_packet(pkt, gateway_ip),
        store=False,
    )


if __name__ == "__main__":
    main()
