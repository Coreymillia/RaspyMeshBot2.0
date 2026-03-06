#!/usr/bin/env python3
"""
Groq AI MeshBot with Waveshare 1.44" LCD HAT Display
-----------------------------------------------------
Replaces the removed 0.96" I2C OLED with the 128x128 colour SPI HAT.
All Meshtastic + Groq AI logic is unchanged.
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

import time, sys, os, threading, random, math
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

GROQ_API_KEY = _load_api_key()
MODEL        = "llama-3.1-8b-instant"
SERIAL_PORT  = "/dev/ttyACM0"
MAX_MESH_MSG_LEN = 200

# ==== HAT LCD SETUP ====
# LCD drivers are in the same directory (copied from /root/Raspyjack/)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_lcd_ok = False
_lcd    = None
_lcd_lock = threading.Lock()

try:
    import RPi.GPIO as GPIO
    import LCD_1in44, LCD_Config
    LCD_Config.GPIO_Init()
    _lcd = LCD_1in44.LCD()
    _lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    _lcd.LCD_Clear()
    # Make sure backlight is ON (mode_selector may have left it low)
    GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.HIGH)
    # Button pins (active LOW, internal pull-up)
    KEY3_PIN = 16   # backlight toggle
    GPIO.setup(KEY3_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
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

# ── Draw helpers ─────────────────────────────────────────────────────────────
_BL_ON = True   # backlight state

# Screensaver mode — set by presence of flag file written by mode_selector
SCREENSAVER_MODE     = os.path.exists('/tmp/meshbot_screensaver')
_screensaver_pause   = threading.Event()  # set = screensaver paused

def _toggle_backlight():
    global _BL_ON
    if not _lcd_ok:
        return
    _BL_ON = not _BL_ON
    try:
        GPIO.output(LCD_Config.LCD_BL_PIN, GPIO.HIGH if _BL_ON else GPIO.LOW)
    except Exception:
        pass


def _draw_display(status="booting", node_id="", peer_count=0,
                  msg_count=0, last_sender="", last_time="",
                  last_preview="", ai_status="ready"):
    """Build and push a full 128x128 frame to the LCD."""
    if not _lcd_ok:
        return

    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Header bar ──────────────────────────────────────────────────────────
    STATUS_COLOR = {
        'ok':       (0,  130, 60),
        'booting':  (0,  70, 140),
        'error':    (160, 0,  0),
        'ai_busy':  (140, 90, 0),
    }.get(status, (0, 70, 140))

    draw.rectangle([(0, 0), (128, 16)], fill=STATUS_COLOR)
    draw.text((4, 3), "MESH AI BOT", font=F9, fill=(255, 255, 255))
    # Status dot on the right
    dot = {'ok': '●', 'booting': '○', 'error': '✖', 'ai_busy': '◉'}.get(status, '○')
    draw.text((108, 3), dot, font=F9,
              fill=(0, 255, 80) if status == 'ok' else (255, 180, 0))

    y = 20

    # ── Node info ───────────────────────────────────────────────────────────
    draw.text((2, y), "NODE:", font=F8, fill=(120, 120, 120))
    draw.text((36, y), node_id[:16] if node_id else "connecting...", font=F8,
              fill=(200, 230, 255))
    y += 11

    draw.text((2, y), "PEERS:", font=F8, fill=(120, 120, 120))
    draw.text((40, y), str(peer_count), font=F8, fill=(100, 220, 100))
    y += 11

    # Divider
    draw.line([(0, y), (128, y)], fill=(50, 50, 50))
    y += 4

    # ── Message stats ───────────────────────────────────────────────────────
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

    # Divider
    draw.line([(0, y), (128, y)], fill=(50, 50, 50))
    y += 4

    # ── Last message preview ─────────────────────────────────────────────────
    draw.text((2, y), "MSG:", font=F8, fill=(120, 120, 120))
    preview = last_preview[:20] if last_preview else "..."
    draw.text((30, y), preview, font=F8, fill=(220, 220, 220))
    y += 11

    # ── AI status ────────────────────────────────────────────────────────────
    ai_col = (100, 220, 100) if ai_status == 'ready' else \
             (255, 180,  0)  if ai_status == 'busy'  else (200, 80, 80)
    draw.text((2, y), "AI:", font=F8, fill=(120, 120, 120))
    draw.text((22, y), ai_status, font=F8, fill=ai_col)
    y += 11

    # Divider
    draw.line([(0, y), (128, y)], fill=(50, 50, 50))
    y += 3

    # ── Clock footer ─────────────────────────────────────────────────────────
    now = datetime.now().strftime("%H:%M:%S")
    draw.text((2, y), now, font=F8, fill=(80, 80, 80))
    draw.text((72, y), "KEY3:backlight", font=F8, fill=(40, 40, 40))

    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


REPLY_DISPLAY_S = 30   # seconds to hold the reply screen


def _draw_reply(sender, reply_text):
    """Show the AI reply word-wrapped on the full 128x128 screen."""
    if not _lcd_ok:
        return
    import textwrap
    img  = Image.new('RGB', (128, 128), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([(0, 0), (128, 16)], fill=(120, 0, 60))
    label = "AI -> " + sender[:14]
    draw.text((4, 3), label, font=F9, fill=(255, 255, 255))

    # Word-wrap reply at ~21 chars per line, up to 6 lines
    lines = textwrap.wrap(reply_text, width=21)[:6]
    y = 20
    for line in lines:
        draw.text((2, y), line, font=F8, fill=(220, 240, 200))
        y += 11

    # Footer hint
    footer = "showing " + str(REPLY_DISPLAY_S) + "s..."
    draw.text((2, 114), footer, font=F8, fill=(60, 60, 60))
    with _lcd_lock:
        _lcd.LCD_ShowImage(img, 0, 0)


def _show_reply_bg(bot, sender, reply_text):
    """Show reply screen for REPLY_DISPLAY_S seconds, then restore status."""
    def _worker():
        if SCREENSAVER_MODE:
            _screensaver_pause.set()
        _draw_reply(sender, reply_text)
        time.sleep(REPLY_DISPLAY_S)
        if SCREENSAVER_MODE:
            _screensaver_pause.clear()
        else:
            bot.update_display(status='ok')
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ==== MATRIX RAIN SCREENSAVER ====
def _matrix_rain_thread():
    """Efficient matrix rain animation using column draws — fast on Pi Zero 2W."""
    if not _lcd_ok:
        return

    W, H   = 128, 128
    COL_W  = 8          # pixels per column
    ROW_H  = 8          # pixels per row
    COLS   = W // COL_W  # 16 columns
    ROWS   = H // ROW_H  # 16 rows
    CHARS  = "01ABCDEFabcdef@#$%&*+=-<>?!"

    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 8)
    except Exception:
        font = ImageFont.load_default()

    # Per-column state
    cols = [{
        'head':  random.randint(-10, 0),
        'speed': random.uniform(0.4, 1.1),
        'trail': random.randint(4, 11),
        'chars': [random.choice(CHARS) for _ in range(ROWS)],
    } for _ in range(COLS)]

    while True:
        if _screensaver_pause.is_set():
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
                        color = (200, 255, 200)       # bright head
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
        time.sleep(0.08)   # ~12 FPS target


# ==== MESSAGE HANDLING ====
def send_long_message(interface, text, destinationId):
    if len(text) <= MAX_MESH_MSG_LEN:
        interface.sendText(text, destinationId=destinationId)
        return
    chunks = [text[i:i+MAX_MESH_MSG_LEN] for i in range(0, len(text), MAX_MESH_MSG_LEN)]
    for i, chunk in enumerate(chunks):
        part_text = f"[{i+1}/{len(chunks)}] {chunk}" if len(chunks) > 1 else chunk
        interface.sendText(part_text, destinationId=destinationId)
        if i < len(chunks) - 1:
            time.sleep(1)


# ==== CANNED BROADCAST REPLIES ====
CANNED_TEST = [
    "Test received! Signal looks good. DM this node for full AI chat.",
    "Copy that! Your test reached the bot. DM me to chat with AI.",
    "Test acknowledged! Node is online. DM this node to try AI chat.",
    "Got your test loud and clear! DM me for a full AI conversation.",
    "Test confirmed. AI mesh node online. DM me to try it out.",
    "Test copy! I am an AI bot on Meshtastic. DM me to have a convo.",
    "Heard you! Bot is active and listening. DM this node to chat with AI.",
]
CANNED_GREET = [
    "Hey there! I am an AI bot on the mesh. DM this node to chat!",
    "Hello from the mesh! I am a Groq AI bot. Send me a DM to talk.",
    "Hi! AI mesh bot here. Direct message me for a full AI conversation.",
    "Howdy! You have reached an AI-powered Meshtastic node. DM me!",
    "Greetings from the mesh! I am an AI bot. DM this node for a chat.",
    "Hey! AI bot online. DM me and I will respond with Groq AI.",
    "Hello! There is an AI bot on this node. Try sending me a DM!",
]
CANNED_IDENT = [
    "I am an AI-powered Meshtastic bot on a Raspberry Pi. DM me to chat!",
    "This is an automated AI mesh node. Send a DM to start a conversation.",
    "AI bot here, powered by Groq LLaMA AI. DM this node to talk.",
    "You found an AI bot on the mesh! DM me for a full conversation.",
    "Automated AI node online. DM me and I will reply with real AI.",
    "This node runs a Groq AI chatbot. DM me to try it out!",
    "Yep, a real AI bot! Send me a direct message and I will reply.",
]
CANNED_GENERIC = [
    "AI mesh bot here! DM this node for an AI-powered conversation.",
    "Mesh bot online! Send a direct message to this node for AI chat.",
    "You have reached an AI bot on the mesh. DM me to have a conversation!",
    "AI node active. Direct message this node to chat with Groq AI.",
    "This is an automated AI bot. DM me for an AI conversation!",
    "Mesh AI bot here. I reply to direct messages with AI. Try a DM!",
    "Bot online! DM this node and I will reply with AI responses.",
    "AI-powered mesh node here. Send me a DM to start chatting!",
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
        self.interface        = None
        self.my_node_id       = None
        self.message_count    = 0
        self.last_sender      = ""
        self.last_message_time = None
        self.last_preview     = ""
        self.ai_status              = "ready"
        self.broadcast_count        = 0
        self.broadcast_window_start = datetime.now()
        pub.subscribe(self.on_receive, "meshtastic.receive.text")
        _draw_display(status='booting')

    def _peer_count(self):
        try:
            nodes = self.interface.nodes or {}
            return max(0, len(nodes) - 1)   # exclude self
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
        if elapsed < 3600: return f"{elapsed//60}m ago"
        return f"{elapsed//3600}h ago"

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

    def connect(self):
        print("Connecting to Meshtastic device...")
        _draw_display(status='booting', node_id='connecting...')
        self.interface = meshtastic.serial_interface.SerialInterface(SERIAL_PORT)
        time.sleep(3)
        if hasattr(self.interface, "myInfo") and self.interface.myInfo:
            self.my_node_id = self.interface.myInfo.my_node_num
            print(f"Connected as node {self._node_short()}")
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
            self.update_display(status='ai_busy')

            ai_reply = query_groq(text)
            print(f"AI reply: {ai_reply}")

            self.ai_status = "ready"
            send_long_message(self.interface, ai_reply, from_node)
            print(f"Sent DM to {from_node}")
            _show_reply_bg(self, self.last_sender, ai_reply)

        except Exception as e:
            print(f"on_receive error: {e}")
            _draw_display(status='error', last_preview=str(e)[:20])

    def _handle_broadcast(self, text, from_node):
        """Reply to open-channel messages with canned responses, max 3 per 24h."""
        now = datetime.now()
        if (now - self.broadcast_window_start).total_seconds() >= 86400:
            self.broadcast_count = 0
            self.broadcast_window_start = now
        if self.broadcast_count >= 3:
            print(f"Broadcast rate limit reached, skipping: '{text}'")
            return
        low = text.lower()
        if any(k in low for k in ("test", "testing", "check", "radio check", "qso")):
            reply = random.choice(CANNED_TEST)
        elif any(k in low for k in ("hello", "hi ", "hey", "howdy", "hola", "greetings", "sup", "yo ")):
            reply = random.choice(CANNED_GREET)
        elif any(k in low for k in ("who", "what", "bot", "anyone", "anybody", "there")):
            reply = random.choice(CANNED_IDENT)
        else:
            reply = random.choice(CANNED_GENERIC)
        self.broadcast_count += 1
        print(f"Broadcast reply ({self.broadcast_count}/3 today): {reply}")
        try:
            self.interface.sendText(reply)
        except Exception as e:
            print(f"Broadcast send error: {e}")


    def run(self):
        self.connect()
        print("Groq AI MeshBot ready!")
        self.update_display(status='ok')

        if SCREENSAVER_MODE:
            print("[SAVER] Plasma screensaver display mode active")
            threading.Thread(target=_matrix_rain_thread, daemon=True).start()

        key3_was_low = False
        while True:
            time.sleep(5)
            if not SCREENSAVER_MODE:
                self.update_display(status='ok')

            # KEY3: backlight toggle
            if _lcd_ok:
                try:
                    key3_now = GPIO.input(KEY3_PIN) == GPIO.LOW
                    if key3_now and not key3_was_low:
                        _toggle_backlight()
                    key3_was_low = key3_now
                except Exception:
                    pass


# ==== MAIN ====
if __name__ == "__main__":
    bot = GroqMeshBot()
    bot.run()
