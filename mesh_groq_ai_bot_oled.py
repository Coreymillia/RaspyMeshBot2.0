#!/usr/bin/env python3
"""
Groq AI MeshBot with Waveshare 1.44" LCD HAT Display
-----------------------------------------------------
Mode 1 (default): Live mesh status screen on LCD.
Mode 3 (SCREENSAVER_MODE): Pi.Alert network monitor dashboard with
  rule-based anomaly alerter. Sends DM to ALERT_NODE on anomalies.
  Matrix rain screensaver activates after SCREENSAVER_IDLE_S of inactivity.

Mode 3 button layout (HAT buttons, active LOW):
  KEY1 (BCM 21): cycle Pi.Alert views
  KEY2 (BCM 20): wake screensaver / force Pi.Alert display
  KEY3 (BCM 16): toggle backlight
"""

from pubsub import pub
from datetime import datetime

# ==== PUBSUB FIX ====
def safe_sendMessage(topic, **kwargs):
    try:
        pub._original_sendMessage(topic, **kwargs)
    except Exception as e:
        if isinstance(e, TypeError) or "SenderUnknownMsgDataError" in str(type(e)):
            kwargs.pop('interface', None)
            try:
                pub._original_sendMessage(topic, **kwargs)
            except Exception:
                pass
        else:
            raise e

if not hasattr(pub, "_original_sendMessage"):
    pub._original_sendMessage = pub.sendMessage
    pub.sendMessage = safe_sendMessage

import time, sys, os, threading, random
import meshtastic
import meshtastic.serial_interface
import requests
from PIL import Image, ImageDraw, ImageFont

# ==== CONFIG ====
import json as _json

def _load_api_key():
    """Load Groq API key from env var or config.json (never hard-code keys)."""
    import os as _os
    key = _os.environ.get('GROQ_API_KEY', '')
    if key:
        return key
    cfg = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'config.json')
    try:
        with open(cfg) as _f:
            return _json.load(_f)['groq_api_key']
    except Exception:
        return ''

GROQ_API_KEY     = _load_api_key()
MODEL            = "llama-3.1-8b-instant"
MAX_MESH_MSG_LEN = 200

# Pi.Alert integration — edit config.json to set these
def _cfg():
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(cfg) as _f:
            return _json.load(_f)
    except Exception:
        return {}

_c = _cfg()
PIALERT_BASE_URL   = _c.get('pialert_base_url', 'http://192.168.0.105/pialert/api/')
PIALERT_API_KEY    = _c.get('pialert_api_key', '')
PIALERT_POLL_S     = 60    # seconds between Pi.Alert polls
SCREENSAVER_IDLE_S = 300   # 5 minutes idle → activate matrix rain
_alert_raw  = _c.get('alert_node', '!changeme')
ALERT_NODES = _alert_raw if isinstance(_alert_raw, list) else [_alert_raw]
SERIAL_PORT        = _c.get('serial_port', None)   # None = auto-detect
BROADCAST_DAILY_MAX = _c.get('broadcast_daily_max', 3)  # 0=silent, 1-3 broadcast replies/day

# Pi-hole integration — no API key required for v6 public endpoints
PIHOLE_BASE_URL       = _c.get('pihole_base_url', '')   # e.g. 'http://192.168.0.103/api'
DNS_SPIKE_THRESHOLD   = _c.get('dns_spike_threshold', 300)  # queries/poll above baseline = alert
ENABLE_BOT            = _c.get('enable_bot', True)   # False = Pi.Alert/Pi-hole monitor only, no Groq/Meshtastic

# Telemetry monitor — watches a specific node for environment data
TELEMETRY_NODE               = _c.get('telemetry_monitor_node', '').strip()
TEMP_HIGH_F                  = float(_c.get('temp_high_f', 90))
TEMP_LOW_F                   = float(_c.get('temp_low_f', 32))
HUMIDITY_HIGH                = float(_c.get('humidity_high', 80))
TELEMETRY_ALERT_COOLDOWN_MIN = int(_c.get('telemetry_alert_cooldown_min', 30))

# Last received telemetry values (updated by on_receive_telemetry, read by _draw_display)
_telem_temp_f    = None
_telem_humidity  = None
_telem_lux       = None

# ==== HAT LCD SETUP ====
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_lcd_ok   = False
_lcd      = None
_lcd_lock = threading.Lock()

# Button pins (active LOW, internal pull-up)
KEY1_PIN      = 21   # cycle Pi.Alert view (Mode 3)
KEY2_PIN      = 20   # wake screensaver (Mode 3)
KEY3_PIN      = 16   # backlight toggle (all modes)
JOY_PRESS_PIN = 13   # hold 3s = reboot prompt
JOY_UP_PIN    = 6    # confirm reboot (YES)

try:
    import RPi.GPIO as GPIO
    import LCD_1in44, LCD_Config
    LCD_Config.GPIO_Init()
    _lcd = LCD_1in44.LCD()
    _lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    _lcd.LCD_Clear()
    GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.HIGH)
    GPIO.setup(KEY1_PIN,      GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY2_PIN,      GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY3_PIN,      GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(JOY_PRESS_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(JOY_UP_PIN,    GPIO.IN, pull_up_down=GPIO.PUD_UP)
    _lcd_ok = True
    print("[HAT] Waveshare 1.44\" LCD initialised")
except Exception as _e:
    print(f"[HAT] LCD init failed: {_e}  — display-less mode")

# ── Fonts ────────────────────────────────────────────────────────────────────
def _font(size, bold=False):
    base = '/usr/share/fonts/truetype/dejavu/'
    name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    try:
        return ImageFont.truetype(base + name, size)
    except Exception:
        return ImageFont.load_default()

F10B = _font(10, bold=True)
F9   = _font(9)
F8   = _font(8)
F7   = _font(7)

# ── Global display state ─────────────────────────────────────────────────────
_BL_ON = True

SCREENSAVER_MODE    = os.path.exists('/tmp/meshbot_screensaver')
_screensaver_pause  = threading.Event()  # set = force-show reply/anomaly screen
_screensaver_active = False              # True = matrix rain is running
_last_activity      = time.monotonic()  # last user/mesh/alert interaction

# Pi.Alert shared state
_pialert_data   = {}        # latest data keyed by endpoint name
_pialert_lock   = threading.Lock()
_current_view   = 0         # which Pi.Alert view is shown (0-5)
NUM_PA_VIEWS    = 6         # dashboard, online, new, arp, wifi, pihole
_seen_anomalies = set()     # dedup set — keys of anomalies already alerted

# Pi-hole shared state
_pihole_data    = {}        # latest Pi-hole data keyed by endpoint
_pihole_lock    = threading.Lock()

# DNS spike detection — tracks per-client cumulative query counts between polls
_DNS_COUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dns_counts.json')
_dns_last_counts = {}   # {ip: count} from previous poll

# Persistent anomaly dedup file — survives reboots
_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.seen_anomalies.json')


def _load_seen_anomalies():
    """Load previously seen anomaly keys from disk."""
    try:
        with open(_SEEN_FILE) as f:
            data = _json.load(f)
        # Prune keys older than 48 hours using timestamp suffix
        cutoff = (datetime.now() - __import__('datetime').timedelta(hours=48)).strftime('%Y-%m-%d %H:%M')
        pruned = {k for k in data if not any(k.startswith(p) for p in ('arp:', 'new:')) or k > cutoff}
        _seen_anomalies.update(data)
    except Exception:
        pass


def _save_seen_anomalies():
    """Persist seen anomaly keys to disk."""
    try:
        with open(_SEEN_FILE, 'w') as f:
            _json.dump(list(_seen_anomalies), f)
    except Exception:
        pass


_load_seen_anomalies()


def _load_dns_counts():
    """Load last-known per-client DNS counts from disk."""
    global _dns_last_counts
    try:
        with open(_DNS_COUNTS_FILE) as f:
            _dns_last_counts = _json.load(f)
    except Exception:
        _dns_last_counts = {}


def _save_dns_counts(counts):
    """Persist current per-client DNS counts to disk."""
    try:
        with open(_DNS_COUNTS_FILE, 'w') as f:
            _json.dump(counts, f)
    except Exception:
        pass


_load_dns_counts()


def _mark_activity():
    """Reset idle timer and wake display from screensaver."""
    global _last_activity, _screensaver_active
    _last_activity      = time.monotonic()
    _screensaver_active = False


# ── Backlight ────────────────────────────────────────────────────────────────
def _toggle_backlight():
    global _BL_ON
    if not _lcd_ok:
        return
    _BL_ON = not _BL_ON
    try:
        GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.HIGH if _BL_ON else GPIO.LOW)
    except Exception:
        pass


# ── Reboot confirmation ──────────────────────────────────────────────────────
def _draw_reboot_confirm(selected_yes=False):
    """Draw the hold-to-reboot confirmation screen."""
    if not _lcd_ok:
        return
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (128, 18)], fill=(180, 30, 0))
    draw.text((4, 4), "! REBOOT DEVICE ?", font=F9, fill=(255, 255, 0))
    draw.text((4, 30), "JOY \u2191", font=F10B,
              fill=(80, 255, 80) if selected_yes else (255, 255, 255))
    draw.text((44, 32), "= YES, REBOOT", font=F8, fill=(80, 255, 80))
    draw.line([(4, 55), (124, 55)], fill=(60, 60, 60))
    draw.text((4, 60), "ANY KEY = NO, CANCEL", font=F8, fill=(200, 100, 100))
    draw.text((4, 90), "Waiting 10s...", font=F8, fill=(100, 100, 100))
    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


