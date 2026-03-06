# RaspyMeshBot 2.0

A Raspberry Pi Zero 2 W project that combines a **Groq AI-powered Meshtastic chatbot** with a **3-mode boot selector** on a Waveshare 1.44" LCD HAT.

Boot the Pi, pick your mode on the screen with a button press, and go.

---

## Modes

| Key | Mode | What it does |
|-----|------|-------------|
| KEY1 | **MeshBot** | AI chatbot — status screen on the LCD |
| KEY2 | **RaspyJack** | Security toolkit (separate install required) |
| KEY3 | **MeshBot + Screensaver** | Same AI chatbot but with matrix rain on the LCD |

The boot selector times out after **7 seconds** and defaults to MeshBot.

---

## What MeshBot Does

- Listens on your Meshtastic mesh radio for **direct messages** and replies using **Groq AI** (LLaMA 3.1)
- Also listens on the **open channel** and replies with canned messages (max 3 per 24 hours, so it stays polite on busy meshes)
- Open channel keyword responses: `test/testing`, greetings (`hello/hi/hey`), identity questions (`who/bot/anyone`), and generic messages
- **28 unique canned replies** so responses are never repetitive
- Displays node ID, peer count, message count, last sender, and AI status on the LCD

---

## Hardware

- Raspberry Pi Zero 2 W
- Waveshare 1.44" LCD HAT (128x128, SPI, buttons KEY1/KEY2/KEY3)
- Any Meshtastic-compatible radio connected via USB (tested with Heltec Vision Master T190 on `/dev/ttyACM0`)

---

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/RaspyMeshBot2.0.git
cd RaspyMeshBot2.0
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Add your Groq API key

Get a free key at https://console.groq.com

```bash
cp config.example.json config.json
nano config.json   # replace YOUR_GROQ_API_KEY_HERE with your actual key
```

Or set it as an environment variable instead:
```bash
export GROQ_API_KEY="your_key_here"
```

### 4. Check your serial port

The bot defaults to `/dev/ttyACM0`. If your radio shows up differently:
```bash
ls /dev/ttyACM* /dev/ttyUSB*
```
Edit `SERIAL_PORT` near the top of `mesh_groq_ai_bot_oled.py`.

### 5. Install the systemd services

```bash
# Copy and edit the service files (replace YOUR_USER with your username)
sudo cp systemd/meshbot.service /etc/systemd/system/
sudo nano /etc/systemd/system/meshbot.service   # update YOUR_USER paths

sudo cp systemd/mode-selector.service /etc/systemd/system/
sudo cp mode_selector.py /root/mode_selector.py

sudo systemctl daemon-reload
sudo systemctl enable meshbot.service
sudo systemctl enable mode-selector.service
```

### 6. Reboot

```bash
sudo reboot
```

The mode selector will appear on the LCD for 7 seconds on every boot.

---

## Running manually (without systemd)

```bash
python3 mesh_groq_ai_bot_oled.py
```

For screensaver mode:
```bash
touch /tmp/meshbot_screensaver
python3 mesh_groq_ai_bot_oled.py
```

---

## File Overview

| File | Purpose |
|------|---------|
| `mesh_groq_ai_bot_oled.py` | Main bot — Meshtastic listener, Groq AI, matrix rain screensaver |
| `mode_selector.py` | Boot menu shown on the LCD HAT at startup |
| `LCD_1in44.py` | Waveshare 1.44" LCD driver (SPI) |
| `LCD_Config.py` | GPIO/SPI hardware config for the LCD |
| `config.example.json` | API key config template |
| `systemd/` | systemd service files for auto-start on boot |

---

## Notes

- **RaspyJack** (KEY2) is a separate project and must be installed independently at `/root/Raspyjack/`
- The bot runs as your user but `mode_selector.py` must run as root (GPIO access)
- On a busy mesh, the 3/24h broadcast reply limit keeps the bot from being annoying
- The matrix rain screensaver is optimized for the Pi Zero 2W — ~12 FPS using PIL column draws instead of per-pixel math

---

## License

MIT
