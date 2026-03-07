# RaspyMeshBot 2.0

A Raspberry Pi Zero 2 W project that combines a **Groq AI-powered Meshtastic chatbot** with a **3-mode boot selector** on a Waveshare 1.44" LCD HAT.

Boot the Pi, pick your mode on the screen with a button press, and go.

---

## Photos

### UI Screenshots

| Boot Selector | MeshBot Status | Matrix Rain Interrupted |
|:---:|:---:|:---:|
| ![Mode Selector](images/mode_selector.jpg) | ![MeshBot Status](images/meshbot_status.jpg) | ![Matrix Rain](images/matrix_rain_interrupt.jpg) |
| *3-mode menu on boot — auto-selects MeshBot after 7s* | *Live status: node ID, peer count, DMs, last sender* | *Matrix rain pauses when a mesh message arrives* |

### Hardware (pre-case)

| Mode Selector + T190 | MeshBot + T190 | Matrix Rain + T190 |
|:---:|:---:|:---:|
| ![Mode selector on hardware](images/IMG_20260305_235716.jpg) | ![MeshBot status on hardware](images/IMG_20260306_000305.jpg) | ![Matrix rain on hardware](images/IMG_20260305_235553.jpg) |
| *Boot selector — also showing T190 with last received message* | *MeshBot status: 99 peers, 2 DMs, AI ready* | *Matrix rain screensaver interrupted by incoming message* |

### In the Printed Case

| Pi.Alert Dashboard (Mode 3) | AI Bot Reply (Mode 3) |
|:---:|:---:|
| ![Pi.Alert dashboard in case](images/IMG_20260306_175915.jpg) | ![AI bot reply in case](images/IMG_20260306_175437.jpg) |
| *Pi.Alert live view: 48 devices, 21 online, 35 new — KEY1 cycles views* | *AI bot DM reply displayed on screen for 30 seconds* |

| RaspyJack Splash (Mode 2) | RaspyJack Menu (Mode 2) |
|:---:|:---:|
| ![RaspyJack splash in case](images/IMG_20260306_175706.jpg) | ![RaspyJack menu in case](images/IMG_20260306_175713.jpg) |
| *RaspyJack loading — the T190 still shows the last mesh message received* | *RaspyJack security toolkit menu: Reverse Shell, Responder, MITM, DNS Spoofing and more* |

---

## Modes

| Key | Mode | What it does |
|-----|------|-------------|
| KEY1 | **MeshBot** | AI chatbot with live status screen on the LCD |
| KEY2 | **RaspyJack** | Security toolkit (separate install — see below) |
| KEY3 | **MeshBot + Pi.Alert Monitor** | Full AI chatbot + Pi.Alert network dashboard + idle matrix rain screensaver |

The boot selector times out after **7 seconds** and defaults to MeshBot automatically.

---

## What MeshBot Does

**Direct messages (DMs):** Anyone on the mesh who sends a private message to this node gets a real AI-generated reply powered by Groq LLaMA 3.1. The reply is sent back as a DM.

**Open channel:** The bot also listens to public mesh traffic and replies with canned responses. The daily broadcast limit is configurable from **0 to 3 replies per 24 hours** (default 3) — set to 0 to go completely silent on the open channel. It responds to:
- `test`, `testing`, `check`, `radio check`, `qso`, `copy` → acknowledges the test and invites a DM
- `hello`, `hi`, `hey`, `howdy`, `hola`, `morning`, `evening`, etc. → greeting + invite to DM
- `who`, `what`, `bot`, `anyone`, `robot`, `human`, `alive`, etc. → explains what the node is
- `flight`, `airline`, `flying`, `altitude`, `wifi`, etc. → airborne-specific reply
- Profanity → dramatic self-destruct humour reply
- Everything else → generic "AI bot here, DM me" style message
- **10% chance on any message** → random cryptic symbol/hex reply regardless of keywords

There are **74 unique canned replies** across 7 categories so responses are not repetitive.