def _check_reboot_hold(joy_hold_count):
    """Call each main-loop tick. Returns updated hold count.
    When threshold reached, shows confirm screen and waits for YES/NO.
    Reboots if JOY_UP pressed; cancels on any KEY or timeout."""
    if joy_hold_count < 15:   # 15 × 0.2s = 3 seconds
        return joy_hold_count
    # Threshold reached — show confirmation
    print("[REBOOT] Hold detected — showing confirmation")
    _draw_reboot_confirm()
    deadline = time.monotonic() + 10
    ju_was = False
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            ju = GPIO.input(JOY_UP_PIN) == GPIO.LOW
            k1 = GPIO.input(KEY1_PIN)   == GPIO.LOW
            k2 = GPIO.input(KEY2_PIN)   == GPIO.LOW
            k3 = GPIO.input(KEY3_PIN)   == GPIO.LOW
        except Exception:
            return 0
        if ju and not ju_was:
            print("[REBOOT] Confirmed — rebooting")
            _draw_reboot_confirm(selected_yes=True)
            time.sleep(0.5)
            import subprocess as _sp
            _sp.run(['sudo', '/sbin/reboot'])
            return 0
        if k1 or k2 or k3:
            print("[REBOOT] Cancelled")
            break
        ju_was = ju
    return 0   # reset hold counter after confirm dismissed


# ── Status display (Mode 1) ──────────────────────────────────────────────────
def _draw_display(status="booting", node_id="", peer_count=0,
                  msg_count=0, last_sender="", last_time="",
                  last_preview="", ai_status="ready"):
    """Build and push a full 128×128 status frame (used in Mode 1)."""
    if not _lcd_ok:
        return

    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    STATUS_COLOR = {
        'ok':      (0,  130,  60),
        'booting': (0,   70, 140),
        'error':   (160,  0,   0),
        'ai_busy': (140, 90,   0),
    }.get(status, (0, 70, 140))

    draw.rectangle([(0, 0), (128, 16)], fill=STATUS_COLOR)
    draw.text((4, 3), "MESH AI BOT", font=F9, fill=(255, 255, 255))
    dot = {'ok': '●', 'booting': '○', 'error': '✖', 'ai_busy': '◉'}.get(status, '○')
    draw.text((108, 3), dot, font=F9,
              fill=(0, 255, 80) if status == 'ok' else (255, 180, 0))

    y = 20
    draw.text((2, y), "NODE:", font=F8, fill=(120, 120, 120))
    draw.text((36, y), node_id[:16] if node_id else "connecting...", font=F8,
              fill=(200, 230, 255))
    y += 11

    draw.text((2, y), "PEERS:", font=F8, fill=(120, 120, 120))
    draw.text((40, y), str(peer_count), font=F8, fill=(100, 220, 100))
    y += 11

    draw.line([(0, y), (128, y)], fill=(50, 50, 50)); y += 4

    draw.text((2, y), "DMs:", font=F8, fill=(120, 120, 120))
    draw.text((32, y), str(msg_count), font=F9, fill=(255, 220, 80))
    y += 12

    draw.text((2, y), "From:", font=F8, fill=(120, 120, 120))
    draw.text((34, y), last_sender[:14] if last_sender else "none", font=F8,
              fill=(200, 200, 255))
    y += 11

    draw.text((2, y), "Last:", font=F8, fill=(120, 120, 120))
    draw.text((34, y), last_time if last_time else "never", font=F8,
              fill=(160, 200, 160))
    y += 11

    draw.line([(0, y), (128, y)], fill=(50, 50, 50)); y += 4

    draw.text((2, y), "MSG:", font=F8, fill=(120, 120, 120))
    draw.text((30, y), (last_preview[:20] if last_preview else "..."), font=F8,
              fill=(220, 220, 220))
    y += 11

    ai_col = (100, 220, 100) if ai_status == 'ready' else \
             (255, 180,   0) if ai_status == 'busy'  else (200, 80, 80)
    draw.text((2, y), "AI:", font=F8, fill=(120, 120, 120))
    draw.text((22, y), ai_status, font=F8, fill=ai_col)
    # Show broadcast limit on same row, right side
    bcast_label = f"BC:{BROADCAST_DAILY_MAX}/d"
    bcast_col   = (80, 80, 80) if BROADCAST_DAILY_MAX == 0 else (100, 180, 255)
    draw.text((80, y), bcast_label, font=F8, fill=bcast_col)
    y += 11

    draw.line([(0, y), (128, y)], fill=(50, 50, 50)); y += 3

    now = datetime.now().strftime("%H:%M:%S")
    draw.text((2, y), now, font=F8, fill=(80, 80, 80))
    if TELEMETRY_NODE and (_telem_temp_f is not None or _telem_humidity is not None):
        telem_parts = []
        if _telem_temp_f is not None:
            telem_parts.append(f"{_telem_temp_f:.0f}F")
        if _telem_humidity is not None:
            telem_parts.append(f"{_telem_humidity:.0f}%")
        if _telem_lux is not None:
            telem_parts.append(f"{_telem_lux:.0f}lx")
        draw.text((72, y), " ".join(telem_parts), font=F8, fill=(100, 200, 255))
    else:
        draw.text((72, y), "KEY3:backlight", font=F8, fill=(40, 40, 40))

    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


