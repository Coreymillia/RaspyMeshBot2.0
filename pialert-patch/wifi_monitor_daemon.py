#!/usr/bin/env python3
"""
wifi_monitor_daemon.py — 802.11 passive RF monitor for CYDPiAlert.

Puts the Pi's wlan0 into monitor mode, sniffs management frames with scapy,
and writes two JSON files that Pi.Alert's patched index.php serves to the CYD:

  /tmp/wifi_status.json   — summary (status, deauth count, rogue AP count, etc.)
  /tmp/wifi_detail.json   — full detail (rogue AP list, deauths, channel map, etc.)

Auto-learn mode:
  On first run (no whitelist file) the daemon learns all visible BSSIDs for
  LEARN_SECONDS, saves them as known-good, then flags anything new as a rogue AP.
  On subsequent runs the saved whitelist is loaded and learning is skipped.

Usage:
  sudo python3 wifi_monitor_daemon.py [interface]   (default: wlan0)

Dependencies:
  pip3 install scapy
  apt install iw python3-scapy   (or pip install scapy)
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Try scapy ────────────────────────────────────────────────────────────────
try:
    from scapy.all import (Dot11, Dot11Deauth, Dot11Elt, Dot11ProbeReq,
                           RadioTap, sniff)
except ImportError:
    print("[wifi_monitor] ERROR: scapy not installed.")
    print("  Run: sudo pip3 install scapy")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
IFACE            = sys.argv[1] if len(sys.argv) > 1 else "wlan0"
LEARN_SECONDS    = 60
WHITELIST_FILE   = "/etc/wifi_monitor_whitelist.json"
CHANNELS         = list(range(1, 14))   # 2.4 GHz channels 1–13
HOP_INTERVAL     = 0.3                  # seconds per channel
WRITE_INTERVAL   = 5                    # seconds between JSON file writes
DEAUTH_MAXKEEP   = 20                   # max deauth events to keep in memory
PROBE_RATE_WINDOW = 60                  # seconds for probe-per-minute calculation
DEAUTH_WARN_COUNT = 3                   # deauths in 5min before warning
DEAUTH_ANOMALY    = 10                  # deauths in 5min before anomaly
PROBE_SPIKE_RATE  = 100                 # probes/min before spike warning
STATUS_FILE      = "/tmp/wifi_status.json"
DETAIL_FILE      = "/tmp/wifi_detail.json"
MAX_ROGUE_APS    = 20
MAX_TOP_BSSIDS   = 8

# ── State (all access under `lock`) ─────────────────────────────────────────
lock = threading.Lock()

deauth_count      = 0
deauth_recent     = deque(maxlen=DEAUTH_MAXKEEP)   # {"ts","epoch","src","dst","reason"}

probe_count       = 0
probe_timestamps  = deque()   # epoch floats, for rolling rate calc

known_aps   = {}   # BSSID → ap_dict
rogue_aps   = {}   # BSSID → ap_dict
bssid_counts = defaultdict(int)       # BSSID → total frame count
channel_activity = defaultdict(int)   # channel (int) → frame count

learning       = True
learn_end_time = 0.0
whitelist      = set()   # known-good BSSIDs

wifi_status    = "ok"
status_reason  = "Initialising"

# ── Helpers ──────────────────────────────────────────────────────────────────
def ap_dict(bssid, ssid, channel, rssi, hidden):
    return {
        "bssid":     bssid,
        "ssid":      ssid,
        "channel":   channel,
        "rssi":      rssi,
        "hidden":    hidden,
        "last_seen": datetime.now().strftime("%H:%M:%S"),
    }

def get_rssi(pkt):
    if pkt.haslayer(RadioTap):
        rt = pkt[RadioTap]
        for attr in ("dBm_AntSignal", "dbm_antsignal"):
            try:
                v = getattr(rt, attr)
                if v is not None:
                    return int(v)
            except (AttributeError, TypeError):
                pass
    return -100

def get_channel(pkt):
    if pkt.haslayer(RadioTap):
        rt = pkt[RadioTap]
        for attr in ("ChannelFrequency", "channel_freq"):
            try:
                freq = getattr(rt, attr)
                if freq and 2412 <= freq <= 2484:
                    return (freq - 2407) // 5
            except (AttributeError, TypeError):
                pass
    return 0

def extract_ssid(pkt):
    try:
        elt = pkt.getlayer(Dot11Elt)
        while elt:
            if elt.ID == 0:
                raw = elt.info
                ssid = raw.decode("utf-8", errors="replace").strip("\x00") if raw else ""
                return ssid, (len(ssid) == 0)
            if not hasattr(elt.payload, "ID"):
                break
            elt = elt.payload
    except Exception:
        pass
    return "", False

# ── Whitelist ────────────────────────────────────────────────────────────────
def load_whitelist():
    global whitelist
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE) as f:
                whitelist = set(json.load(f))
            print(f"[wifi_monitor] Loaded {len(whitelist)} known APs from whitelist.")
            return True
        except Exception as e:
            print(f"[wifi_monitor] Could not load whitelist: {e}")
    return False

def save_whitelist():
    try:
        with open(WHITELIST_FILE, "w") as f:
            json.dump(sorted(whitelist), f, indent=2)
        print(f"[wifi_monitor] Saved {len(whitelist)} APs to whitelist.")
    except Exception as e:
        print(f"[wifi_monitor] Could not save whitelist: {e}")

# ── Frame handler ─────────────────────────────────────────────────────────────
def handle_frame(pkt):
    global deauth_count, probe_count

    if not pkt.haslayer(Dot11):
        return

    dot11 = pkt[Dot11]
    now   = time.time()
    rssi  = get_rssi(pkt)
    ch    = get_channel(pkt)

    with lock:
        if ch > 0:
            channel_activity[ch] += 1

        # ── Deauth / Disassoc (type=0, subtype=12 or 10) ──────────────────
        if dot11.type == 0 and dot11.subtype in (10, 12):
            deauth_count += 1
            reason = 0
            if pkt.haslayer(Dot11Deauth):
                try:
                    reason = int(pkt[Dot11Deauth].reason)
                except Exception:
                    pass
            deauth_recent.append({
                "ts":     datetime.now().strftime("%H:%M:%S"),
                "epoch":  now,
                "src":    dot11.addr2 or "??:??:??:??:??:??",
                "dst":    dot11.addr1 or "ff:ff:ff:ff:ff:ff",
                "reason": reason,
            })

        # ── Probe requests (type=0, subtype=4) ───────────────────────────
        elif dot11.type == 0 and dot11.subtype == 4:
            probe_count += 1
            probe_timestamps.append(now)
            # Trim entries older than the rate window
            while probe_timestamps and (now - probe_timestamps[0]) > PROBE_RATE_WINDOW:
                probe_timestamps.popleft()

        # ── Beacons + probe responses (type=0, subtype=8 or 5) ───────────
        elif dot11.type == 0 and dot11.subtype in (5, 8):
            bssid = dot11.addr3 or dot11.addr2
            if not bssid or bssid == "ff:ff:ff:ff:ff:ff":
                return
            bssid_counts[bssid] += 1
            ssid, hidden = extract_ssid(pkt)
            entry = ap_dict(bssid, ssid, ch, rssi, hidden)
            if learning:
                known_aps[bssid] = entry
                whitelist.add(bssid)
            elif bssid in whitelist:
                known_aps[bssid] = entry
            else:
                rogue_aps[bssid] = entry

        # Track all source MACs
        src = dot11.addr2
        if src and src != "ff:ff:ff:ff:ff:ff":
            bssid_counts[src] += 1

    _update_status(now)

def _update_status(now):
    global wifi_status, status_reason
    with lock:
        recent_deauths_5m = sum(1 for d in deauth_recent if now - d["epoch"] < 300)
        probe_rate = len(probe_timestamps)   # probes in last PROBE_RATE_WINDOW seconds

        if len(rogue_aps) > 0 or recent_deauths_5m >= DEAUTH_ANOMALY:
            wifi_status   = "anomaly"
            status_reason = (f"{len(rogue_aps)} rogue AP(s)" if rogue_aps
                             else f"{recent_deauths_5m} deauths/5m")
        elif probe_rate >= PROBE_SPIKE_RATE or recent_deauths_5m >= DEAUTH_WARN_COUNT:
            wifi_status   = "warning"
            status_reason = ("Probe spike" if probe_rate >= PROBE_SPIKE_RATE
                             else f"{recent_deauths_5m} deauths/5m")
        else:
            wifi_status   = "ok"
            status_reason = "Clean"

# ── JSON writers ──────────────────────────────────────────────────────────────
def build_and_write_json():
    while True:
        time.sleep(WRITE_INTERVAL)
        now = time.time()
        with lock:
            probe_rate        = len(probe_timestamps)
            recent_deauths_5m = sum(1 for d in deauth_recent if now - d["epoch"] < 300)
            last_ts           = deauth_recent[-1]["ts"] if deauth_recent else ""

            top_bssids = sorted(bssid_counts.items(), key=lambda x: -x[1])[:MAX_TOP_BSSIDS]
            ch_map     = {str(ch): channel_activity.get(ch, 0) for ch in CHANNELS}
            rogue_list = list(rogue_aps.values())[:MAX_ROGUE_APS]
            deauth_list = [
                {"ts": d["ts"], "src": d["src"], "dst": d["dst"], "reason": d["reason"]}
                for d in list(deauth_recent)[-5:]
            ]

            status_snap = {
                "status":           wifi_status,
                "status_reason":    status_reason,
                "deauth_count":     deauth_count,
                "deauth_5min":      recent_deauths_5m,
                "last_deauth_ts":   last_ts,
                "rogue_ap_count":   len(rogue_aps),
                "probe_count":      probe_count,
                "probe_rate":       probe_rate,
                "probe_spike":      probe_rate >= PROBE_SPIKE_RATE,
                "known_ap_count":   len(known_aps),
                "hidden_ssid_count": sum(1 for ap in known_aps.values() if ap.get("hidden")),
                "learning":         learning,
                "learn_ends_in":    max(0, int(learn_end_time - now)) if learning else 0,
            }
            detail_snap = {
                "status":          wifi_status,
                "rogue_aps":       rogue_list,
                "recent_deauths":  deauth_list,
                "top_bssids":      [{"bssid": b, "count": c} for b, c in top_bssids],
                "channel_map":     ch_map,
                "probe_spike":     probe_rate >= PROBE_SPIKE_RATE,
                "probe_rate":      probe_rate,
            }

        try:
            with open(STATUS_FILE, "w") as f:
                json.dump(status_snap, f)
            with open(DETAIL_FILE, "w") as f:
                json.dump(detail_snap, f)
        except Exception as e:
            print(f"[wifi_monitor] Write error: {e}")

# ── Channel hopper ────────────────────────────────────────────────────────────
def channel_hopper():
    idx = 0
    while True:
        ch = CHANNELS[idx % len(CHANNELS)]
        try:
            subprocess.run(
                ["iw", "dev", IFACE, "set", "channel", str(ch)],
                capture_output=True, timeout=1
            )
        except Exception:
            pass
        idx += 1
        time.sleep(HOP_INTERVAL)

# ── Learning timer ────────────────────────────────────────────────────────────
def learning_timer():
    global learning
    remaining = max(0, learn_end_time - time.time())
    time.sleep(remaining)
    with lock:
        learning = False
    save_whitelist()
    print(f"[wifi_monitor] Learn complete — {len(whitelist)} APs whitelisted. Now monitoring.")
    _update_status(time.time())

# ── Monitor mode setup ────────────────────────────────────────────────────────
def setup_monitor_mode():
    global IFACE
    base = IFACE
    print(f"[wifi_monitor] Attempting monitor mode on {base}...")
    try:
        subprocess.run(["ip", "link", "set", base, "down"], capture_output=True, timeout=5)
        result = subprocess.run(["iw", base, "set", "monitor", "control"],
                                capture_output=True, timeout=5)
        if result.returncode != 0:
            # Monitor mode not supported — bring interface back up in managed mode
            print(f"[wifi_monitor] Monitor mode not supported on {base} "
                  f"({result.stderr.decode().strip()}) — falling back to managed mode.")
            subprocess.run(["ip", "link", "set", base, "up"], capture_output=True, timeout=5)
            return False
        subprocess.run(["ip", "link", "set", base, "up"], capture_output=True, timeout=5)
        print(f"[wifi_monitor] Monitor mode active on {IFACE}.")
        return True
    except Exception as e:
        print(f"[wifi_monitor] Monitor mode setup error: {e} — bringing interface up.")
        subprocess.run(["ip", "link", "set", base, "up"], capture_output=True, timeout=5)
        return False

def sniff_with_retry():
    """Sniff frames, retrying if the interface isn't ready yet."""
    while True:
        try:
            print(f"[wifi_monitor] Sniffing on {IFACE}...")
            sniff(iface=IFACE, prn=handle_frame, store=False)
            break  # clean exit (KeyboardInterrupt)
        except Exception as e:
            msg = str(e)
            if "Network is down" in msg or "errno 100" in msg.lower():
                print(f"[wifi_monitor] Interface not ready ({msg}), retrying in 5s...")
                time.sleep(5)
                # Make sure interface is up
                subprocess.run(["ip", "link", "set", IFACE, "up"],
                                capture_output=True, timeout=5)
            else:
                print(f"[wifi_monitor] Sniff error: {e}")
                break

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[wifi_monitor] Must run as root (sudo).")
        sys.exit(1)

    if load_whitelist():
        learning = False
        status_reason = "Clean"
    else:
        learn_end_time = time.time() + LEARN_SECONDS
        print(f"[wifi_monitor] Auto-learn: {LEARN_SECONDS}s window. "
              f"Make sure all your APs are visible.")
        threading.Thread(target=learning_timer, daemon=True).start()

    monitor_ok = setup_monitor_mode()
    if not monitor_ok:
        print(f"[wifi_monitor] Running in managed mode — beacon frames only. "
              f"Install nexmon for full monitor mode support.")

    threading.Thread(target=channel_hopper,       daemon=True).start()
    threading.Thread(target=build_and_write_json,  daemon=True).start()

    print(f"[wifi_monitor] Writing status to {STATUS_FILE} every {WRITE_INTERVAL}s.")

    try:
        sniff_with_retry()
    except KeyboardInterrupt:
        print("\n[wifi_monitor] Stopped.")
