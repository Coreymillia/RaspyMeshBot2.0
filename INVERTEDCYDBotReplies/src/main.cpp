/**
 * CYDBotReplies — Displays Groq AI DM replies and system alerts from RaspyMeshBot2.0
 *
 * Hardware: ESP32 "Cheap Yellow Display" (ESP32-2432S028)
 *   - ILI9341 320x240 TFT (landscape)
 *   - XPT2046 resistive touch
 *
 * Display: One full-screen message at a time.
 *   Touch left half  → newer message (back toward latest)
 *   Touch right half → older message
 *   New message      → auto-jumps to newest
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
XPT2046_Touchscreen ts(XPT2046_CS);  // no TIRQ — use polling mode

// ---- Settings + fetch ----
#include "Portal.h"
#include "BotMessages.h"

// ---- Colors (RGB565) ----
#define COL_BG         0x0000
#define COL_HEADER     0x0210   // dark green
#define COL_HEADER_TXT 0x07E0   // bright green
#define COL_DIM        0x2104   // very dark grey
#define COL_WHITE      0xFFFF
#define COL_DM         0x07E0   // green  — AI DM reply
#define COL_SYSTEM     0xFC60   // orange — Pi-Alert system alert
#define COL_SENSOR     0xFFE0   // yellow — telemetry/sensor alert
#define COL_GREY       0x6B4D   // dim grey for meta info
#define COL_NAV        0x4228   // very dim nav hint

// ---- Layout ----
#define SCREEN_W    320
#define SCREEN_H    240
#define HEADER_H    26
#define CONTENT_Y   (HEADER_H + 2)          // content starts here
#define CONTENT_H   (SCREEN_H - CONTENT_Y)  // 212px
#define CONTENT_W   (SCREEN_W - 8)          // 312px, 4px margins each side
#define MARGIN_X    4
#define LINE_H      13   // px per line at textSize=1 (wider for readability)

// ---- Per-line color palette — PuTTY-style terminal colors (RGB565) ----
static const uint16_t LINE_PALETTE[] = {
    0xFFFF,   // white
    0x07E0,   // bright green
    0x07FF,   // cyan
    0xFFE0,   // yellow
    0xFC60,   // orange
    0xF800,   // red
    0xF81F,   // magenta
    0xFB96,   // hot pink
    0xAFE0,   // lime green
    0x867D,   // sky blue
};
#define NUM_LINE_COLORS 10

// ---- State ----
static int  cur_idx      = 0;    // current message (0 = newest)
static bool needs_redraw = true;
static unsigned long last_fetch_ms = 0;
#define FETCH_INTERVAL_MS 5000

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

// Draw header: "BOT REPLIES  < x/N >"
static void drawHeader(int idx, int total) {
    gfx->fillRect(0, 0, SCREEN_W, HEADER_H, COL_HEADER);

    gfx->setTextColor(COL_HEADER_TXT, COL_HEADER);
    gfx->setTextSize(2);
    gfx->setCursor(6, 5);
    gfx->print("BOT REPLIES");

    // nav counter on right: "< 1/5 >"
    gfx->setTextSize(1);
    gfx->setTextColor(COL_WHITE, COL_HEADER);
    char nav[20];
    if (total == 0) {
        snprintf(nav, sizeof(nav), "no msgs");
    } else {
        snprintf(nav, sizeof(nav), "< %d/%d >", idx + 1, total);
    }
    gfx->setCursor(SCREEN_W - strlen(nav) * 6 - 4, 9);
    gfx->print(nav);
}

// Wrap and draw text with per-line color cycling and newline support.
static int drawWrappedText(const char* text, int x, int y, int maxW, int maxY) {
    gfx->setTextSize(1);
    int cols = maxW / 6;   // 6px per char at textSize=1
    int len  = strlen(text);
    int cy   = y;
    int ln   = 0;          // line counter drives color

    int i = 0;
    while (i < len && cy + LINE_H <= maxY) {
        // Skip carriage returns
        if (text[i] == '\r') { i++; continue; }
        // Newline → blank line spacing
        if (text[i] == '\n') { cy += LINE_H; i++; continue; }

        // Find end of this visual line
        int end = i + cols;
        if (end >= len) {
            end = len;
        } else {
            // Break early at any newline within range
            for (int j = i; j < end; j++) {
                if (text[j] == '\n' || text[j] == '\r') { end = j; break; }
            }
            // If still at cols, back up to last space
            if (end == i + cols) {
                int sp = end;
                while (sp > i && text[sp] != ' ') sp--;
                if (sp > i) end = sp;
            }
        }

        // Copy and trim trailing spaces
        char line[64];
        int llen = end - i;
        if (llen > 63) llen = 63;
        strncpy(line, text + i, llen);
        line[llen] = '\0';
        while (llen > 0 && line[llen - 1] == ' ') line[--llen] = '\0';

        if (llen > 0) {
            gfx->setTextColor(LINE_PALETTE[ln % NUM_LINE_COLORS], COL_BG);
            gfx->setCursor(x, cy);
            gfx->print(line);
            ln++;
        }
        cy += LINE_H;

        i = end;
        // Advance past the space or newline we broke on
        if (i < len && (text[i] == ' ' || text[i] == '\n' || text[i] == '\r')) i++;
        while (i < len && text[i] == ' ') i++;
    }
    return cy;
}

// Draw the current message full-screen
static void drawCurrentMessage() {
    gfx->fillRect(0, CONTENT_Y, SCREEN_W, CONTENT_H, COL_BG);

    if (bm_count == 0) {
        gfx->setTextColor(COL_GREY, COL_BG);
        gfx->setTextSize(1);
        gfx->setCursor(MARGIN_X, CONTENT_Y + 20);
        gfx->print("No messages yet.");
        gfx->setCursor(MARGIN_X, CONTENT_Y + 34);
        gfx->print("Waiting for bot activity...");
        return;
    }

    if (cur_idx >= bm_count) cur_idx = bm_count - 1;
    BotMsg& m = bm_msgs[cur_idx];
    uint16_t col = msgColor(m.type);

    int cy = CONTENT_Y + 4;

    // Type badge + recipient + timestamp on one line
    gfx->setTextSize(1);
    gfx->setTextColor(col, COL_BG);
    gfx->setCursor(MARGIN_X, cy);
    char badge[32];
    snprintf(badge, sizeof(badge), "[%s]", msgLabel(m.type));
    gfx->print(badge);

    if (m.to[0]) {
        int bx = MARGIN_X + strlen(badge) * 6 + 4;
        gfx->setTextColor(COL_GREY, COL_BG);
        gfx->setCursor(bx, cy);
        gfx->print(m.to);
    }

    // Timestamp right-aligned
    if (m.ts[0]) {
        gfx->setTextColor(COL_GREY, COL_BG);
        gfx->setCursor(SCREEN_W - strlen(m.ts) * 6 - MARGIN_X, cy);
        gfx->print(m.ts);
    }

    cy += LINE_H + 2;
    // Divider under meta line
    gfx->drawFastHLine(MARGIN_X, cy, CONTENT_W, COL_DIM);
    cy += 5;

    // Full message body — use all remaining content area
    drawWrappedText(m.text, MARGIN_X, cy, CONTENT_W, SCREEN_H - 2);
}

static void fullRedraw() {
    drawHeader(cur_idx, bm_count);
    drawCurrentMessage();
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
#define TOUCH_DEBOUNCE_MS 450

static void handleTouch() {
    if (!ts.touched()) return;
    unsigned long now = millis();
    if (now - last_touch_ms < TOUCH_DEBOUNCE_MS) return;
    last_touch_ms = now;

    TS_Point p = ts.getPoint();
    // Map raw touch X to screen X in landscape (rotation 1)
    int tx = map(p.x, 200, 3800, 0, SCREEN_W);

    int midX = SCREEN_W / 2;
    if (tx < midX) {
        // left half → newer (lower index)
        if (cur_idx > 0) { cur_idx--; needs_redraw = true; }
    } else {
        // right half → older (higher index)
        if (cur_idx < bm_count - 1) { cur_idx++; needs_redraw = true; }
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
            cur_idx = 0;   // jump to newest
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