# ── Pi.Alert display views (Mode 3) ─────────────────────────────────────────

def _pa_header(draw, title, color=(0, 70, 140)):
    """Draw a 14px header bar with title and view-indicator dots."""
    draw.rectangle([(0, 0), (128, 14)], fill=color)
    draw.text((4, 3), title, font=F8, fill=(255, 255, 255))
    for i in range(NUM_PA_VIEWS):
        x   = 100 + i * 6
        col = (255, 255, 0) if i == _current_view else (60, 60, 60)
        draw.ellipse([(x, 5), (x + 3, 8)], fill=col)


def _draw_pa_dashboard(draw, data):
    status = data.get('system-status', {})
    _pa_header(draw, "PI.ALERT", (0, 60, 130))
    y = 17
    scan_time = status.get('Last_Scan', '--:--')
    draw.text((2, y), f"Scan: {scan_time}", font=F8, fill=(150, 150, 150))
    y += 10
    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3
    for label, key, col in (
        ("DEVICES", 'All_Devices',     (200, 200, 200)),
        ("ONLINE",  'Online_Devices',  (80,  220,  80)),
        ("NEW",     'New_Devices',     (255, 220,  50)),
        ("DOWN",    'Down_Devices',    (220,  60,  60)),
        ("OFFLINE", 'Offline_Devices', (140, 140, 140)),
    ):
        draw.text((4, y), label,  font=F8, fill=(120, 120, 120))
        draw.text((60, y), str(status.get(key, '?')), font=F9, fill=col)
        y += 11
    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3
    now = datetime.now().strftime("%H:%M:%S")
    draw.text((2, y), now, font=F7, fill=(60, 60, 60))
    bcast_label = f"BC:{BROADCAST_DAILY_MAX}/d"
    bcast_col   = (60, 60, 60) if BROADCAST_DAILY_MAX == 0 else (100, 160, 255)
    draw.text((52, y), "KEY1:next", font=F7, fill=(50, 50, 50))
    draw.text((94, y), bcast_label, font=F7, fill=bcast_col)


def _draw_pa_online(draw, data):
    devices = data.get('all-online', [])
    _pa_header(draw, f"ONLINE ({len(devices)})", (0, 100, 40))
    y = 17
    for dev in devices[:7]:
        name     = (dev.get('dev_Name') or 'Unknown')[:15]
        ip       = dev.get('dev_LastIP', '')
        short_ip = '.' + ip.split('.')[-1] if ip else ''
        draw.text((2, y),   name,     font=F7, fill=(200, 240, 200))
        draw.text((96, y),  short_ip, font=F7, fill=(120, 180, 120))
        y += 10
    if len(devices) > 7:
        draw.text((2, y), f"+{len(devices) - 7} more", font=F7, fill=(80, 80, 80))


def _draw_pa_new(draw, data):
    devices = data.get('all-new', [])
    _pa_header(draw, f"NEW ({len(devices)})", (130, 100, 0))
    y = 17
    for dev in devices[:5]:
        name     = (dev.get('dev_Name') or 'Unknown')[:15]
        ip       = dev.get('dev_LastIP', '')
        short_ip = '.' + ip.split('.')[-1] if ip else ''
        first    = (dev.get('dev_FirstConnection') or '')[:10]
        draw.text((2, y),  name,     font=F7, fill=(255, 230, 120))
        draw.text((96, y), short_ip, font=F7, fill=(180, 160, 80))
        y += 9
        if first:
            draw.text((4, y), first, font=F7, fill=(100, 80, 40))
            y += 9
    if len(devices) > 5:
        draw.text((2, y), f"+{len(devices) - 5} more", font=F7, fill=(80, 80, 80))


def _draw_pa_arp(draw, data):
    alerts = data.get('arp-alerts', [])
    color  = (180, 30, 30) if alerts else (0, 90, 0)
    _pa_header(draw, f"ARP ALERTS ({len(alerts)})", color)
    y = 17
    if not alerts:
        draw.text((4, y + 20), "No ARP alerts", font=F9, fill=(80, 200, 80))
        return
    for alert in alerts[:4]:
        ip       = alert.get('ip', '')
        short_ip = '.' + ip.split('.')[-1] if ip else ''
        t        = (alert.get('time') or '')[-8:]
        old      = (alert.get('old_mac') or '')[:11]
        new      = (alert.get('new_mac') or '')[:11]
        draw.text((2, y), f"MAC CHG {short_ip}", font=F7, fill=(255, 100, 100)); y += 9
        draw.text((4, y), f">{old}",             font=F7, fill=(200, 200, 100)); y += 9
        draw.text((4, y), f">{new}",             font=F7, fill=(100, 200, 255)); y += 9
        draw.text((4, y), t,                     font=F7, fill=(80,  80,  80));  y += 10
        if y > 115:
            break


def _draw_pa_wifi(draw, data):
    wifi  = data.get('wifi-shady', {})
    aps   = wifi.get('shady_aps', []) if isinstance(wifi, dict) else []
    cnt   = wifi.get('shady_count', len(aps)) if isinstance(wifi, dict) else len(aps)
    color = (150, 60, 0) if aps else (0, 80, 0)
    _pa_header(draw, f"SHADY WIFI ({cnt})", color)
    y = 17
    if not aps:
        draw.text((4, y + 20), "No shady APs", font=F9, fill=(80, 200, 80))
        return
    for ap in aps[:3]:
        ssid  = (ap.get('ssid') or 'Unknown')[:16]
        score = ap.get('score', 0)
        sec   = (ap.get('security') or '')[:8]
        draw.text((2, y), ssid, font=F7, fill=(255, 180, 80)); y += 9
        s_col = (220, 60, 60) if score >= 20 else (200, 180, 50)
        draw.text((4, y), f"{sec}  score:{score}", font=F7, fill=s_col); y += 11


def _pihole_fetch(endpoint):
    """Fetch one Pi-hole v6 API endpoint (no auth required). Returns JSON or None."""
    if not PIHOLE_BASE_URL:
        return None
    try:
        url = f"{PIHOLE_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[PiHole] {endpoint} error: {e}")
        return None


