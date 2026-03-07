#!/usr/bin/env python3
"""arpwatch_daemon.py — Passive ARP anomaly detector for Pi.Alert

Listens on eth0 (or a configured interface) for ARP traffic and detects:
  GATEWAY_MAC  — the gateway's IP is now being claimed by a different MAC
  ARP_SPOOF    — a device's MAC is oscillating between two values (active poisoning)
  MAC_CHANGE   — any other device changed its MAC for a known IP

The 10 most recent anomalies are written atomically to /tmp/arp_alerts.json
so the Pi.Alert API can serve them to ESP32 CYD monitors.

Usage:
  sudo python3 arpwatch_daemon.py [--iface eth0] [--gateway 192.168.0.1] \
                                  [--out /tmp/arp_alerts.json]

Dependencies:
  pip3 install scapy
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

try:
    from scapy.all import ARP, sniff
except ImportError:
    sys.exit("scapy not installed.  Run: sudo pip3 install scapy")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MAX_ALERTS     = 10    # alerts kept in the JSON file
SPOOF_WINDOW_S = 120   # seconds: back-and-forth MAC flip within this window → ARP_SPOOF

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
alerts     = []    # list of alert dicts, newest first
ip_table   = {}    # ip -> {"mac": str, "prev_mac": str|None, "changed_at": float}
state_lock = threading.Lock()
alert_file = "/tmp/arp_alerts.json"


def _get_default_gateway():
    """Return the default gateway IP, or None if it cannot be determined."""
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "default" and "via" in parts:
                return parts[parts.index("via") + 1]
    except Exception:
        pass
    return None


def _write_alerts():
    """Atomically write the alerts list to alert_file (called with state_lock held)."""
    dir_ = os.path.dirname(os.path.abspath(alert_file))
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(alerts, f)
        os.chmod(tmp, 0o644)  # readable by www-data / Apache
        os.replace(tmp, alert_file)
    except Exception as exc:
        print(f"[arpwatch] Failed to write {alert_file}: {exc}", flush=True)
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _add_alert(alert_type, ip, old_mac, new_mac):
    """Record an anomaly, deduplicate within a short window, persist to JSON."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "type":    alert_type,
        "ip":      ip,
        "old_mac": old_mac,
        "new_mac": new_mac,
        "time":    now_str,
    }
    with state_lock:
        # Suppress exact duplicates already in the 5 most recent alerts
        for a in alerts[:5]:
            if (a["type"] == alert_type and a["ip"] == ip
                    and a["new_mac"] == new_mac):
                return
        alerts.insert(0, entry)
        del alerts[MAX_ALERTS:]
        _write_alerts()

    print(
        f"[arpwatch] {now_str}  {alert_type:<14}  ip={ip:<15}  "
        f"{old_mac} -> {new_mac}",
        flush=True,
    )


def _process_packet(pkt, gateway_ip):
    """ARP packet handler — classify and record anomalies."""
    if not pkt.haslayer(ARP):
        return
    arp = pkt[ARP]
    # op 1 = who-has (request), op 2 = is-at (reply); gratuitous ARPs use op 1
    if arp.op not in (1, 2):
        return

    src_ip  = arp.psrc
    src_mac = arp.hwsrc.lower()

    if not src_ip or src_ip == "0.0.0.0" or not src_mac:
        return

    with state_lock:
        entry = ip_table.get(src_ip)

        if entry is None:
            # First sighting — record without alerting
            ip_table[src_ip] = {
                "mac":        src_mac,
                "prev_mac":   None,
                "changed_at": 0.0,
            }
            return

        if entry["mac"] == src_mac:
            return  # nothing changed

        # MAC changed for a known IP
        old_mac    = entry["mac"]
        prev_mac   = entry["prev_mac"]
        changed_at = entry["changed_at"]

        # Update table before releasing the lock
        ip_table[src_ip] = {
            "mac":        src_mac,
            "prev_mac":   old_mac,
            "changed_at": time.monotonic(),
        }

    # Classify outside the lock
    now = time.monotonic()
    if (prev_mac == src_mac
            and changed_at > 0
            and (now - changed_at) < SPOOF_WINDOW_S):
        alert_type = "ARP_SPOOF"
    elif src_ip == gateway_ip:
        alert_type = "GATEWAY_MAC"
    else:
        alert_type = "MAC_CHANGE"

    _add_alert(alert_type, src_ip, old_mac, src_mac)


def main():
    parser = argparse.ArgumentParser(
        description="Passive ARP anomaly detector for Pi.Alert"
    )
    parser.add_argument("--iface",   default="eth0",
                        help="Network interface to sniff (default: eth0)")
    parser.add_argument("--gateway", default=None,
                        help="Gateway IP — auto-detected from routing table if omitted")
    parser.add_argument("--out",     default="/tmp/arp_alerts.json",
                        help="Output JSON path (default: /tmp/arp_alerts.json)")
    args = parser.parse_args()

    global alert_file
    alert_file = args.out

    gateway_ip = args.gateway or _get_default_gateway()
    if gateway_ip:
        print(f"[arpwatch] Monitoring gateway: {gateway_ip}", flush=True)
    else:
        print(
            "[arpwatch] Warning: could not detect gateway. "
            "GATEWAY_MAC detection disabled.",
            flush=True,
        )
        gateway_ip = ""

    print(
        f"[arpwatch] Sniffing ARP on {args.iface}, "
        f"writing alerts to {alert_file}",
        flush=True,
    )

    # Write an empty list immediately so the API endpoint never 404s on startup
    with state_lock:
        _write_alerts()

    def _sigterm(sig, frame):
        print("[arpwatch] SIGTERM received — exiting.", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    sniff(
        iface=args.iface,
        filter="arp",
        prn=lambda pkt: _process_packet(pkt, gateway_ip),
        store=False,
    )


if __name__ == "__main__":
    main()
