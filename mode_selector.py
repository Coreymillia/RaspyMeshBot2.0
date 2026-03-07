#!/usr/bin/env python3
"""
Boot Mode Selector for Pi-Bot
Displays a 5-second menu on the Waveshare 1.44" LCD HAT.

KEY1 (BCM 21)  →  Mode 1: MeshBot  (default on timeout)
KEY2 (BCM 20)  →  Mode 2: RaspyJack
KEY3 (BCM 16)  →  [reserved: Mode 3 screensaver]
"""
import sys, os, time, subprocess, threading, socket, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote_plus

# RaspyJack LCD drivers live in /root/Raspyjack
sys.path.insert(0, '/root/Raspyjack')

import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont
import LCD_1in44
import LCD_Config

# ── Button pins (active LOW, internal pull-up) ──────────────────────────────
KEY1_PIN          = 21   # MeshBot
KEY2_PIN          = 20   # RaspyJack
KEY3_PIN          = 16   # Pi.Alert Monitor
JOYSTICK_PRESS    = 13   # Settings portal

_CHATBOT_DIR = '/home/coreymillia/MESH_CHATBOT'
_CONFIG_PATH = os.path.join(_CHATBOT_DIR, 'config.json')

# ── Font helper ─────────────────────────────────────────────────────────────
_FONT_PATH_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_PATH      = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

