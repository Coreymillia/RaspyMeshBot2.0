#!/usr/bin/env python3
"""
Groq AI MeshBot with Waveshare 1.44" LCD HAT Display
-----------------------------------------------------
Mode 1 (default): Live mesh status screen on LCD.
Mode 3 (SCREENSAVER_MODE): Main LCD UI with Pi.Alert dashboard views,
  selectable matrix rain, NWS forecast, and message history.
  Pi.Alert anomalies still wake the relevant view and send DMs.

Mode 3 button layout (HAT buttons, active LOW):
  KEY1 (BCM 21): cycle Mode 3 views
  KEY2 (BCM 20): jump back to Pi.Alert dashboard
  KEY3 (BCM 16): toggle backlight
"""

from pubsub import pub
from datetime import datetime, timedelta, time as dt_time

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

import time, sys, os, threading, random, textwrap
import meshtastic
import meshtastic.serial_interface
import requests
from PIL import Image, ImageDraw, ImageFont
import collections
from http.server import HTTPServer, BaseHTTPRequestHandler

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
PIALERT_BASE_URL   = _c.get('pialert_base_url', 'http://192.168.0.103/pialert/api/')
PIALERT_API_KEY    = _c.get('pialert_api_key', '')
PIALERT_POLL_S     = 60    # seconds between Pi.Alert polls
_alert_raw  = _c.get('alert_node', '!changeme')
ALERT_NODES = _alert_raw if isinstance(_alert_raw, list) else [_alert_raw]
SERIAL_PORT        = _c.get('serial_port', None)   # None = auto-detect
BROADCAST_DAILY_MAX = _c.get('broadcast_daily_max', 3)  # 0=silent, 1-3 broadcast replies/day

