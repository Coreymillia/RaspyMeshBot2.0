#!/usr/bin/env python3
"""
rgb_status_daemon.py — RGB LED network health indicator for Pi.Alert Pi

Hardware: Common-cathode RGB LED wired to:
  Red   → GPIO 17 (Pin 11)  via 220Ω resistor
  Green → GPIO 27 (Pin 13)  via 220Ω resistor
  Blue  → GPIO 22 (Pin 15)  via 220Ω resistor
  GND   → any GND pin

Status priority (highest wins):
  ARP anomaly    → RED fast blink      (MAC change / gateway spoof)
  WiFi anomaly   → MAGENTA fast blink  (rogue AP / deauth flood)
  ARP warning    → YELLOW slow blink   (ARP rate spike)
  WiFi warning   → BLUE slow blink     (probe spike)
  DNS spike      → ORANGE pulse        (Pi-hole query spike)
  Pi-hole down   → RED solid           (can't reach Pi-hole API)
  All clear      → GREEN slow breathe
  Boot           → AMBER breathe       (first 10s)
"""

import time
import json
import math
import threading
import os
import requests

# ── Config ─────────────────────────────────────────────────────────────────────
ARP_STATUS_FILE   = "/tmp/arp_status.json"
WIFI_STATUS_FILE  = "/tmp/wifi_status.json"
PIHOLE_BASE       = "http://localhost:8080/api"
PIHOLE_PASSWORD   = "Pihole710"
TOKEN_REFRESH_S   = 1500   # refresh session token every 25 min (validity = 30 min)
POLL_INTERVAL_S   = 5
DNS_SPIKE_QUERIES = 500    # queries per poll above previous reading = spike
BOOT_HOLD_S       = 10     # seconds to show boot animation before going to normal

# ── GPIO / PWM setup ───────────────────────────────────────────────────────────
RGB_RED   = 17
RGB_GREEN = 27
RGB_BLUE  = 22

try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RGB_RED,   GPIO.OUT)
    GPIO.setup(RGB_GREEN, GPIO.OUT)
    GPIO.setup(RGB_BLUE,  GPIO.OUT)
    _red_pwm = GPIO.PWM(RGB_RED,   1000)
    _grn_pwm = GPIO.PWM(RGB_GREEN, 1000)
    _blu_pwm = GPIO.PWM(RGB_BLUE,  1000)
    _red_pwm.start(0)
    _grn_pwm.start(0)
    _blu_pwm.start(0)
    _gpio_ok = True
    print("[RGB] GPIO initialised")
except Exception as e:
    _gpio_ok = False
    print(f"[RGB] GPIO init failed: {e}")

def _pwm_set(r, g, b):
    if not _gpio_ok:
        return
    try:
        _red_pwm.ChangeDutyCycle(max(0.0, min(100.0, float(r))))
        _grn_pwm.ChangeDutyCycle(max(0.0, min(100.0, float(g))))
        _blu_pwm.ChangeDutyCycle(max(0.0, min(100.0, float(b))))
    except Exception:
        pass

# ── Mode definitions ────────────────────────────────────────────────────────────
# (r, g, b 0-100, style, period_s)
MODES = {
    #  name              r     g     b   style          period
    "boot":           (100,  50,   0, "breathe",      2.0),
    "normal":         (  0, 100,   0, "breathe",      4.0),
    "pihole_down":    (100,   0,   0, "solid",        0.0),
    "dns_spike":      (100,  40,   0, "pulse",        1.0),
    "wifi_warning":   (  0,   0, 100, "blink",        1.0),
    "arp_warning":    (100, 100,   0, "blink",        1.0),
    "wifi_anomaly":   (100,   0, 100, "fast_blink",   0.25),
    "arp_anomaly":    (100,   0,   0, "fast_blink",   0.25),
}

# Priority order — first match wins
MODE_PRIORITY = [
    "arp_anomaly",
    "wifi_anomaly",
    "arp_warning",
    "wifi_warning",
    "dns_spike",
    "pihole_down",
    "normal",
]

# ── Shared state ───────────────────────────────────────────────────────────────
_active_flags = set()   # which modes are currently flagged
_flags_lock   = threading.Lock()
_current_mode = "boot"
_mode_lock    = threading.Lock()

def _set_flag(name, active: bool):
    with _flags_lock:
        if active:
            _active_flags.add(name)
        else:
            _active_flags.discard(name)

def _resolve_mode():
    """Return highest-priority active mode."""
    with _flags_lock:
        flags = set(_active_flags)
    for m in MODE_PRIORITY:
        if m in flags:
            return m
    return "normal"

