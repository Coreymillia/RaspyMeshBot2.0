<?php
// Check API Key
// Print Api-Key for debugging
// echo $_POST['api-key'];
$config_file = "../../config/pialert.conf";
$config_file_lines = file($config_file);
$config_file_lines_bypass = array_values(preg_grep('/^PIALERT_APIKEY\s.*/', $config_file_lines));
if ($config_file_lines_bypass != False) {
	$apikey_line = explode("'", $config_file_lines_bypass[0]);
	$pia_apikey = trim($apikey_line[1]);
} else {echo "No API-Key is set\n";exit;}

// Exit if API-Key is unequal
if ($_REQUEST['api-key'] != $pia_apikey) {
	echo "Wrong API-Key\n";
	exit;
}

// When API is correct
// include db.php
require '../php/server/db.php';
// Overwrite variable from db.php because of current working dir
$DBFILE = '../../db/pialert.db';

// Set maximum execution time to 30 seconds
ini_set('max_execution_time', '30');

// Secure and verify query
if (isset($_REQUEST['mac'])) {
	$mac_address = str_replace('-', ':', strtolower($_REQUEST['mac']));
	if (filter_var($mac_address, FILTER_VALIDATE_MAC) === False) {echo 'Invalid MAC Address.';exit;}
}

// Open DB
OpenDB();

// Action functions
if (isset($_REQUEST['get']) && !empty($_REQUEST['get'])) {
	$action = $_REQUEST['get'];
	switch ($action) {
	case 'mac-status':getStatusofMAC($mac_address);
		break;
	case 'all-online':getAllOnline();
		break;
	case 'all-offline':getAllOffline();
		break;
	case 'system-status':getSystemStatus();
		break;
	case 'all-online-icmp':getAllOnline_ICMP();
		break;
	case 'all-offline-icmp':getAllOffline_ICMP();
		break;
	case 'all-new':getAllNew();
		break;
	case 'all-down':getAllDown();
		break;
	case 'recent-events':getRecentEvents();
		break;
	case 'ip-changes':getIPChanges();
		break;
	case 'online-uptime':getOnlineUptime();
		break;
	case 'device-presence':getDevicePresence();
		break;
	case 'all-device-ips':getAllDeviceIPs();
		break;
	case 'arp-alerts':getArpAlerts();
		break;
	case 'arp-status':getArpStatus();
		break;
	case 'arp-reset':resetArpBaseline();
		break;
	case 'wifi-status':getWifiStatus();
		break;
	case 'wifi-detail':getWifiDetail();
		break;
	case 'wifi-scan':getWifiScan();
		break;
	case 'wifi-shady':getWifiShady();
		break;
	case 'ble-devices':getBleDevices();
		break;
	}
}

// set-device-name: POST api-key + mac + name  →  updates dev_Name in Devices table
if (isset($_REQUEST['set']) && $_REQUEST['set'] === 'device-name') {
	setDeviceName();
}

