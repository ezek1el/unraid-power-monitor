<?PHP
/*
 * fritz-unraid-power-monitor - API-Backend
 * Fragt InfluxDB ab und liefert JSON fuer das Dashboard-Tile.
 *
 * GET-basiert, weil Unraid 7.x/nginx bei POST auf Plugin-Endpunkte zickt.
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate');

$cfgFile = '/boot/config/plugins/fritz.power.monitor/settings.cfg';

$defaults = [
    'INFLUX_URL'   => 'http://192.168.88.100:8086',
    'INFLUX_DB'    => 'fritzbox',
    'INFLUX_USER'  => '',
    'INFLUX_PASS'  => '',
    'DEVICE'       => 'steckdose_fileserver',
    'PRICE_KWH'    => '0.2656',
    'HISTORY_HRS'  => '6',
];

function loadConfig(string $path, array $defaults): array {
    $cfg = $defaults;
    if (is_file($path)) {
        $parsed = @parse_ini_file($path);
        if (is_array($parsed)) {
            foreach ($parsed as $k => $v) {
                $cfg[strtoupper($k)] = $v;
            }
        }
    }
    return $cfg;
}

function influxQuery(array $cfg, string $query) {
    $url = rtrim($cfg['INFLUX_URL'], '/') . '/query?' . http_build_query([
        'db'    => $cfg['INFLUX_DB'],
        'q'     => $query,
        'epoch' => 'ms',
    ]);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 8,
        CURLOPT_CONNECTTIMEOUT => 4,
    ]);

    // Auth nur setzen, wenn konfiguriert - InfluxDB laeuft haeufig ohne
    if ($cfg['INFLUX_USER'] !== '') {
        curl_setopt($ch, CURLOPT_USERPWD, $cfg['INFLUX_USER'] . ':' . $cfg['INFLUX_PASS']);
    }

    $body = curl_exec($ch);
    $err  = curl_error($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($body === false) {
        return ['error' => 'Verbindung zu InfluxDB fehlgeschlagen: ' . $err];
    }
    if ($code !== 200) {
        return ['error' => "InfluxDB antwortete mit HTTP $code"];
    }

    $json = json_decode($body, true);
    if (!is_array($json)) {
        return ['error' => 'Antwort von InfluxDB nicht lesbar'];
    }
    if (isset($json['error'])) {
        return ['error' => 'InfluxDB: ' . $json['error']];
    }
    return $json;
}

/* Holt die Wertereihe aus einer InfluxDB-Antwort. Leeres Array, wenn nichts da. */
function seriesValues(array $result): array {
    return $result['results'][0]['series'][0]['values'] ?? [];
}

$cfg    = loadConfig($cfgFile, $defaults);
$device = preg_replace('/[^a-z0-9_]/', '', strtolower($cfg['DEVICE']));
$hours  = max(1, min(168, (int)$cfg['HISTORY_HRS']));
$price  = (float)$cfg['PRICE_KWH'];

/*
 * Eine Query mit mehreren Statements spart Round-Trips.
 * Reihenfolge: aktueller Wert, Durchschnitt/Min/Max, Verlauf.
 */
$q = implode('; ', [
    "SELECT last(\"watt\") AS watt, last(\"celsius\") AS celsius FROM \"power\" WHERE \"device\"='$device'",
    "SELECT mean(\"watt\") AS avg, min(\"watt\") AS min, max(\"watt\") AS max FROM \"power\" WHERE \"device\"='$device' AND time > now() - {$hours}h",
    "SELECT mean(\"watt\") FROM \"power\" WHERE \"device\"='$device' AND time > now() - {$hours}h GROUP BY time(5m) fill(none)",
]);

$res = influxQuery($cfg, $q);

if (isset($res['error'])) {
    echo json_encode(['ok' => false, 'error' => $res['error']]);
    exit;
}

$cur  = $res['results'][0]['series'][0]['values'][0] ?? null;
$agg  = $res['results'][1]['series'][0]['values'][0] ?? null;
$hist = $res['results'][2]['series'][0]['values'] ?? [];

if ($cur === null) {
    echo json_encode([
        'ok'    => false,
        'error' => "Keine Daten fuer Geraet '$device' gefunden",
    ]);
    exit;
}

$watt = (float)($cur[1] ?? 0);

/* Watt -> Euro/Jahr: W * 24h * 365d / 1000 * Preis */
$costFactor = 24 * 365 / 1000 * $price;

$series = [];
foreach ($hist as $point) {
    if ($point[1] === null) continue;
    $series[] = ['t' => (int)$point[0], 'w' => round((float)$point[1], 2)];
}

echo json_encode([
    'ok'       => true,
    'device'   => $device,
    'ts'       => (int)($cur[0] ?? 0),
    'watt'     => round($watt, 2),
    'celsius'  => $cur[2] !== null ? round((float)$cur[2], 1) : null,
    'avg'      => $agg && $agg[1] !== null ? round((float)$agg[1], 2) : null,
    'min'      => $agg && $agg[2] !== null ? round((float)$agg[2], 2) : null,
    'max'      => $agg && $agg[3] !== null ? round((float)$agg[3], 2) : null,
    'eur_year' => round($watt * $costFactor, 2),
    'kwh_year' => round($watt * 24 * 365 / 1000, 1),
    'hours'    => $hours,
    'price'    => $price,
    'series'   => $series,
]);