# ── LED render thread ──────────────────────────────────────────────────────────
def _led_thread():
    t = 0.0
    last_mode = None
    while True:
        with _mode_lock:
            mode = _current_mode
        if mode != last_mode:
            t = 0.0
            last_mode = mode

        cfg = MODES.get(mode, MODES["normal"])
        r_max, g_max, b_max, style, period = cfg

        if style == "solid":
            _pwm_set(r_max, g_max, b_max)

        elif style == "breathe":
            brightness = math.sin(math.pi * t / period) ** 2
            _pwm_set(r_max * brightness, g_max * brightness, b_max * brightness)
            t += 0.05
            if t >= period:
                t = 0.0

        elif style in ("blink", "fast_blink"):
            on = (t % period) < (period / 2)
            _pwm_set(r_max if on else 0, g_max if on else 0, b_max if on else 0)
            t += 0.05
            if t >= period:
                t = 0.0

        elif style == "pulse":
            # sharp flash at the start of each period
            on = (t % period) < 0.12
            _pwm_set(r_max if on else 0, g_max if on else 0, b_max if on else 0)
            t += 0.05
            if t >= period:
                t = 0.0

        time.sleep(0.05)

# ── Pollers ────────────────────────────────────────────────────────────────────
def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

_last_dns_queries = None
_pihole_sid       = None
_pihole_sid_ts    = 0.0
_pihole_lock      = threading.Lock()

def _pihole_get_token():
    """Authenticate with Pi-hole v6 and return a session ID."""
    try:
        r = requests.post(f"{PIHOLE_BASE}/auth",
                          json={"password": PIHOLE_PASSWORD}, timeout=5)
        r.raise_for_status()
        sid = r.json().get("session", {}).get("sid")
        if sid:
            print(f"[RGB] Pi-hole auth ok — new session token")
            return sid
    except Exception as e:
        print(f"[RGB] Pi-hole auth failed: {e}")
    return None

def _pihole_fetch(path):
    """GET a Pi-hole v6 API endpoint, refreshing the session token if needed."""
    global _pihole_sid, _pihole_sid_ts
    with _pihole_lock:
        if _pihole_sid is None or (time.time() - _pihole_sid_ts) > TOKEN_REFRESH_S:
            _pihole_sid    = _pihole_get_token()
            _pihole_sid_ts = time.time()
        sid = _pihole_sid
    if not sid:
        return None
    try:
        r = requests.get(f"{PIHOLE_BASE}/{path.lstrip('/')}",
                         headers={"sid": sid}, timeout=4)
        if r.status_code == 401:
            # Token expired — force refresh next call
            with _pihole_lock:
                _pihole_sid = None
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[RGB] Pi-hole fetch error: {e}")
        return None

def _poll_loop():
    global _last_dns_queries, _current_mode

    # Boot animation — hold for BOOT_HOLD_S before polling
    with _mode_lock:
        _current_mode = "boot"
    print(f"[RGB] Boot mode for {BOOT_HOLD_S}s")
    time.sleep(BOOT_HOLD_S)

    while True:
        # ── ARP status ────────────────────────────────────────────────────────
        arp = _read_json(ARP_STATUS_FILE)
        if arp:
            s = arp.get("status", "ok")
            _set_flag("arp_anomaly", s == "anomaly")
            _set_flag("arp_warning", s == "warning")
            print(f"[RGB] ARP status: {s}")
        else:
            print("[RGB] ARP status file not found — skipping")

        # ── WiFi status ───────────────────────────────────────────────────────
        wifi = _read_json(WIFI_STATUS_FILE)
        if wifi:
            s = wifi.get("status", "ok")
            _set_flag("wifi_anomaly", s == "anomaly")
            _set_flag("wifi_warning", s == "warning")
            print(f"[RGB] WiFi status: {s}")
        else:
            print("[RGB] WiFi status file not found — skipping")

        # ── Pi-hole health ────────────────────────────────────────────────────
        data = _pihole_fetch("stats/summary")
        if data:
            total = int(data.get("queries", {}).get("total", 0))
            _set_flag("pihole_down", False)
            if _last_dns_queries is not None:
                delta = total - _last_dns_queries
                _set_flag("dns_spike", delta > DNS_SPIKE_QUERIES)
                if delta > DNS_SPIKE_QUERIES:
                    print(f"[RGB] DNS spike: +{delta} queries")
            _last_dns_queries = total
            print(f"[RGB] Pi-hole ok — total queries: {total}")
        else:
            _set_flag("pihole_down", True)
            _set_flag("dns_spike", False)
            print("[RGB] Pi-hole unreachable")

        # ── Resolve and apply top-priority mode ───────────────────────────────
        mode = _resolve_mode()
        with _mode_lock:
            _current_mode = mode
        print(f"[RGB] Active mode: {mode}")

        time.sleep(POLL_INTERVAL_S)

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[RGB] Starting Pi.Alert RGB status daemon")

    threading.Thread(target=_led_thread, daemon=True, name="led-render").start()
    threading.Thread(target=_poll_loop,  daemon=True, name="poller").start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[RGB] Shutting down")
        _pwm_set(0, 0, 0)
        if _gpio_ok:
            GPIO.cleanup()