//example curl -k -X POST -F 'api-key=key' -F 'get=system-status' https://url/pialert/api/
function getSystemStatus() {
	# Detect Language
	foreach (glob("../../config/setting_language*") as $filename) {
		$pia_lang_selected = str_replace('setting_language_', '', basename($filename));
	}
	if (strlen($pia_lang_selected) == 0) {$pia_lang_selected = 'en_us';}
	$en_us = array("On", "Off");
	$de_de = array("Ein", "Aus");
	$es_es = array("En", "Off");
	$fr_fr = array("Allumé", "Éteint");
	$it_it = array("Acceso", "Spento");

	# Check Scanning Status
	if (file_exists("../../db/setting_stoparpscan")) {$temp_api_online_devices['Scanning'] = $$pia_lang_selected[1];} else { $temp_api_online_devices['Scanning'] = $$pia_lang_selected[0];}

	global $db;
	$results = $db->query('SELECT * FROM Online_History WHERE data_source="main_scan_local" ORDER BY Scan_Date DESC LIMIT 1');
	while ($row = $results->fetchArray()) {
		$time_raw = explode(' ', $row['Scan_Date']);
		$temp_api_online_devices['Last_Scan'] = $time_raw[1];
	}
	unset($results);
	$result = $db->query(
		'SELECT
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=0) as All_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=0 AND dev_PresentLastScan=1) as Online_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=0 AND dev_NewDevice=1) as New_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=0 AND dev_AlertDeviceDown=1 AND dev_PresentLastScan=0) as Down_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=0 AND dev_AlertDeviceDown=0 AND dev_PresentLastScan=0) as Offline_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_Archived=1) as Archived_Devices
   ');
	$row = $result->fetchArray(SQLITE3_NUM);
	$temp_api_online_devices['All_Devices'] = $row[0];
	$temp_api_online_devices['Online_Devices'] = $row[1];
	$temp_api_online_devices['New_Devices'] = $row[2];
	$temp_api_online_devices['Down_Devices'] = $row[3];
	$temp_api_online_devices['Offline_Devices'] = $row[4];
	$temp_api_online_devices['Archived_Devices'] = $row[5];
	unset($results);

	$result = $db->query(
		"SELECT
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=0) as All_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=0 AND dev_PresentLastScan=1) as Online_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=0 AND dev_NewDevice=1) as New_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=0 AND dev_AlertDeviceDown=1 AND dev_PresentLastScan=0) as Down_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=0 AND dev_AlertDeviceDown=0 AND dev_PresentLastScan=0) as Offline_Devices,
        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='local' AND dev_Archived=1) as Archived_Devices
   ");
	$subrow = $result->fetchArray(SQLITE3_NUM);
	$temp_api_online_devices['local']['All_Devices'] = $subrow[0];
	$temp_api_online_devices['local']['Online_Devices'] = $subrow[1];
	$temp_api_online_devices['local']['New_Devices'] = $subrow[2];
	$temp_api_online_devices['local']['Down_Devices'] = $subrow[3];
	$temp_api_online_devices['local']['Offline_Devices'] = $subrow[4];
	$temp_api_online_devices['local']['Archived_Devices'] = $subrow[5];
	unset($result);
	$results = $db->query('SELECT * FROM Satellites');
	while ($row = $results->fetchArray()) {
		$sat_token = $row['sat_token'];
		$sat_name = $row['sat_name'];

		$result = $db->query(
			"SELECT
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=0) as All_Devices,
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=0 AND dev_PresentLastScan=1) as Online_Devices,
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=0 AND dev_NewDevice=1) as New_Devices,
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=0 AND dev_AlertDeviceDown=1 AND dev_PresentLastScan=0) as Down_Devices,
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=0 AND dev_AlertDeviceDown=0 AND dev_PresentLastScan=0) as Offline_Devices,
	        (SELECT COUNT(*) FROM Devices WHERE dev_ScanSource='".$row['sat_token']."' AND dev_Archived=1) as Archived_Devices
	   ");
		$subrow = $result->fetchArray(SQLITE3_NUM);
		$temp_api_online_devices[$sat_name]['All_Devices'] = $subrow[0];
		$temp_api_online_devices[$sat_name]['Online_Devices'] = $subrow[1];
		$temp_api_online_devices[$sat_name]['New_Devices'] = $subrow[2];
		$temp_api_online_devices[$sat_name]['Down_Devices'] = $subrow[3];
		$temp_api_online_devices[$sat_name]['Offline_Devices'] = $subrow[4];
		$temp_api_online_devices[$sat_name]['Archived_Devices'] = $subrow[5];
		unset($result);
	}
	unset($results);
	$results = $db->query('SELECT * FROM Online_History WHERE data_source="icmp_scan" ORDER BY Scan_Date DESC LIMIT 1');
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['All_Devices_ICMP'] = $row['All_Devices'];
		$temp_api_online_devices['Offline_Devices_ICMP'] = $row['Down_Devices'];
		$temp_api_online_devices['Online_Devices_ICMP'] = $row['Online_Devices'];
	}
	unset($results);
	$results = $db->query('SELECT * FROM Online_History WHERE data_source="icmp_scan" ORDER BY Scan_Date DESC LIMIT 1');
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['All_Devices_ICMP'] = $row['All_Devices'];
		$temp_api_online_devices['Offline_Devices_ICMP'] = $row['Down_Devices'];
		$temp_api_online_devices['Online_Devices_ICMP'] = $row['Online_Devices'];
	}
	unset($results);
	$result = $db->query('SELECT COUNT(*) as count FROM Services');
	$row = $result->fetchArray(SQLITE3_ASSOC);
	if ($row) {
		$temp_api_online_devices['All_Services'] = $row['count'];
	}
	$api_online_devices = $temp_api_online_devices;
	$json = json_encode($api_online_devices);
	echo $json;
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=mac-status' -F 'mac=dc:a6:32:23:06:d3' https://url/pialert/api/
function getStatusofMAC($query_mac) {
	global $db;
	$sql = 'SELECT * FROM Devices WHERE dev_MAC="' . $query_mac . '"';
	$result = $db->query($sql);
	$row = $result->fetchArray(SQLITE3_ASSOC);
	$json = json_encode($row);
	echo $json;
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=all-online' https://url/pialert/api/
function getAllOnline() {
	global $db;
	$sql = 'SELECT * FROM Devices WHERE dev_PresentLastScan="1" ORDER BY dev_LastConnection DESC';
	$api_online_devices = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['dev_MAC'] = $row['dev_MAC'];
		$temp_api_online_devices['dev_Name'] = $row['dev_Name'];
		$temp_api_online_devices['dev_Vendor'] = $row['dev_Vendor'];
		$temp_api_online_devices['dev_LastIP'] = $row['dev_LastIP'];
		$temp_api_online_devices['dev_Infrastructure'] = $row['dev_Infrastructure'];
		$temp_api_online_devices['dev_Infrastructure_port'] = $row['dev_Infrastructure_port'];
		$api_online_devices[$i] = $temp_api_online_devices;
		$i++;
	}
	$json = json_encode($api_online_devices);
	echo $json;
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=all-offline' https://url/pialert/api/
function getAllOffline() {
	global $db;
	$sql = 'SELECT * FROM Devices WHERE dev_PresentLastScan="0"';
	$api_online_devices = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['dev_MAC'] = $row['dev_MAC'];
		$temp_api_online_devices['dev_Name'] = $row['dev_Name'];
		$temp_api_online_devices['dev_Vendor'] = $row['dev_Vendor'];
		$temp_api_online_devices['dev_LastIP'] = $row['dev_LastIP'];
		$temp_api_online_devices['dev_Infrastructure'] = $row['dev_Infrastructure'];
		$temp_api_online_devices['dev_Infrastructure_port'] = $row['dev_Infrastructure_port'];
		$api_online_devices[$i] = $temp_api_online_devices;
		$i++;
	}
	$json = json_encode($api_online_devices);
	echo $json;
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=all-online-icmp' https://url/pialert/api/
function getAllOnline_ICMP() {
	global $db;
	$sql = 'SELECT * FROM ICMP_Mon WHERE icmp_PresentLastScan="1"';
	$api_online_devices = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['icmp_ip'] = $row['icmp_ip'];
		$temp_api_online_devices['icmp_hostname'] = $row['icmp_hostname'];
		$temp_api_online_devices['icmp_avgrtt'] = $row['icmp_avgrtt'];
		$api_online_devices[$i] = $temp_api_online_devices;
		$i++;
	}
	$json = json_encode($api_online_devices);
	echo $json;
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=all-offline-icmp' https://url/pialert/api/
function getAllOffline_ICMP() {
	global $db;
	$sql = 'SELECT * FROM ICMP_Mon WHERE icmp_PresentLastScan="0"';
	$api_online_devices = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$temp_api_online_devices['icmp_ip'] = $row['icmp_ip'];
		$temp_api_online_devices['icmp_hostname'] = $row['icmp_hostname'];
		$api_online_devices[$i] = $temp_api_online_devices;
		$i++;
	}
	$json = json_encode($api_online_devices);
	echo $json;
	echo "\n";
}