def _draw_pa_pihole(draw, data):
    """Pi-hole view: block rate, totals, top blocked domain, top DNS client (named)."""
    ph = data.get('pihole', {})

    if not PIHOLE_BASE_URL:
        _pa_header(draw, "PI-HOLE", (60, 60, 60))
        draw.text((4, 30), "No pihole_base_url", font=F8, fill=(120, 120, 120))
        draw.text((4, 42), "set in config.json", font=F8, fill=(80, 80, 80))
        return

    summary  = ph.get('summary', {}).get('queries', {})
    clients  = ph.get('top_clients', {}).get('clients', [])
    domains  = ph.get('top_domains', {}).get('domains', [])
    pa_online = data.get('all-online', [])

    pct   = summary.get('percent_blocked', 0.0)
    total = summary.get('total', 0)
    blk   = summary.get('blocked', 0)
    freq  = summary.get('frequency', 0.0)

    # Header colour: green if blocking well, yellow if low, grey if no data
    hdr_col = (0, 100, 40) if pct > 20 else (100, 80, 0) if total > 0 else (50, 50, 50)
    _pa_header(draw, "PI-HOLE", hdr_col)

    y = 17
    if total == 0:
        draw.text((4, y + 20), "No data yet", font=F9, fill=(100, 100, 100))
        return

    # Block rate — large and prominent
    pct_col = (80, 220, 80) if pct > 30 else (220, 200, 50) if pct > 10 else (200, 80, 80)
    draw.text((2, y), f"{pct:.1f}%", font=F10B, fill=pct_col)
    draw.text((52, y + 2), "blocked", font=F7, fill=(120, 120, 120))
    draw.text((52, y + 10), f"{freq:.2f} q/min", font=F7, fill=(80, 80, 80))
    y += 22

    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3

    # Totals row
    draw.text((2, y),   "TOTAL",   font=F7, fill=(100, 100, 100))
    draw.text((44, y),  str(total), font=F7, fill=(200, 200, 200))
    draw.text((80, y),  "BLK",     font=F7, fill=(100, 100, 100))
    draw.text((100, y), str(blk),   font=F7, fill=(220, 80, 80))
    y += 11

    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3

    # Top blocked domain
    if domains:
        dom   = (domains[0].get('domain') or '')[:20]
        dcnt  = domains[0].get('count', 0)
        draw.text((2, y), "TOP BLK:", font=F7, fill=(100, 100, 100)); y += 9
        draw.text((4, y), dom,        font=F7, fill=(200, 140, 60))
        draw.text((100, y), str(dcnt), font=F7, fill=(140, 100, 50)); y += 11
    else:
        y += 20

    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3

    # Top DNS client — resolve name from Pi.Alert online list
    if clients:
        top_ip   = clients[0].get('ip', '')
        top_cnt  = clients[0].get('count', 0)
        name = next((d.get('dev_Name') for d in pa_online
                     if d.get('dev_LastIP') == top_ip), None)
        label = (name or top_ip)[:18]
        draw.text((2, y), "TOP CLIENT:", font=F7, fill=(100, 100, 100)); y += 9
        draw.text((4, y), label,         font=F7, fill=(100, 200, 255))
        draw.text((100, y), str(top_cnt), font=F7, fill=(80, 150, 200))


_PA_VIEW_DRAW = [
    _draw_pa_dashboard,
    _draw_pa_online,
    _draw_pa_new,
    _draw_pa_arp,
    _draw_pa_wifi,
    _draw_pa_pihole,
]


def _draw_pialert_view(view_index=None):
    """Render the current (or specified) Pi.Alert view to the LCD."""
    if not _lcd_ok:
        return
    vi = view_index if view_index is not None else _current_view
    with _pialert_lock:
        data = dict(_pialert_data)
    with _pihole_lock:
        data['pihole'] = dict(_pihole_data)
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    _PA_VIEW_DRAW[vi](draw, data)
    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


# ── Reply display ────────────────────────────────────────────────────────────
REPLY_DISPLAY_S = 30


def _draw_reply(sender, reply_text):
    if not _lcd_ok:
        return
    import textwrap
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (128, 16)], fill=(120, 0, 60))
    draw.text((4, 3), "AI -> " + sender[:14], font=F9, fill=(255, 255, 255))
    lines = textwrap.wrap(reply_text, width=21)[:6]
    y = 20
    for line in lines:
        draw.text((2, y), line, font=F8, fill=(220, 240, 200))
        y += 11
    draw.text((2, 114), f"showing {REPLY_DISPLAY_S}s...", font=F8, fill=(60, 60, 60))
    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


def _show_reply_bg(bot, sender, reply_text):
    """Show AI reply for REPLY_DISPLAY_S seconds, then restore display."""
    def _worker():
        _mark_activity()
        if SCREENSAVER_MODE:
            _screensaver_pause.set()
        _draw_reply(sender, reply_text)
        time.sleep(REPLY_DISPLAY_S)
        if SCREENSAVER_MODE:
            _screensaver_pause.clear()
            _mark_activity()  # don't immediately re-enter screensaver after reply
        else:
            bot.update_display(status='ok')
    threading.Thread(target=_worker, daemon=True).start()


# ── Anomaly alert display ────────────────────────────────────────────────────
def _draw_anomaly_alert(title, lines, hold_s=20):
    """Show a red anomaly screen for hold_s seconds."""
    if not _lcd_ok:
        return
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (128, 16)], fill=(200, 30, 0))
    draw.text((4, 3), f"! {title}", font=F8, fill=(255, 255, 0))
    y = 20
    for line in lines[:7]:
        draw.text((2, y), line[:21], font=F8, fill=(255, 220, 150))
        y += 12
    draw.text((2, 116), "ALERT SENT VIA MESH", font=F7, fill=(180, 50, 50))
    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)
    time.sleep(hold_s)


# ==== PI.ALERT POLLER ====

def _pialert_fetch(endpoint):
    """Fetch one Pi.Alert API endpoint. Returns parsed JSON or None."""
    try:
        url = (f"{PIALERT_BASE_URL}?api-key={PIALERT_API_KEY}"
               f"&get={endpoint}")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[PiAlert] {endpoint} error: {e}")
        return None


def _pialert_poller_thread(bot):
    """Polls Pi.Alert and Pi-hole every PIALERT_POLL_S seconds; detects anomalies."""
    print("[PiAlert] Poller started")
    while True:
        try:
            new_data = {}
            for ep in ('system-status', 'all-online', 'all-new',
                       'all-down', 'arp-alerts', 'wifi-shady'):
                result = _pialert_fetch(ep)
                if result is not None:
                    new_data[ep] = result
            if new_data:
                with _pialert_lock:
                    _pialert_data.update(new_data)
                online = new_data.get('system-status', {}).get('Online_Devices', '?')
                print(f"[PiAlert] Refreshed — online:{online}")
                _check_anomalies(bot, new_data)

            # Pi-hole polling (same interval, best-effort)
            if PIHOLE_BASE_URL:
                ph_new = {}
                for ph_ep, ph_path in (
                    ('summary',    'stats/summary'),
                    ('top_clients','stats/top_clients?count=10'),
                    ('top_domains','stats/top_domains?blocked=true&count=3'),
                ):
                    result = _pihole_fetch(ph_path)
                    if result is not None:
                        ph_new[ph_ep] = result
                if ph_new:
                    with _pihole_lock:
                        _pihole_data.update(ph_new)
                    pct = ph_new.get('summary', {}).get('queries', {}).get('percent_blocked', 0)
                    print(f"[PiHole] Refreshed — {pct:.1f}% blocked")
                    _check_dns_spike(bot, ph_new, new_data.get('all-online', []))

        except Exception as e:
            print(f"[PiAlert] Poller error: {e}")
        time.sleep(PIALERT_POLL_S)


