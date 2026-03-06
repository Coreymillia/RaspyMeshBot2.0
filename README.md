# RaspyMeshBot 2.0

A Raspberry Pi Zero 2 W project that combines a **Groq AI-powered Meshtastic chatbot** with a **3-mode boot selector** on a Waveshare 1.44" LCD HAT.

Boot the Pi, pick your mode on the screen with a button press, and go.

---

## Photos

| Boot Selector | MeshBot Status | Matrix Rain Interrupted |
|:---:|:---:|:---:|
| ![Mode Selector](images/mode_selector.jpg) | ![MeshBot Status](images/meshbot_status.jpg) | ![Matrix Rain](images/matrix_rain_interrupt.jpg) |
| *3-mode menu on boot — auto-selects MeshBot after 7s* | *Live status: node ID, peer count, DMs, last sender* | *Matrix rain pauses when a mesh message arrives* |

---

## Modes

| Key | Mode | What it does |
|-----|------|-------------|
| KEY1 | **MeshBot** | AI chatbot with live status screen on the LCD |
| KEY2 | **RaspyJack** | Security toolkit (separate install — see below) |
| KEY3 | **MeshBot + Matrix Rain** | Full AI chatbot with matrix rain screensaver on the LCD |

The boot selector times out after **7 seconds** and defaults to MeshBot automatically.

---

## What MeshBot Does

**Direct messages (DMs):** Anyone on the mesh who sends a private message to this node gets a real AI-generated reply powered by Groq LLaMA 3.1. The reply is sent back as a DM.

**Open channel:** The bot also listens to public mesh traffic and replies to messages with canned responses — limited to **3 replies per 24 hours** so it never becomes annoying on a busy mesh. It responds to:
- `test`, `testing`, `radio check` → acknowledges the test and invites a DM
- `hello`, `hi`, `hey`, `howdy` → greeting + invite to DM
- `who`, `bot`, `anyone there` → explains what the node is
- Everything else → generic "AI bot here, DM me" style message

There are **28 unique canned replies** so responses are not repetitive.

**LCD display (KEY1/KEY3 modes):**
- KEY1 shows a live status screen: node ID, peer count, DM count, last sender, time since last message, message preview, AI status
- KEY3 shows matrix rain animation. When a message arrives the screensaver pauses, the reply is shown on screen for 30 seconds, then the screensaver resumes
- KEY3 on the status/screensaver screen toggles the backlight

---

## Hardware