function getAllNew() {
	global $db;
	$sql = 'SELECT dev_MAC, dev_Name, dev_Vendor, dev_LastIP, dev_FirstConnection FROM Devices WHERE dev_NewDevice="1" ORDER BY dev_FirstConnection DESC LIMIT 40';
	$results = $db->query($sql);
	$devices = array();
	$i = 0;
	while ($row = $results->fetchArray()) {
		$devices[$i] = array('dev_MAC'=>$row['dev_MAC'],'dev_Name'=>$row['dev_Name'],'dev_Vendor'=>$row['dev_Vendor'],'dev_LastIP'=>$row['dev_LastIP'],'dev_FirstConnection'=>$row['dev_FirstConnection']);
		$i++;
	}
	echo json_encode($devices);
	echo "
";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=all-down' https://url/pialert/api/
function getAllDown() {
	global $db;
	$sql = 'SELECT dev_Name, dev_LastIP, dev_Vendor FROM Devices
	        WHERE dev_AlertDeviceDown=1 AND dev_PresentLastScan=0 AND dev_Archived=0
	        ORDER BY dev_Name ASC';
	$results_array = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$results_array[$i]['dev_Name']   = $row['dev_Name'];
		$results_array[$i]['dev_LastIP'] = $row['dev_LastIP'];
		$results_array[$i]['dev_Vendor'] = $row['dev_Vendor'];
		$i++;
	}
	echo json_encode($results_array);
	echo "\n";
}

