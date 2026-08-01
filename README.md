# fritz-unraid-power-monitor

Unraid-Plugin, das den Stromverbrauch einer FRITZ!Smart Energy Steckdose im
Dashboard anzeigt. Die Daten kommen aus einer InfluxDB, die von einem
Collector-Script befuellt wird.

![Version](https://img.shields.io/badge/version-2026.08.01-blue)
![Unraid](https://img.shields.io/badge/unraid-%3E%3D6.12-orange)

## Was es zeigt

- Aktuelle Leistung in Watt, farbcodiert nach frei waehlbaren Schwellen
- Sparkline ueber den gewaehlten Zeitraum
- Mittel-, Min- und Maximalwert
- Temperatur der Steckdose
- Hochrechnung auf kWh und Euro pro Jahr

## Voraussetzungen

- InfluxDB 1.x, erreichbar vom Unraid-Server
- Ein Collector, der Messwerte in die Datenbank schreibt (siehe unten)
- FRITZ!Box mit mindestens einer FRITZ!Smart Energy Steckdose

## Installation

In Unraid unter **Plugins → Install Plugin** folgende URL eintragen:

```
https://raw.githubusercontent.com/ezek1el3000/fritz-unraid-power-monitor/main/fritz.power.monitor.plg
```

Anschliessend unter **Settings → Stromverbrauch (FRITZ!)** konfigurieren.
Der Button *Verbindung testen* prueft, ob InfluxDB antwortet und Daten
fuer das angegebene Geraet vorliegen.

## Datenformat

Das Plugin erwartet ein Measurement `power` mit einem Tag `device` und
den Feldern `watt` sowie optional `celsius`:

```
power,device=steckdose_fileserver,ain=116300401843 watt=41.05,volt=237.0,energy_wh=2046608,celsius=21.5
```

## Collector

Das Plugin liest nur — Daten sammelt ein separates Script. Eine
funktionierende Variante liegt unter [`collector/fritzpower.py`](collector/fritzpower.py).

Konfiguration nach `/mnt/cache/appdata/fritzpower/config`:

```ini
FB_HOST=192.168.178.1
FB_USER=monitoring
FB_PASS=geheim
INFLUX_URL=http://192.168.178.10:8086
INFLUX_DB=fritzbox
```

Rechte einschraenken, da das Passwort im Klartext steht:

```bash
chmod 600 /mnt/cache/appdata/fritzpower/config
```

Datenbank anlegen:

```bash
docker exec -it Influxdb influx -execute "CREATE DATABASE fritzbox"
```

Als User-Script mit Cron `* * * * *` einrichten:

```bash
#!/bin/bash
python3 /mnt/cache/appdata/fritzpower/fritzpower.py
```

### Hinweis zum FRITZ!Box-Benutzer

Fuer den Collector genuegt ein Benutzer mit der Berechtigung
*Smart-Home-Geraete steuern*. Ein Konto mit Vollzugriff ist nicht noetig
und im Schadensfall deutlich unangenehmer.

## Entwicklung

```bash
./build.sh              # Version = aktuelles Datum
./build.sh 2026.08.15   # explizite Version
```

Das Script erzeugt `packages/fritz.power.monitor-<version>.txz` und zieht
die Version in der `.plg` nach.

## Lizenz

MIT
