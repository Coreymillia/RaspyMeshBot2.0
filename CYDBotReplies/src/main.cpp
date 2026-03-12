/**
 * CYDBotReplies — Displays Groq AI DM replies and system alerts from RaspyMeshBot2.0
 *
 * Hardware: ESP32 "Cheap Yellow Display" (ESP32-2432S028)
 *   - ILI9341 320x240 TFT (landscape)
 *   - XPT2046 resistive touch
 *
 * Display: Scrolling message list, color-coded by type.
 *   Touch upper half  → scroll up (older messages)
 *   Touch lower half  → scroll down (newer messages)
 *   New message       → auto-jumps to newest (index 0)
 *
 * Pi bot runs HTTP server on port 8766.
 * CYD polls GET /messages every 5 seconds.
 */

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <XPT2046_Touchscreen.h>
#include <SPI.h>
#include <WiFi.h>

// ---- Display pins (CYD standard) ----
#define GFX_BL 21
Arduino_DataBus *bus = new Arduino_ESP32SPI(2, 15, 14, 13, 12);
Arduino_GFX *gfx = new Arduino_ILI9341(bus, GFX_NOT_DEFINED, 1 /* landscape */);

// ---- Touch pins (CYD standard) ----
#define XPT2046_CS   33
#define XPT2046_CLK  25
#define XPT2046_MOSI 32
#define XPT2046_MISO 39
SPIClass touchSPI(VSPI);
XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_MOSI);

// ---- Settings + fetch ----
#include "Portal.h"
#include "BotMessages.h"

// ---- Colors (RGB565) ----
#define COL_BG        0x0000   // black
#define COL_HEADER    0x0210   // dark green
#define COL_HEADER_TXT 0x07E0  // bright green
#define COL_DIM       0x2104   // very dark grey divider
#define COL_WHITE     0xFFFF
#define COL_DM        0x07E0   // green  — AI DM reply
#define COL_SYSTEM    0xFC60   // orange — Pi-Alert system alert
#define COL_SENSOR    0xFFE0   // yellow — telemetry/sensor alert
#define COL_SCROLLBAR 0x39C7   // mid grey
#define COL_GREY      0x6B4D   // dim grey for timestamps/to

// ---- Layout ----
#define SCREEN_W      320
#define SCREEN_H      240
#define HEADER_H      22
#define MSG_ROW_H     36    // height per message: 2 text lines + padding
#define ROWS_Y        (HEADER_H + 2)
#define VISIBLE_MSGS  ((SCREEN_H - ROWS_Y) / MSG_ROW_H)   // ~6

// ---- State ----
static int  scroll_offset  = 0;  // 0 = newest at top
static bool needs_redraw   = true;
static unsigned long last_fetch_ms = 0;
#define FETCH_INTERVAL_MS  5000

// ---- Helpers ----
static uint16_t msgColor(const char* type) {
    if (strncmp(type, "dm",     2) == 0) return COL_DM;
    if (strncmp(type, "system", 6) == 0) return COL_SYSTEM;
    if (strncmp(type, "sensor", 6) == 0) return COL_SENSOR;
    return COL_WHITE;
}

static const char* msgLabel(const char* type) {
    if (strncmp(type, "dm",     2) == 0) return "DM";
    if (strncmp(type, "system", 6) == 0) return "SYS";
    if (strncmp(type, "sensor", 6) == 0) return "ENV";
    return "MSG";
}

// Draw the header bar
static void drawHeader() {
    gfx->fillRect(0, 0, SCREEN_W, HEADER_H, COL_HEADER);
    gfx->setTextColor(COL_HEADER_TXT);
    gfx->setTextSize(2);
    gfx->setCursor(6, 3);
    gfx->print("BOT REPLIES");

    // message count on the right
    gfx->setTextSize(1);
    gfx->setTextColor(COL_GREY);
    char buf[16];
    snprintf(buf, sizeof(buf), "%d msgs", bm_count);
    gfx->setCursor(SCREEN_W - strlen(buf) * 6 - 4, 7);
    gfx->print(buf);
}

