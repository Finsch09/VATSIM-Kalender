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