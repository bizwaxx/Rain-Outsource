"""Outcome-blind selection of cold, wet early-season college baseball days at the fixture venues.
Rule fixed BEFORE looking at outcomes: ERA5 temp at 18:00 local <= 50F and >= 0.10 in of rain in the 24 h before 18:00,
Feb 10 - Mar 31, 2017-2025. Then every home game on those days found on ESPN's scoreboard is kept, whatever happened."""
import json, os, hashlib, urllib.request, time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
C = os.environ.get("COLDWET_CACHE", "/tmp/coldwet-cache"); os.makedirs(C, exist_ok=True)
def get(url):
    p = os.path.join(C, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if os.path.exists(p): return json.load(open(p))
    for a in range(4):
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rainout-research"}), timeout=40).read()); break
        except Exception as e:
            if a == 3: raise
            time.sleep(5 * (a + 1))
    json.dump(d, open(p, "w")); return d
VENUES = {  # espn venue name fragment: (lat, lon, field_type, fixture venue label)
 "Disch-Falk": (30.27993, -97.72619, "turf", "UFCU Disch-Falk Field, Austin TX (FieldTurf since 2008)"),
 "Baylor Ballpark": (31.55132, -97.1077, "dirt_infield", "Baylor Ballpark, Waco TX (Bermuda grass, dirt infield)"),
 "Bobcat Ballpark": (29.88835, -97.92939, "turf", "Bobcat Ballpark, San Marcos TX (artificial turf)"),
 "Dan Law": (33.5887, -101.87694, "turf", "Dan Law Field at Rip Griffin Park, Lubbock TX (FieldTurf)"),
 "Horner Ballpark": (32.71181, -96.94545, "turf", "Horner Ballpark, Dallas TX (AstroTurf)"),
 "Lupton": (32.70277, -97.37212, "dirt_infield", "Lupton Stadium, Fort Worth TX (Bermuda grass, dirt infield)"),
 "Olsen Field": (30.60582, -96.34138, "dirt_infield", "Olsen Field at Blue Bell Park, College Station TX (Bermuda grass, dirt infield)"),
 "Schroeder": (29.72686, -95.34546, "turf", "Schroeder Park, Houston TX (FieldTurf since 2014)"),
}
HOME = {"Disch-Falk": "Texas Longhorns", "Baylor Ballpark": "Baylor Bears", "Bobcat Ballpark": "Texas State Bobcats", "Dan Law": "Texas Tech Red Raiders", "Horner Ballpark": "Dallas Baptist Patriots", "Lupton": "TCU Horned Frogs", "Olsen Field": "Texas A&M Aggies", "Schroeder": "Houston Cougars"}
TZ = ZoneInfo("America/Chicago")
cands = []
for name, (lat, lon, ft, label) in VENUES.items():
    for y in range(2017, 2026):
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={y}-02-09&end_date={y}-03-31"
               "&hourly=temperature_2m,precipitation&temperature_unit=fahrenheit&precipitation_unit=inch&timezone=America%2FChicago")
        h = get(url)["hourly"]; t = h["time"]; idx = {s: n for n, s in enumerate(t)}
        d = date(y, 2, 10)
        while d <= date(y, 3, 31):
            k = idx.get(f"{d}T18:00")
            if k is not None and k >= 24:
                rain = sum(h["precipitation"][k - 24:k] or [0]); temp = h["temperature_2m"][k]
                if temp is not None and temp <= 55 and rain >= 0.10:
                    cands.append({"venue_key": name, "date": str(d), "era5_temp_f_18": temp, "era5_rain_24h_in": round(rain, 2)})
            d += timedelta(days=1)
print(len(cands), "candidate venue-days")
games = []
for c in cands:
    d = c["date"].replace("-", "")
    for day in (d,):
        sb = get(f"https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard?dates={day}&limit=500")
        for e in sb.get("events", []):
            comp = e["competitions"][0]; vn = (comp.get("venue") or {}).get("fullName") or ""
            home = [t["team"].get("displayName", "") for t in comp["competitors"] if t.get("homeAway") == "home"]
            if not home or home[0] != HOME[c["venue_key"]]: continue
            if comp.get("neutralSite") or (vn and c["venue_key"].lower() not in vn.lower()): continue
            start = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)
            if str(start.date()) != c["date"]: continue
            games.append({**c, "espn_id": e["id"], "name": e["name"], "start_local": start.strftime("%Y-%m-%dT%H:%M"),
                          "status": comp["status"]["type"]["name"], "detail": comp["status"]["type"].get("detail"), "espn_venue": vn})
