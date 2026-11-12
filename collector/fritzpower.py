#!/usr/bin/env python3
"""Liest FRITZ!Smart Energy Steckdosen aus und schreibt nach InfluxDB.

Aufruf: fritzpower.py [-c CONFIG] [-v] [-n]
"""
import argparse
import base64
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.client import HTTPException

CFG_PATH = "/mnt/cache/appdata/fritzpower/config"
SID_CACHE = "/tmp/fritzpower.sid"
LOGIN_PAUSE = "/tmp/fritzpower.pause"

TIMEOUT = 10            # Sekunden je HTTP-Anfrage
SID_TTL = 19 * 60       # die Box haelt eine SID 20 min, jeder Zugriff verlaengert sie
MAX_BLOCK_WAIT = 15     # so lange wird eine Login-Sperre der Box abgewartet
RETRY_AFTER_FAIL = 300  # Pause nach abgelehntem Login
NO_SID = "0" * 16
FIELDS = ("watt", "volt", "energy_wh", "celsius")


class CollectorError(Exception):
    """Erwartbarer Fehler: wird als eine Zeile gemeldet, ohne Traceback."""


def load_config(path):
    cfg = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            # KEY="wert" ist erlaubt, so schreibt auch das Plugin seine settings.cfg
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            cfg[key] = value
    required = ("FB_HOST", "FB_USER", "FB_PASS", "INFLUX_URL", "INFLUX_DB")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise CollectorError(f"In {path} fehlt: {', '.join(missing)}")
    return cfg


def read_text(path):
    with open(path, encoding="ascii") as f:
        return f.read().strip()


def write_private(path, text):
    """Schreibt atomar und nur fuer den Besitzer lesbar."""
    tmp = f"{path}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise  # Statuscode wertet der Aufrufer aus
    except (OSError, HTTPException) as e:
        # HTTPException: Verbindung steht, aber die Antwort bricht ab oder ist kein HTTP
        host = urllib.parse.urlsplit(url).hostname
        detail = getattr(e, "reason", None) or str(e) or type(e).__name__
        raise CollectorError(f"{host} nicht erreichbar: {detail}") from None


def login_response(challenge, password):
    """Antwort auf die Challenge der Box: PBKDF2 ab FRITZ!OS 7.24, sonst MD5."""
    if challenge.startswith("2$"):
        try:
            _, iter1, salt1, iter2, salt2 = challenge.split("$")
            hash1 = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt1), int(iter1))
            hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iter2))
        except ValueError:
            raise CollectorError(f"Unbekanntes Challenge-Format der FRITZ!Box: {challenge}") from None
        return f"{salt2}${hash2.hex()}"
    # MD5-Verfahren aelterer Boxen: Zeichen ausserhalb von ISO-8859-1 zaehlen als "."
    legacy = "".join(c if ord(c) < 256 else "." for c in password)
    md5 = hashlib.md5(f"{challenge}-{legacy}".encode("utf-16-le")).hexdigest()
    return f"{challenge}-{md5}"


def session_info(raw):
    try:
        root = ET.fromstring(raw)
        block = int(root.findtext("BlockTime") or 0)
    except (ET.ParseError, ValueError) as e:
        raise CollectorError(f"Unerwartete Antwort von login_sid.lua: {e}") from None
    return root.findtext("SID") or NO_SID, root.findtext("Challenge") or "", block


def pause_login(seconds, reason):
    until = int(time.time() + seconds)
    write_private(LOGIN_PAUSE, str(until))
    raise CollectorError(f"{reason} - naechster Versuch ab {time.strftime('%H:%M:%S', time.localtime(until))}")


def check_login_pause(cfg_path):
    """Nach einem abgelehnten Login nicht jede Minute erneut bei der Box anklopfen."""
    try:
        if os.path.getmtime(cfg_path) > os.path.getmtime(LOGIN_PAUSE):
            return  # Zugangsdaten wurden seither geaendert
        until = int(read_text(LOGIN_PAUSE))
    except (OSError, ValueError):
        return
    if time.time() < until:
        raise CollectorError(
            f"Login pausiert bis {time.strftime('%H:%M:%S', time.localtime(until))} "
            f"(Config aendern oder {LOGIN_PAUSE} loeschen hebt die Pause auf)")