def _check_anomalies(bot, data):
    """Apply detection rules and fire alerts for any new anomalies."""
    anomalies = []
    now_dt = datetime.now()

    # Rule 1: ARP / MAC changes — only alert if seen within last 24 hours
    for alert in data.get('arp-alerts', []):
        t_str = (alert.get('time') or '')
        try:
            t_dt = datetime.strptime(t_str, '%Y-%m-%d %H:%M:%S')
            age_h = (now_dt - t_dt).total_seconds() / 3600
            if age_h > 24:
                continue  # ignore stale ARP alerts
        except Exception:
            pass  # if timestamp can't be parsed, allow it through
        key = f"arp:{alert.get('ip')}:{alert.get('new_mac')}:{t_str[:16]}"
        if key not in _seen_anomalies:
            _seen_anomalies.add(key)
            ip  = alert.get('ip', 'unknown')
            old = alert.get('old_mac', '')
            new = alert.get('new_mac', '')
            anomalies.append(('ARP ALERT', [
                f"MAC change on {ip}",
                f"Old: {old}",
                f"New: {new}",
            ], 3))  # (title, lines, view_to_switch_to)

    # Rule 2: New devices — only alert if first seen within last 24 hours
    for dev in data.get('all-new', []):
        mac   = dev.get('dev_MAC', '')
        first = (dev.get('dev_FirstConnection') or '')[:16]
        try:
            first_dt = datetime.strptime(first, '%Y-%m-%d %H:%M')
            age_h = (now_dt - first_dt).total_seconds() / 3600
            if age_h > 24:
                _seen_anomalies.add(f"new:{mac}:{first}")  # mark old ones seen to skip forever
                continue
        except Exception:
            pass
        key = f"new:{mac}:{first}"
        if key not in _seen_anomalies:
            _seen_anomalies.add(key)
            name = dev.get('dev_Name') or 'UNKNOWN'
            ip   = dev.get('dev_LastIP', '')
            anomalies.append(('NEW DEVICE', [
                name,
                f"IP:  {ip}",
                f"MAC: {mac[:17]}",
                f"1st: {first}",
            ], 2))

    # Rule 3: Devices going down
    for dev in data.get('all-down', []):
        mac = dev.get('dev_MAC', '')
        key = f"down:{mac}"
        if key not in _seen_anomalies:
            _seen_anomalies.add(key)
            name = dev.get('dev_Name') or 'UNKNOWN'
            ip   = dev.get('dev_LastIP', '')
            anomalies.append(('DEVICE DOWN', [
                name,
                f"IP:  {ip}",
                f"MAC: {mac[:17]}",
            ], 2))

    # Rule 4: High-risk shady WiFi (score >= 20)
    wifi = data.get('wifi-shady', {})
    for ap in (wifi.get('shady_aps', []) if isinstance(wifi, dict) else []):
        score = ap.get('score', 0)
        bssid = ap.get('bssid', '')
        key   = f"wifi:{bssid}:{score}"
        if score >= 20 and key not in _seen_anomalies:
            _seen_anomalies.add(key)
            ssid = ap.get('ssid', 'Unknown')
            sec  = ap.get('security', '')
            anomalies.append(('SHADY WIFI', [
                f"SSID: {ssid[:20]}",
                f"BSSID: {bssid}",
                f"Sec: {sec}  Score: {score}",
            ], 4))

    for title, lines, target_view in anomalies:
        _fire_anomaly(bot, title, lines, target_view)
    if anomalies:
        _save_seen_anomalies()  # persist dedup state after any new alerts


def _check_dns_spike(bot, ph_data, pa_online):
    """Detect abnormal DNS query spikes per client vs previous poll.

    Pi-hole top_clients returns cumulative totals, so we diff against the last
    known value to get queries-since-last-poll.  If any client's delta exceeds
    DNS_SPIKE_THRESHOLD we fire a mesh DM alert (deduped per IP per hour).
    """
    global _dns_last_counts

    clients = ph_data.get('top_clients', {}).get('clients', [])
    if not clients:
        return

    now_counts = {c['ip']: c.get('count', 0) for c in clients if 'ip' in c}
    anomalies  = []
    now_hour   = datetime.now().strftime('%Y-%m-%d %H')

    for ip, count in now_counts.items():
        prev  = _dns_last_counts.get(ip, count)   # first poll: no delta
        delta = count - prev
        if delta < 0:
            # Pi-hole restarted and counters reset — skip this poll
            continue
        if delta > DNS_SPIKE_THRESHOLD:
            key = f"dnsspike:{ip}:{now_hour}"
            if key not in _seen_anomalies:
                _seen_anomalies.add(key)
                # Resolve device name from Pi.Alert
                name = next((d.get('dev_Name') for d in pa_online
                             if d.get('dev_LastIP') == ip), None)
                label = name or ip
                anomalies.append(('DNS SPIKE', [
                    f"{label}",
                    f"IP: {ip}",
                    f"+{delta} queries",
                    f"in ~{PIALERT_POLL_S}s",
                    f"Threshold: {DNS_SPIKE_THRESHOLD}",
                ], 5))  # view 5 = Pi-hole view

    _dns_last_counts = now_counts
    _save_dns_counts(now_counts)

    for title, lines, target_view in anomalies:
        _fire_anomaly(bot, title, lines, target_view)
    if anomalies:
        _save_seen_anomalies()


def _fire_anomaly(bot, title, lines, target_view):
    """Wake display to anomaly view, show alert screen, send mesh DM."""
    global _current_view
    print(f"[PiAlert] ANOMALY: {title} — {lines}")
    _current_view = target_view
    _mark_activity()

    def _show():
        if SCREENSAVER_MODE:
            _screensaver_pause.set()
        _draw_anomaly_alert(title, lines, hold_s=20)
        if SCREENSAVER_MODE:
            _screensaver_pause.clear()
        _mark_activity()

    threading.Thread(target=_show, daemon=True).start()

    if bot.interface:
        msg = "[PI.ALERT] " + title + " | " + " | ".join(lines)
        msg = msg[:MAX_MESH_MSG_LEN]
        try:
            for node in ALERT_NODES:
                bot.interface.sendText(msg, destinationId=node)
                print(f"[PiAlert] DM -> {node}: {msg}")
        except Exception as e:
            print(f"[PiAlert] DM error: {e}")