//example curl -k -X POST -F 'api-key=key' -F 'get=recent-events' https://url/pialert/api/
function getRecentEvents() {
	global $db;
	$sql = 'SELECT e.eve_DateTime, e.eve_EventType, e.eve_IP, d.dev_Name
	        FROM Events e
	        LEFT JOIN Devices d ON e.eve_MAC = d.dev_MAC
	        WHERE e.eve_EventType NOT LIKE "VOIDED%"
	        ORDER BY e.eve_DateTime DESC LIMIT 20';
	$results_array = array();
	$results = $db->query($sql);
	$i = 0;
	while ($row = $results->fetchArray()) {
		$results_array[$i]['eve_DateTime']  = $row['eve_DateTime'];
		$results_array[$i]['eve_EventType'] = $row['eve_EventType'];
		$results_array[$i]['eve_IP']        = $row['eve_IP'];
		$results_array[$i]['dev_Name']      = $row['dev_Name'] ? $row['dev_Name'] : 'Unknown';
		$i++;
	}
	echo json_encode($results_array);
	echo "\n";
}

// Returns online devices sorted newest-connection-first with server-computed
// uptime in minutes, so the ESP32 doesn't need its own clock.
function getOnlineUptime() {
	global $db;
	$now = time();
	$sql = 'SELECT dev_LastIP, dev_Name, dev_LastConnection
	        FROM Devices WHERE dev_PresentLastScan=1
	        ORDER BY dev_LastConnection DESC LIMIT 40';
	$results = $db->query($sql);
	$out = array();
	$i = 0;
	while ($row = $results->fetchArray()) {
		$conn_time = strtotime($row['dev_LastConnection']);
		$minutes = ($conn_time > 0) ? (int)floor(($now - $conn_time) / 60) : 0;
		$out[$i]['dev_LastIP'] = $row['dev_LastIP'];
		$out[$i]['dev_Name']   = $row['dev_Name'];
		$out[$i]['minutes']    = $minutes;
		$i++;
	}
	echo json_encode($out);
	echo "\n";
}

