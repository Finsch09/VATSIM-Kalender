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