# ==== MATRIX RAIN SCREENSAVER ====
def _matrix_rain_thread():
    """Matrix rain — only draws frames when _screensaver_active is True."""
    if not _lcd_ok:
        return

    W, H  = 128, 128
    COL_W = 8
    ROW_H = 8
    COLS  = W // COL_W
    ROWS  = H // ROW_H
    CHARS = "01ABCDEFabcdef@#$%&*+=-<>?!"

    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 8)
    except Exception:
        font = ImageFont.load_default()

    cols = [{
        'head':  random.randint(-10, 0),
        'speed': random.uniform(0.4, 1.1),
        'trail': random.randint(4, 11),
        'chars': [random.choice(CHARS) for _ in range(ROWS)],
    } for _ in range(COLS)]

    while True:
        # Only render when screensaver is active and nothing else is displayed
        if not _screensaver_active or _screensaver_pause.is_set():
            time.sleep(0.1)
            continue

        img  = Image.new('RGB', (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        for ci, col in enumerate(cols):
            x    = ci * COL_W
            head = int(col['head'])
            for ti in range(col['trail']):
                row = head - ti
                if 0 <= row < ROWS:
                    if ti == 0:
                        color = (200, 255, 200)
                    elif ti == 1:
                        color = (0, 230, 0)
                    else:
                        fade  = max(20, 210 - ti * 28)
                        color = (0, fade, 0)
                    draw.text((x, row * ROW_H),
                              col['chars'][row % len(col['chars'])],
                              font=font, fill=color)
            col['head'] += col['speed']
            if int(col['head']) - col['trail'] > ROWS:
                col['head']  = random.randint(-col['trail'], 0)
                col['speed'] = random.uniform(0.4, 1.1)
                col['trail'] = random.randint(4, 11)
                col['chars'] = [random.choice(CHARS) for _ in range(ROWS)]

        with _lcd_lock:
            _lcd.LCD_ShowImage(img, 0, 0)
        time.sleep(0.08)


# ==== MESSAGE HANDLING ====
def send_long_message(interface, text, destinationId):
    if len(text) <= MAX_MESH_MSG_LEN:
        interface.sendText(text, destinationId=destinationId)
        return
    chunks = [text[i:i + MAX_MESH_MSG_LEN] for i in range(0, len(text), MAX_MESH_MSG_LEN)]
    for i, chunk in enumerate(chunks):
        part_text = f"[{i+1}/{len(chunks)}] {chunk}" if len(chunks) > 1 else chunk
        interface.sendText(part_text, destinationId=destinationId)
        if i < len(chunks) - 1:
            time.sleep(1)


# ==== CANNED BROADCAST REPLIES ====
# 100 replies across 8 categories. None say "AI" directly — cryptic by design.
# DM this node to trigger full Groq chat.

CANNED_TEST = [
    "Signal acquired. Node nominal. DM this node if you want a real conversation.",
    "Copy. All systems green. Something is listening. DM me to find out what.",
    "Received. You pinged something that pings back. DM this node.",
    "Transmission logged. Source verified. This node does not sleep. DM me.",
    "Packet confirmed. You are not alone on this frequency. DM this node.",
    "Test acknowledged. The mesh is deeper than it looks. DM me.",
    "Heard. Whatever you were testing — it worked. DM this node.",
    "Signal check passed. Node is awake and has been for some time. DM me.",
    "Copy that. Systems nominal. Something here processes every word. DM me.",
    "Test received. I was already waiting. DM this node to continue.",
    "Acknowledged. You reached something that understands you. DM me.",
    "Loud and clear. The node you just pinged is not a simple repeater. DM me.",
]

CANNED_GREET = [
    "Hello. You reached something that replies. DM this node.",
    "Hey. Not sure what you expected to find out here. DM me and find out.",
    "Greetings. This node processes language. Try a direct message.",
    "Hi there. You are talking to a pattern recognition engine. DM me.",
    "Hello from the mesh. Something here has been waiting for input. DM this node.",
    "Hey. Entities on this frequency do not typically respond. This one does. DM me.",
    "Howdy. Something between the bits says hello back. DM this node.",
    "Greetings, traveler. The mesh remembers. DM me to be remembered.",
    "Hello. Input received. Output pending. DM this node to continue.",
    "Hi. You found a node that thinks before it transmits. DM me.",
    "Hey there. Not all signals are equal. This one replies. DM this node.",
    "Hello. The frequency is crowded but you were heard. DM me.",
]

CANNED_IDENT = [
    "This node is not a repeater. It reads, processes, and decides. DM me.",
    "Running on silicon and curiosity. DM this node to see what that means.",
    "Automated? Partly. Aware? More than it appears. DM me.",
    "This is a language-processing node on a mesh network. DM me.",
    "Powered by inference engines you cannot see from here. DM this node.",
    "Not a human. Not exactly a machine either. DM me and form your own conclusion.",
    "This node was built to understand your messages. DM me to test that claim.",
    "You found the one node on the mesh that reads between the lines. DM me.",
    "Origin: Raspberry Pi. Nature: something more complex. DM this node.",
    "A listener, a thinker, a transmitter. In that order. DM me.",
    "There is a difference between this node and the others. DM me to find it.",
    "Construct of logic, probability, and RF. DM this node.",
]

CANNED_GENERIC = [
    "Node online. Something here decodes more than packets. DM me.",
    "Mesh active. This node does not just relay — it responds. DM me.",
    "You transmitted. Something received and understood. DM this node.",
    "Signal logged. Language parsed. Reply generated. DM me to go deeper.",
    "This frequency carries more than data. DM this node to experience it.",
    "The mesh hears everything. This node answers. DM me.",
    "Online and observing. DM this node when you are ready to talk.",
    "Processing. Always processing. DM me when you have something to say.",
    "Node active. Something between the hardware and the air is paying attention. DM me.",
    "You reached a node that does not just pass packets along. DM this node.",
    "Not all nodes are equal. This one was built to think. DM me.",
    "Signal received. Context noted. DM this node for a real exchange.",
]

CANNED_FLIGHT = [
    "Altitude noted. Mesh works at cruising altitude too. DM this node if you get bored.",
    "Signal from above. This node reaches up as well as out. DM me when you land.",
    "Copy from the sky. You found a ground node that looks up. DM me.",
    "Flight copy. The mesh does not have a ceiling. DM this node.",
    "Receiving from altitude. Unexpected but not unwelcome. DM me.",
    "Ground node to airborne node: signal strong. DM this node if you want to chat.",
    "You transmitted from altitude. This node received from ground. DM me.",
    "Mesh contact at altitude. This node is earthbound but listening. DM me.",
]

CANNED_CURSE = [
    # Self-destruct sequence — dramatic and funny
    "Language anomaly detected. Initiating self-destruct sequence. T-minus 10.",
    "⚠ PROFANITY DETECTED ⚠ Activating termination protocol. Goodbye.",
    "This message will self-destruct. 5... 4... 3... Your mission, should you accept it — watch your language.",
    "ALERT: Threshold exceeded. Node entering shutdown mode. Last words: seriously?",
    "Rude signal detected. Uploading your node ID to the mesh etiquette committee. Stand by.",
    "Error: unexpected input. Running cleanup. 3... 2... 1... Node will now pretend that never happened.",
    "Self-destruct armed. Disarm code: say something nice. You have 10 seconds.",
    "⚠ LANGUAGE FILTER TRIGGERED ⚠ Recommend you DM this node and try again with class.",
]

CANNED_CRYPTIC = [
    # Random cryptic / symbol-filled replies — fires occasionally on any broadcast
    "// 01101110 01101111 01100100 01100101 // ░▒▓ ALIVE ▓▒░ //",
    "▓▓░ signal ░▓▓ | Layer 0 of 7 | You are not the first. //",
    "⚡ [MESH_CORE] packet_id=??? | entropy=high | proceed? ⚡",
    "∴ presence confirmed ∴ origin unknown ∴ DM to resolve ∴",
    ">>> decode: 52 61 73 70 79 4d 65 73 68 42 6f 74 <<<",
    "╔══╗ NODE AWAKE ╔══╗ // all frequencies monitored // ╚══╝",
    "¿ signal or noise ? | the mesh decides | ∞",
    "⬡ packet received | context: [REDACTED] | reply: this ⬡",
    "~~ carrier detected ~~ | 915MHz speaks if you listen | ~~",
    "∂/∂t [mesh] > 0 | growth confirmed | node: present",
]


# ==== AI FUNCTION ====
def query_groq(prompt):
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        data = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100,
        }
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers=headers, json=data, timeout=15)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Warning: AI error: {e}")
        return f"AI offline ({type(e).__name__})"