// Returns the 20 most recently seen (MAC, IP) pairs so you can track which
// MAC addresses have been using which IP addresses over time.
function getIPChanges() {
	global $db;
	$sql = 'SELECT e.eve_MAC, COALESCE(d.dev_Name, "Unknown") as dev_Name,
	               e.eve_IP, MAX(e.eve_DateTime) as last_seen
	        FROM Events e
	        LEFT JOIN Devices d ON e.eve_MAC = d.dev_MAC
	        WHERE e.eve_IP != "" AND e.eve_IP IS NOT NULL
	        GROUP BY e.eve_MAC, e.eve_IP
	        ORDER BY last_seen DESC
	        LIMIT 20';
	$results = $db->query($sql);
	$out = array();
	$i = 0;
	while ($row = $results->fetchArray()) {
		$out[$i]['eve_MAC']   = $row['eve_MAC'];
		$out[$i]['dev_Name']  = $row['dev_Name'];
		$out[$i]['eve_IP']    = $row['eve_IP'];
		$out[$i]['last_seen'] = $row['last_seen'];
		$i++;
	}
	echo json_encode($out);
	echo "\n";
}
// Returns each non-archived device with a count of how many distinct days
// in the last 30 days it appeared in any event.  Sorted most-present first.
function getDevicePresence() {
	global $db;
	$sql = "SELECT d.dev_Name, d.dev_LastIP,
	               COUNT(DISTINCT DATE(e.eve_DateTime)) as days_seen
	        FROM Devices d
	        LEFT JOIN Events e ON d.dev_MAC = e.eve_MAC
	          AND e.eve_DateTime > datetime('now', '-30 days')
	        WHERE d.dev_Archived = 0
	        GROUP BY d.dev_MAC
	        ORDER BY days_seen DESC, d.dev_Name ASC
	        LIMIT 40";
	$results = $db->query($sql);
	$out = array();
	$i = 0;
	while ($row = $results->fetchArray()) {
		$out[$i]['dev_Name']   = $row['dev_Name'];
		$out[$i]['dev_LastIP'] = $row['dev_LastIP'];
		$out[$i]['days_seen']  = (int)$row['days_seen'];
		$i++;
	}
	echo json_encode($out);
	echo "\n";
}

// Updates the friendly name for a device given its MAC address.
// POST params: api-key, set=device-name, mac=<mac>, name=<name>
// Skips update if the device already has a non-Unknown name (won't overwrite human labels).
function setDeviceName() {
	global $db;
	$mac  = isset($_REQUEST['mac'])  ? strtolower(trim($_REQUEST['mac']))  : '';
	$name = isset($_REQUEST['name']) ? trim($_REQUEST['name'])              : '';
	if (!filter_var($mac, FILTER_VALIDATE_MAC) || $name === '') {
		echo json_encode(['ok' => false, 'error' => 'Invalid mac or name']);
		return;
	}
	$mac = str_replace('-', ':', $mac);
	// Only rename if currently unnamed / unknown (preserve manual labels)
	$check = $db->query("SELECT dev_Name FROM Devices WHERE dev_MAC='" . SQLite3::escapeString($mac) . "'");
	$row = $check ? $check->fetchArray(SQLITE3_ASSOC) : null;
	if (!$row) {
		echo json_encode(['ok' => false, 'error' => 'Device not found']);
		return;
	}
	$existing = trim($row['dev_Name'] ?? '');
	if ($existing !== '' && strtolower($existing) !== 'unknown' && $existing !== '(unknown)') {
		echo json_encode(['ok' => true, 'skipped' => true, 'reason' => 'already named', 'name' => $existing]);
		return;
	}
	$safeName = SQLite3::escapeString($name);
	$db->exec("UPDATE Devices SET dev_Name='" . $safeName . "' WHERE dev_MAC='" . SQLite3::escapeString($mac) . "'");
	echo json_encode(['ok' => true, 'mac' => $mac, 'name' => $name]);
	echo "\n";
}

// Returns last known IP + MAC + name for all non-archived devices.
// Used by the ESP scanner to probe every known device, not just online ones
// (ESP32s don't respond to ping so Pi.Alert often marks them offline incorrectly).
function getAllDeviceIPs() {
	global $db;
	$sql = "SELECT dev_MAC, dev_Name, dev_LastIP FROM Devices
	        WHERE dev_Archived = 0 AND dev_LastIP != '' AND dev_LastIP IS NOT NULL
	        ORDER BY dev_LastConnection DESC";
	$results = $db->query($sql);
	$out = array();
	$i = 0;
	while ($row = $results->fetchArray()) {
		$out[$i]['dev_MAC']    = $row['dev_MAC'];
		$out[$i]['dev_Name']   = $row['dev_Name'];
		$out[$i]['dev_LastIP'] = $row['dev_LastIP'];
		$i++;
	}
	echo json_encode($out);
	echo "\n";
}

