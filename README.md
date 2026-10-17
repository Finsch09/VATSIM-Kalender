# Dokumentation: VATSIM-Buchungen automatisch in den iCloud-Kalender (Debian)

Oct 13, 2026 · @Finsch

## Überblick

Ein kleiner Webdienst auf einem Debian-Server holt alle 30 Minuten die VATSIM-Buchungen und schreibt die Buchungen des Mitgliedes 1793092 (Kürzel FS) in den iCloud-Kalender „VATGER Buchungen“. Gelöschte Buchungen werden auch aus dem Kalender entfernt.

**Ablauf bei jedem Lauf:**

1. Cron ruft per `curl` den lokalen Dienst `http://127.0.0.1:8000/sync` auf (mit Token im Header `X-Token`).
2. Der Dienst (Flask unter gunicorn) lädt `https://atc-bookings.vatsim.net/api/booking`.
3. Für jede Buchung mit `cid` 1793092 wird ein Termin `KÜRZEL | CALLSIGN` (z. B. `FS | EDDH_TWR`) per CalDAV in iCloud gespeichert. Die Zeiten sind UTC.
4. Die UID `vatsim-booking-<id>@sync` ist pro Buchung fest, daher entstehen keine Duplikate, der Termin wird nur aktualisiert.
5. Kalendereinträge mit dem Präfix `vatsim-booking-`, deren Buchung nicht mehr in der API steht und die noch nicht beendet sind, werden gelöscht.

**Komponenten:** Debian 12 (Python 3.11), Python-venv, Flask, gunicorn, `caldav` 1.6.0, systemd-Dienst `vatsim-sync`, Cronjob des Benutzers root. nginx und HTTPS sind optional und nicht eingerichtet.

## Voraussetzungen und verwendete Werte

Du brauchst einen Debian-Server mit root-Zugriff und ausgehendem Internet, eine Apple-ID mit Zwei-Faktor-Authentifizierung und einen vorhandenen iCloud-Kalender.

| Wert | In dieser Installation |
| --- | --- |
| Betriebssystem | Debian 12, Python 3.11 |
| Systembenutzer | `vatsim` (ohne Login-Shell) |
| Programmordner | `/opt/vatsim-sync` |
| Konfiguration | `/etc/vatsim-sync.env` |
| Dienstname | `vatsim-sync` (Port 8000, nur 127.0.0.1) |
| Ziel-Kalender | `VATGER Buchungen` |
| Mitglieder (cid) | 1793092 = FS |
| Datenquelle | `https://atc-bookings.vatsim.net/api/booking` |
| Takt | alle 30 Minuten (`*/30 * * * *`) |

## Schritt 1: App-spezifisches Apple-Passwort

Das normale Apple-ID-Passwort funktioniert für CalDAV nicht. Du brauchst ein App-spezifisches Passwort.

