#!/usr/bin/env python3
"""Liest FRITZ!Smart Energy Steckdosen aus und schreibt nach InfluxDB."""
import hashlib, urllib.request, urllib.parse, urllib.error, re, os, time, sys
import xml.etree.ElementTree as ET

CFG_PATH = "/mnt/cache/appdata/fritzpower/config"
SID_CACHE = "/tmp/fritzpower.sid"

def load_config(path):
    cfg = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg

def http_get(url, timeout=10):
    return urllib.request.urlopen(url, timeout=timeout).read().decode()

def new_sid(host, user, password):
    raw = http_get(f"http://{host}/login_sid.lua")
    challenge = re.search(r"<Challenge>(.*?)</Challenge>", raw).group(1)
    md5 = hashlib.md5(f"{challenge}-{password}".encode("utf-16-le")).hexdigest()
    response = urllib.parse.quote(f"{challenge}-{md5}")
    user_q = urllib.parse.quote(user)
    raw = http_get(f"http://{host}/login_sid.lua?username={user_q}&response={response}")
    sid = re.search(r"<SID>(.*?)</SID>", raw).group(1)
    if sid == "0" * 16:
        raise RuntimeError("Login fehlgeschlagen - Zugangsdaten pruefen")
    return sid

def get_sid(host, user, password):
    # Gecachte SID wiederverwenden, solange sie gueltig ist
    if os.path.exists(SID_CACHE) and time.time() - os.path.getmtime(SID_CACHE) < 900:
        sid = open(SID_CACHE).read().strip()
        try:
            raw = http_get(f"http://{host}/webservices/homeautoswitch.lua"
                           f"?switchcmd=getswitchlist&sid={sid}")
            if "0000000000000000" not in raw:
                return sid
        except urllib.error.HTTPError:
            pass
    sid = new_sid(host, user, password)
    with open(SID_CACHE, "w") as f:
        f.write(sid)
    os.chmod(SID_CACHE, 0o600)
    return sid

def slug(name):
    # Grafana-freundlicher Tag-Wert: keine Leerzeichen, keine Umlaute
    trans = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
    return re.sub(r"[^a-z0-9_]", "_", name.lower().translate(trans))

def parse_devices(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for dev in root.findall("device"):
        pm = dev.find("powermeter")
        if pm is None:
            continue  # kein Messgeraet, ueberspringen
        name = dev.findtext("name", "unknown")
        ain = dev.get("identifier", "").replace(" ", "")
        def val(node, tag, factor):
            t = node.findtext(tag)
            return None if t in (None, "", "inval") else int(t) / factor
        entry = {
            "name": slug(name),
            "ain": ain,
            "watt": val(pm, "power", 1000.0),
            "volt": val(pm, "voltage", 1000.0),
            "energy_wh": val(pm, "energy", 1.0),
            "present": dev.findtext("present") == "1",
        }
        temp = dev.find("temperature")
        entry["celsius"] = val(temp, "celsius", 10.0) if temp is not None else None
        out.append(entry)
    return out

def to_line_protocol(devices):
    lines = []
    for d in devices:
        if not d["present"]:
            continue
        fields = {k: d[k] for k in ("watt", "volt", "energy_wh", "celsius")
                  if d[k] is not None}
        if not fields:
            continue
        field_str = ",".join(f"{k}={v}" for k, v in fields.items())
        lines.append(f"power,device={d['name']},ain={d['ain']} {field_str}")
    return "\n".join(lines)

def write_influx(url, db, payload):
    req = urllib.request.Request(
        f"{url}/write?db={db}&precision=s",
        data=payload.encode(),
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)

def main():
    cfg = load_config(CFG_PATH)
    host = cfg["FB_HOST"]
    sid = get_sid(host, cfg["FB_USER"], cfg["FB_PASS"])
    xml_text = http_get(f"http://{host}/webservices/homeautoswitch.lua"
                        f"?switchcmd=getdevicelistinfos&sid={sid}")
    devices = parse_devices(xml_text)
    payload = to_line_protocol(devices)
    if not payload:
        print("Keine Messwerte gefunden", file=sys.stderr)
        return 1
    write_influx(cfg["INFLUX_URL"], cfg["INFLUX_DB"], payload)
    print(payload)
    return 0

if __name__ == "__main__":
    sys.exit(main())
