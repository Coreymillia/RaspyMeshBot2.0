#!/usr/bin/env python3
"""
Boot Mode Selector for Pi-Bot
Displays a 5-second menu on the Waveshare 1.44" LCD HAT.

KEY1 (BCM 21)  →  Mode 1: MeshBot  (default on timeout)
KEY2 (BCM 20)  →  Mode 2: RaspyJack
KEY3 (BCM 16)  →  [reserved: Mode 3 screensaver]
"""
import sys, os, time, subprocess

# RaspyJack LCD drivers live in /root/Raspyjack
sys.path.insert(0, '/root/Raspyjack')

import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont
import LCD_1in44
import LCD_Config

# ── Button pins (active LOW, internal pull-up) ──────────────────────────────
KEY1_PIN = 21   # MeshBot
KEY2_PIN = 20   # RaspyJack
KEY3_PIN = 16   # (future)

TIMEOUT_S = 7   # seconds before defaulting to MeshBot

# ── Font helper ─────────────────────────────────────────────────────────────
_FONT_PATH_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_PATH      = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

# ── Display ──────────────────────────────────────────────────────────────────
def draw_menu(lcd, remaining, selected=None):
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
    draw.text((8, 79), "KEY3  MeshBot+Saver", font=f9, fill=(255, 255, 255))
    draw.text((8, 92), "Plasma Screensaver", font=f8, fill=(200, 200, 200))

    # Countdown row
    secs = max(0, int(remaining))
    draw.text((4, 108), "Default: MeshBot", font=f8, fill=(180, 180, 0))
    draw.text((4, 118), f"Auto in {secs}s", font=f8, fill=(255, 200, 0))

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
    draw_selected(lcd, "MeshBot+Plasma", (60, 0, 120))
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
    GPIO.setup(KEY1_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY2_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(KEY3_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    start    = time.monotonic()
    last_rem = -1.0

    while True:
        elapsed   = time.monotonic() - start
        remaining = max(0.0, TIMEOUT_S - elapsed)

        # Redraw ~twice a second for countdown
        if abs(remaining - last_rem) >= 0.45:
            draw_menu(lcd, remaining)
            last_rem = remaining

        # Timeout → default to MeshBot
        if remaining <= 0:
            draw_menu(lcd, 0, selected=1)
            time.sleep(0.4)
            launch_meshbot(lcd)

        # KEY1 → MeshBot
        if GPIO.input(KEY1_PIN) == GPIO.LOW:
            draw_menu(lcd, remaining, selected=1)
            time.sleep(0.25)  # debounce
            launch_meshbot(lcd)

        # KEY2 → RaspyJack
        if GPIO.input(KEY2_PIN) == GPIO.LOW:
            draw_menu(lcd, remaining, selected=2)
            time.sleep(0.25)
            launch_raspyjack(lcd)

        # KEY3 → MeshBot + Plasma Screensaver
        if GPIO.input(KEY3_PIN) == GPIO.LOW:
            draw_menu(lcd, remaining, selected=3)
            time.sleep(0.25)
            launch_meshbot_screensaver(lcd)

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