1. Auf [appleid.apple.com](https://appleid.apple.com) anmelden.
2. Unter „Anmelden und Sicherheit“ den Punkt „App-spezifische Passwörter“ öffnen.
3. Ein neues Passwort erzeugen (Name z. B. `vatsim-sync`). Es hat das Format `xxxx-xxxx-xxxx-xxxx`.
4. Das Passwort sofort notieren, es wird nur einmal angezeigt.

Voraussetzung ist, dass die Zwei-Faktor-Authentifizierung für die Apple-ID aktiv ist.

## Schritt 2: Systemvorbereitung und Benutzer

Alle Befehle als root. Zuerst die Pakete, danach ein eigener Systembenutzer für den Dienst.

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip curl
```

`python3-venv` ist wichtig: Fehlt es, wird das venv ohne `pip` angelegt (genau das passierte bei der Erstinstallation).

```bash
useradd --system --create-home --home-dir /opt/vatsim-sync --shell /usr/sbin/nologin vatsim
```

Für nginx und HTTPS (optional, siehe Schritt 9) kommen `nginx certbot python3-certbot-nginx` dazu.

## Schritt 3: Python-Umgebung (venv, caldav<2)

Das venv muss mit `caldav` Version 1.x gebaut werden. Neuere Versionen (2.x und 3.x) nutzen `niquests` mit HTTP/3 und brechen auf diesem Server mit `OSError 90 Message too long` ab.

```bash
rm -rf /opt/vatsim-sync/venv
python3 -m venv /opt/vatsim-sync/venv
/opt/vatsim-sync/venv/bin/pip install --upgrade pip
/opt/vatsim-sync/venv/bin/pip install "caldav<2" flask requests gunicorn
chown -R vatsim:vatsim /opt/vatsim-sync
```

Kontrolle: `caldav` muss 1.x sein, `niquests` und `urllib3-future` dürfen nicht auftauchen.

```bash
/opt/vatsim-sync/venv/bin/pip list | grep -i -E "caldav|niquests|urllib3|requests|flask|gunicorn"
```

Ergebnis in dieser Installation: `caldav 1.6.0`, `requests 2.34.2`, `urllib3 2.8.0`, `Flask 3.1.3`, `gunicorn 26.2.0`.

Nach jedem Neuaufbau des venv muss der Dienst neu gestartet werden (`systemctl restart vatsim-sync`), sonst läuft er mit den alten, bereits geladenen Paketen weiter.

## Schritt 4: Vollständiger Code app.py

Datei `/opt/vatsim-sync/app.py` (mit `nano` anlegen, danach `chown vatsim:vatsim /opt/vatsim-sync/app.py`). Das ist der Endstand mit Kalendersuche ohne Leerzeichen-Problem und Löschlogik.

```python
import os
from datetime import datetime, timezone
import requests
import caldav
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

API_URL = "https://atc-bookings.vatsim.net/api/booking"

# cid -> Kürzel
MEMBERS = {
    1793092: "FS",
}

def get_calendar():
    client = caldav.DAVClient(
        url="https://caldav.icloud.com/",
        username=os.environ["ICLOUD_USER"],
        password=os.environ["ICLOUD_APP_PW"],
    )
    wanted = os.environ.get("ICLOUD_CAL", "Privat").strip().casefold()
    cals = client.principal().calendars()
    for c in cals:
        if (c.name or "").strip().casefold() == wanted:
            return c
    raise RuntimeError(f"Kalender '{wanted}' nicht gefunden. Vorhanden: {[c.name for c in cals]}")

def ics_time(s):
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")

def build_ics(uid, summary, start, end, description):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//vatsim-sync//DE\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTSTART:{ics_time(start)}\r\n"
        f"DTEND:{ics_time(end)}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

@app.get("/sync")
def sync():
    if request.headers.get("X-Token") != os.environ["API_TOKEN"]:
        abort(401)

    resp = requests.get(API_URL, timeout=20)
    resp.raise_for_status()
    bookings = resp.json()
    if not isinstance(bookings, list) or not bookings:
        return jsonify(ok=False, error="API lieferte keine Daten, nichts geändert"), 502

    cal = get_calendar()
    written = []
    current_uids = set()

    for b in bookings:
        initials = MEMBERS.get(b.get("cid"))
        if not initials:
            continue

        summary = f"{initials} | {b['callsign']}"
        uid = f"vatsim-booking-{b['id']}@sync"
        current_uids.add(uid)
        desc = f"{b.get('type', '')} {b.get('division', '')} {b.get('subdivision') or ''}".strip()

        cal.save_event(build_ics(uid, summary, b["start"], b["end"], desc))
        written.append(summary)

    # Verwaiste Einträge löschen (nur eigene, nur aktuelle/zukünftige)
    deleted = []
    now = datetime.now(timezone.utc)
    for ev in cal.events():
        try:
            vevent = ev.vobject_instance.vevent
            uid = str(vevent.uid.value)
            if not uid.startswith("vatsim-booking-") or uid in current_uids:
                continue
            end = vevent.dtend.value
            if isinstance(end, datetime):
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                if end < now:
                    continue
            summary = str(vevent.summary.value)
            ev.delete()
            deleted.append(summary)
        except Exception:
            continue

    return jsonify(ok=True, count=len(written), events=written, deleted=deleted)
```

Syntaxprüfung ohne Start des Dienstes:

```bash
/opt/vatsim-sync/venv/bin/python -m py_compile /opt/vatsim-sync/app.py && echo "Syntax OK"
```

**Hinweise zur Logik:**

- Es wird die `cid` (Mitgliedsnummer) geprüft, nicht die Buchungs-`id`. Für die Buchungs-ID müsste `b.get("cid")` durch `b.get("id")` ersetzt werden.
- Die VATSIM-Zeiten sind UTC und werden mit `Z` in den Kalender geschrieben. Das Gerät rechnet in die Ortszeit um.
- Löschlogik: Gelöscht wird nur, wenn die API erfolgreich war und Daten lieferte, die UID mit `vatsim-booking-` beginnt, nicht mehr in der API steht und der Termin noch nicht zu Ende ist.

## Schritt 5: Konfigurationsdatei /etc/vatsim-sync.env

Zugangsdaten stehen nie im Code, sondern in einer geschützten Datei. Zuerst ein zufälliges Token für den Aufruf des Dienstes erzeugen:

```bash
openssl rand -hex 32
```

Dann `nano /etc/vatsim-sync.env` mit diesem Inhalt (ohne `export`, ohne Anführungszeichen):

```
ICLOUD_USER=deine@apple-id.de
ICLOUD_APP_PW=xxxx-xxxx-xxxx-xxxx
ICLOUD_CAL=VATGER Buchungen
API_TOKEN=hier-das-erzeugte-token
```

Rechte setzen, damit nur root und der Dienstbenutzer lesen dürfen:

```bash
chown root:vatsim /etc/vatsim-sync.env
chmod 640 /etc/vatsim-sync.env
```

**Wichtig:** systemd liest diese Datei samt Leerzeichen im Kalendernamen korrekt. Die Shell (`. /etc/vatsim-sync.env`) dagegen nicht: Sie meldet `Buchungen: command not found`. Die Datei deshalb nie per `source` laden, sondern den Dienst per `curl` testen oder das Prüfskript aus Schritt 7 nutzen, das die Datei selbst einliest.

Nach jeder Änderung an der Datei: `systemctl restart vatsim-sync`.

## Schritt 6: systemd-Dienst

Datei `/etc/systemd/system/vatsim-sync.service`:

```ini
[Unit]
Description=VATSIM Bookings nach iCloud Kalender
After=network-online.target
Wants=network-online.target

[Service]
User=vatsim
Group=vatsim
WorkingDirectory=/opt/vatsim-sync
EnvironmentFile=/etc/vatsim-sync.env
ExecStart=/opt/vatsim-sync/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 --timeout 120 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Aktivieren und starten:

```bash
systemctl daemon-reload
systemctl enable --now vatsim-sync
systemctl status vatsim-sync --no-pager
```

Erwartet: `active (running)`. Der Dienst startet nach einem Server-Neustart automatisch (`enable`). Direkt nach einem `restart` braucht gunicorn 1 bis 3 Sekunden, bis der Port offen ist; ein sofortiger `curl` meldet sonst `Failed to connect`.

## Schritt 7: Test und Prüfskript

**Manueller Sync** (jederzeit möglich, kein Neustart nötig):

```bash
TOKEN=$(grep '^API_TOKEN=' /etc/vatsim-sync.env | cut -d= -f2-)
curl -H "X-Token: $TOKEN" http://127.0.0.1:8000/sync
```

Erwartete Antwort, z. B. `{"count":1,"deleted":[],"events":["FS | EDDH_TWR"],"ok":true}`. `"count":0` ist kein Fehler: Es liegt dann keine Buchung für deine cid vor.

| Antwort | Bedeutung |
| --- | --- |
| `401 Unauthorized` | Token falsch oder Platzhalter `DEIN_TOKEN` nicht ersetzt |
| `500 Internal Server Error` | Fehler in der App, Ursache mit `journalctl -u vatsim-sync -n 30 --no-pager` ansehen |
| `502` mit Fehlertext | VATSIM-API lieferte keine Daten, es wurde nichts geändert |
| `Failed to connect` | Dienst läuft nicht oder startet noch |

**Prüfskript** `/opt/vatsim-sync/check.py`: zeigt, welche Buchungen die API für deine cid enthält und welche Termine tatsächlich im iCloud-Kalender liegen. Es liest die env-Datei selbst ein.

```python
import requests, caldav

env = {}
for line in open("/etc/vatsim-sync.env"):
    line = line.rstrip("\n")
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k] = v

print("=== Buchungen in der VATSIM-API ===")
for b in requests.get("https://atc-bookings.vatsim.net/api/booking", timeout=20).json():
    if b.get("cid") in (1793092):
        print(b["cid"], b["callsign"], b["start"], "->", b["end"], "(UTC)")

print("\n=== Termine im iCloud-Kalender ===")
client = caldav.DAVClient(url="https://caldav.icloud.com/",
                          username=env["ICLOUD_USER"], password=env["ICLOUD_APP_PW"])
for cal in client.principal().calendars():
    if (cal.name or "").strip().casefold() == env["ICLOUD_CAL"].strip().casefold():
        print("Kalender:", repr(cal.name))
        events = cal.events()
        print("Anzahl:", len(events))
        for e in events:
            d = e.vobject_instance.vevent
            print(d.summary.value, d.dtstart.value, "->", d.dtend.value)
```

Aufruf: `/opt/vatsim-sync/venv/bin/python /opt/vatsim-sync/check.py`

**Kalender ansehen:** Ein Termin erscheint auf iPhone oder Mac erst nach einigen Minuten und nur, wenn der Kalender „VATGER Buchungen“ eingeblendet ist und man am passenden Tag nachsieht. Zur Kontrolle hilft [icloud.com/calendar](https://www.icloud.com/calendar). In dieser Installation war der Termin nach kurzer Zeit sichtbar.

**Kalendernamen auflisten** (wenn `ICLOUD_CAL` nicht passt):

```bash
sudo -u vatsim /opt/vatsim-sync/venv/bin/python - <<'EOF'
import caldav
env = dict(l.rstrip("\n").split("=", 1) for l in open("/etc/vatsim-sync.env") if "=" in l)
c = caldav.DAVClient(url="https://caldav.icloud.com/", username=env["ICLOUD_USER"], password=env["ICLOUD_APP_PW"])
for cal in c.principal().calendars():
    print(repr(cal.name))
EOF
```

## Schritt 8: Cronjob

Der Cronjob läuft als root, weil nur root und die Gruppe `vatsim` die env-Datei lesen dürfen. Er liest das Token direkt aus der Datei, daher muss es nirgends kopiert werden.

```bash
touch /var/log/vatsim-sync.log
crontab -e
```

Zeile am Ende einfügen:

```cron
*/30 * * * * TOKEN=$(grep '^API_TOKEN=' /etc/vatsim-sync.env | cut -d= -f2-); curl -s -H "X-Token: $TOKEN" http://127.0.0.1:8000/sync >> /var/log/vatsim-sync.log 2>&1
```

Der Job läuft zur vollen und halben Stunde (xx:00 und xx:30), also 48 Mal pro Tag, in Serverzeit (UTC). Kontrolle mit `crontab -l` und `systemctl status cron --no-pager`.

| Ausdruck | Läuft |
| --- | --- |
| `*/30 * * * *` | alle 30 Minuten (eingestellt) |
| `0 * * * *` | jede volle Stunde |
| `*/15 * * * *` | alle 15 Minuten |
| `0 */2 * * *` | alle 2 Stunden |
| `0 6 * * *` | einmal täglich um 06:00 UTC |

**Prüfen, ob er läuft:**

```bash
tail -n 5 /var/log/vatsim-sync.log
journalctl -t CRON --since "1 hour ago" --no-pager | grep "(root)"
```

Im Log steht pro Lauf eine Zeile mit `"ok":true`. Nach `grep vatsim-sync` im Syslog findet man nichts, weil Cron nur den Befehlsanfang (`TOKEN=...`) protokolliert. Sofortiger Test mit exakt dem Cron-Befehl:

```bash
TOKEN=$(grep '^API_TOKEN=' /etc/vatsim-sync.env | cut -d= -f2-); curl -s -H "X-Token: $TOKEN" http://127.0.0.1:8000/sync >> /var/log/vatsim-sync.log 2>&1; tail -n 2 /var/log/vatsim-sync.log
```

**Pausieren:** In `crontab -e` ein `#` vor die Zeile setzen. Den Cron-Dienst selbst nicht stoppen, auf diesem Server führt er auch andere Jobs aus.

## Schritt 9: Optional nginx und HTTPS

Dieser Schritt wurde in dieser Installation nicht ausgeführt. Er ist nur nötig, wenn der Dienst von außen über eine Domain erreichbar sein soll. Für den Cronjob auf demselben Server reicht `127.0.0.1:8000`.

Voraussetzung: DNS-Eintrag der Domain zeigt auf den Server, Ports 80 und 443 sind offen.

```bash
apt install -y nginx certbot python3-certbot-nginx
```

Datei `/etc/nginx/sites-available/vatsim-sync`:

```nginx
server {
    listen 80;
    server_name deine-domain.de;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/vatsim-sync /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d deine-domain.de
```

Bei öffentlichem Zugang sollte das API-Token erneuert werden (`openssl rand -hex 32`, in `/etc/vatsim-sync.env` eintragen, `systemctl restart vatsim-sync`).

## Aufgetretene Probleme und Lösungen

Diese Probleme traten beim Aufbau auf, in der Reihenfolge ihres Auftretens. Wer die Anleitung nachbaut, kann sie mit den Schritten oben direkt vermeiden.

| Meldung / Symptom | Ursache | Lösung |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'caldav'` | Pakete lagen nicht im venv | Pakete mit `venv/bin/pip` installieren |
| `venv/bin/pip: No such file or directory` | venv ohne pip angelegt, `python3-venv` fehlte | `apt install python3-venv python3-pip`, venv löschen und neu anlegen |
| `OSError 90 Message too long` über `niquests` | `caldav` 2.x und 3.x nutzen niquests mit HTTP/3, das auf dem Server scheitert | venv neu mit `"caldav<2"` (1.6.0, nutzt `requests`) |
| 500 trotz korrektem venv | gunicorn lief noch mit den alten, geladenen Paketen | `systemctl restart vatsim-sync` |
| `401 Unauthorized` | Platzhalter `DEIN_TOKEN` im curl stehen gelassen | echtes Token aus `/etc/vatsim-sync.env` verwenden |
| `StopIteration` in `get_calendar` | Kalender heißt bei iCloud `'VATGER Buchungen '` mit Leerzeichen am Ende | Vergleich mit `.strip().casefold()` (Code in Schritt 4) |
| `line 3: Buchungen: command not found` | env-Datei per Shell geladen, Leerzeichen im Wert | nur für Tests betroffen, Datei nicht per `source` laden |
| `Failed to connect to 127.0.0.1 port 8000` direkt nach Neustart | gunicorn braucht 1 bis 3 Sekunden | kurz warten (`sleep 3`) |
| Termin im Kalender nicht sichtbar | Anderer Tag, Kalender nicht eingeblendet oder Synchronisation verzögert | Kalender einblenden, Datum prüfen, aktualisieren, `check.py` oder iCloud im Browser nutzen |
| `deleted` enthält gleichnamigen Eintrag | Zwei Buchungen mit gleichem Callsign an verschiedenen Tagen, andere ID | harmlos, Löschlogik arbeitet korrekt |

## Betrieb und Wartung

| Aufgabe | Befehl |
| --- | --- |
| Dienst neu starten (nach Änderung an `app.py` oder env-Datei) | `systemctl restart vatsim-sync` |
| Dienst-Status | `systemctl status vatsim-sync --no-pager` |
| Fehlermeldungen ansehen | `journalctl -u vatsim-sync -n 30 --no-pager` |
| Sync sofort auslösen | curl-Befehl aus Schritt 7 |
| Cron-Ergebnis prüfen | `tail -n 5 /var/log/vatsim-sync.log` |
| Kalender-Inhalt prüfen | `/opt/vatsim-sync/venv/bin/python /opt/vatsim-sync/check.py` |
| Cronjob pausieren | `crontab -e`, `#` vor die Zeile |

**Mitglieder oder Format ändern:** In `app.py` das Dictionary `MEMBERS` (cid → Kürzel) bzw. die Zeile `summary = f"{initials} | {b['callsign']}"` anpassen, dann Dienst neu starten.

**Kalender wechseln:** `ICLOUD_CAL` in `/etc/vatsim-sync.env` ändern, Dienst neu starten.

**Verhalten des Syncs:**

- Pro Buchung gibt es genau einen Termin (feste UID), mehrfaches Ausführen erzeugt keine Duplikate.
- Wird ein Termin im Kalender von Hand gelöscht, legt der nächste Lauf ihn wieder an, solange die Buchung bei VATSIM existiert.
- Beendete Buchungen bleiben als Historie im Kalender, sie werden nie gelöscht.
- Eigene Termine im Kalender ohne das UID-Präfix `vatsim-booking-` werden nie angefasst.

**Sicherheit:** App-Passwort und Token stehen nur in `/etc/vatsim-sync.env` (Rechte 640). Der Dienst lauscht nur auf 127.0.0.1. Ein Token, das in einem Chat oder Ticket sichtbar war, sollte bei öffentlichem Zugang erneuert werden.

**Log-Größe:** Bei einer Zeile alle 30 Minuten wächst `/var/log/vatsim-sync.log` nur langsam. Bei Bedarf eine `logrotate`-Regel ergänzen.