# ── Display ──────────────────────────────────────────────────────────────────
def draw_menu(lcd, selected=None):
    img  = Image.new('RGB', (128, 128), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    f9   = _font(_FONT_PATH, 9)
    f8   = _font(_FONT_PATH, 8)

    # Title bar
    draw.rectangle([(0, 0), (128, 14)], fill=(0, 70, 140))
    draw.text((4, 3), "PI-BOT  MODE SELECT", font=f8, fill=(255, 255, 255))

    # Mode 1 box
    bg1 = (0, 160, 70) if selected == 1 else (30, 30, 30)
    draw.rectangle([(3, 17), (124, 43)], fill=bg1, outline=(80, 80, 80))
    draw.text((8, 19), "KEY1  MeshBot", font=f9, fill=(255, 255, 255))
    draw.text((8, 32), "AI Mesh Radio Bot", font=f8, fill=(200, 200, 200))

    # Mode 2 box
    bg2 = (180, 0, 60) if selected == 2 else (30, 30, 30)
    draw.rectangle([(3, 47), (124, 73)], fill=bg2, outline=(80, 80, 80))
    draw.text((8, 49), "KEY2  RaspyJack", font=f9, fill=(255, 255, 255))
    draw.text((8, 62), "Security Toolkit", font=f8, fill=(200, 200, 200))

    # Mode 3 box
    bg3 = (80, 0, 160) if selected == 3 else (30, 30, 30)
    draw.rectangle([(3, 77), (124, 103)], fill=bg3, outline=(80, 80, 80))
    draw.text((8, 79), "KEY3  Pi.Alert Mon", font=f9, fill=(255, 255, 255))
    draw.text((8, 92), "Network Dashboard", font=f8, fill=(200, 200, 200))

    # Settings footer
    draw.line([(0, 106), (128, 106)], fill=(50, 50, 50))
    draw.text((4, 109), "JOYSTICK  \u2699 Settings", font=f8, fill=(180, 140, 0))
    draw.text((4, 119), "Press to configure", font=f8, fill=(100, 100, 100))

    lcd.LCD_ShowImage(img, 0, 0)


def draw_selected(lcd, label, color):
    img  = Image.new('RGB', (128, 128), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    f12b = _font(_FONT_PATH_BOLD, 12)
    f9   = _font(_FONT_PATH, 9)
    draw.rectangle([(0, 0), (128, 128)], fill=color)
    draw.text((10, 45), "LAUNCHING", font=f12b, fill=(255, 255, 255))
    draw.text((10, 65), label, font=f9, fill=(220, 220, 220))
    lcd.LCD_ShowImage(img, 0, 0)


# ── Settings portal ──────────────────────────────────────────────────────────
def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '?.?.?.?'


def _settings_html(cfg):
    def v(k, default=''):
        val = cfg.get(k, default)
        if isinstance(val, list):
            val = ', '.join(val)
        return str(val).replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')

    bot_checked = 'checked' if cfg.get('enable_bot', True) else ''
    return f"""<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshBot Settings</title><style>
body{{background:#111;color:#eee;font-family:monospace;padding:16px;max-width:480px;margin:auto}}
h2{{color:#0af}}label{{display:block;margin-top:12px;color:#aaa;font-size:13px}}
input[type=text],input[type=password]{{width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;padding:6px;border-radius:4px;font-family:monospace;font-size:13px}}
.sec{{border-left:3px solid #0af;padding-left:10px;margin-top:20px}}
.sec h3{{color:#0af;margin:0 0 4px 0;font-size:14px}}
.hint{{font-size:11px;color:#666;margin-top:2px}}
.row{{display:flex;align-items:center;gap:8px;margin-top:10px}}
.row input{{width:auto}}
button{{margin-top:24px;width:100%;padding:12px;background:#0a6;color:#fff;border:none;border-radius:6px;font-size:16px;cursor:pointer}}
</style></head><body>
<h2>&#9881; MeshBot Settings</h2>
<p style="color:#666;font-size:12px">Saved to config.json &mdash; restart bot to apply.</p>
<form method="POST" action="/save">
<div class="sec"><h3>Groq AI</h3>
<label>API Key</label>
<input type="password" name="groq_api_key" value="{v('groq_api_key')}">
<div class="hint">From console.groq.com &mdash; leave blank to disable AI replies</div>
<div class="row">
<input type="checkbox" name="enable_bot" value="true" {bot_checked}>
<span style="font-size:13px">Enable Mesh Bot (Groq AI + Meshtastic)</span></div>
<div class="hint">Uncheck to run Pi.Alert monitor only &mdash; no Groq key or radio needed</div>
</div>
<div class="sec"><h3>Meshtastic</h3>
<label>Serial Port</label>
<input type="text" name="serial_port" value="{v('serial_port')}">
<div class="hint">e.g. /dev/ttyACM0 &mdash; leave blank to auto-detect</div>
<label>Alert Node(s)</label>
<input type="text" name="alert_node" value="{v('alert_node')}">
<div class="hint">Node ID(s) for anomaly DMs &mdash; comma-separate multiple</div>
<label>Broadcast Daily Max</label>
<input type="text" name="broadcast_daily_max" value="{v('broadcast_daily_max', '3')}">
<div class="hint">0=silent, 1&ndash;3 open-channel replies per day</div>
</div>
<div class="sec"><h3>Pi.Alert</h3>
<label>Base URL</label>
<input type="text" name="pialert_base_url" value="{v('pialert_base_url')}">
<div class="hint">e.g. http://192.168.0.105/pialert/api/</div>
<label>API Key</label>
<input type="password" name="pialert_api_key" value="{v('pialert_api_key')}">
</div>
<div class="sec"><h3>Pi-hole</h3>
<label>Base URL</label>
<input type="text" name="pihole_base_url" value="{v('pihole_base_url')}">
<div class="hint">e.g. http://192.168.0.103/api &mdash; leave blank to disable</div>
<label>DNS Spike Threshold</label>
<input type="text" name="dns_spike_threshold" value="{v('dns_spike_threshold', '300')}">
<div class="hint">Queries per poll that trigger an alert (300 = 5 q/s sustained)</div>
</div>
<button type="submit">&#128190; Save Config</button>
</form></body></html>"""


def draw_settings_screen(lcd, ip, port):
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    f9b  = _font(_FONT_PATH_BOLD, 9)
    f8   = _font(_FONT_PATH, 8)
    f7   = _font(_FONT_PATH, 7)
    draw.rectangle([(0, 0), (128, 14)], fill=(140, 100, 0))
    draw.text((4, 3), "\u2699 SETTINGS PORTAL", font=f8, fill=(255, 255, 255))
    draw.text((4, 20), "Browse to:", font=f8, fill=(160, 160, 160))
    draw.text((4, 33), f"{ip}", font=f9b, fill=(0, 220, 255))
    draw.text((4, 47), f"port {port}", font=f8, fill=(0, 180, 200))
    draw.line([(0, 60), (128, 60)], fill=(50, 50, 50))
    draw.text((4, 64), "from any device on", font=f7, fill=(130, 130, 130))
    draw.text((4, 74), "your network.", font=f7, fill=(130, 130, 130))
    draw.text((4, 90), "Press any KEY", font=f8, fill=(180, 180, 0))
    draw.text((4, 101), "to cancel.", font=f8, fill=(130, 130, 130))
    lcd.LCD_ShowImage(img, 0, 0)


def draw_saved_screen(lcd):
    img  = Image.new('RGB', (128, 128), (0, 60, 0))
    draw = ImageDraw.Draw(img)
    f12b = _font(_FONT_PATH_BOLD, 12)
    f9   = _font(_FONT_PATH, 9)
    draw.text((10, 45), "CONFIG SAVED", font=f12b, fill=(255, 255, 255))
    draw.text((10, 68), "Returning to menu", font=f9, fill=(180, 255, 180))
    lcd.LCD_ShowImage(img, 0, 0)


def launch_settings_portal(lcd):
    local_ip = _get_local_ip()
    PORT     = 8080
    draw_settings_screen(lcd, local_ip, PORT)

    saved    = threading.Event()
    cancelled = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_GET(self):
            try:
                with open(_CONFIG_PATH) as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            html = _settings_html(cfg).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode()
            params = parse_qs(body)

            def get(key, default=''):
                vals = params.get(key, [default])
                return unquote_plus(vals[0]) if vals else default

            try:
                with open(_CONFIG_PATH) as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}

            cfg['groq_api_key']      = get('groq_api_key')
            cfg['pialert_base_url']  = get('pialert_base_url')
            cfg['pialert_api_key']   = get('pialert_api_key')
            cfg['pihole_base_url']   = get('pihole_base_url')
            cfg['enable_bot']        = (get('enable_bot') == 'true')
            sp = get('serial_port').strip()
            cfg['serial_port']       = sp if sp else None
            alert_raw = get('alert_node')
            nodes = [n.strip() for n in alert_raw.split(',') if n.strip()]
            cfg['alert_node'] = nodes if len(nodes) > 1 else (nodes[0] if nodes else '')
            try:
                cfg['broadcast_daily_max'] = int(get('broadcast_daily_max', '3'))
            except ValueError:
                cfg['broadcast_daily_max'] = 3
            try:
                cfg['dns_spike_threshold'] = int(get('dns_spike_threshold', '300'))
            except ValueError:
                cfg['dns_spike_threshold'] = 300

            with open(_CONFIG_PATH, 'w') as f:
                json.dump(cfg, f, indent=4)

            resp = b"""<html><body style="background:#111;color:#0f0;font-family:monospace;padding:20px">
<h2>&#10003; Config Saved!</h2>
<p>You can close this page. Press any button on the device to return to the menu.</p>
</body></html>"""
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            saved.set()

    server = HTTPServer(('0.0.0.0', PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    k1w = k2w = k3w = False
    while not saved.is_set():
        time.sleep(0.1)
        k1 = GPIO.input(KEY1_PIN) == GPIO.LOW
        k2 = GPIO.input(KEY2_PIN) == GPIO.LOW
        k3 = GPIO.input(KEY3_PIN) == GPIO.LOW
        if (k1 and not k1w) or (k2 and not k2w) or (k3 and not k3w):
            cancelled.set()
            break
        k1w, k2w, k3w = k1, k2, k3

    server.shutdown()

    if saved.is_set():
        draw_saved_screen(lcd)
        time.sleep(2)

    # Re-exec ourselves to restart the menu with fresh config
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Mode launchers ───────────────────────────────────────────────────────────
def launch_meshbot(lcd):
    draw_selected(lcd, "MeshBot", (0, 100, 40))
    time.sleep(0.8)
    # Clear screensaver flag so meshbot uses status display
    try:
        import os as _os
        if _os.path.exists('/tmp/meshbot_screensaver'):
            _os.remove('/tmp/meshbot_screensaver')
    except Exception:
        pass
    try:
        GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.LOW)
    except Exception:
        pass
    GPIO.cleanup()
    # Block until systemctl delivers the start command — Popen exits too fast
    # and systemd kills the child process before it can reach D-Bus.
    subprocess.run(
        ['systemctl', 'start', 'meshbot.service'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sys.exit(0)


def launch_raspyjack(lcd):
    draw_selected(lcd, "RaspyJack", (120, 0, 40))
    time.sleep(0.8)
    # Release GPIO cleanly so RaspyJack can re-init from scratch
    GPIO.cleanup()
    # exec — replace this process with raspyjack.py
    os.execv('/usr/bin/python3', ['python3', '/root/Raspyjack/raspyjack.py'])



def launch_meshbot_screensaver(lcd):
    draw_selected(lcd, "Pi.Alert Monitor", (60, 0, 120))
    time.sleep(0.8)
    try:
        GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.LOW)
    except Exception:
        pass
    GPIO.cleanup()
    # Write flag file so meshbot knows to run plasma screensaver display
    try:
        open('/tmp/meshbot_screensaver', 'w').close()
    except Exception:
        pass
    subprocess.run(
        ['systemctl', 'start', 'meshbot.service'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sys.exit(0)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Init hardware
    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()

    # Button setup
    GPIO.setup(KEY1_PIN,       GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY2_PIN,       GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY3_PIN,       GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(JOYSTICK_PRESS, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    draw_menu(lcd)

    jp_was_low = False

    while True:
        # KEY1 → MeshBot
        if GPIO.input(KEY1_PIN) == GPIO.LOW:
            draw_menu(lcd, selected=1)
            time.sleep(0.25)
            launch_meshbot(lcd)

        # KEY2 → RaspyJack
        if GPIO.input(KEY2_PIN) == GPIO.LOW:
            draw_menu(lcd, selected=2)
            time.sleep(0.25)
            launch_raspyjack(lcd)

        # KEY3 → Pi.Alert Monitor
        if GPIO.input(KEY3_PIN) == GPIO.LOW:
            draw_menu(lcd, selected=3)
            time.sleep(0.25)
            launch_meshbot_screensaver(lcd)

        # Joystick press → Settings portal
        jp = GPIO.input(JOYSTICK_PRESS) == GPIO.LOW
        if jp and not jp_was_low:
            launch_settings_portal(lcd)
            draw_menu(lcd)  # Only reached if portal was cancelled
        jp_was_low = jp

        time.sleep(0.05)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # On any display/GPIO failure fall back to MeshBot silently
        try:
            GPIO.cleanup()
        except Exception:
            pass
        subprocess.Popen(
            ['systemctl', 'start', 'meshbot.service'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        sys.exit(0)