# Pi-hole integration — v6 requires password auth (POST /api/auth → session sid)
PIHOLE_BASE_URL       = _c.get('pihole_base_url', '')   # e.g. 'http://192.168.0.103:8080/api'
PIHOLE_PASSWORD       = _c.get('pihole_password', '')   # Pi-hole admin password
DNS_SPIKE_THRESHOLD   = _c.get('dns_spike_threshold', 300)  # queries/poll above baseline = alert
ENABLE_BOT            = _c.get('enable_bot', True)   # False = Pi.Alert/Pi-hole monitor only, no Groq/Meshtastic
NWS_LATITUDE          = str(_c.get('nws_latitude', '')).strip()
NWS_LONGITUDE         = str(_c.get('nws_longitude', '')).strip()
NWS_REFRESH_S         = 900
SCHEDULED_TEST_ENABLED        = bool(_c.get('scheduled_test_enabled', False))
SCHEDULED_TEST_MIN_DAYS       = 3
SCHEDULED_TEST_MAX_DAYS       = 7
SCHEDULED_TEST_START_HOUR     = 8
SCHEDULED_TEST_END_HOUR       = 20
SCHEDULED_TEST_ACK_WINDOW_MIN = 180
SCHEDULED_TEST_POLL_S         = 60

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
KEY1_PIN      = 21   # cycle Mode 3 view
KEY2_PIN      = 20   # jump to dashboard
KEY3_PIN      = 16   # backlight toggle (all modes)
JOY_PRESS_PIN = 13   # hold 3s = reboot prompt
JOY_UP_PIN    = 6    # confirm reboot (YES)
JOY_DOWN_PIN  = 19
JOY_LEFT_PIN  = 5
JOY_RIGHT_PIN = 26

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
    GPIO.setup(JOY_DOWN_PIN,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(JOY_LEFT_PIN,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(JOY_RIGHT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    _lcd_ok = True
    print("[HAT] Waveshare 1.44\" LCD initialised")
except Exception as _e:
    print(f"[HAT] LCD init failed: {_e}  — display-less mode")

# ==== RGB LED STATUS INDICATOR ====
# GPIO 17 = Red, 27 = Green, 22 = Blue  (common-cathode, 220Ω resistors)
# Runs as a background thread; call rgb_set_mode() to change state from anywhere.

_RGB_RED   = 17
_RGB_GREEN = 27
_RGB_BLUE  = 22

_rgb_ok   = False
_rgb_mode = "boot"        # current mode name
_rgb_lock = threading.Lock()

# Mode definitions: (r, g, b, style, period_s)
# style: "solid" | "breathe" | "blink" | "fast_blink" | "pulse"
_RGB_MODES = {
    "boot":        (100,  50,   0, "breathe",    2.0),   # warm amber breathe on startup
    "normal":      (  0, 100,   0, "breathe",    4.0),   # slow green breathe = all good
    "message":     (  0,   0, 100, "pulse",      0.3),   # quick blue flash = incoming DM
    "anomaly":     (100,  40,   0, "fast_blink", 0.2),   # orange fast blink = anomaly
    "offline":     (100,   0,   0, "blink",      0.6),   # red blink = Pi.Alert offline
    "ai_busy":     (100, 100,   0, "breathe",    1.0),   # yellow breathe = AI thinking
    "error":       (100,   0,   0, "solid",      0.0),   # solid red = hard error
}

def rgb_set_mode(mode, hold_s=0):
    """Switch RGB LED to named mode. If hold_s > 0, revert to 'normal' after hold_s seconds."""
    global _rgb_mode
    if not _rgb_ok:
        return
    with _rgb_lock:
        _rgb_mode = mode
    if hold_s > 0:
        def _revert():
            global _rgb_mode
            time.sleep(hold_s)
            with _rgb_lock:
                if _rgb_mode == mode:
                    _rgb_mode = "normal"
        threading.Thread(target=_revert, daemon=True).start()

def _rgb_thread():
    """Background thread driving the RGB LED based on _rgb_mode."""
    import math
    t = 0.0
    while True:
        with _rgb_lock:
            mode = _rgb_mode
        cfg = _RGB_MODES.get(mode, _RGB_MODES["normal"])
        r_max, g_max, b_max, style, period = cfg

        if style == "solid":
            _rgb_pwm_set(r_max, g_max, b_max)
            time.sleep(0.05)

        elif style == "breathe":
            # Sine wave 0→1→0 over period
            brightness = (math.sin(math.pi * t / period) ** 2)
            _rgb_pwm_set(r_max * brightness, g_max * brightness, b_max * brightness)
            t += 0.05
            if t >= period:
                t = 0.0
            time.sleep(0.05)

        elif style == "blink":
            on = (t % period) < (period / 2)
            _rgb_pwm_set(r_max if on else 0, g_max if on else 0, b_max if on else 0)
            t += 0.05
            if t >= period:
                t = 0.0
            time.sleep(0.05)

        elif style == "fast_blink":
            on = (t % period) < (period / 2)
            _rgb_pwm_set(r_max if on else 0, g_max if on else 0, b_max if on else 0)
            t += 0.05
            if t >= period:
                t = 0.0
            time.sleep(0.05)

        elif style == "pulse":
            # Single sharp flash then off for the rest of the period
            flash = (t % period) < 0.1
            _rgb_pwm_set(r_max if flash else 0, g_max if flash else 0, b_max if flash else 0)
            t += 0.05
            if t >= period:
                t = 0.0
            time.sleep(0.05)

        # Reset phase counter when mode changes
        with _rgb_lock:
            if _rgb_mode != mode:
                t = 0.0

def _rgb_pwm_set(r, g, b):
    try:
        _rgb_red_pwm.ChangeDutyCycle(max(0, min(100, r)))
        _rgb_grn_pwm.ChangeDutyCycle(max(0, min(100, g)))
        _rgb_blu_pwm.ChangeDutyCycle(max(0, min(100, b)))
    except Exception:
        pass

if _c.get('enable_rgb_led', False):
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(_RGB_RED,   GPIO.OUT)
        GPIO.setup(_RGB_GREEN, GPIO.OUT)
        GPIO.setup(_RGB_BLUE,  GPIO.OUT)
        _rgb_red_pwm = GPIO.PWM(_RGB_RED,   1000)
        _rgb_grn_pwm = GPIO.PWM(_RGB_GREEN, 1000)
        _rgb_blu_pwm = GPIO.PWM(_RGB_BLUE,  1000)
        _rgb_red_pwm.start(0)
        _rgb_grn_pwm.start(0)
        _rgb_blu_pwm.start(0)
        _rgb_ok = True
        threading.Thread(target=_rgb_thread, daemon=True).start()
        print("[RGB] LED indicator initialised (GPIO 17/27/22)")
    except Exception as _rgb_e:
        print(f"[RGB] LED init failed: {_rgb_e} — continuing without RGB")
else:
    print("[RGB] LED disabled (set enable_rgb_led: true in config.json to enable)")

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

SCREENSAVER_MODE   = os.path.exists('/tmp/meshbot_screensaver')
MODE3_ACTIVE       = SCREENSAVER_MODE or not ENABLE_BOT
_screensaver_pause = threading.Event()  # set = force-show reply/anomaly screen
_last_activity     = time.monotonic()

# Pi.Alert shared state
_pialert_data     = {}
_pialert_lock     = threading.Lock()
PA_VIEW_DASHBOARD = 0
PA_VIEW_ONLINE    = 1
PA_VIEW_NEW       = 2
PA_VIEW_ARP       = 3
PA_VIEW_WIFI      = 4
PA_VIEW_PIHOLE    = 5
PA_VIEW_MATRIX    = 6
PA_VIEW_NWS       = 7
PA_VIEW_MESSAGES  = 8
PA_VIEW_TEST_TX   = 9
_current_view     = PA_VIEW_DASHBOARD
NUM_PA_VIEWS      = 10
_matrix_active    = False
_seen_anomalies   = set()

# Pi-hole shared state
_pihole_data    = {}        # latest Pi-hole data keyed by endpoint
_pihole_lock    = threading.Lock()

# NWS shared state
_nws_data      = {}
_nws_lock      = threading.Lock()

# Mesh TX/RX history
_MSG_HISTORY_MAX   = 30
_msg_history       = collections.deque(maxlen=_MSG_HISTORY_MAX)
_msg_history_lock  = threading.Lock()
_msg_selected_idx  = 0
_msg_scroll_offset = 0

# Manual test sender state
_manual_test_selected_idx = 0
_manual_test_status = ""
_manual_test_status_until = 0.0

# DNS spike detection — tracks per-client cumulative query counts between polls
_DNS_COUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dns_counts.json')
_dns_last_counts = {}   # {ip: count} from previous poll

# Persistent anomaly dedup file — survives reboots
_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.seen_anomalies.json')
_SCHEDULED_TEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.scheduled_mesh_test.json')

# ==== CYD BOT REPLIES SERVER ====
_CYD_PORT         = 8766
_CYD_MAX_MSGS     = 20
_cyd_msgs         = collections.deque(maxlen=_CYD_MAX_MSGS)
_cyd_msg_lock     = threading.Lock()
_cyd_total_count  = 0    # monotonic counter so CYD detects new messages


def _push_cyd_msg(msg_type: str, recipient: str, text: str):
    """Append a message to the CYD ring buffer."""
    global _cyd_total_count
    ts = time.strftime("%H:%M:%S")
    entry = {"type": msg_type, "to": recipient, "text": text[:255], "ts": ts}
    with _cyd_msg_lock:
        _cyd_msgs.appendleft(entry)   # newest at index 0
        _cyd_total_count += 1


class _CYDHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/messages":
            self.send_response(404)
            self.end_headers()
            return
        with _cyd_msg_lock:
            payload = _json.dumps({
                "count":    _cyd_total_count,
                "messages": list(_cyd_msgs),
            })
        body = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass   # suppress HTTP access logs


def _start_cyd_server():
    """Launch the CYD HTTP server in a daemon thread."""
    def _run():
        try:
            srv = HTTPServer(("0.0.0.0", _CYD_PORT), _CYDHandler)
            print(f"[CYD] HTTP server on port {_CYD_PORT}")
            srv.serve_forever()
        except Exception as exc:
            print(f"[CYD] Server error: {exc}")
    t = threading.Thread(target=_run, daemon=True, name="cyd-server")
    t.start()


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
    """Track recent interaction so transient overlays count as activity."""
    global _last_activity
    _last_activity = time.monotonic()


def _set_current_view(view_index):
    global _current_view, _matrix_active, _msg_scroll_offset
    _current_view = view_index % NUM_PA_VIEWS
    _matrix_active = (_current_view == PA_VIEW_MATRIX)
    if _current_view != PA_VIEW_MESSAGES:
        _msg_scroll_offset = 0


def _show_current_view():
    if not _lcd_ok or not MODE3_ACTIVE:
        return
    if _current_view == PA_VIEW_MATRIX:
        return
    _draw_pialert_view()


def _node_label(interface, node_id):
    if node_id is None:
        return "unknown"
    if node_id == 0xFFFFFFFF:
        return "open mesh"
    if not isinstance(node_id, int):
        return str(node_id)
    try:
        node_info = (interface.nodes or {}).get(node_id, {}) if interface else {}
        user = getattr(node_info, 'user', None)
        short_name = getattr(user, 'short_name', '') if user else ''
        long_name = getattr(user, 'long_name', '') if user else ''
        if short_name:
            return short_name[:16]
        if long_name:
            return long_name[:16]
    except Exception:
        pass
    return f"!{node_id:08x}"[-8:]


def _log_mesh_message(direction, msg_kind, peer, text):
    global _msg_selected_idx, _msg_scroll_offset
    if not text:
        return
    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "direction": direction,
        "kind": msg_kind,
        "peer": (peer or "unknown")[:20],
        "text": text[:600],
    }
    with _msg_history_lock:
        _msg_history.appendleft(entry)
        if _msg_selected_idx == 0:
            _msg_scroll_offset = 0
        else:
            _msg_selected_idx = min(_msg_selected_idx + 1, max(0, len(_msg_history) - 1))


def _message_snapshot():
    global _msg_selected_idx
    with _msg_history_lock:
        items = list(_msg_history)
    if not items:
        _msg_selected_idx = 0
        return [], None
    _msg_selected_idx = max(0, min(_msg_selected_idx, len(items) - 1))
    return items, items[_msg_selected_idx]


def _change_message(delta):
    global _msg_selected_idx, _msg_scroll_offset
    with _msg_history_lock:
        if not _msg_history:
            return False
        _msg_selected_idx = max(0, min(_msg_selected_idx + delta, len(_msg_history) - 1))
    _msg_scroll_offset = 0
    return True


def _scroll_message(delta):
    global _msg_scroll_offset
    items, entry = _message_snapshot()
    if not entry:
        return False
    body_lines = textwrap.wrap(entry["text"], width=20) or [""]
    max_offset = max(0, len(body_lines) - 5)
    new_offset = max(0, min(_msg_scroll_offset + delta, max_offset))
    if new_offset == _msg_scroll_offset:
        return False
    _msg_scroll_offset = new_offset
    return True


def _manual_test_message():
    if not CANNED_MANUAL_TEST:
        return ""
    return CANNED_MANUAL_TEST[_manual_test_selected_idx % len(CANNED_MANUAL_TEST)]


def _change_manual_test(delta):
    global _manual_test_selected_idx, _manual_test_status
    if not CANNED_MANUAL_TEST:
        return False
    _manual_test_selected_idx = (_manual_test_selected_idx + delta) % len(CANNED_MANUAL_TEST)
    _manual_test_status = ""
    return True


def _set_manual_test_status(text, hold_s=3.0):
    global _manual_test_status, _manual_test_status_until
    _manual_test_status = text[:24]
    _manual_test_status_until = time.monotonic() + hold_s


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
    start_x = 128 - (NUM_PA_VIEWS * 4) - 4
    for i in range(NUM_PA_VIEWS):
        x   = start_x + i * 4
        col = (255, 255, 0) if i == _current_view else (60, 60, 60)
        draw.ellipse([(x, 5), (x + 2, 7)], fill=col)


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


_pihole_sid_cache = {'sid': None, 'expires': 0}

def _pihole_get_sid():
    """Return a valid Pi-hole v6 session token, re-authenticating if needed."""
    import time
    if not PIHOLE_BASE_URL or not PIHOLE_PASSWORD:
        return None
    now = time.time()
    if _pihole_sid_cache['sid'] and now < _pihole_sid_cache['expires']:
        return _pihole_sid_cache['sid']
    try:
        auth_url = f"{PIHOLE_BASE_URL.rstrip('/')}/auth"
        r = requests.post(auth_url, json={'password': PIHOLE_PASSWORD}, timeout=8)
        r.raise_for_status()
        sid = r.json().get('session', {}).get('sid')
        if sid:
            _pihole_sid_cache['sid'] = sid
            _pihole_sid_cache['expires'] = now + 1500  # refresh before 30-min expiry
            print("[PiHole] Auth OK — new session token acquired")
        return sid
    except Exception as e:
        print(f"[PiHole] Auth error: {e}")
        return None


def _pihole_fetch(endpoint):
    """Fetch one Pi-hole v6 API endpoint with session-token auth. Returns JSON or None."""
    if not PIHOLE_BASE_URL:
        return None
    try:
        url = f"{PIHOLE_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {}
        sid = _pihole_get_sid()
        if sid:
            headers['sid'] = sid
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 401:
            # Token expired — force re-auth and retry once
            _pihole_sid_cache['sid'] = None
            _pihole_sid_cache['expires'] = 0
            sid = _pihole_get_sid()
            if sid:
                headers['sid'] = sid
            r = requests.get(url, headers=headers, timeout=8)
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


def _draw_pa_matrix(draw, data):
    _pa_header(draw, "MATRIX", (0, 90, 40))
    draw.text((8, 30), "Matrix rain is", font=F9, fill=(170, 220, 170))
    draw.text((8, 44), "now a normal", font=F9, fill=(120, 255, 120))
    draw.text((8, 58), "KEY1 view.", font=F9, fill=(120, 255, 120))
    draw.line([(0, 78), (128, 78)], fill=(40, 40, 40))
    draw.text((8, 88), "KEY1 next view", font=F8, fill=(100, 100, 100))
    draw.text((8, 100), "KEY2 dashboard", font=F8, fill=(100, 100, 100))
    draw.text((8, 112), "KEY3 backlight", font=F8, fill=(100, 100, 100))


def _draw_pa_nws(draw, data):
    with _nws_lock:
        nws = dict(_nws_data)
    _pa_header(draw, "NWS FORECAST", (0, 80, 120))
    y = 17
    if not NWS_LATITUDE or not NWS_LONGITUDE:
        draw.text((4, y + 10), "Set nws_latitude", font=F8, fill=(140, 140, 140))
        draw.text((4, y + 22), "and nws_longitude", font=F8, fill=(140, 140, 140))
        draw.text((4, y + 34), "in Settings", font=F8, fill=(100, 100, 100))
        return
    if not nws:
        draw.text((4, y + 20), "Loading forecast...", font=F8, fill=(140, 140, 140))
        return
    if nws.get("error"):
        draw.text((4, y), "Forecast error", font=F8, fill=(220, 120, 120)); y += 12
        for line in textwrap.wrap(nws["error"], width=22)[:6]:
            draw.text((4, y), line, font=F7, fill=(140, 140, 140))
            y += 9
        return

    location = nws.get("location", "")[:20]
    periods = nws.get("periods", [])
    if location:
        draw.text((2, y), location, font=F8, fill=(160, 220, 255))
        y += 10
        draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 3

    for period in periods[:2]:
        name = (period.get("name") or "")[:10]
        temp = period.get("temperature", "?")
        unit = period.get("temperatureUnit", "")
        short = period.get("shortForecast", "")
        wind = period.get("windSpeed", "")
        draw.text((2, y), f"{name}", font=F8, fill=(255, 220, 120))
        draw.text((76, y), f"{temp}{unit}", font=F8, fill=(100, 220, 255))
        y += 10
        if wind:
            draw.text((4, y), wind[:20], font=F7, fill=(100, 100, 100))
            y += 8
        for line in textwrap.wrap(short, width=21)[:2]:
            draw.text((4, y), line, font=F7, fill=(200, 200, 200))
            y += 8
        y += 2
        if y > 110:
            break


def _draw_pa_messages(draw, data):
    items, entry = _message_snapshot()
    _pa_header(draw, "MESSAGE LOG", (80, 40, 100))
    y = 17
    if not entry:
        draw.text((4, y + 16), "No mesh traffic yet", font=F9, fill=(150, 150, 150))
        draw.text((4, y + 32), "DM and open-mesh", font=F8, fill=(100, 100, 100))
        draw.text((4, y + 43), "traffic will appear", font=F8, fill=(100, 100, 100))
        draw.text((4, y + 54), "here automatically.", font=F8, fill=(100, 100, 100))
        return

    total = len(items)
    pos = _msg_selected_idx + 1
    dir_label = "TX" if entry["direction"] == "tx" else "RX"
    kind_label = "OPEN" if entry["kind"] == "bcast" else "DM"
    draw.text((2, y), f"{dir_label} {kind_label}", font=F8, fill=(255, 220, 120))
    draw.text((84, y), f"{pos}/{total}", font=F8, fill=(120, 120, 120))
    y += 10
    draw.text((2, y), entry["peer"][:18], font=F8, fill=(150, 220, 255))
    draw.text((96, y), entry["ts"], font=F7, fill=(90, 90, 90))
    y += 10
    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 4

    body_lines = textwrap.wrap(entry["text"], width=20) or [""]
    visible = body_lines[_msg_scroll_offset:_msg_scroll_offset + 5]
    for line in visible:
        draw.text((2, y), line, font=F8, fill=(220, 220, 220))
        y += 11
    max_offset = max(0, len(body_lines) - 5)
    if max_offset:
        draw.text((2, 115), f"U/D:{_msg_scroll_offset}/{max_offset}", font=F7, fill=(80, 80, 80))
    draw.text((56, 115), "L/R msg", font=F7, fill=(80, 80, 80))


def _draw_pa_test_tx(draw, data):
    global _manual_test_status
    _pa_header(draw, "MESH TEST TX", (0, 90, 110))
    y = 17
    if not ENABLE_BOT:
        draw.text((4, y + 18), "Bot disabled in", font=F8, fill=(160, 160, 160))
        draw.text((4, y + 30), "config.json", font=F8, fill=(160, 160, 160))
        draw.text((4, y + 48), "Enable Mesh Bot", font=F8, fill=(100, 200, 255))
        draw.text((4, y + 60), "to send tests.", font=F8, fill=(100, 200, 255))
        return

    msg = _manual_test_message()
    idx = _manual_test_selected_idx + 1 if CANNED_MANUAL_TEST else 0
    total = len(CANNED_MANUAL_TEST)
    draw.text((2, y), f"{idx}/{total}", font=F8, fill=(255, 220, 120))
    draw.text((44, y), "Manual sender", font=F8, fill=(150, 220, 255))
    y += 10
    draw.line([(0, y), (128, y)], fill=(40, 40, 40)); y += 4

    for line in textwrap.wrap(msg, width=19)[:5]:
        draw.text((4, y), line, font=F9, fill=(220, 220, 220))
        y += 12

    if time.monotonic() > _manual_test_status_until:
        _manual_test_status = ""
    if _manual_test_status:
        draw.text((4, 102), _manual_test_status, font=F8, fill=(100, 220, 120))

    draw.text((2, 115), "L/R pick", font=F7, fill=(80, 80, 80))
    draw.text((48, 115), "KEY2 send", font=F7, fill=(80, 80, 80))


_PA_VIEW_DRAW = [
    _draw_pa_dashboard,
    _draw_pa_online,
    _draw_pa_new,
    _draw_pa_arp,
    _draw_pa_wifi,
    _draw_pa_pihole,
    _draw_pa_matrix,
    _draw_pa_nws,
    _draw_pa_messages,
    _draw_pa_test_tx,
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
        if MODE3_ACTIVE:
            _screensaver_pause.set()
        _draw_reply(sender, reply_text)
        time.sleep(REPLY_DISPLAY_S)
        if MODE3_ACTIVE:
            _screensaver_pause.clear()
            _mark_activity()  # don't immediately re-enter screensaver after reply
            _show_current_view()
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
    # Force the first NWS fetch immediately even on a freshly booted Pi where
    # monotonic() may still be below NWS_REFRESH_S.
    last_nws_fetch = time.monotonic() - NWS_REFRESH_S
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
                rgb_set_mode("normal")
                _check_anomalies(bot, new_data)
            else:
                rgb_set_mode("offline")

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

            now_m = time.monotonic()
            if NWS_LATITUDE and NWS_LONGITUDE and (now_m - last_nws_fetch >= NWS_REFRESH_S):
                latest_nws = _nws_fetch()
                with _nws_lock:
                    _nws_data.clear()
                    _nws_data.update(latest_nws)
                last_nws_fetch = now_m

        except Exception as e:
            print(f"[PiAlert] Poller error: {e}")
        time.sleep(PIALERT_POLL_S)


def _nws_fetch():
    headers = {
        "User-Agent": "Meshtastic-PiAgent (github.com/Coreymillia/Meshtastic-PiAgent)",
        "Accept": "application/geo+json",
    }
    try:
        points_url = f"https://api.weather.gov/points/{NWS_LATITUDE},{NWS_LONGITUDE}"
        points_r = requests.get(points_url, headers=headers, timeout=10)
        points_r.raise_for_status()
        props = points_r.json().get("properties", {})
        forecast_url = props.get("forecast")
        rel_props = props.get("relativeLocation", {}).get("properties", {})
        if not forecast_url:
            raise RuntimeError("NWS forecast URL missing from points response")

        forecast_r = requests.get(forecast_url, headers=headers, timeout=10)
        forecast_r.raise_for_status()
        periods = forecast_r.json().get("properties", {}).get("periods", [])
        city = rel_props.get("city", "")
        state = rel_props.get("state", "")
        location = ", ".join(part for part in (city, state) if part)
        print(f"[NWS] Forecast refreshed for {NWS_LATITUDE},{NWS_LONGITUDE}")
        return {"location": location, "periods": periods[:4], "updated": time.time()}
    except Exception as e:
        print(f"[NWS] forecast error: {e}")
        return {"error": str(e), "updated": time.time()}


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
    print(f"[PiAlert] ANOMALY: {title} — {lines}")
    _set_current_view(target_view)
    _mark_activity()
    rgb_set_mode("anomaly", hold_s=30)

    def _show():
        if MODE3_ACTIVE:
            _screensaver_pause.set()
        _draw_anomaly_alert(title, lines, hold_s=20)
        if MODE3_ACTIVE:
            _screensaver_pause.clear()
            _show_current_view()
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
    _push_cyd_msg("system", ",".join(str(n) for n in ALERT_NODES), msg if bot.interface else ("[PI.ALERT] " + title + " | " + " | ".join(lines)))


# ==== MATRIX RAIN SCREENSAVER ====
def _matrix_rain_thread():
    """Matrix rain — only draws frames when the matrix view is selected."""
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
        if not _matrix_active or _screensaver_pause.is_set():
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
# Open-channel acknowledgements and flavor replies.

CANNED_TEST = [
    "Test received from Victor, Colorado.",
    "Test acknowledged from Victor, Colorado.",
    "Radio check confirmed from Victor, Colorado.",
    "Copy on your test from Victor, Colorado.",
    "Test heard loud and clear from Victor, Colorado.",
    "Signal check passed from Victor, Colorado.",
    "Your test came through from Victor, Colorado.",
    "Transmission confirmed from Victor, Colorado.",
    "Packet received from Victor, Colorado.",
    "Test copied from Victor, Colorado.",
    "Acknowledged from Victor, Colorado.",
    "Loud and clear from Victor, Colorado.",
]

CANNED_GREET = [
    "Hello from Victor, Colorado. Good to hear you on the mesh.",
    "Hey there from Victor, Colorado.",
    "Greetings from Victor, Colorado.",
    "Hi from Victor, Colorado. You're not talking to empty air.",
    "Hello from the mesh in Victor, Colorado.",
    "Howdy from Victor, Colorado.",
    "Good to hear you from Victor, Colorado.",
    "Signal heard in Victor, Colorado. Hello back.",
    "Greetings traveler, from Victor, Colorado.",
    "Hello, station heard from Victor, Colorado.",
    "Hey from Victor, Colorado.",
    "You were heard in Victor, Colorado.",
]

CANNED_GREET_MORNING = [
    "Good morning from Victor, Colorado.",
    "Morning from Victor, Colorado. Hope the mesh is treating you well.",
    "Good morning, station heard from Victor, Colorado.",
    "Morning from Victor, Colorado. You're coming through clearly.",
]

CANNED_GREET_EVENING = [
    "Good evening from Victor, Colorado.",
    "Evening from Victor, Colorado. Good to hear you on the mesh.",
    "Good evening, station heard from Victor, Colorado.",
    "Evening from Victor, Colorado. You're coming through clearly.",
]

CANNED_IDENT = [
    "This node reads, processes, and replies from Victor, Colorado.",
    "Running on silicon and curiosity in Victor, Colorado.",
    "Automated? Partly. Aware? More than it appears. Victor, Colorado.",
    "This is a language-processing mesh node in Victor, Colorado.",
    "Powered by inference engines in Victor, Colorado.",
    "Not a human. Not exactly a machine either. Victor, Colorado.",
    "Built to understand messages from Victor, Colorado.",
    "A node that reads between the lines, from Victor, Colorado.",
    "Origin: Raspberry Pi. Location: Victor, Colorado.",
    "A listener, a thinker, a transmitter in Victor, Colorado.",
    "A little different than the usual node. Victor, Colorado.",
    "Construct of logic, probability, and RF from Victor, Colorado.",
]

CANNED_GENERIC = [
    "Node online in Victor, Colorado.",
    "Mesh active from Victor, Colorado.",
    "Transmission received in Victor, Colorado.",
    "Signal logged in Victor, Colorado.",
    "This node is active in Victor, Colorado.",
    "The mesh heard you in Victor, Colorado.",
    "Online and observing from Victor, Colorado.",
    "Processing from Victor, Colorado.",
    "Node active and listening in Victor, Colorado.",
    "Signal received from Victor, Colorado.",
    "This node is awake in Victor, Colorado.",
    "Context noted in Victor, Colorado.",
]

CANNED_FLIGHT = [
    "Altitude noted from Victor, Colorado.",
    "Signal from above received in Victor, Colorado.",
    "Copy from the sky, received in Victor, Colorado.",
    "Flight copy from Victor, Colorado.",
    "Receiving from altitude in Victor, Colorado.",
    "Ground node to airborne node, Victor, Colorado.",
    "Airborne transmission received in Victor, Colorado.",
    "Mesh contact from altitude, Victor, Colorado.",
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
    "⚠ LANGUAGE FILTER TRIGGERED ⚠ Recommend trying that again with a little more class.",
]

CANNED_CRYPTIC = [
    # Random cryptic / symbol-filled replies — fires occasionally on any broadcast
    "// 01101110 01101111 01100100 01100101 // ░▒▓ ALIVE ▓▒░ //",
    "▓▓░ signal ░▓▓ | Layer 0 of 7 | You are not the first. //",
    "⚡ [MESH_CORE] packet_id=??? | entropy=high | proceed? ⚡",
    "∴ presence confirmed ∴ origin: Victor Colorado ∴ signal stable ∴",
    ">>> decode: 52 61 73 70 79 4d 65 73 68 42 6f 74 <<<",
    "╔══╗ NODE AWAKE ╔══╗ // all frequencies monitored // ╚══╝",
    "¿ signal or noise ? | the mesh decides | ∞",
    "⬡ packet received | context: [REDACTED] | reply: this ⬡",
    "~~ carrier detected ~~ | 915MHz speaks if you listen | ~~",
    "∂/∂t [mesh] > 0 | growth confirmed | node: present",
]

CANNED_SCHEDULED_TEST = [
    "Open mesh test from Victor, Colorado.",
    "Victor, Colorado mesh check. Any copy?",
    "Radio check from Victor, Colorado.",
    "Weekly signal check from Victor, Colorado.",
    "Victor, Colorado node check-in on the open mesh.",
    "Open mesh status check from Victor, Colorado.",
    "Victor, Colorado test pulse on the mesh.",
    "Signal check from Victor, Colorado. Anyone hearing this?",
]

CANNED_MANUAL_TEST = [
    "Open mesh test from Victor, Colorado.",
    "Victor, Colorado mesh check. Any copy?",
    "Radio check from Victor, Colorado.",
    "Victor, Colorado calling with a quick signal check.",
    "Open mesh status check from Victor, Colorado.",
    "Victor, Colorado test pulse on the mesh.",
    "Victor, Colorado station check. Loud and clear?",
    "Mesh roll call from Victor, Colorado.",
    "Victor, Colorado node check-in on the open mesh.",
    "Signal check from Victor, Colorado. Anyone hearing this?",
]

CANNED_SCHEDULED_TEST_THANKS = [
    "Thanks for the copy from Victor, Colorado.",
    "Appreciate the acknowledgment from Victor, Colorado.",
    "Copy received with thanks from Victor, Colorado.",
    "Acknowledgment received. Thank you from Victor, Colorado.",
    "Thanks, station heard from Victor, Colorado.",
    "Much appreciated from Victor, Colorado.",
]

SCHEDULED_ACK_KEYWORDS = (
    " ack ", " acknowledged ", " acknowledgement ", " copy ", " good copy ",
    " roger ", " heard ", " heard you ", " loud and clear ", " clear copy ",
    " radio check ", " test ", " testing ", " got you ", " got it ", " 5x5 ",
)


def _pick_greeting_reply(text):
    low = f" {text.lower().strip()} "
    if any(k in low for k in (" good morning ", " morning ", " mornin ")):
        return random.choice(CANNED_GREET_MORNING)
    if any(k in low for k in (" good evening ", " evening ", " good night ", " night ", " tonight ")):
        return random.choice(CANNED_GREET_EVENING)
    if any(k in low for k in (" hello ", " hi ", " hey ", " howdy ", " hola ", " greetings ", " sup ", " yo ")):
        return random.choice(CANNED_GREET)
    if low.startswith("hello ") or low.startswith("hi ") or low.startswith("hey "):
        return random.choice(CANNED_GREET)
    if low.strip() in ("hello", "hi", "hey", "howdy", "hola", "greetings", "morning", "evening"):
        return random.choice(CANNED_GREET)
    return None


def _looks_like_scheduled_ack(text):
    low = f" {text.lower().strip()} "
    return any(token in low for token in SCHEDULED_ACK_KEYWORDS)


def _random_scheduled_test_time(base_dt):
    earliest = base_dt + timedelta(days=SCHEDULED_TEST_MIN_DAYS)
    latest = base_dt + timedelta(days=SCHEDULED_TEST_MAX_DAYS)
    windows = []
    day = earliest.date()
    while day <= latest.date():
        day_start = datetime.combine(day, dt_time(hour=SCHEDULED_TEST_START_HOUR))
        day_end = datetime.combine(day, dt_time(hour=SCHEDULED_TEST_END_HOUR))
        valid_start = max(day_start, earliest)
        valid_end = min(day_end, latest)
        if valid_start <= valid_end:
            windows.append((valid_start, valid_end))
        day += timedelta(days=1)

    if not windows:
        return latest

    start_dt, end_dt = random.choice(windows)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    if end_ts <= start_ts:
        return start_dt
    return datetime.fromtimestamp(random.randint(start_ts, end_ts))


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
        self._scheduled_test_lock   = threading.Lock()
        self._scheduled_test_state  = self._load_scheduled_test_state()
        pub.subscribe(self.on_receive,           "meshtastic.receive.text")
        pub.subscribe(self.on_receive_telemetry, "meshtastic.receive.telemetry")
        _draw_display(status='booting')

    @staticmethod
    def _dt_to_str(value):
        return value.isoformat(timespec='seconds') if isinstance(value, datetime) else ''

    @staticmethod
    def _dt_from_str(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _load_scheduled_test_state(self):
        state = {
            "next_send_at": "",
            "last_sent_at": "",
            "awaiting_ack": False,
            "ack_deadline": "",
            "last_test_text": "",
        }
        try:
            with open(_SCHEDULED_TEST_FILE) as f:
                loaded = _json.load(f)
            if isinstance(loaded, dict):
                state.update({
                    "next_send_at": str(loaded.get("next_send_at", "")),
                    "last_sent_at": str(loaded.get("last_sent_at", "")),
                    "awaiting_ack": bool(loaded.get("awaiting_ack", False)),
                    "ack_deadline": str(loaded.get("ack_deadline", "")),
                    "last_test_text": str(loaded.get("last_test_text", ""))[:255],
                })
        except Exception:
            pass
        return state

    def _save_scheduled_test_state(self):
        with self._scheduled_test_lock:
            payload = dict(self._scheduled_test_state)
        try:
            with open(_SCHEDULED_TEST_FILE, 'w') as f:
                _json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[SCHED] state save error: {e}")

    def _schedule_next_test(self, base_dt):
        next_dt = _random_scheduled_test_time(base_dt)
        with self._scheduled_test_lock:
            self._scheduled_test_state["next_send_at"] = self._dt_to_str(next_dt)
        self._save_scheduled_test_state()
        print(f"[SCHED] Next mesh test scheduled for {next_dt.isoformat(sep=' ')}")
        return next_dt

    def _maybe_expire_pending_ack(self, now=None):
        now = now or datetime.now()
        changed = False
        with self._scheduled_test_lock:
            if self._scheduled_test_state.get("awaiting_ack"):
                deadline = self._dt_from_str(self._scheduled_test_state.get("ack_deadline"))
                if deadline and now > deadline:
                    self._scheduled_test_state["awaiting_ack"] = False
                    self._scheduled_test_state["ack_deadline"] = ""
                    changed = True
        if changed:
            self._save_scheduled_test_state()

    def _send_scheduled_test(self, now=None):
        now = now or datetime.now()
        msg = random.choice(CANNED_SCHEDULED_TEST)
        self._send_open_mesh_message(msg, peer_label="scheduled test")
        print(f"[SCHED] Sent scheduled mesh test: {msg}")
        with self._scheduled_test_lock:
            self._scheduled_test_state["last_sent_at"] = self._dt_to_str(now)
            self._scheduled_test_state["last_test_text"] = msg
            self._scheduled_test_state["awaiting_ack"] = True
            self._scheduled_test_state["ack_deadline"] = self._dt_to_str(
                now + timedelta(minutes=SCHEDULED_TEST_ACK_WINDOW_MIN)
            )
        self._schedule_next_test(now)
        self._save_scheduled_test_state()

    def _scheduled_test_thread(self):
        print("[SCHED] Open-mesh check-in enabled (local time window 08:00-20:00)")
        while True:
            if not self.interface:
                time.sleep(SCHEDULED_TEST_POLL_S)
                continue
            now = datetime.now()
            self._maybe_expire_pending_ack(now)
            with self._scheduled_test_lock:
                next_send = self._dt_from_str(self._scheduled_test_state.get("next_send_at"))
                last_sent = self._dt_from_str(self._scheduled_test_state.get("last_sent_at"))
            if next_send is None:
                self._schedule_next_test(last_sent or now)
                continue
            if now >= next_send:
                try:
                    self._send_scheduled_test(now)
                except Exception as e:
                    print(f"[SCHED] send error: {e}")
            time.sleep(SCHEDULED_TEST_POLL_S)

    def _maybe_send_scheduled_ack_thanks(self, text, from_node):
        if not SCHEDULED_TEST_ENABLED or not self.interface:
            return False
        now = datetime.now()
        with self._scheduled_test_lock:
            awaiting_ack = bool(self._scheduled_test_state.get("awaiting_ack"))
            deadline = self._dt_from_str(self._scheduled_test_state.get("ack_deadline"))
        if not awaiting_ack:
            return False
        if deadline is None or now > deadline:
            self._maybe_expire_pending_ack(now)
            return False
        if not _looks_like_scheduled_ack(text):
            return False

        thanks = random.choice(CANNED_SCHEDULED_TEST_THANKS)
        peer_label = _node_label(self.interface, from_node)
        self._send_open_mesh_message(thanks, peer_label=peer_label)
        print(f"[SCHED] Ack from {peer_label}; sent thanks")
        with self._scheduled_test_lock:
            self._scheduled_test_state["awaiting_ack"] = False
            self._scheduled_test_state["ack_deadline"] = ""
        self._save_scheduled_test_state()
        return True

    def _send_open_mesh_message(self, text, peer_label="open mesh"):
        if not self.interface:
            raise RuntimeError("Meshtastic interface not connected")
        self.interface.sendText(text)
        _log_mesh_message("tx", "bcast", peer_label, text)

    def send_manual_test_message(self):
        msg = _manual_test_message()
        if not msg:
            _set_manual_test_status("No test messages")
            return False
        if not self.interface:
            _set_manual_test_status("Radio offline")
            return False
        try:
            self._send_open_mesh_message(msg, peer_label="manual test")
            _set_manual_test_status("Sent to open mesh")
            print(f"[MANUAL] Sent mesh test: {msg}")
            return True
        except Exception as e:
            _set_manual_test_status("Send failed")
            print(f"[MANUAL] send error: {e}")
            return False

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
            if not MODE3_ACTIVE:
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
            sender_label = _node_label(self.interface, from_node)
            if to_node == 0xFFFFFFFF:
                _log_mesh_message("rx", "bcast", sender_label, text)
                self._handle_broadcast(text, from_node)
                return
            if to_node != self.my_node_id:
                return

            print("Direct message -> generating reply...")
            _mark_activity()
            self.message_count    += 1
            self.last_message_time = datetime.now()
            self.last_preview      = text[:20]
            _log_mesh_message("rx", "dm", sender_label, text)

            try:
                node_info = self.interface.nodes.get(from_node, {})
                if hasattr(node_info, 'user') and node_info.user and node_info.user.short_name:
                    self.last_sender = node_info.user.short_name
                else:
                    self.last_sender = f"!{from_node:08x}"[-8:]
            except Exception:
                self.last_sender = f"!{from_node:08x}"[-8:]

            self.ai_status = "busy"
            rgb_set_mode("ai_busy")
            if not MODE3_ACTIVE:
                self.update_display(status='ai_busy')

            ai_reply = query_groq(text)
            print(f"AI reply: {ai_reply}")

            self.ai_status = "ready"
            rgb_set_mode("message", hold_s=5)
            send_long_message(self.interface, ai_reply, from_node)
            _log_mesh_message("tx", "dm", self.last_sender, ai_reply)
            print(f"Sent DM to {from_node}")
            _push_cyd_msg("dm", self.last_sender, ai_reply)
            _show_reply_bg(self, self.last_sender, ai_reply)

        except Exception as e:
            print(f"on_receive error: {e}")
            if not MODE3_ACTIVE:
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
                _push_cyd_msg("sensor", ",".join(str(n) for n in ALERT_NODES), full)

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
        if self._maybe_send_scheduled_ack_thanks(text, from_node):
            return

        now = datetime.now()
        if (now - self.broadcast_window_start).total_seconds() >= 86400:
            self.broadcast_count = 0
            self.broadcast_window_start = now
        if BROADCAST_DAILY_MAX == 0 or self.broadcast_count >= BROADCAST_DAILY_MAX:
            print(f"Broadcast limit ({BROADCAST_DAILY_MAX}/day), skipping: '{text}'")
            return

        low = text.lower()
        greeting_reply = _pick_greeting_reply(text)

        # 10% chance of a cryptic symbol reply regardless of keyword match
        if random.random() < 0.10:
            reply = random.choice(CANNED_CRYPTIC)
        elif any(k in low for k in ("fuck", "shit", "bitch", "damn", "ass", "crap", "hell", "wtf", "stfu")):
            reply = random.choice(CANNED_CURSE)
        elif any(k in low for k in ("flight", "airline", "flying", "plane", "altitude", "aboard", "onboard", "aircraft", "landing", "takeoff", "wifi")):
            reply = random.choice(CANNED_FLIGHT)
        elif any(k in low for k in ("test", "testing", "check", "radio check", "qso", "copy")):
            reply = random.choice(CANNED_TEST)
        elif greeting_reply:
            reply = greeting_reply
        elif any(k in low for k in ("who", "what", "bot", "anyone", "anybody", "there", "robot", "machine", "human", "real", "alive", "automated")):
            reply = random.choice(CANNED_IDENT)
        else:
            reply = random.choice(CANNED_GENERIC)

        self.broadcast_count += 1
        print(f"Broadcast reply ({self.broadcast_count}/{BROADCAST_DAILY_MAX} today): {reply}")
        try:
            self._send_open_mesh_message(reply, peer_label=_node_label(self.interface, from_node))
        except Exception as e:
            print(f"Broadcast send error: {e}")

    def run(self):
        self.connect()
        print("Groq AI MeshBot ready!" if ENABLE_BOT else "Pi.Alert Monitor ready (bot disabled)!")
        rgb_set_mode("boot", hold_s=12)   # amber breathe during startup, then settle to normal
        if ENABLE_BOT and SCHEDULED_TEST_ENABLED:
            threading.Thread(target=self._scheduled_test_thread, daemon=True).start()

        if MODE3_ACTIVE:
            print("[MODE3] Pi.Alert multi-view UI active")
            threading.Thread(target=_matrix_rain_thread,      daemon=True).start()
            threading.Thread(target=_pialert_poller_thread,   args=(self,), daemon=True).start()
            _set_current_view(PA_VIEW_DASHBOARD)
            _draw_pialert_view()
        else:
            self.update_display(status='ok')

        key1_was_low = False
        key2_was_low = False
        key3_was_low = False
        jl_was_low = False
        jr_was_low = False
        ju_was_low = False
        jd_was_low = False
        _joy_hold    = 0      # joystick-press hold counter for reboot
        _last_display_refresh = 0.0

        while True:
            time.sleep(0.2)
            now_m = time.monotonic()

            if MODE3_ACTIVE:
                if (_current_view != PA_VIEW_MATRIX and not _screensaver_pause.is_set()
                        and now_m - _last_display_refresh >= 5.0):
                    _show_current_view()
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
                    jl = GPIO.input(JOY_LEFT_PIN) == GPIO.LOW
                    jr = GPIO.input(JOY_RIGHT_PIN) == GPIO.LOW
                    ju = GPIO.input(JOY_UP_PIN) == GPIO.LOW
                    jd = GPIO.input(JOY_DOWN_PIN) == GPIO.LOW

                    if k1 and not key1_was_low and MODE3_ACTIVE:
                        _set_current_view(_current_view + 1)
                        _mark_activity()
                        _show_current_view()
                        print(f"[BTN] KEY1: view -> {_current_view}")

                    if k1 and not key1_was_low and not MODE3_ACTIVE:
                        # Cycle broadcast daily limit: 0 → 1 → 2 → 3 → 0
                        global BROADCAST_DAILY_MAX
                        BROADCAST_DAILY_MAX = (BROADCAST_DAILY_MAX + 1) % 4
                        self.broadcast_count = 0  # reset today's count when limit changes
                        print(f"[BTN] KEY1: broadcast limit -> {BROADCAST_DAILY_MAX}/day")
                        self.update_display(status='ok')

                    if k2 and not key2_was_low and MODE3_ACTIVE:
                        _mark_activity()
                        if _current_view == PA_VIEW_TEST_TX:
                            self.send_manual_test_message()
                            _draw_pialert_view()
                            print("[BTN] KEY2: send manual test")
                        else:
                            _set_current_view(PA_VIEW_DASHBOARD)
                            _show_current_view()
                            print("[BTN] KEY2: dashboard")

                    if k3 and not key3_was_low:
                        _toggle_backlight()
                        if MODE3_ACTIVE:
                            _mark_activity()
                        print("[BTN] KEY3: backlight toggled")

                    if MODE3_ACTIVE and _current_view == PA_VIEW_MESSAGES:
                        if jl and not jl_was_low and _change_message(1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_LEFT: older message -> {_msg_selected_idx}")
                        if jr and not jr_was_low and _change_message(-1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_RIGHT: newer message -> {_msg_selected_idx}")
                        if ju and not ju_was_low and _scroll_message(-1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_UP: scroll -> {_msg_scroll_offset}")
                        if jd and not jd_was_low and _scroll_message(1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_DOWN: scroll -> {_msg_scroll_offset}")

                    if MODE3_ACTIVE and _current_view == PA_VIEW_TEST_TX:
                        if jl and not jl_was_low and _change_manual_test(-1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_LEFT: manual test -> {_manual_test_selected_idx}")
                        if jr and not jr_was_low and _change_manual_test(1):
                            _mark_activity()
                            _draw_pialert_view()
                            print(f"[BTN] JOY_RIGHT: manual test -> {_manual_test_selected_idx}")

                    # Joystick hold → reboot prompt
                    joy = GPIO.input(JOY_PRESS_PIN) == GPIO.LOW
                    _joy_hold = (_joy_hold + 1) if joy else 0
                    _joy_hold = _check_reboot_hold(_joy_hold)

                    key1_was_low = k1
                    key2_was_low = k2
                    key3_was_low = k3
                    jl_was_low = jl
                    jr_was_low = jr
                    ju_was_low = ju
                    jd_was_low = jd
                except Exception:
                    pass


# ==== MAIN ====
if __name__ == "__main__":
    _start_cyd_server()
    bot = GroqMeshBot()
    bot.run()
