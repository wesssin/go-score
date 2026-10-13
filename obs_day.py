#!/usr/bin/env python3
"""obs_day.py - dumps what the buoys and wind stations recorded on a trip day (for calibration notes).
Usage: python obs_day.py YYYY-MM-DD [start_hour end_hour]   (HST, default 5-15). Writes diag/obs_<date>.txt."""
import os, sys, urllib.request
from datetime import datetime, timedelta, timezone

HST = timezone(timedelta(hours=-10))
day = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=HST)
h0, h1 = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (5, 15)
t0, t1 = day + timedelta(hours=h0), day + timedelta(hours=h1)
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "windward-go-score/obs"})
    return urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")


def comp(d):
    try:
        return COMPASS[int((float(d) % 360) / 22.5 + 0.5) % 16]
    except Exception:  # noqa
        return d


def ndbc(station, ext, cols):
    out = []
    try:
        lines = get("https://www.ndbc.noaa.gov/data/realtime2/%s.%s" % (station, ext)).splitlines()
    except Exception as e:  # noqa
        return ["%s.%s: failed (%s)" % (station, ext, e)]
    head = lines[0].lstrip("#").split()
    for ln in lines[2:]:
        p = ln.split()
        if len(p) < len(head):
            continue
        t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc).astimezone(HST)
        if not (t0 <= t <= t1):
            continue
        slot = t.replace(minute=0 if t.minute < 30 else 30, second=0)
        out.append((slot, t, dict(zip(head, p))))
    out.sort(key=lambda x: x[1])
    seen, rows = set(), []
    for slot, t, r in out:            # one reading per half hour (CDIP buoys report at odd minutes)
        if slot not in seen:
            seen.add(slot)
            rows.append(cols(t, r))
    return rows


def m2ft(v):
    try:
        return "%.1f" % (float(v) * 3.28084)
    except Exception:  # noqa
        return "-"


def ms2kt(v):
    try:
        return "%.0f" % (float(v) * 1.94384)
    except Exception:  # noqa
        return "-"


L = ["Observations %s, %d:00-%d:00 HST (pulled %s UTC)" % (sys.argv[1], h0, h1, datetime.utcnow().strftime("%Y-%m-%d %H:%M"))]
for st, name in (("51202", "Mokapu buoy"), ("51211", "Pearl Harbor entrance buoy (south)")):
    L.append("\n== %s %s: waves split into swell and wind sea" % (st, name))
    L.append("time   total ft  swell ft @ s from   wind-sea ft @ s from   steepness")
    L += ndbc(st, "spec", lambda t, r: "%s  %6s    %4s @ %4s %-4s      %4s @ %4s %-4s    %s" % (
        t.strftime("%H:%M"), m2ft(r.get("WVHT")), m2ft(r.get("SwH")), r.get("SwP"), comp(r.get("SwD")),
        m2ft(r.get("WWH")), r.get("WWP"), comp(r.get("WWD")), r.get("STEEPNESS")))
L.append("\n== 51202 Mokapu buoy (standard file: height, dominant period, direction)")
L += ndbc("51202", "txt", lambda t, r: "%s  %s ft @ %s s (avg %s s) from %s" % (
    t.strftime("%H:%M"), m2ft(r.get("WVHT")), r.get("DPD"), r.get("APD"), comp(r.get("MWD"))))
for st, name in (("MOKH1", "Coconut Island wind"),):
    L.append("\n== %s %s" % (st, name))
    L += ndbc(st, "txt", lambda t, r: "%s  %s kt (gust %s) from %s" % (
        t.strftime("%H:%M"), ms2kt(r.get("WSPD")), ms2kt(r.get("GST")), comp(r.get("WDIR"))))
try:
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=PHNG&data=sknt&data=drct&data=gust"
           "&year1=%d&month1=%d&day1=%d&year2=%d&month2=%d&day2=%d&tz=Etc/UTC&format=onlycomma&missing=-"
           % (day.year, day.month, day.day, (day + timedelta(days=2)).year, (day + timedelta(days=2)).month, (day + timedelta(days=2)).day))
    L.append("\n== PHNG Kaneohe MCBH airport wind")
    for ln in get(url).splitlines()[1:]:
        p = ln.split(",")
        t = datetime.strptime(p[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(HST)
        if t0 <= t <= t1 and t.minute >= 50:
            L.append("%s  %s kt (gust %s) from %s" % (t.strftime("%H:%M"), p[2], p[4], comp(p[3])))
except Exception as e:  # noqa
    L.append("PHNG failed: %s" % e)
# Model wave partitions at the Mokapu buoy for the same hours (to compare with the buoy's swell / wind-sea split)
import json
MARINE = ("https://marine-api.open-meteo.com/v1/marine?latitude=21.417&longitude=-157.668&hourly=wave_height,wave_period,"
          "wave_direction,wind_wave_height,wind_wave_period,wind_wave_direction,swell_wave_height,swell_wave_period,"
          "swell_wave_direction,secondary_swell_wave_height,secondary_swell_wave_period,secondary_swell_wave_direction"
          "&start_date=%s&end_date=%s&timezone=Pacific/Honolulu&length_unit=imperial" % (sys.argv[1], sys.argv[1]))
for m in ("", "ncep_gfswave016", "ncep_gfswave025", "ecmwf_wam025", "meteofrance_wave"):
    L.append("\n== Model %s at Mokapu buoy: total | wind sea | swell 1 | swell 2  (ft @ s from)" % (m or "Open-Meteo default"))
    try:
        h = json.loads(get(MARINE + ("&models=" + m if m else "")))["hourly"]
    except Exception as e:  # noqa
        L.append("failed: %s" % e)
        continue

    def part(i, a, b, c):
        v = [h.get(k, [None] * 99)[i] for k in (a, b, c)]
        return "-" if v[0] is None else "%.1f @ %s %s" % (v[0], "%.0f" % v[1] if v[1] else "?", comp(v[2]) if v[2] is not None else "")
    for i, tm in enumerate(h["time"]):
        hr = int(tm[11:13])
        if h0 <= hr <= h1 and hr % 2 == 1:
            L.append("%s  %s | %s | %s | %s" % (tm[11:16], part(i, "wave_height", "wave_period", "wave_direction"),
                     part(i, "wind_wave_height", "wind_wave_period", "wind_wave_direction"),
                     part(i, "swell_wave_height", "swell_wave_period", "swell_wave_direction"),
                     part(i, "secondary_swell_wave_height", "secondary_swell_wave_period", "secondary_swell_wave_direction")))
txt = "\n".join(L)
print(txt)
os.makedirs("diag", exist_ok=True)
open("diag/obs_%s.txt" % sys.argv[1], "w").write(txt + "\n")