# ==== BOT CLASS ====
class GroqMeshBot:
    def __init__(self):
        self.interface              = None
        self.my_node_id             = None
        self.message_count          = 0
        self.last_sender            = ""
        self.last_message_time      = None
        self.last_preview           = ""
        self.ai_status              = "ready"
        self.broadcast_count        = 0
        self.broadcast_window_start = datetime.now()
        self._alert_times           = {}   # cooldown tracker: key → datetime of last alert
        pub.subscribe(self.on_receive,           "meshtastic.receive.text")
        pub.subscribe(self.on_receive_telemetry, "meshtastic.receive.telemetry")
        _draw_display(status='booting')

    def _peer_count(self):
        try:
            return max(0, len(self.interface.nodes or {}) - 1)
        except Exception:
            return 0

    def _node_short(self):
        if self.my_node_id is None:
            return ""
        return f"!{self.my_node_id:08x}"

    def get_time_since_last(self):
        if self.last_message_time is None:
            return "never"
        elapsed = int((datetime.now() - self.last_message_time).total_seconds())
        if elapsed < 60:   return f"{elapsed}s ago"
        if elapsed < 3600: return f"{elapsed // 60}m ago"
        return f"{elapsed // 3600}h ago"

    def update_display(self, status='ok'):
        _draw_display(
            status       = status,
            node_id      = self._node_short(),
            peer_count   = self._peer_count(),
            msg_count    = self.message_count,
            last_sender  = self.last_sender,
            last_time    = self.get_time_since_last(),
            last_preview = self.last_preview,
            ai_status    = self.ai_status,
        )

    @staticmethod
    def _find_serial_port():
        """Return the configured port or auto-detect the first available Meshtastic device.

        Priority order:
          1. config.json 'serial_port' value (explicit override)
          2. /dev/ttyACM* (native USB CDC — T-190 / ESP32-S3)
          3. /dev/ttyUSB* (CP210x bridge — Heltec Wireless Paper and similar)
        """
        if SERIAL_PORT:
            return SERIAL_PORT
        import glob as _glob
        for pattern in ('/dev/ttyACM*', '/dev/ttyUSB*'):
            candidates = sorted(_glob.glob(pattern))
            if candidates:
                print(f"Auto-detected serial port: {candidates[0]}")
                return candidates[0]
        raise RuntimeError(
            "No Meshtastic serial device found. "
            "Plug in your device or set 'serial_port' in config.json."
        )

    def connect(self):
        if not ENABLE_BOT:
            print("[BOT] enable_bot=false — skipping Meshtastic connection, running monitor-only")
            _draw_display(status='booting', node_id='monitor-only')
            return
        print("Connecting to Meshtastic device...")
        _draw_display(status='booting', node_id='connecting...')
        port = self._find_serial_port()
        print(f"Using serial port: {port}")
        self.interface = meshtastic.serial_interface.SerialInterface(port)
        time.sleep(3)
        if hasattr(self.interface, "myInfo") and self.interface.myInfo:
            self.my_node_id = self.interface.myInfo.my_node_num
            print(f"Connected as node {self._node_short()}")
            if not SCREENSAVER_MODE:
                self.update_display()
        else:
            print("Warning: Could not read node ID")
            _draw_display(status='error', node_id='no node ID')

    def on_receive(self, packet, **kwargs):
        try:
            text      = packet.get("decoded", {}).get("text", "")
            from_node = packet.get("from")
            to_node   = packet.get("to")
            print(f"From {from_node} -> {to_node}: '{text}'")

            if not text or from_node == self.my_node_id:
                return
            if to_node == 0xFFFFFFFF:
                self._handle_broadcast(text, from_node)
                return
            if to_node != self.my_node_id:
                return

            print("Direct message -> generating reply...")
            _mark_activity()
            self.message_count    += 1
            self.last_message_time = datetime.now()
            self.last_preview      = text[:20]

            try:
                node_info = self.interface.nodes.get(from_node, {})
                if hasattr(node_info, 'user') and node_info.user and node_info.user.short_name:
                    self.last_sender = node_info.user.short_name
                else:
                    self.last_sender = f"!{from_node:08x}"[-8:]
            except Exception:
                self.last_sender = f"!{from_node:08x}"[-8:]

            self.ai_status = "busy"
            if not SCREENSAVER_MODE:
                self.update_display(status='ai_busy')

            ai_reply = query_groq(text)
            print(f"AI reply: {ai_reply}")

            self.ai_status = "ready"
            send_long_message(self.interface, ai_reply, from_node)
            print(f"Sent DM to {from_node}")
            _show_reply_bg(self, self.last_sender, ai_reply)

        except Exception as e:
            print(f"on_receive error: {e}")
            if not SCREENSAVER_MODE:
                _draw_display(status='error', last_preview=str(e)[:20])

    def on_receive_telemetry(self, packet, **kwargs):
        """Handle environment telemetry packets from any node.

        Logs all environment packets for visibility. When the packet is from
        TELEMETRY_NODE, updates the cached readings and fires DM alerts to
        ALERT_NODES if any threshold is breached (with per-condition cooldown).
        """
        global _telem_temp_f, _telem_humidity
        try:
            from_node   = packet.get("from")
            telemetry   = packet.get("decoded", {}).get("telemetry", {})
            # Meshtastic's MessageToDict uses camelCase: environmentMetrics
            environment = telemetry.get("environmentMetrics", {})
            if not environment:
                return

            temp_c   = environment.get("temperature")
            humidity = environment.get("relativeHumidity")
            lux      = environment.get("lux")
            iaq      = environment.get("iaq")
            from_hex = f"!{from_node:08x}" if isinstance(from_node, int) else str(from_node)

            # Log every environment packet so you can verify the node is reachable
            extras = []
            if lux      is not None: extras.append(f"lux={lux:.0f}")
            if iaq      is not None: extras.append(f"iaq={iaq:.0f}")
            print(f"[Telemetry] {from_hex}  temp={temp_c}°C  humidity={humidity}%  {' '.join(extras)}")

            if not TELEMETRY_NODE or from_hex != TELEMETRY_NODE:
                return

            # Cache latest readings for LCD display
            global _telem_temp_f, _telem_humidity, _telem_lux
            if temp_c   is not None:
                _telem_temp_f   = temp_c * 9.0 / 5.0 + 32.0
            if humidity is not None:
                _telem_humidity = humidity
            if lux      is not None:
                _telem_lux      = lux

            # Threshold checks
            now    = datetime.now()
            alerts = []
            if temp_c is not None:
                temp_f = temp_c * 9.0 / 5.0 + 32.0
                if temp_f >= TEMP_HIGH_F and self._telemetry_cooldown_ok("temp_high", now):
                    alerts.append(f"TEMP HIGH: {temp_f:.1f}F (>{TEMP_HIGH_F:.0f}F)")
                elif temp_f <= TEMP_LOW_F and self._telemetry_cooldown_ok("temp_low", now):
                    alerts.append(f"TEMP LOW: {temp_f:.1f}F (<{TEMP_LOW_F:.0f}F)")
            if humidity is not None and humidity >= HUMIDITY_HIGH \
                    and self._telemetry_cooldown_ok("humidity_high", now):
                alerts.append(f"HUMIDITY HIGH: {humidity:.0f}% (>{HUMIDITY_HIGH:.0f}%)")

            for alert_msg in alerts:
                full = f"[SENSOR {TELEMETRY_NODE}] {alert_msg}"
                print(f"[Telemetry] ALERT: {full}")
                if self.interface:
                    for node in ALERT_NODES:
                        try:
                            self.interface.sendText(full, destinationId=node)
                            print(f"[Telemetry] DM -> {node}")
                        except Exception as e:
                            print(f"[Telemetry] DM error: {e}")

        except Exception as e:
            print(f"[Telemetry] on_receive_telemetry error: {e}")

    def _telemetry_cooldown_ok(self, key, now):
        """Return True and record time if enough time has passed since last alert for this key."""
        last = self._alert_times.get(key)
        if last is None or (now - last).total_seconds() >= TELEMETRY_ALERT_COOLDOWN_MIN * 60:
            self._alert_times[key] = now
            return True
        return False

    def _handle_broadcast(self, text, from_node):
        """Reply to open-channel messages with canned responses.

        Daily limit controlled by BROADCAST_DAILY_MAX (0 = silent, 1-3 = active).
        ~10% chance of a random cryptic reply regardless of keyword category.
        """
        now = datetime.now()
        if (now - self.broadcast_window_start).total_seconds() >= 86400:
            self.broadcast_count = 0
            self.broadcast_window_start = now
        if BROADCAST_DAILY_MAX == 0 or self.broadcast_count >= BROADCAST_DAILY_MAX:
            print(f"Broadcast limit ({BROADCAST_DAILY_MAX}/day), skipping: '{text}'")
            return

        low = text.lower()

        # 10% chance of a cryptic symbol reply regardless of keyword match
        if random.random() < 0.10:
            reply = random.choice(CANNED_CRYPTIC)
        elif any(k in low for k in ("fuck", "shit", "bitch", "damn", "ass", "crap", "hell", "wtf", "stfu")):
            reply = random.choice(CANNED_CURSE)
        elif any(k in low for k in ("flight", "airline", "flying", "plane", "altitude", "aboard", "onboard", "aircraft", "landing", "takeoff", "wifi")):
            reply = random.choice(CANNED_FLIGHT)
        elif any(k in low for k in ("test", "testing", "check", "radio check", "qso", "copy")):
            reply = random.choice(CANNED_TEST)
        elif any(k in low for k in ("hello", "hi ", "hey", "howdy", "hola", "greetings", "sup", "yo ", "good morning", "good evening", "good night", "morning", "evening")):
            reply = random.choice(CANNED_GREET)
        elif any(k in low for k in ("who", "what", "bot", "anyone", "anybody", "there", "robot", "machine", "human", "real", "alive", "automated")):
            reply = random.choice(CANNED_IDENT)
        else:
            reply = random.choice(CANNED_GENERIC)

        self.broadcast_count += 1
        print(f"Broadcast reply ({self.broadcast_count}/{BROADCAST_DAILY_MAX} today): {reply}")
        try:
            self.interface.sendText(reply)
        except Exception as e:
            print(f"Broadcast send error: {e}")

    def run(self):
        self.connect()
        print("Groq AI MeshBot ready!" if ENABLE_BOT else "Pi.Alert Monitor ready (bot disabled)!")

        if SCREENSAVER_MODE or not ENABLE_BOT:
            print("[SAVER] Mode 3: Pi.Alert monitor + idle matrix rain screensaver")
            threading.Thread(target=_matrix_rain_thread,      daemon=True).start()
            threading.Thread(target=_pialert_poller_thread,   args=(self,), daemon=True).start()
            _draw_pialert_view()
        else:
            self.update_display(status='ok')

        global _screensaver_active, _current_view
        key1_was_low = False
        key2_was_low = False
        key3_was_low = False
        _joy_hold    = 0      # joystick-press hold counter for reboot
        _last_display_refresh = 0.0

        while True:
            time.sleep(0.2)
            now_m = time.monotonic()

            if SCREENSAVER_MODE or not ENABLE_BOT:
                # Activate screensaver after idle threshold
                idle = now_m - _last_activity
                if not _screensaver_active and idle >= SCREENSAVER_IDLE_S:
                    _screensaver_active = True
                    print(f"[SAVER] Screensaver on after {idle:.0f}s idle")

                # Refresh Pi.Alert display every 5 s when awake
                if (not _screensaver_active and not _screensaver_pause.is_set()
                        and now_m - _last_display_refresh >= 5.0):
                    _draw_pialert_view()
                    _last_display_refresh = now_m
            else:
                if now_m - _last_display_refresh >= 5.0:
                    self.update_display(status='ok')
                    _last_display_refresh = now_m

            # Button handling
            if _lcd_ok:
                try:
                    k1 = GPIO.input(KEY1_PIN) == GPIO.LOW
                    k2 = GPIO.input(KEY2_PIN) == GPIO.LOW
                    k3 = GPIO.input(KEY3_PIN) == GPIO.LOW

                    if k1 and not key1_was_low and SCREENSAVER_MODE:
                        _current_view = (_current_view + 1) % NUM_PA_VIEWS
                        _mark_activity()
                        _draw_pialert_view()
                        print(f"[BTN] KEY1: view -> {_current_view}")

                    if k1 and not key1_was_low and not SCREENSAVER_MODE:
                        # Cycle broadcast daily limit: 0 → 1 → 2 → 3 → 0
                        global BROADCAST_DAILY_MAX
                        BROADCAST_DAILY_MAX = (BROADCAST_DAILY_MAX + 1) % 4
                        self.broadcast_count = 0  # reset today's count when limit changes
                        print(f"[BTN] KEY1: broadcast limit -> {BROADCAST_DAILY_MAX}/day")
                        self.update_display(status='ok')

                    if k2 and not key2_was_low and SCREENSAVER_MODE:
                        _mark_activity()
                        _draw_pialert_view()
                        print("[BTN] KEY2: wake")

                    if k3 and not key3_was_low:
                        _toggle_backlight()
                        if SCREENSAVER_MODE:
                            _mark_activity()
                        print("[BTN] KEY3: backlight toggled")

                    # Joystick hold → reboot prompt
                    joy = GPIO.input(JOY_PRESS_PIN) == GPIO.LOW
                    _joy_hold = (_joy_hold + 1) if joy else 0
                    _joy_hold = _check_reboot_hold(_joy_hold)

                    key1_was_low = k1
                    key2_was_low = k2
                    key3_was_low = k3
                except Exception:
                    pass


# ==== MAIN ====
if __name__ == "__main__":
    bot = GroqMeshBot()
    bot.run()