// Returns the 10 most recent ARP anomalies written by arpwatch_daemon.py.
// Reads alerts[] from arp_status.json (v2 daemon) or arp_alerts.json (v1).
function getArpAlerts() {
	$status_file = '/tmp/arp_status.json';
	$alerts_file = '/tmp/arp_alerts.json';
	// Prefer the v2 rich file; fall back to legacy v1 file
	$src = file_exists($status_file) ? $status_file : $alerts_file;
	if (!$src || !file_exists($src)) {
		echo json_encode([]);
		echo "\n";
		return;
	}
	$raw = file_get_contents($src);
	if ($raw === false) { echo json_encode([]); echo "\n"; return; }
	$data = json_decode($raw, true);
	// v2 file is an object with an "alerts" key; v1 file is a bare array
	if (isset($data['alerts'])) {
		echo json_encode($data['alerts']);
	} else {
		echo $raw;   // v1 bare array — return as-is
	}
	echo "\n";
}

// Returns the full ARP status object written by arpwatch_daemon.py v2.
// Fields: gateway_ip, gateway_mac_current, gateway_mac_expected, status,
//         last_arp_ts, arp_rate, duplicate_arp_count, gateway_mac_changes,
//         last_anomaly, last_anomaly_ts, top_talkers, last_events, alerts, iface
function getArpStatus() {
	$status_file = '/tmp/arp_status.json';
	if (!file_exists($status_file)) {
		echo json_encode(['status' => 'unavailable', 'error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($status_file);
	if ($json === false) {
		echo json_encode(['status' => 'unavailable', 'error' => 'read error']);
		echo "\n";
		return;
	}
	echo $json;
	echo "\n";
}

// Triggers an ARP baseline reset by writing a flag file that the daemon
// picks up in its next write cycle (avoids signal permission issues since
// the daemon runs as root and this PHP runs as www-data).
function resetArpBaseline() {
	$flag_file = '/tmp/arpwatch_reset_flag';
	if (file_put_contents($flag_file, '1') === false) {
		echo json_encode(['ok' => false, 'error' => 'could not write reset flag to /tmp']);
		echo "\n";
		return;
	}
	chmod($flag_file, 0644);
	echo json_encode(['ok' => true, 'msg' => 'reset flag written — daemon will clear counters within 5s']);
	echo "\n";
}


function getWifiStatus() {
	$f = '/tmp/wifi_status.json';
	if (!file_exists($f)) {
		echo json_encode(['status' => 'unavailable', 'error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($f);
	echo ($json !== false) ? $json : json_encode(['error' => 'read error']);
	echo "\n";
}

// Returns the Wi-Fi RF monitor detail written by wifi_monitor_daemon.py.
function getWifiDetail() {
	$f = '/tmp/wifi_detail.json';
	if (!file_exists($f)) {
		echo json_encode(['status' => 'unavailable', 'error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($f);
	echo ($json !== false) ? $json : json_encode(['error' => 'read error']);
	echo "\n";
}

// Returns the Wi-Fi AP scan list written by wifi_scan_daemon.py.
function getWifiScan() {
	$f = '/tmp/wifi_scan.json';
	if (!file_exists($f)) {
		echo json_encode(['error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($f);
	echo ($json !== false) ? $json : json_encode(['error' => 'read error']);
	echo "\n";
}

// Returns the shady-network list written by wifi_scan_daemon.py.
function getWifiShady() {
	$f = '/tmp/wifi_shady.json';
	if (!file_exists($f)) {
		echo json_encode(['error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($f);
	echo ($json !== false) ? $json : json_encode(['error' => 'read error']);
	echo "\n";
}

// Returns the BLE device list written by ble_scan_daemon.py.
function getBleDevices() {
	$f = '/tmp/ble_devices.json';
	if (!file_exists($f)) {
		echo json_encode(['error' => 'daemon not running']);
		echo "\n";
		return;
	}
	$json = file_get_contents($f);
	echo ($json !== false) ? $json : json_encode(['error' => 'read error']);
	echo "\n";
}
?>
