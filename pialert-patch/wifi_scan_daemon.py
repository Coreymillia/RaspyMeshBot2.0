#!/usr/bin/env python3
"""wifi_scan_daemon.py — Wi-Fi AP scanner + shady-network scorer.

Uses 'iw dev wlan0 scan' (works in managed mode, no monitor mode needed).
Writes /tmp/wifi_scan.json  — full sorted AP list
       /tmp/wifi_shady.json — APs that exceed the shady-score threshold

Run as root (or with cap_net_admin) so iw can trigger active scans.
"""

import json, re, subprocess, time, datetime, os

INTERFACE      = 'wlan0'
SCAN_INTERVAL  = 30          # seconds between scans
SCAN_FILE      = '/tmp/wifi_scan.json'
SHADY_FILE     = '/tmp/wifi_shady.json'
SHADY_THRESHOLD = 15         # score >= this → include in shady list

# SSID fragments that raise suspicion (lowercase match)
SUSPICIOUS_SSID_FRAGMENTS = [
    'free wifi', 'freewifi', 'free_wifi', 'public wifi',
    'hotel wifi', 'airport wifi', 'pineapple', 'pine ap',
    'evil twin', 'hacker', 'linvor', 'dps_', 'atm_',
]

# OUI prefixes associated with cheap pentesting / skimmer hardware (8-char format)
SUSPICIOUS_OUI = {
    'ac:23:3f',  # cheap BLE/WiFi modules
    'f8:1d:78',
    '00:06:66',  # HC-06 modules sometimes used in skimmers
}


# ---------------------------------------------------------------------------
# iw scan
# ---------------------------------------------------------------------------
def run_scan():
    try:
        r = subprocess.run(
            ['iw', 'dev', INTERFACE, 'scan'],
            capture_output=True, text=True, timeout=25
        )
        return r.stdout
    except Exception:
        return ''


def parse_scan(output):
    """Parse 'iw dev wlan0 scan' output → list of AP dicts."""
    aps = []
    cur = None

    for raw in output.splitlines():
        line = raw.strip()

        # ── New BSS block ────────────────────────────────────────────────────
        m = re.match(r'^BSS ([0-9a-f:]{17})', line, re.I)
        if m:
            if cur:
                aps.append(_finalise(cur))
            cur = {
                'bssid': m.group(1).lower(),
                'ssid': None, 'hidden': False,
                'channel': 0, 'freq': 0, 'rssi': -100,
                '_wpa3': False, '_wpa2': False, '_wpa': False,
                '_wep': False, '_privacy': False,
            }
            continue

        if cur is None:
            continue

        # ── SSID ─────────────────────────────────────────────────────────────
        m = re.match(r'^SSID: (.*)', line)
        if m:
            ssid = m.group(1).strip()
            cur['ssid'] = ssid
            cur['hidden'] = (len(ssid) == 0)
            continue

        # ── Frequency / channel ───────────────────────────────────────────────
        m = re.match(r'^freq: (\d+)', line)
        if m:
            freq = int(m.group(1))
            cur['freq'] = freq
            if 2412 <= freq <= 2484:
                cur['channel'] = 14 if freq == 2484 else (freq - 2407) // 5
            elif 5000 <= freq <= 5900:
                cur['channel'] = (freq - 5000) // 5
            continue

        # ── Signal ───────────────────────────────────────────────────────────
        m = re.match(r'^signal: ([-\d.]+) dBm', line)
        if m:
            cur['rssi'] = int(float(m.group(1)))
            continue

        # ── Security hints ────────────────────────────────────────────────────
        if 'WPA3' in line or 'SAE' in line:
            cur['_wpa3'] = True
        if 'RSN:' in line or 'WPA2' in line:
            cur['_wpa2'] = True
        if re.match(r'^WPA:\s', line) or 'WPA Version' in line:
            cur['_wpa'] = True
        if 'WEP' in line:
            cur['_wep'] = True
        if 'Privacy' in line:
            cur['_privacy'] = True

    if cur:
        aps.append(_finalise(cur))

    return aps


def _finalise(ap):
    if ap.get('_wpa3'):
        ap['security'] = 'WPA3'
    elif ap.get('_wpa2'):
        ap['security'] = 'WPA2'
    elif ap.get('_wpa'):
        ap['security'] = 'WPA'
    elif ap.get('_wep') or ap.get('_privacy'):
        ap['security'] = 'WEP'
    else:
        ap['security'] = 'Open'
    if ap['ssid'] is None:
        ap['ssid'] = ''
        ap['hidden'] = True
    for k in ('_wpa3', '_wpa2', '_wpa', '_wep', '_privacy'):
        ap.pop(k, None)
    return ap