// Draw the thin scroll indicator on the right edge
static void drawScrollBar() {
    if (bm_count <= VISIBLE_MSGS) return;
    int track_h = SCREEN_H - ROWS_Y;
    int thumb_h = track_h * VISIBLE_MSGS / bm_count;
    if (thumb_h < 8) thumb_h = 8;
    int max_off = bm_count - VISIBLE_MSGS;
    int thumb_y = ROWS_Y + (track_h - thumb_h) * scroll_offset / max_off;
    gfx->fillRect(SCREEN_W - 4, ROWS_Y, 4, track_h, COL_DIM);
    gfx->fillRect(SCREEN_W - 4, thumb_y, 4, thumb_h, COL_SCROLLBAR);
}

// Wrap text into lines fitting msgW chars per line, draw at (x, y)
// Returns height used
static int drawWrappedText(const char* text, int x, int y, int maxW_px,
                            uint16_t color, int maxLines) {
    gfx->setTextSize(1);
    gfx->setTextColor(color);
    int charW  = 6;                     // 6px per char at textSize 1
    int cols   = maxW_px / charW;
    int len    = strlen(text);
    int drawn  = 0;
    int cy     = y;
    int lineH  = 9;

    for (int i = 0; i < len && drawn < maxLines; ) {
        // find break point
        int end = i + cols;
        if (end >= len) {
            end = len;
        } else {
            // prefer space break
            int sp = end;
            while (sp > i && text[sp] != ' ') sp--;
            if (sp > i) end = sp + 1;   // include the space then skip it
        }
        char line[64];
        int llen = end - i;
        if (llen > 63) llen = 63;
        strncpy(line, text + i, llen);
        line[llen] = 0;
        gfx->setCursor(x, cy);
        gfx->print(line);
        cy += lineH;
        drawn++;
        i = end;
        // skip leading space on next line
        while (i < len && text[i] == ' ') i++;
    }
    return cy - y;
}

// Draw a single message row at pixel y
static void drawMsgRow(int idx, int y) {
    if (idx < 0 || idx >= bm_count) return;
    BotMsg& m = bm_msgs[idx];
    uint16_t col = msgColor(m.type);

    // background
    gfx->fillRect(0, y, SCREEN_W - 4, MSG_ROW_H - 1, COL_BG);

    // top line: [label] [to] ... [ts]
    gfx->setTextSize(1);
    gfx->setTextColor(col);
    gfx->setCursor(2, y + 2);
    char label[6];
    snprintf(label, sizeof(label), "[%s]", msgLabel(m.type));
    gfx->print(label);

    gfx->setTextColor(COL_GREY);
    int lx = 2 + strlen(label) * 6 + 4;
    gfx->setCursor(lx, y + 2);
    gfx->print(m.to[0] ? m.to : "—");

    // timestamp at far right
    gfx->setTextColor(COL_DIM + 0x2104);
    gfx->setCursor(SCREEN_W - strlen(m.ts) * 6 - 8, y + 2);
    gfx->print(m.ts);

    // second line: message text (up to 2 lines)
    int textW = SCREEN_W - 10;
    drawWrappedText(m.text, 2, y + 13, textW, COL_WHITE, 2);

    // divider
    gfx->drawFastHLine(0, y + MSG_ROW_H - 1, SCREEN_W - 4, COL_DIM);
}

// Redraw the full message list
static void drawMessages() {
    gfx->fillRect(0, ROWS_Y, SCREEN_W, SCREEN_H - ROWS_Y, COL_BG);

    if (bm_count == 0) {
        gfx->setTextColor(COL_GREY);
        gfx->setTextSize(1);
        gfx->setCursor(10, ROWS_Y + 20);
        gfx->print("No messages yet.");
        gfx->setCursor(10, ROWS_Y + 34);
        gfx->print("Waiting for bot activity...");
    } else {
        for (int i = 0; i < VISIBLE_MSGS; i++) {
            int idx = scroll_offset + i;
            if (idx >= bm_count) break;
            drawMsgRow(idx, ROWS_Y + i * MSG_ROW_H);
        }
    }
    drawScrollBar();
}