def new_sid(cfg, cfg_path):
    check_login_pause(cfg_path)
    url = f"http://{cfg['FB_HOST']}/login_sid.lua?version=2"
    sid, challenge, block = session_info(http(url))
    if sid != NO_SID:
        return sid  # Box verlangt im Heimnetz keine Anmeldung
    if block > MAX_BLOCK_WAIT:
        pause_login(block, f"FRITZ!Box sperrt Anmeldungen noch {block} s")
    time.sleep(block)
    form = urllib.parse.urlencode({
        "username": cfg["FB_USER"],
        "response": login_response(challenge, cfg["FB_PASS"]),
    })
    sid, _, block = session_info(http(url, data=form.encode()))
    if sid == NO_SID:
        pause_login(max(block, RETRY_AFTER_FAIL), "Login abgelehnt, FB_USER/FB_PASS pruefen")
    try:
        os.remove(LOGIN_PAUSE)
    except OSError:
        pass
    return sid


def cached_sid():
    try:
        if time.time() - os.path.getmtime(SID_CACHE) >= SID_TTL:
            return None  # auf der Box laengst abgelaufen, gar nicht erst senden
        sid = read_text(SID_CACHE)
    except (OSError, ValueError):
        return None
    return sid if re.fullmatch(r"[0-9a-f]{16}", sid) and sid != NO_SID else None


def get_devicelist(cfg, cfg_path):
    url = (f"http://{cfg['FB_HOST']}/webservices/homeautoswitch.lua"
           f"?switchcmd=getdevicelistinfos&sid=")
    sid = cached_sid()
    if sid:
        try:
            raw = http(url + sid)
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
        else:
            try:
                os.utime(SID_CACHE)  # Zugriff hat die SID auf der Box verlaengert
            except OSError:
                pass
            return raw
    sid = new_sid(cfg, cfg_path)
    write_private(SID_CACHE, sid)
    try:
        return http(url + sid)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise CollectorError("Anmeldung ok, aber kein Zugriff: dem FRITZ!Box-Benutzer "
                                 "fehlt das Recht 'Smart Home'") from None
        raise


def slug(name):
    # Grafana-freundlicher Tag-Wert: keine Leerzeichen, keine Umlaute
    trans = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
    return re.sub(r"[^a-z0-9_]", "_", name.lower().translate(trans))


def number(node, tag, factor):
    """Messwert als float. None, wenn Knoten oder Wert fehlen oder 'inval' drinsteht."""
    if node is None:
        return None
    try:
        return int(node.findtext(tag) or "") / factor
    except ValueError:
        return None


def parse_devices(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise CollectorError(f"Antwort der FRITZ!Box ist kein gueltiges XML: {e}") from None
    out = []
    for dev in root.findall("device"):
        pm = dev.find("powermeter")
        if pm is None:
            continue  # kein Messgeraet, ueberspringen
        ain = dev.get("identifier", "").replace(" ", "")
        out.append({
            "name": slug(dev.findtext("name") or "") or ain,
            "ain": ain,
            "watt": number(pm, "power", 1000.0),
            "volt": number(pm, "voltage", 1000.0),
            "energy_wh": number(pm, "energy", 1.0),
            "celsius": number(dev.find("temperature"), "celsius", 10.0),
            "present": dev.findtext("present") == "1",
        })
    return out


def to_line_protocol(devices):
    lines = []
    for d in devices:
        fields = ",".join(f"{k}={d[k]}" for k in FIELDS if d[k] is not None)
        if d["present"] and fields and d["name"] and d["ain"]:
            lines.append(f"power,device={d['name']},ain={d['ain']} {fields}")
    return "\n".join(lines)


def write_influx(cfg, payload):
    query = urllib.parse.urlencode({"db": cfg["INFLUX_DB"], "precision": "s"})
    headers = {}
    if cfg.get("INFLUX_USER"):
        login = f"{cfg['INFLUX_USER']}:{cfg.get('INFLUX_PASS', '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()
    try:
        http(f"{cfg['INFLUX_URL'].rstrip('/')}/write?{query}", payload.encode(), headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()
        raise CollectorError(f"InfluxDB lehnt den Schreibvorgang ab (HTTP {e.code}): {detail}") from None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default=os.environ.get("FRITZPOWER_CONFIG", CFG_PATH),
                    help="Pfad zur Konfiguration (Standard: %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="geschriebene Zeilen ausgeben")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="nur auslesen und anzeigen, nichts nach InfluxDB schreiben")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        payload = to_line_protocol(parse_devices(get_devicelist(cfg, args.config)))
        if not payload:
            raise CollectorError("Keine Messwerte gefunden (keine Steckdose verbunden?)")
        if not args.dry_run:
            write_influx(cfg, payload)
    except CollectorError as e:
        print(f"fritzpower: {e}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        host = urllib.parse.urlsplit(e.url or "").hostname
        print(f"fritzpower: HTTP {e.code} von {host}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"fritzpower: {e}", file=sys.stderr)
        return 1
    if args.verbose or args.dry_run:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())