- Raspberry Pi Zero 2 W
- [Waveshare 1.44" LCD HAT](https://www.waveshare.com/1.44inch-lcd-hat.htm) (128×128 SPI, buttons KEY1/KEY2/KEY3)
- Meshtastic radio connected via USB — **only tested with the Heltec Vision Master T190**
- Internet connection on the Pi (for Groq AI replies)

---

## ⚠️ Important: Radio Firmware Version

**This project has only been tested with the Heltec Vision Master T190 running Meshtastic firmware 2.6.x.**

### Why 2.6.x?

The `meshtastic` Python library communicates with the radio over USB serial using Protobuf messages. Meshtastic's Python library is versioned to **match the firmware major version** — the library used here targets firmware 2.6. If you upgrade the radio to firmware 2.7+, the Protobuf definitions may not match and the serial connection can fail or time out, breaking the bot entirely.

> **Rule of thumb:** keep the meshtastic Python library version and the radio firmware on the same major version number.

### The phone app no longer connects — is that normal?

Yes, and it is a known Meshtastic issue. Firmware versions 2.6.x and 2.7.x both have widespread Bluetooth/BLE bugs where the phone app fails to pair or re-connect after a firmware update. This is **not caused by this project** — it affects many devices across many firmware builds.

**The radio still works perfectly for this project** because the bot communicates over USB serial, not Bluetooth.

### Configuring the radio without the phone app

If you need to change radio settings (channel, frequency, node name, etc.) and the phone app won't connect, use the **Meshtastic web client over USB serial**:

1. Connect the T190 to any PC via USB
2. Go to **https://client.meshtastic.org** in Chrome or Edge
3. Click **Serial** → select your COM port → **Connect**
4. Make your changes in the Config tab, save, and reboot the radio

This works regardless of Bluetooth state and is often more reliable than the phone app anyway.

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

Get a free key at https://console.groq.com — the free tier is more than enough for a mesh bot.

```bash
cp config.example.json config.json
nano config.json   # replace YOUR_GROQ_API_KEY_HERE with your actual key
```

Or export it as an environment variable instead:
```bash
export GROQ_API_KEY="your_key_here"
```

### 4. Check your serial port

The bot defaults to `/dev/ttyACM0`. Verify your radio's port:
```bash
ls /dev/ttyACM* /dev/ttyUSB*
```
If different, edit `SERIAL_PORT` near the top of `mesh_groq_ai_bot_oled.py`.

### 5. Install the systemd services

These make the bot and boot selector start automatically on every boot.

```bash
# MeshBot service — update YOUR_USER to your Linux username in both places
sudo cp systemd/meshbot.service /etc/systemd/system/
sudo nano /etc/systemd/system/meshbot.service

# Boot mode selector — runs as root from /root/
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

The 3-mode selector will appear on the LCD for 7 seconds on every boot. Press a key or wait for the default.

---

## Installing RaspyJack (KEY2 — Optional)

RaspyJack is a separate open source security toolkit by [7h30th3r0n3](https://github.com/7h30th3r0n3/raspyjack). It must be installed at `/root/Raspyjack/` for KEY2 to work.

**If RaspyJack is not installed, KEY2 will not crash the project** — the mode selector will silently fall back to starting MeshBot instead. KEY1 and KEY3 work completely independently of RaspyJack.

### Install RaspyJack

Run as root — RaspyJack expects to live at `/root/Raspyjack`.

```bash
sudo apt install git
sudo su
cd /root
git clone https://github.com/7h30th3r0n3/raspyjack.git
mv raspyjack Raspyjack
cd Raspyjack
chmod +x install_raspyjack.sh
sudo ./install_raspyjack.sh
sudo reboot
```

> **Note:** The folder name matters. It must be `/root/Raspyjack` (capital R). The clone command creates it lowercase — the `mv` above fixes that.

### Update RaspyJack

> ⚠️ Back up your loot folder before updating — it will be deleted.

```bash
sudo su
cd /root
rm -rf Raspyjack
git clone https://github.com/7h30th3r0n3/raspyjack.git
mv raspyjack Raspyjack
sudo reboot
```

---

## Running Manually (without systemd)

```bash
# MeshBot with status screen
python3 mesh_groq_ai_bot_oled.py

# MeshBot with matrix rain screensaver
touch /tmp/meshbot_screensaver
python3 mesh_groq_ai_bot_oled.py

# Boot selector (requires root for GPIO)
sudo python3 mode_selector.py
```

---

## File Overview

| File | Purpose |
|------|---------|
| `mesh_groq_ai_bot_oled.py` | Main bot — Meshtastic listener, Groq AI, broadcast replies, matrix rain |
| `mode_selector.py` | Boot menu displayed on the LCD HAT at startup |
| `LCD_1in44.py` | Waveshare 1.44" LCD SPI driver |
| `LCD_Config.py` | GPIO/SPI hardware configuration for the LCD |
| `config.example.json` | API key config template — copy to `config.json` and fill in |
| `requirements.txt` | Python dependencies |
| `systemd/` | Ready-to-use systemd service files for auto-start on boot |
| `images/` | Project photos |

---

## Notes

- The bot runs as your regular user; `mode_selector.py` runs as root (needed for GPIO at boot)
- Matrix rain is optimised for the Pi Zero 2W — uses PIL column draws (~12 FPS) instead of per-pixel math
- The 3/24h broadcast cap means the bot will never spam a channel even if left running indefinitely
- Groq AI replies are capped at 100 tokens to keep mesh messages short

---

## License

MIT