static void fullRedraw() {
    drawHeader();
    drawMessages();
    needs_redraw = false;
}

// Show a status message in the middle of the screen
static void showStatus(const char* line1, const char* line2 = nullptr) {
    gfx->fillScreen(COL_BG);
    gfx->setTextColor(COL_GREY);
    gfx->setTextSize(1);
    gfx->setCursor(10, 110);
    gfx->print(line1);
    if (line2) {
        gfx->setCursor(10, 122);
        gfx->print(line2);
    }
}

// ---- Touch handling ----
static unsigned long last_touch_ms = 0;
#define TOUCH_DEBOUNCE_MS 400

static void handleTouch() {
    if (!ts.touched()) return;
    unsigned long now = millis();
    if (now - last_touch_ms < TOUCH_DEBOUNCE_MS) return;
    last_touch_ms = now;

    TS_Point p = ts.getPoint();
    // XPT2046 raw: x 200-3800, y 200-3800 for landscape
    // Map to screen coords (landscape, rotation 1)
    int tx = map(p.x, 200, 3800, 0, SCREEN_H);   // raw X → screen Y
    int ty = map(p.y, 3800, 200, 0, SCREEN_W);   // raw Y → screen X (inverted)

    int midY = SCREEN_H / 2;
    int max_off = bm_count - VISIBLE_MSGS;
    if (max_off < 0) max_off = 0;

    if (tx < midY) {
        // upper half → scroll up (older messages = higher index)
        if (scroll_offset < max_off) {
            scroll_offset++;
            needs_redraw = true;
        }
    } else {
        // lower half → scroll down (newer = lower index)
        if (scroll_offset > 0) {
            scroll_offset--;
            needs_redraw = true;
        }
    }
}

// ---- Setup ----
void setup() {
    Serial.begin(115200);
    Serial.println("CYDBotReplies starting...");

    if (!gfx->begin()) {
        Serial.println("gfx->begin() failed!");
    }
    gfx->invertDisplay(true);
    gfx->fillScreen(COL_BG);

    pinMode(GFX_BL, OUTPUT);
    digitalWrite(GFX_BL, HIGH);

    pinMode(0, INPUT_PULLUP);

    touchSPI.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
    ts.begin(touchSPI);
    ts.setRotation(1);

    brLoadSettings();

    bool showPortal = !br_has_settings;
    // Hold BOOT button at startup to force portal
    showStatus("Hold BOOT to change settings...");
    for (int i = 0; i < 30 && !showPortal; i++) {
        if (digitalRead(0) == LOW) showPortal = true;
        delay(100);
    }

    if (showPortal) {
        brRunPortal(gfx);  // never returns — reboots on save
    }

    // Connect WiFi
    if (!brConnectWiFi(gfx)) {
        showStatus("WiFi failed.", "Hold BOOT on reboot to reconfigure.");
        delay(5000);
        ESP.restart();
    }

    Serial.printf("WiFi OK, IP: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("Bot endpoint: http://%s:%u/messages\n", br_bot_ip, br_bot_port);

    // Initial fetch
    showStatus("Connected.", "Fetching messages...");
    bmFetch(br_bot_ip, br_bot_port);

    fullRedraw();
}

// ---- Loop ----
void loop() {
    handleTouch();

    unsigned long now = millis();
    if (now - last_fetch_ms >= FETCH_INTERVAL_MS) {
        last_fetch_ms = now;
        bool ok = bmFetch(br_bot_ip, br_bot_port);

        if (bm_new_msg) {
            scroll_offset = 0;   // jump to newest
            needs_redraw  = true;
        } else if (ok) {
            // Update header count even if no new messages
            needs_redraw = true;
        }
    }

    if (needs_redraw) {
        fullRedraw();
    }

    delay(20);
}