json.dump(games, open(os.environ.get("COLDWET_OUT", "/tmp/coldwet-games.json"), "w"), indent=1)
from collections import Counter
print(len(games), Counter(g["status"] for g in games))
for g in games: print(g["date"], g["venue_key"], g["start_local"][11:], g["status"], g["name"], g["era5_temp_f_18"], g["era5_rain_24h_in"])

# Second pass: each home team's ESPN season schedule (the scoreboard misses many games).
ids = json.load(open(os.path.join(C, "ids.json"))); seen = {g["espn_id"] for g in games}
cand = {(c["venue_key"], c["date"]): c for c in cands}
for vk, team in HOME.items():
    for y in range(2017, 2026):
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/teams/{ids[team]}/schedule?season={y}")
        for e in s.get("events", []):
            comp = e["competitions"][0]
            home = [t["team"].get("displayName", "") for t in comp["competitors"] if t.get("homeAway") == "home"]
            if not home or home[0] != team or comp.get("neutralSite") or e["id"] in seen: continue
            start = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)
            c = cand.get((vk, str(start.date())))
            if not c: continue
            vn = (comp.get("venue") or {}).get("fullName") or ""
            if vn and vk.lower() not in vn.lower(): continue
            seen.add(e["id"])
            games.append({**c, "espn_id": e["id"], "name": e["name"], "start_local": start.strftime("%Y-%m-%dT%H:%M"),
                          "status": comp["status"]["type"]["name"], "detail": comp["status"]["type"].get("detail"), "espn_venue": vn})
for g in games: g.setdefault("sport", "baseball")
json.dump(games, open(os.environ.get("COLDWET_OUT", "/tmp/coldwet-games.json"), "w"), indent=1)
print("after schedules:", len(games), Counter(g["status"] for g in games))
for g in games[10:]: print(g["date"], g["venue_key"], g["start_local"][11:], g["status"], g["name"], g["era5_temp_f_18"], g["era5_rain_24h_in"])

# Third pass: college softball at the same campuses (ESPN college-softball team schedules).
SB = {"Disch-Falk": "Texas Longhorns", "Baylor Ballpark": "Baylor Bears", "Bobcat Ballpark": "Texas State Bobcats",
      "Dan Law": "Texas Tech Red Raiders", "Olsen Field": "Texas A&M Aggies", "Schroeder": "Houston Cougars"}
st = get("https://site.api.espn.com/apis/site/v2/sports/baseball/college-softball/teams?limit=1000")
sids = {t["team"]["displayName"]: t["team"]["id"] for t in st["sports"][0]["leagues"][0]["teams"]}
for vk, team in SB.items():
    if team not in sids: print("no softball id", team); continue
    for y in range(2017, 2026):
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/baseball/college-softball/teams/{sids[team]}/schedule?season={y}")
        for e in s.get("events", []):
            comp = e["competitions"][0]
            home = [t["team"].get("displayName", "") for t in comp["competitors"] if t.get("homeAway") == "home"]
            if not home or home[0] != team or comp.get("neutralSite"): continue
            start = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)
            c = cand.get((vk, str(start.date())))
            if not c: continue
            vn = (comp.get("venue") or {}).get("fullName") or ""
            games.append({**c, "sport": "softball", "espn_id": e["id"], "name": e["name"], "start_local": start.strftime("%Y-%m-%dT%H:%M"),
                          "status": comp["status"]["type"]["name"], "detail": comp["status"]["type"].get("detail"), "espn_venue": vn})
json.dump(games, open(os.environ.get("COLDWET_OUT", "/tmp/coldwet-games.json"), "w"), indent=1)
print("with softball:", len(games), Counter((g["sport"], g["status"]) for g in games))
for g in games: print(g["sport"], g["date"], g["venue_key"], g["start_local"][11:], g["status"], g["name"], "|", g["espn_venue"], g["era5_temp_f_18"], g["era5_rain_24h_in"])