# ---------------------------------------------------------------------------
# Shady scoring
# ---------------------------------------------------------------------------
def score_shady(aps):
    # Build SSID → [ap] map for evil-twin detection
    ssid_map = {}
    for ap in aps:
        key = ap['ssid'].lower()
        ssid_map.setdefault(key, []).append(ap)

    results = []
    for ap in aps:
        score = 0
        flags = []
        ssid_lower = ap['ssid'].lower()

        # Evil twin: same non-empty SSID from multiple BSSIDs
        if ap['ssid'] and len(ssid_map[ssid_lower]) > 1:
            score += 40
            flags.append('evil_twin')

        # Open network
        if ap['security'] == 'Open' and ap['ssid']:
            score += 20
            flags.append('open')

        # Hidden SSID
        if ap['hidden']:
            score += 10
            flags.append('hidden_ssid')

        # Unusually strong signal (possible rogue nearby)
        if ap['rssi'] > -30:
            score += 15
            flags.append('strong_signal')

        # Suspicious SSID keyword
        for frag in SUSPICIOUS_SSID_FRAGMENTS:
            if frag in ssid_lower:
                score += 25
                flags.append('suspicious_ssid')
                break

        # Random-looking SSID (all hex chars)
        clean = ssid_lower.replace(':', '').replace('-', '').replace('_', '')
        if ap['ssid'] and re.match(r'^[0-9a-f]{6,}$', clean):
            score += 15
            flags.append('random_ssid')

        # Suspicious OUI
        if ap['bssid'][:8] in SUSPICIOUS_OUI:
            score += 30
            flags.append('suspicious_oui')

        results.append({'score': min(score, 100), 'flags': flags, 'ap': ap})

    return results


def channel_counts(aps):
    counts = {}
    for ap in aps:
        ch = str(ap['channel'])
        counts[ch] = counts.get(ch, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Write JSON
# ---------------------------------------------------------------------------
def write_json(aps):
    ts = datetime.datetime.now().strftime('%H:%M:%S')

    # ── wifi_scan.json ────────────────────────────────────────────────────────
    scan_data = {
        'ap_count': len(aps),
        'aps': [
            {
                'ssid':     ap['ssid'] if ap['ssid'] else '<hidden>',
                'bssid':    ap['bssid'],
                'channel':  ap['channel'],
                'rssi':     ap['rssi'],
                'security': ap['security'],
                'hidden':   ap['hidden'],
            }
            for ap in sorted(aps, key=lambda x: x['rssi'], reverse=True)
        ],
        'scan_time':     ts,
        'channel_counts': channel_counts(aps),
    }
    with open(SCAN_FILE, 'w') as f:
        json.dump(scan_data, f)

    # ── wifi_shady.json ───────────────────────────────────────────────────────
    scored = score_shady(aps)
    shady  = sorted([s for s in scored if s['score'] >= SHADY_THRESHOLD],
                    key=lambda x: x['score'], reverse=True)

    max_score = max((s['score'] for s in scored), default=0)
    status = 'threat' if max_score >= 60 else 'warning' if max_score >= 30 else 'clean'

    shady_data = {
        'shady_count': len(shady),
        'max_score':   max_score,
        'status':      status,
        'shady_aps': [
            {
                'ssid':     s['ap']['ssid'] if s['ap']['ssid'] else '<hidden>',
                'bssid':    s['ap']['bssid'],
                'channel':  s['ap']['channel'],
                'rssi':     s['ap']['rssi'],
                'security': s['ap']['security'],
                'score':    s['score'],
                'flags':    s['flags'],
            }
            for s in shady
        ],
        'scan_time': ts,
    }
    with open(SHADY_FILE, 'w') as f:
        json.dump(shady_data, f)

    return len(aps), len(shady)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    print(f'[wifi_scan_daemon] Starting on {INTERFACE}, interval={SCAN_INTERVAL}s',
          flush=True)
    while True:
        try:
            output     = run_scan()
            aps        = parse_scan(output)
            ap_cnt, sh = write_json(aps)
            print(f'[wifi_scan_daemon] {ap_cnt} APs, {sh} shady', flush=True)
        except Exception as e:
            print(f'[wifi_scan_daemon] Error: {e}', flush=True)
        time.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    main()
