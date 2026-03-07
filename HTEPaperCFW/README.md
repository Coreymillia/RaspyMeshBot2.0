# HTEPaperCFW — Heltec Wireless Paper Custom Firmware

Custom Meshtastic firmware builds for the **Heltec Wireless Paper** (ESP32-S3 e-ink device), targeting USB serial compatibility with the RaspyMeshBot 2.0 project.

---

## Why this exists

RaspyMeshBot 2.0 talks to the Meshtastic radio over USB serial using the `meshtastic` Python library. The **Heltec Vision Master T190** is the primary tested radio and runs firmware **2.5.20**, which is the last confirmed working version for USB serial with this stack.

The **Heltec Wireless Paper** is an alternative radio candidate. It uses the same ESP32-S3 chip and the same `heltec_wifi_lora_32_V3` board definition. We previously built a working custom firmware for it at 2.7.11 (that build was not saved). This folder is the working environment to rebuild and modify 2.7.x firmware for the Wireless Paper.

---

## Firmware versions

| Folder | Tag | Purpose |
|--------|-----|---------|
| `firmware-2.7.19/` | `v2.7.19.bb3d6d5` | **Working copy** — modify and build here |
| `firmware-2.5.20/` | `v2.5.20.4c97351` | Reference only — source comparison, no submodules |

Both are **shallow clones** (`--depth 1`) to save disk space.

---

## Build output

Pre-built binaries are in `output/`:

| File | Use |
|------|-----|
| `firmware-heltec-wireless-paper-2.7.19.a092f6b.bin` | OTA update binary |
| `firmware-heltec-wireless-paper-2.7.19.a092f6b.factory.bin` | Full flash (use this for web flasher / esptool) |
| `littlefs-heltec-wireless-paper-2.7.19.a092f6b.bin` | Filesystem partition |

The unmodified 2.7.19 build compiles **cleanly** with zero errors (verified).

---

## How to build

### Prerequisites

- [PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/) (CLI — `pip install platformio`)
- Python 3.x, Git
- The `espressif32@6.12.0` platform installs automatically on first build

### Build the Wireless Paper target

```bash
cd firmware-2.7.19
pio run -e heltec-wireless-paper
```

First build takes ~12 minutes (downloads platform + all libs). Subsequent builds: ~2–3 minutes.

Output lands in `.pio/build/heltec-wireless-paper/`.

### Build the InkHUD variant (optional)

InkHUD is a newer e-ink UI framework included in 2.7.x. It won't affect serial behavior but gives a better display layout:

```bash
pio run -e heltec-wireless-paper-inkhud
```

---

## How to flash

### Option A — Meshtastic Web Flasher (easiest)

1. Go to **[flasher.meshtastic.org](https://flasher.meshtastic.org)** in Chrome or Edge
2. Select **Heltec Wireless Paper** from the device dropdown
3. If 2.7.19 isn't in the dropdown yet, choose **Upload .bin** and select the `.factory.bin` from `output/`
4. Click Flash — the device reboots automatically

### Option B — esptool (full control)

```bash
pip install esptool
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
  write_flash 0x0 output/firmware-heltec-wireless-paper-2.7.19.a092f6b.factory.bin
```

---

## Serial architecture — what we know

### USB mode
The Wireless Paper (like the T190) uses `board = heltec_wifi_lora_32_V3` in its PlatformIO config. The board definition sets:
```
-DARDUINO_USB_MODE=1
```
This enables **Hardware CDC** — the ESP32-S3's native USB peripheral acts as a CDC ACM device, appearing as `/dev/ttyACM0` on Linux. No external CH340/CP2102 chip is needed or present.

### Key differences between 2.5.20 and 2.7.19

| Item | 2.5.20 | 2.7.19 |
|------|--------|--------|
| Platform | `espressif32@6.9.0` | `espressif32@6.12.0` |
| `USE_EINK` location | `variant.h` | `platformio.ini` |
| `StreamAPI` | Simple poll loop | Refactored with `SERIAL_HAS_ON_RECEIVE` |
| New display files | — | `einkDetect.h`, `nicheGraphics.h` (InkHUD) |
| Extra env | — | `heltec-wireless-paper-inkhud` |

The `StreamAPI` refactor in 2.7.x (`src/mesh/StreamAPI.cpp`) introduces `SERIAL_HAS_ON_RECEIVE` — a callback-driven receive path. If the ESP32 platform version has a bug in `Serial.onReceive()`, this can cause silent serial failures. This is the most likely cause of serial connectivity problems.

### The fix to try first

In `firmware-2.7.19/variants/esp32s3/heltec_wireless_paper/platformio.ini`, add this to `build_flags`:
```ini
-D ARDUINO_USB_CDC_ON_BOOT=1
```
This forces the USB CDC stack to initialize before `setup()` runs, which resolves a race condition seen on some ESP32-S3 boards where the Python library connects before the device's CDC stack is ready.

---

## Next steps / planned modifications

- [ ] Test factory binary on the Wireless Paper device via web flasher
- [ ] Verify `/dev/ttyACM0` appears on host when device is connected
- [ ] Test `meshtastic --port /dev/ttyACM0 --info` to confirm serial API responds
- [ ] If serial fails: add `ARDUINO_USB_CDC_ON_BOOT=1` to variant platformio.ini and rebuild
- [ ] If still failing: investigate `StreamAPI.cpp` and disable `SERIAL_HAS_ON_RECEIVE` path for this variant
- [ ] Once serial is confirmed working: test full RaspyMeshBot Mode 3 integration

---

## Rebuilding from scratch (clone)

```bash
# Working build (2.7.19)
git clone --depth 1 --branch v2.7.19.bb3d6d5 --recurse-submodules --shallow-submodules \
  https://github.com/meshtastic/firmware.git firmware-2.7.19

# Reference source (2.5.20, no submodules needed)
git clone --depth 1 --branch v2.5.20.4c97351 --no-recurse-submodules \
  https://github.com/meshtastic/firmware.git firmware-2.5.20
```