**LCD display (KEY1 mode):**
- Shows a live status screen: node ID, peer count, DM count, last sender, time since last message, message preview, AI status
- The current broadcast limit (`BC:N/d`) is shown live on the status screen
- **KEY1** cycles the broadcast daily limit: 0 → 1 → 2 → 3 → 0 (resets today's count each time)
- **KEY3** toggles the backlight

**LCD display (KEY3 mode — Pi.Alert Monitor):**

See [Mode 3: Pi.Alert Monitor](#mode-3-pialert-monitor) below.

---

## Mode 3: Pi.Alert Monitor

KEY3 launches a combined AI chatbot + live network security dashboard. The MeshBot runs in the background exactly as normal while the LCD shows data pulled from your [Pi.Alert](https://github.com/jokob-sk/Pi.Alert) instance.

### What's on the display

Five views cycle with **KEY1**. A row of five dots in the top-right corner shows which view is active.

| View | Content |
|------|---------|
| 0 — Dashboard | Scan time, total/online/new/down/offline device counts |
| 1 — Online | Live list of online devices with last IP |
| 2 — New | Recently-seen new devices with first-seen timestamp |
| 3 — ARP Alerts | MAC-change events (red header when alerts exist) |
| 4 — Shady WiFi | Suspicious access points with security score |

### Buttons in Mode 3

| Button | Pin | Action |
|--------|-----|--------|
| KEY1 | BCM 21 | Cycle to next Pi.Alert view |
| KEY2 | BCM 20 | Wake display from screensaver |
| KEY3 | BCM 16 | Toggle backlight |

### Screensaver

After **5 minutes of inactivity** (no button presses, no incoming mesh messages, no new anomalies) the matrix rain screensaver activates automatically. Press **KEY2** to wake back to the Pi.Alert dashboard.

### Anomaly alerter

The bot watches the Pi.Alert feed every 60 seconds and applies four detection rules:

| Rule | Trigger | Recency filter |
|------|---------|----------------|
| ARP alert | MAC address changed on a known IP | Last 24 hours |
| New device | Device first seen on the network | Last 24 hours |
| Device down | Known device stopped responding | Any |
| Shady WiFi | Access point with score ≥ 20 | Any |

When an anomaly is detected the bot:
1. Wakes the display and switches to the relevant view
2. Shows a red alert screen for 20 seconds
3. Sends a **private DM** to node `!edac358a` over the mesh

Anomalies are deduplicated across reboots via `.seen_anomalies.json` so the same event will never generate a second alert unless it reappears after 48 hours.

### Pi.Alert requirements

- A running Pi.Alert instance accessible on your local network
- Your Pi.Alert IP, API key, and the mesh node ID you want to receive alerts — all set in `config.json` (see Setup step 4)
- The `pialert-patch/` directory in this repo contains the daemon scripts that extend Pi.Alert with ARP watching, WiFi scanning, and BLE scanning on the Pi.Alert host

---

## Hardware

- Raspberry Pi Zero 2 W
- [Waveshare 1.44" LCD HAT](https://www.waveshare.com/1.44inch-lcd-hat.htm) (128×128 SPI, buttons KEY1/KEY2/KEY3)
- Meshtastic radio connected via USB — **only tested with the Heltec Vision Master T190**
- Internet connection on the Pi (for Groq AI replies)

---

## ⚠️ Important: Radio Firmware Version

**This project has only been tested with the Heltec Vision Master T190 running Meshtastic firmware 2.5.x (specifically 2.5.20).**

### Why 2.5.x and not newer?

Starting with firmware **2.6.x**, Heltec appears to have dropped reliable USB serial support on the T190. The `meshtastic` Python library communicates with the radio over USB serial using Protobuf messages — if the firmware breaks serial the bot cannot connect at all. Staying on **2.5.20** is the last known-good version for this use case.

> **Rule of thumb:** if your serial connection is failing or timing out, downgrade the T190 firmware to 2.5.20 before debugging anything else.

### How to flash firmware 2.5.20 on the T190

The Meshtastic web flasher no longer lists 2.5.20 in its dropdown, but you can upload the binary directly:

1. Download **[firmware-esp32s3-2.5.20.4c97351.zip](https://github.com/meshtastic/firmware/releases/download/v2.5.20.4c97351/firmware-esp32s3-2.5.20.4c97351.zip)** from the official Meshtastic release
2. Extract the zip — inside you will find `firmware-heltec-vision-master-t190-2.5.20.4c97351.bin`
3. Plug the T190 into your PC via USB
4. Go to **[flasher.meshtastic.org](https://flasher.meshtastic.org)** in Chrome or Edge (requires WebSerial — Firefox does not work)
5. Select **Heltec Vision Master T190** as the device
6. Choose **Upload .bin** and select the file extracted in step 2
7. Click Flash and wait for it to complete — the T190 will reboot automatically

> The zip contains firmware for every ESP32-S3 device. The T190-specific file is the one with `heltec-vision-master-t190` in the name.

### Does inverting the T190 display affect the bot?

**No.** The T190's display orientation (normal or inverted) is a Meshtastic firmware setting that only controls what appears on the radio's own screen. The Pi communicates with the T190 exclusively over USB serial — it never reads or writes the radio's display. Flip/invert/rotate the T190 display freely without touching any code.

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

## ⚠️ Boot timing — wait before panicking

On first power-on, `meshbot.service` sometimes loses a race with the USB serial port and crashes once before restarting. systemd automatically restarts it within 5 seconds. **Always wait at least 15–20 seconds** after the boot selector splash screen before deciding Mode 3 failed to load. The LCD will go blank briefly during the restart and then show the Pi.Alert dashboard.

---

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/Coreymillia/RaspyMeshBot2.0.git
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

### 4. Configure Pi.Alert integration (Mode 3 only)

All Pi.Alert settings live in `config.json`. Add these fields (alongside your Groq key):

```json
{
    "groq_api_key": "your_groq_key",
    "pialert_base_url": "http://YOUR_PIALERT_IP/pialert/api/",
    "pialert_api_key": "your_pialert_api_key",
    "alert_node": "!your_node_id"
}
```

**How to find your Pi.Alert API key:** on your Pi.Alert host, run:
```bash
grep API_KEY /opt/pialert/config/pialert.conf
```

**How to find the node ID to DM on anomaly:** open the Meshtastic app or web client, find the node you want to receive alerts, and copy its ID — it starts with `!` followed by 8 hex characters (e.g. `!edac358a`). Set this as `alert_node` in `config.json`. **If you skip this step, alerts will be sent to a placeholder node and silently fail** — no harm done, but you won't receive them.

### 5. Check your serial port

The bot defaults to `/dev/ttyACM0`. Verify your radio's port:
```bash
ls /dev/ttyACM* /dev/ttyUSB*
```
If different, edit `SERIAL_PORT` near the top of `mesh_groq_ai_bot_oled.py`.

### 6. Install the systemd services

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

### 7. Reboot

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
# MeshBot with status screen (Mode 1)
python3 mesh_groq_ai_bot_oled.py

# MeshBot with Pi.Alert monitor + matrix rain (Mode 3)
touch /tmp/meshbot_screensaver
python3 mesh_groq_ai_bot_oled.py

# Boot selector (requires root for GPIO)
sudo python3 mode_selector.py
```

---

## File Overview

| File | Purpose |
|------|---------|
| `mesh_groq_ai_bot_oled.py` | Main bot — Meshtastic listener, Groq AI, Pi.Alert monitor, anomaly alerter, matrix rain |
| `mode_selector.py` | Boot menu displayed on the LCD HAT at startup |
| `LCD_1in44.py` | Waveshare 1.44" LCD SPI driver |
| `LCD_Config.py` | GPIO/SPI hardware configuration for the LCD |
| `config.example.json` | API key config template — copy to `config.json` and fill in |
| `requirements.txt` | Python dependencies |
| `systemd/` | Ready-to-use systemd service files for auto-start on boot |
| `images/` | Project photos |
| `pialert-patch/` | Daemon scripts for the Pi.Alert host (ARP watch, WiFi scan, BLE scan) |
| `.seen_anomalies.json` | Auto-generated — persists seen anomaly keys across reboots (do not edit) |

---

## Notes

- The bot runs as your regular user; `mode_selector.py` runs as root (needed for GPIO at boot)
- Matrix rain is optimised for the Pi Zero 2W — uses PIL column draws (~12 FPS) instead of per-pixel math
- The broadcast daily limit (0–3) is shown live on the LCD as `BC:N/d` and can be cycled on the fly without restarting the service
- Groq AI replies are capped at 100 tokens to keep mesh messages short
- `PYTHONUNBUFFERED=1` is set in the systemd service so log output appears immediately in `meshbot.log`
- The Pi.Alert poll interval (`PIALERT_POLL_S`) and screensaver idle timeout (`SCREENSAVER_IDLE_S`) are constants at the top of `mesh_groq_ai_bot_oled.py` and can be tuned freely

---

## License

MIT
