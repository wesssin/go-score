#!/usr/bin/env python3
"""
go_score.py (v3) - windward Oahu go / no-go forecast for a 15' RIB out of Heeia Kea.

Scores every hour 0-10 along your route (bay mouth, U FAD, MM FAD, T FAD), scores each day over
your trip window (6 AM-2 PM HST), and writes a self-contained page: go_score.html

  python go_score.py --open        live forecast, opens the page
  python go_score.py --demo        made-up data, just to see the layout
  python go_score.py --backtest    how the forecast models would have scored your calibration days

FORECAST
  waves : PacIOOS WW3 Hawaii (5 km, splits swell vs wind chop)  +  PacIOOS SWAN Oahu (500 m, near shore)
          Open-Meteo Marine fills any gaps (days 6-7)
  wind  : NWS gridpoint forecast (2.5 km) + Open-Meteo, averaged per spot
  text  : NWS Coastal Waters Forecast, Oahu Windward Waters (PHZ114) + any advisory headline
RIGHT NOW
  Mokapu buoy with NDBC's swell / wind-wave split and steepness label, upstream buoys (Pauwela,
  Waimea, Hanalei, NW Hawaii), Kaneohe airport wind, Coconut Island (HIMB) wind, lifeguard surf
  reports, Hawaii Mesonet stations (needs a free API token in hcdp_token.txt)

PENGUIN BANK (penguin.py)
  Every run: a strict check of the Kaiwi Channel and Penguin Bank; passing days get a badge on their card.
  python go_score.py --penguin  also writes penguin.html, the detailed channel forecast (on demand).

Thresholds are fitted to a handful of Wes's days (Sep 9/13, Jun 13/16/21 2026) - tune in SETTINGS.
"""
import argparse
import csv
import gzip
import html
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

HST = timezone(timedelta(hours=-10))
UTC = timezone.utc

# =============================================================== SETTINGS
# Route spots: (name, lat, lon, nearshore?)  Nearshore spots use the 500 m SWAN model first.
SPOTS = [("Bay mouth", 21.480, -157.770, True),
         ("U FAD", 21.582, -157.692, False),
         ("MM FAD", 21.607, -157.520, False),
         ("T FAD", 21.503, -157.430, False),
         ("LL FAD", 21.748, -157.755, False)]          # Hauula FAD, toward Kahuku
# Routes you might run. Each day is scored per route and the page shows the best one.
ROUTES = [("East to MM & T", ["Bay mouth", "U FAD", "MM FAD", "T FAD"]),
          ("North toward Kahuku", ["Bay mouth", "U FAD", "LL FAD"])]
# Lee effect: wind blowing FROM these directions (degrees) crosses Oahu or only a short stretch of water
# before reaching the spot, so it builds less chop. The factor multiplies the wind penalty (1 = no help).
# Hand-set from the map, not yet tested against a south-wind day - tune as you log trips.
LEE = {"Bay mouth": [(150, 330, 0.35), (120, 150, 0.7)],
       "U FAD": [(190, 300, 0.55), (160, 190, 0.75), (300, 330, 0.8)],
       "LL FAD": [(170, 280, 0.45), (140, 170, 0.75), (280, 320, 0.7)],
       "MM FAD": [(210, 290, 0.8)],
       "T FAD": [(230, 290, 0.85)]}
TRADES = (20, 120)                  # wind from these directions = normal trades
# Settle-then-watch pattern (from Sep 7-9 2026): after a windy trade day, the first light day still carries
# leftover slop ("settling day"); the SECOND light day in a row is when the sea has had time to lay down ("watch day").
WINDY_KT = 11.0                     # window-average wind at/above this = a windy day (trades / storm)
CALM_KT = 9.0                       # window-average wind at/below this = a light day
SOUTH = (120, 300)                  # wind from these directions puts the windward side in the lee
WINDOW = (6, 14)                    # trip window, HST hours: leave 6 AM, home by 2 PM
STRIP_HOURS = list(range(5, 18))
DAYS_AHEAD = 7
WIND_OFFSET_KT = 0.0                # nudge if forecasts run low/high vs. what you find offshore

TIERS = [(9.0, "Epic", "★", "good"),
         (7.5, "Good", "✓", "good"),
         (5.0, "Marginal", "▲", "warning"),
         (0.0, "Stay home", "✕", "critical")]
WIND_PEN = [(0, 0), (5, 0), (8, 1.5), (10, 3), (12, 6), (15, 10)]          # knots
PERIOD_PEN = [(3, 7), (5, 6), (6, 4), (7, 1.5), (9, 0), (30, 0)]            # peak period, s
STEEP_PEN = [(0, 0), (0.012, 0), (0.018, 2), (0.025, 5), (0.035, 7)]       # Hs / (1.56 Tp^2)
HEIGHT_PEN = [(0, 0), (6, 0), (8, 1.5), (12, 4), (20, 8)]                  # feet
CHOP_PEN = [(0, 0), (2.0, 0), (2.5, 0.5), (3.0, 3), (3.5, 5), (4.5, 7)]     # WW3 wind-chop height, feet
WIND_VETO_KT = 12.0                 # route-average wind at/above this for VETO_HOURS window hours caps the day at 3
VETO_HOURS = 2
SPOT_VETO_KT = 15.0                 # any single spot at/above this in the window caps the day at 3
BAD_HOUR = 4.0

UPSTREAM = [("51202", "Mokapu (your buoy)"), ("51205", "Pauwela, Maui – upwind trade swell"),
            ("51201", "Waimea – north swell"), ("51208", "Hanalei – north swell, earlier"),
            ("51001", "NW Hawaii – north swell ~1 day out"), ("51101", "NW Hawaii (backup)")]
STALE_HOURS = 3
WINDWARD_BOX = (21.25, 21.72, -158.05, -157.62)   # lat_min, lat_max, lon_min, lon_max for Mesonet stations
# Rough Koolau crest line (lat, lon), south to north. Stations east of it count as windward.
KOOLAU_CREST = [(21.27, -157.66), (21.32, -157.72), (21.367, -157.793), (21.43, -157.85), (21.50, -157.88),
                (21.55, -157.93), (21.65, -158.00), (21.72, -158.04)]
CACHE_MAX_HOURS = 18                # reuse a saved model run this old if the server times out
HCDP_BASE = "https://api.hcdp.ikewai.org"

M_TO_FT, MS_TO_KT, KT_TO_MPH = 3.28084, 1.94384, 1.15078
UA = "windward-go-score/3.2 (personal use)"
ERDDAP = "https://pae-paha.pacioos.hawaii.edu/erddap/"
NAN = float("nan")


# =============================================================== scoring
def interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def steepness(hs_ft, tp_s):
    return (hs_ft / M_TO_FT) / (1.56 * tp_s * tp_s)


def lee_factor(spot, deg):
    if deg is None:
        return 1.0
    for lo, hi, f in LEE.get(spot, []):
        if lo <= deg % 360 < hi:
            return f
    return 1.0


def hour_score(wind_kt, hs_ft, tp_s, chop_ft=None, lee=1.0):
    wind_p = interp(wind_kt, WIND_PEN) * lee
    chop_p = max(interp(tp_s, PERIOD_PEN), interp(steepness(hs_ft, tp_s), STEEP_PEN),
                 interp(chop_ft, CHOP_PEN) if chop_ft is not None else 0.0)
    height_p = interp(hs_ft, HEIGHT_PEN)
    return max(0.0, 10.0 - (wind_p + chop_p + height_p)), {"wind": wind_p, "short-period chop": chop_p, "wave height": height_p}


def tier_for(score):
    for lo, name, icon, role in TIERS:
        if score >= lo:
            return name, icon, role
    return TIERS[-1][1:]


def day_summary(rows):
    ok = [r for r in rows if r.get("score") is not None]
    if len(ok) < max(3, len(rows) // 2):
        return None
    mean = sum(r["score"] for r in ok) / len(ok)
    worst = min(ok, key=lambda r: r["score"])
    score, notes = mean, []
    windy_hours = sum(1 for r in ok if r["wind"] >= WIND_VETO_KT)
    peak_spot = max(r.get("wind_max_spot", r["wind"]) for r in ok)
    if windy_hours >= VETO_HOURS or peak_spot >= SPOT_VETO_KT:
        score = min(score, 3.0)
        notes.append("route wind %.0f+ kt for %d+ hours" % (WIND_VETO_KT, VETO_HOURS) if windy_hours >= VETO_HOURS
                     else "%.0f+ kt at one spot" % SPOT_VETO_KT)
    elif worst["score"] < BAD_HOUR:
        score = min(score, 6.0)
        notes.append("one rough hour (%s) caps the day" % worst["label"])
    score = round(score, 1)
    pen = {}
    for r in ok:
        for k, v in r["pen"].items():
            pen[k] = pen.get(k, 0) + v / len(ok)
    limiter = max(pen, key=pen.get) if pen and max(pen.values()) > 0.4 else None
    return {"score": score, "worst": worst, "limiter": limiter, "notes": notes,
            "wind_min": min(r["wind"] for r in ok), "wind_max": max(r["wind"] for r in ok),
            "hs_min": min(r["hs"] for r in ok), "hs_max": max(r["hs"] for r in ok),
            "tp_min": min(r["tp"] for r in ok), "tp_max": max(r["tp"] for r in ok),
            "chop_max": max((r.get("ws_hs") or 0) for r in ok),
            "spread_max": max(r.get("spread", 0) for r in ok)}


# =============================================================== HTTP helpers
def run_with_timeout(fn, seconds):
    box = {}

    def work():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa
            box["e"] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise RuntimeError("timed out after %ds" % seconds)
    if "e" in box:
        raise box["e"]
    return box["v"]


def http_get(url, timeout=25, tries=2, accept=None, headers=None):
    last = None
    for attempt in range(tries):
        try:
            h = {"User-Agent": UA}
            if accept:
                h["Accept"] = accept
            h.update(headers or {})
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:160].replace("\n", " ")
            except Exception:  # noqa
                pass
            raise RuntimeError("HTTP %s %s" % (e.code, body))
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("network error: %s" % last)


def hour_floor(dt):
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fnum(s):
    try:
        v = float(s)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


# =============================================================== forecast fetchers
def parse_dds(text, var):
    m = re.search(r"\b%s((?:\s*\[\s*\w+\s*=\s*\d+\s*\])+)\s*;" % re.escape(var), text)
    if not m:
        raise ValueError("variable %s not in dataset" % var)
    return [(a, int(b)) for a, b in re.findall(r"\[\s*(\w+)\s*=\s*(\d+)\s*\]", m.group(1))]


def erddap_point(dataset, variables, t0, t1, lat, lon_west):
    """t1=None means 'through the end of the forecast' (ERDDAP rejects stop times past the last step)."""
    dds = http_get(ERDDAP + "griddap/" + dataset + ".dds")
    pieces = []
    for v in variables:
        q = ""
        for name, _size in parse_dds(dds, v):
            n = name.lower()
            q += ("[(%s):1:(%s)]" % (iso(t0), iso(t1) if t1 else "last") if n.startswith("time") else
                  "[(%.4f)]" % lat if n.startswith("lat") else
                  "[(%.4f)]" % (lon_west + 360.0) if n.startswith("lon") else "[0]")
        pieces.append(v + q)
    url = ERDDAP + "griddap/" + dataset + ".csv?" + urllib.parse.quote(",".join(pieces), safe="[]():,=-.")
    rows = list(csv.reader(io.StringIO(http_get(url, timeout=60))))
    out = {}
    for r in rows[2:]:
        rec = dict(zip(rows[0], r))
        t = datetime.strptime(rec["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        out[t] = {v: fnum(rec.get(v)) for v in variables}
    return out


def in_swan_grid(lat, lon):
    return 21.2 <= lat <= 21.75 and -158.35 <= lon <= -157.6


def fetch_swan(lat, lon, t0, t1):
    raw = erddap_point("swan_oahu", ["shgt", "pper", "mper", "mdir"], t0, t1, lat, lon)
    return {t: {"hs": v["shgt"] * M_TO_FT, "tp": v["pper"], "dir": v["mdir"]}
            for t, v in raw.items() if v["shgt"] is not None and v["pper"]}


def fetch_ww3(lat, lon, t0, t1):
    raw = erddap_point("ww3_hawaii", ["Thgt", "Tper", "Tdir", "shgt", "sper", "whgt", "wper"], t0, t1, lat, lon)
    out = {}
    for t, v in raw.items():
        if v["Thgt"] is None or not v["Tper"]:
            continue
        out[t] = {"hs": v["Thgt"] * M_TO_FT, "tp": v["Tper"], "dir": v["Tdir"],
                  "sw_hs": v["shgt"] * M_TO_FT if v["shgt"] is not None else None, "sw_tp": v["sper"],
                  "ws_hs": v["whgt"] * M_TO_FT if v["whgt"] is not None else None, "ws_tp": v["wper"]}
    return out


def parse_valid_time(vt):
    start, dur = vt.split("/")
    t = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(UTC)
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", dur)
    hours = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) if m else 1
    return t, max(1, hours)


def nws_series(layer, speed=True):
    uom = layer.get("uom", "")
    k = (1 / 1.852 if "km_h" in uom else MS_TO_KT if "m_s" in uom else 1.0 if "kn" in uom else 1 / 1.852) if speed else 1.0
    out = {}
    for item in layer.get("values", []):
        if item.get("value") is None:
            continue
        t, n = parse_valid_time(item["validTime"])
        for i in range(n):
            out[t + timedelta(hours=i)] = item["value"] * k
    return out


def fetch_nws(lat, lon, t0, t1):
    p = json.loads(http_get("https://api.weather.gov/points/%.4f,%.4f" % (lat, lon), accept="application/geo+json"))["properties"]
    g = json.loads(http_get("https://api.weather.gov/gridpoints/%s/%s,%s" % (p["gridId"], p["gridX"], p["gridY"]),
                            accept="application/geo+json"))["properties"]
    ws, gu = nws_series(g["windSpeed"]), nws_series(g.get("windGust", {}))
    wd = nws_series(g.get("windDirection", {}), speed=False)
    out = {t: {"wind": v, "gust": gu.get(t), "dir": wd.get(t)} for t, v in ws.items() if t0 <= t <= t1}
    if not out:
        raise RuntimeError("no wind values")
    return out


def om_multi(url, hourly, extra=None):
    """Open-Meteo accepts several points at once; returns a list of 'hourly' dicts in SPOTS order."""
    q = {"latitude": ",".join("%.4f" % s[1] for s in SPOTS), "longitude": ",".join("%.4f" % s[2] for s in SPOTS),
         "hourly": hourly, "timezone": "UTC", "forecast_days": 8}
    q.update(extra or {})
    d = json.loads(http_get(url + "?" + urllib.parse.urlencode(q)))
    return [x["hourly"] for x in (d if isinstance(d, list) else [d])]


def om_time(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)


def fetch_om_wind():
    res = {}
    for spot, h in zip(SPOTS, om_multi("https://api.open-meteo.com/v1/forecast", "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
                                       {"wind_speed_unit": "kn"})):
        res[spot[0]] = {om_time(ts): {"wind": h["wind_speed_10m"][i], "gust": h["wind_gusts_10m"][i], "dir": h["wind_direction_10m"][i]}
                        for i, ts in enumerate(h["time"]) if h["wind_speed_10m"][i] is not None}
    return res


def fetch_om_marine():
    last = None
    for hourly in ("wave_height,wave_peak_period,wave_period,wave_direction,wind_wave_height,wind_wave_period",
                   "wave_height,wave_period,wave_direction"):
        try:
            data = om_multi("https://marine-api.open-meteo.com/v1/marine", hourly)
            break
        except Exception as e:  # noqa
            last, data = e, None
    if data is None:
        raise RuntimeError(str(last))
    res = {}
    for spot, h in zip(SPOTS, data):
        n = len(h["time"])
        g = lambda k, i: (h.get(k) or [None] * n)[i]  # noqa
        out = {}
        for i, ts in enumerate(h["time"]):
            if g("wave_height", i) is None:
                continue
            ww = g("wind_wave_height", i)
            out[om_time(ts)] = {"hs": g("wave_height", i) * M_TO_FT, "tp": g("wave_peak_period", i) or g("wave_period", i),
                                "dir": g("wave_direction", i), "ws_hs": ww * M_TO_FT if ww is not None else None,
                                "ws_tp": g("wind_wave_period", i)}
        res[spot[0]] = out
    return res


def save_cache(key, data):
    try:
        os.makedirs("cache", exist_ok=True)
        with open(os.path.join("cache", key + ".json"), "w") as f:
            json.dump({"saved": datetime.now(UTC).isoformat(), "data": {t.isoformat(): v for t, v in data.items()}}, f)
    except Exception:  # noqa
        pass


def load_cache(key):
    """Returns (data, age_hours) or (None, None)."""
    try:
        with open(os.path.join("cache", key + ".json")) as f:
            c = json.load(f)
        age = (datetime.now(UTC) - datetime.fromisoformat(c["saved"])).total_seconds() / 3600
        if age > CACHE_MAX_HOURS:
            return None, None
        return {datetime.fromisoformat(t): v for t, v in c["data"].items()}, age
    except Exception:  # noqa
        return None, None


# =============================================================== NWS text forecast (PHZ114)
WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def fetch_cwf():
    return http_get("https://tgftp.nws.noaa.gov/data/forecasts/marine/coastal/ph/phz114.txt")


def parse_cwf(text, today):
    """Returns (headlines, issued_line, {date: {"day": text, "night": text}})."""
    t = text.replace("\r", "")
    heads = [h.strip() for h in re.findall(r"^\.\.\.(.+?)\.\.\.\s*$", t, flags=re.M)]
    issued = next((ln.strip() for ln in t.splitlines() if re.search(r"\b(AM|PM) HST\b", ln, re.I)), "")
    periods = re.findall(r"^\.([A-Za-z][A-Za-z ]+?)\.\.\.(.*?)(?=^\.[A-Za-z]|^\$\$|\Z)", t, flags=re.M | re.S)
    days = {}
    for name, body in periods:
        nm = name.strip().upper()
        body = " ".join(body.split())
        night = "NIGHT" in nm or nm in ("TONIGHT", "OVERNIGHT")
        if nm in ("TODAY", "TONIGHT", "THIS AFTERNOON", "REST OF TODAY", "THIS MORNING", "OVERNIGHT"):
            d = today
        else:
            wd = next((i for i, w in enumerate(WEEKDAYS) if nm.startswith(w)), None)
            if wd is None:
                continue
            d = today + timedelta(days=(wd - today.weekday()) % 7)
        days.setdefault(d, {})["night" if night else "day"] = body
    return heads, issued, days


def nws_wind_range(body):
    if not body:
        return None
    nums = []
    for a, b, c in re.findall(r"(\d+) to (\d+) (?:knots|kt)|(\d+) (?:knots|kt)", body, flags=re.I):
        nums += [int(x) for x in (a, b, c) if x]
    return (min(nums), max(nums)) if nums else None


# =============================================================== observations
def ndbc_rows(station, kind):
    text = http_get("https://www.ndbc.noaa.gov/data/realtime2/%s.%s" % (station, kind))
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hdr = lines[0].lstrip("#").split()
    rows = []
    for ln in lines[2:]:
        parts = ln.split()
        if len(parts) < 5:
            continue
        rec = dict(zip(hdr, parts))
        try:
            rec["t"] = datetime(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), tzinfo=UTC)
        except ValueError:
            continue
        rows.append(rec)
    return rows


def fetch_buoy(station):
    std = ndbc_rows(station, "txt")
    if not std:
        raise RuntimeError("no rows")
    latest = next((r for r in std if fnum(r.get("WVHT")) is not None), None)
    if latest is None:
        raise RuntimeError("no wave height in recent rows")
    hs = fnum(latest["WVHT"])
    out = {"t": latest["t"], "hs": hs * M_TO_FT, "tp": fnum(latest.get("DPD")), "apd": fnum(latest.get("APD")),
           "dir": fnum(latest.get("MWD")), "wind": fnum(latest.get("WSPD"))}
    earlier = next((r for r in std if (latest["t"] - r["t"]) >= timedelta(hours=6) and fnum(r.get("WVHT")) is not None), None)
    out["trend"] = (hs - fnum(earlier["WVHT"])) * M_TO_FT if earlier else None
    try:
        spec = ndbc_rows(station, "spec")
        s = spec[0] if spec else None
        if s and abs((s["t"] - latest["t"]).total_seconds()) < 7200:
            out.update(sw_hs=(fnum(s.get("SwH")) or 0) * M_TO_FT, sw_tp=fnum(s.get("SwP")), sw_dir=s.get("SwD"),
                       ww_hs=(fnum(s.get("WWH")) or 0) * M_TO_FT, ww_tp=fnum(s.get("WWP")), ww_dir=s.get("WWD"),
                       steep_label=s.get("STEEPNESS"))
    except Exception:  # noqa
        pass
    if out["tp"]:
        out["steep"] = steepness(out["hs"], out["tp"])
    return out


def fetch_airport():
    now = datetime.now(HST)
    d0, d1 = now - timedelta(days=1), now + timedelta(days=1)
    q = [("station", "PHNG"), ("data", "sknt"), ("data", "drct"), ("data", "gust"),
         ("year1", d0.year), ("month1", d0.month), ("day1", d0.day), ("year2", d1.year), ("month2", d1.month), ("day2", d1.day),
         ("tz", "Pacific/Honolulu"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"), ("missing", "M"),
         ("trace", "T"), ("direct", "no"), ("report_type", "3"), ("report_type", "4")]
    rows = [r for r in csv.reader(io.StringIO(http_get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q)))) if r]
    best = None
    for r in rows[1:]:
        rec = dict(zip(rows[0], r))
        try:
            t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=HST)
        except Exception:  # noqa
            continue
        if fnum(rec.get("sknt")) is not None and t <= now and (best is None or t > best["t"]):
            best = {"t": t, "kt": fnum(rec["sknt"]), "dir": fnum(rec.get("drct")), "gust": fnum(rec.get("gust"))}
    if not best:
        raise RuntimeError("no recent report")
    return best


def fetch_himb():
    since = iso(datetime.now(UTC) - timedelta(hours=8))
    q = "time,wind_speed,gust_speed,wind_from_direction&time>=" + since
    url = ERDDAP + "tabledap/aws_himb.csv?" + urllib.parse.quote(q, safe="&=,:")
    rows = list(csv.reader(io.StringIO(http_get(url))))
    data = [dict(zip(rows[0], r)) for r in rows[2:]]
    data = [d for d in data if fnum(d.get("wind_speed")) is not None]
    if not data:
        raise RuntimeError("no recent data")
    d = data[-1]
    return {"t": datetime.strptime(d["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
            "kt": fnum(d["wind_speed"]) * MS_TO_KT, "gust": (fnum(d.get("gust_speed")) or 0) * MS_TO_KT or None,
            "dir": fnum(d.get("wind_from_direction"))}


def fetch_lifeguards():
    root = ET.fromstring(http_get("https://www.weather.gov/source/hfo/xml/Oahu.OMR.HFO.xml"))
    latest = {}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        m = re.match(r"^(.+?) reported\s+(.+?) at (.+?)\.?$", title, flags=re.I)
        if not m:
            continue
        name = m.group(1).strip().title()
        pub = item.findtext("pubDate") or ""
        try:
            t = datetime.strptime(pub.strip()[:25].strip(), "%a, %d %b %Y %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            try:
                t = datetime.strptime(pub.strip(), "%d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
            except ValueError:
                t = None
        rec = {"name": name, "surf": m.group(2).strip(), "when": m.group(3).strip(), "t": t,
               "desc": " ".join((item.findtext("description") or "").split())}
        if name not in latest or (t and latest[name]["t"] and t > latest[name]["t"]):
            latest[name] = rec
    if not latest:
        raise RuntimeError("no reports parsed")
    return sorted(latest.values(), key=lambda r: (r["name"] != "Makapuu", r["name"]))


def hcdp_token():
    tok = os.environ.get("HCDP_API_TOKEN", "").strip()
    if not tok and os.path.exists("hcdp_token.txt"):
        tok = open("hcdp_token.txt").read().strip()
    return tok


def is_windward(lat, lon):
    pts = KOOLAU_CREST
    if lat <= pts[0][0]:
        crest = pts[0][1]
    elif lat >= pts[-1][0]:
        crest = pts[-1][1]
    else:
        crest = next(a[1] + (b[1] - a[1]) * (lat - a[0]) / (b[0] - a[0]) for a, b in zip(pts, pts[1:]) if a[0] <= lat <= b[0])
    return lon > crest


def fetch_mesonet(token):
    """Experimental: Hawaii Mesonet stations inside WINDWARD_BOX, latest wind. Prints what it finds."""
    h = {"Authorization": "Bearer " + token}
    st = json.loads(http_get(HCDP_BASE + "/mesonet/db/stations?location=hawaii&limit=500", headers=h))
    st = st if isinstance(st, list) else st.get("stations") or st.get("data") or []
    picks = []
    for s in st:
        lat = fnum(s.get("lat") or s.get("latitude"))
        lon = fnum(s.get("lng") or s.get("lon") or s.get("longitude"))
        if lat is None or lon is None:
            continue
        if WINDWARD_BOX[0] <= lat <= WINDWARD_BOX[1] and WINDWARD_BOX[2] <= lon <= WINDWARD_BOX[3] and is_windward(lat, lon):
            picks.append({"id": str(s.get("station_id") or s.get("id")), "name": s.get("name") or s.get("full_name") or "", "lat": lat, "lon": lon})
    if not picks:
        raise RuntimeError("no Mesonet stations in the windward box (found %d statewide)" % len(st))
    start = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {"station_ids": ",".join(p["id"] for p in picks), "start_date": start, "location": "hawaii", "limit": 5000}
    ms = json.loads(http_get(HCDP_BASE + "/mesonet/db/measurements?" + urllib.parse.urlencode(q), headers=h))
    ms = ms if isinstance(ms, list) else ms.get("measurements") or ms.get("data") or []
    out = []
    for p in picks:
        mine = [m for m in ms if str(m.get("station_id")) == p["id"]]
        var_ids = sorted({str(m.get("variable") or m.get("var_id")) for m in mine})

        def latest(pattern):
            c = [m for m in mine if re.match(pattern, str(m.get("variable") or m.get("var_id")), re.I) and fnum(m.get("value")) is not None]
            return max(c, key=lambda m: str(m.get("timestamp"))) if c else None
        sp, dr, gu = latest(r"^WS(_\d)?(_Avg)?$|^wind_?speed"), latest(r"^WD(rs)?(_\d)?(_Avg)?$|^wind_?dir"), latest(r"^WS.*max|gust")
        p.update(vars=[v for v in var_ids if re.match(r"^(WS|WD)", v)][:8] or var_ids[:6], kt=fnum(sp["value"]) * MS_TO_KT if sp else None, t=sp.get("timestamp") if sp else None,
                 dir=fnum(dr["value"]) if dr else None, gust=fnum(gu["value"]) * MS_TO_KT if gu else None)
        out.append(p)
    return sorted(out, key=lambda p: p.get("kt") is None)


# =============================================================== assemble
def compass(deg):
    if deg is None or (isinstance(deg, float) and math.isnan(deg)):
        return "--"
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]


def vec_mean_dir(dirs, weights=None):
    dirs = [d for d in dirs if d is not None]
    if not dirs:
        return None
    w = weights or [1.0] * len(dirs)
    x = sum(wi * math.cos(math.radians(d)) for d, wi in zip(dirs, w))
    y = sum(wi * math.sin(math.radians(d)) for d, wi in zip(dirs, w))
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def compute_spots(spot_wind, spot_waves, days):
    """Per-spot hourly records (list of days*24) with wind, direction, lee factor, waves and score."""
    today = datetime.now(HST).replace(hour=0, minute=0, second=0, microsecond=0)
    per_spot = {}
    for name, lat, lon, near in SPOTS:
        order = (["SWAN", "WW3", "Open-Meteo"] if near else ["WW3", "SWAN", "Open-Meteo"])
        recs = []
        for d in range(days):
            for h in range(24):
                lt = (today + timedelta(days=d)).replace(hour=h)
                ut = hour_floor(lt)
                rec = {"t": lt, "hour": h}
                srcs = [v[ut] for v in spot_wind.get(name, {}).values() if ut in v and v[ut].get("wind") is not None]
                if srcs:
                    vals = [x["wind"] for x in srcs]
                    rec["wind_raw"] = sum(vals) / len(vals)
                    rec["spread"] = max(vals) - min(vals)
                    gusts = [x["gust"] for x in srcs if x.get("gust")]
                    rec["gust"] = max(gusts) if gusts else None
                    rec["wdir"] = vec_mean_dir([x.get("dir") for x in srcs])
                for src in order:
                    w = spot_waves.get(name, {}).get(src, {}).get(ut)
                    if w and w.get("hs") is not None and w.get("tp"):
                        rec.update(hs=w["hs"], tp=w["tp"], wave_src=src)
                        break
                ww3 = spot_waves.get(name, {}).get("WW3", {}).get(ut) or spot_waves.get(name, {}).get("Open-Meteo", {}).get(ut)
                if ww3:
                    rec["ws_hs"], rec["ws_tp"] = ww3.get("ws_hs"), ww3.get("ws_tp")
                recs.append(rec)
        for i, r in enumerate(recs):
            nb = [recs[j]["wind_raw"] for j in range(max(0, i - 1), min(len(recs), i + 2)) if "wind_raw" in recs[j]]
            if "wind_raw" in r and nb:
                r["wind"] = sum(nb) / len(nb) + WIND_OFFSET_KT
            if "wind" in r and "hs" in r:
                r["lee"] = lee_factor(name, r.get("wdir"))
                r["score"], r["pen"] = hour_score(r["wind"], r["hs"], r["tp"], r.get("ws_hs"), r["lee"])
        per_spot[name] = recs
    return per_spot


def combine(per_spot, names):
    """Route average per hour -> {date: [hour recs]} (rec['spots'] holds the route's spot records)."""
    result = {}
    first = per_spot[names[0]]
    for i in range(len(first)):
        lt = first[i]["t"]
        spots = {nm: per_spot[nm][i] for nm in names}
        scored = [x for x in spots.values() if "score" in x]
        rec = {"t": lt, "hour": lt.hour, "label": lt.strftime("%-I %p").lower().replace(" ", ""), "spots": spots}
        if len(scored) >= 2:
            k = len(scored)
            rec["score"] = sum(x["score"] for x in scored) / k
            rec["wind"] = sum(x["wind"] for x in scored) / k
            rec["wind_max_spot"] = max(x["wind"] for x in scored)
            rec["hs"] = sum(x["hs"] for x in scored) / k
            rec["tp"] = min(x["tp"] for x in scored)
            rec["spread"] = max(x.get("spread", 0) for x in scored)
            rec["gust"] = max((x.get("gust") or 0) for x in scored) or None
            ws = [x["ws_hs"] for x in scored if x.get("ws_hs") is not None]
            rec["ws_hs"] = max(ws) if ws else None
            pen = {}
            for x in scored:
                for kk, v in x["pen"].items():
                    pen[kk] = pen.get(kk, 0) + v / k
            rec["pen"] = pen
        result.setdefault(lt.replace(hour=0), []).append(rec)
    return result


def window(hrs):
    return [r for r in hrs if WINDOW[0] <= r["hour"] < WINDOW[1] and "score" in r]


def pick_best(per_spot):
    """Score every route; per day keep the best one. Returns (days, meta)."""
    routes = {rn: combine(per_spot, names) for rn, names in ROUTES}
    days, meta = {}, {}
    for d in routes[ROUTES[0][0]]:
        scores = {}
        for rn in routes:
            w = window(routes[rn][d])
            sm = day_summary(w) if w else None
            scores[rn] = sm["score"] if sm else None
        order = [rn for rn, _ in ROUTES]
        ranked = sorted((v, -order.index(rn), rn) for rn, v in scores.items() if v is not None)   # ties go to the first route
        best = ranked[-1][2] if ranked else ROUTES[0][0]
        days[d] = routes[best][d]
        meta[d] = {"best": best, "scores": scores}
    return days, meta


def classify_days(info):
    """info: {date: (mean_dir_deg or None, mean_wind_kt)} -> {date: [(kind, text)]}.
    kinds: settle (first light day after a windy one), watch (2nd light day in a row after a windy one), lee (south wind)."""
    def light(x):
        return x is not None and x[1] is not None and (x[1] <= CALM_KT or (x[0] is not None and SOUTH[0] <= x[0] <= SOUTH[1] and x[1] < WINDY_KT))

    def windy(x):
        return x is not None and x[1] is not None and x[1] >= WINDY_KT
    out = {}
    for d in sorted(info):
        cur, p1, p2 = info[d], info.get(d - timedelta(days=1)), info.get(d - timedelta(days=2))
        fl = []
        if light(cur) and windy(p1):
            fl.append(("settle", "Settling day: wind backs off after the trades \u2013 expect leftover slop"))
        elif light(cur) and light(p1) and windy(p2):
            fl.append(("watch", "Watch day: 2nd light day after the trades \u2013 leftover chop should have laid down"))
        dr = cur[0]
        if dr is not None and SOUTH[0] <= dr <= SOUTH[1] and cur[1] is not None and cur[1] < WINDY_KT + 2:
            fl.append(("lee", "Wind from the %s \u2013 windward side in the lee" % compass(dr)))
        if fl:
            out[d] = fl
    return out


def day_wind_info(per_spot):
    """Window-average wind speed and direction at the bay mouth for each forecast day."""
    by_day = {}
    for r in per_spot[SPOTS[0][0]]:
        if WINDOW[0] <= r["hour"] < WINDOW[1] and "wind" in r:
            by_day.setdefault(r["t"].date(), []).append(r)
    return {d: (vec_mean_dir([r.get("wdir") for r in rs], [max(r["wind"], 0.1) for r in rs]),
                sum(r["wind"] for r in rs) / len(rs)) for d, rs in by_day.items() if len(rs) >= 3}


def pattern_flags(per_spot, yesterday=None):
    info = day_wind_info(per_spot)
    if yesterday:   # observed airport wind for the past day(s), so today's flag knows what came before
        for d, v in yesterday.items():
            info.setdefault(d, v)
    return classify_days(info)


# =============================================================== HTML
def hexlum(h):
    h = h.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]


def ramp_rules(ramp):
    return "".join(".c%d{background:%s;color:%s}" % (i, bg, "#0b0b0b" if hexlum(bg) > 0.30 else "#ffffff") for i, bg in enumerate(ramp))


def cell_class(score):
    return "c%d" % max(0, min(12, int(round(score / 10 * 12))))


def mph(kt):
    return kt * KT_TO_MPH


def rng(a, b, nd=0):
    sa, sb = "%.*f" % (nd, a), "%.*f" % (nd, b)
    return sa if sa == sb else sa + "–" + sb


def fmt_day(d):
    return d.strftime("%a %b %-d")


def age_txt(t):
    if not t:
        return ""
    mins = max(0.0, (datetime.now(UTC) - t.astimezone(UTC)).total_seconds() / 60)
    return "%d min ago" % mins if mins < 90 else "%.0f h ago" % (mins / 60) if mins < 48 * 60 else "%.0f days ago" % (mins / 1440)


def esc(s):
    return html.escape(str(s))


def render(days, ctx, status, demo):
    per_spot, meta, flags = ctx.get("per_spot", {}), ctx.get("route_meta", {}), ctx.get("flags", {})
    pbc = ctx.get("penguin")
    pb_days = pbc["days"] if pbc else {}
    summaries, spot_scores = {}, {}
    for d, hrs in days.items():
        win = window(hrs)
        summaries[d] = day_summary(win) if win else None
        spot_scores[d] = {}
        for name, *_ in SPOTS:
            ss = [r["score"] for r in per_spot.get(name, []) if r["t"].date() == d.date() and WINDOW[0] <= r["hour"] < WINDOW[1] and "score" in r]
            if len(ss) >= 3:
                spot_scores[d][name] = sum(ss) / len(ss)
    cwf_days = ctx.get("cwf_days", {})
    scored = [(d, s) for d, s in summaries.items() if s]
    epic = [(d, s) for d, s in scored if s["score"] >= TIERS[0][0] and not s["notes"]]
    if epic:
        d, s = epic[0]
        hero = ("Next epic day", fmt_day(d), "score %.1f" % s["score"])
    elif scored:
        d, s = max(scored, key=lambda x: x[1]["score"])
        hero = ("No epic day in the forecast. Best day", fmt_day(d), "score %.1f, %s" % (s["score"], tier_for(s["score"])[0].lower()))
    else:
        hero = ("No forecast scored yet", "", "see the source list at the bottom")
    watch = [d for d in sorted(flags) if any(k == "watch" for k, _ in flags[d]) and d >= datetime.now(HST).date()]
    if watch:
        hero = (hero[0], hero[1], hero[2] + " \u00b7 \u2605 watch day: %s (2nd light day after the trades)" % watch[0].strftime("%a %b %-d"))
    pb_ok = [d for d in sorted(pb_days) if pb_days[d]["confirmed"]]
    if pb_ok:
        hero = (hero[0], hero[1], hero[2] + " \u00b7 \u2693 Penguin Bank window: %s" % ", ".join(d.strftime("%a %b %-d") for d in pb_ok))

    cards, strips, rows = [], [], []
    for d, hrs in days.items():
        s = summaries[d]
        title = fmt_day(d) + ("  · weekend" if d.weekday() >= 5 else "")
        nws = cwf_days.get(d.date(), {}).get("day")
        nws_w = nws_wind_range(nws)
        spots_line = " · ".join("%s %.1f" % (n.replace(" FAD", ""), v) for n, v in spot_scores[d].items())
        if s:
            name, icon, role = tier_for(s["score"])
            note = s["notes"][0] if s["notes"] else ("held back by " + s["limiter"] if s["limiter"] and s["score"] < 9 else "clean across the window")
            extra = ""
            m = meta.get(d)
            if m and len(ROUTES) > 1:
                others = " · ".join("%s %.1f" % (rn, v) for rn, v in m["scores"].items() if rn != m["best"] and v is not None)
                extra += '<p class="route">Best: <b>%s</b>%s</p>' % (esc(m["best"]), (" · " + esc(others)) if others else "")
            for kind, text in flags.get(d.date(), []):
                extra += '<p class="flag %s">%s %s</p>' % (kind, "★" if kind == "watch" else "↻", esc(text))
            if pbc:
                import penguin as pb_mod
                extra += pb_mod.card_badge(pb_days.get(d.date()), esc)
            if nws_w:
                warn = " – windier than the score assumes" if nws_w[1] >= 15 and s["score"] >= 7.5 else ""
                extra += '<p class="nws">NWS: %s kt%s</p>' % (rng(*nws_w), warn)
            cards.append(
                '<article class="card %s"><h3>%s</h3><div class="big">%.1f</div>'
                '<div class="tier"><span class="dot %s" aria-hidden="true">%s</span> %s</div>'
                '<p class="meta">wind %s mph<br>waves %s ft @ %s s%s</p><p class="spots">%s</p>%s<p class="note">%s</p></article>'
                % (role, title, s["score"], role, icon, name, rng(mph(s["wind_min"]), mph(s["wind_max"])),
                   rng(s["hs_min"], s["hs_max"], 1), rng(s["tp_min"], s["tp_max"]),
                   ("<br>chop up to %.1f ft" % s["chop_max"]) if s["chop_max"] else "", esc(spots_line), extra, esc(note)))
        else:
            past = d.date() == datetime.now(HST).date() and datetime.now(HST).hour >= WINDOW[1]
            cards.append('<article class="card none"><h3>%s</h3><div class="big">–</div><div class="tier">%s</div>%s</article>'
                         % (title, "today's window has passed" if past else "no forecast",
                            ('<p class="nws">NWS: %s kt</p>' % rng(*nws_w)) if nws_w else ""))
        cells = []
        for r in hrs:
            if r["hour"] not in STRIP_HOURS:
                continue
            inwin = WINDOW[0] <= r["hour"] < WINDOW[1]
            if "score" in r:
                lines = ["%s  route score %.1f" % (r["label"], r["score"])]
                for nm, sp in r["spots"].items():
                    if "score" in sp:
                        lines.append("%s %.1f: %.0f mph %s%s, %.1f ft @ %.0f s" % (
                            nm.replace(" FAD", ""), sp["score"], mph(sp["wind"]), compass(sp.get("wdir")),
                            " (lee)" if sp.get("lee", 1) < 0.9 else "", sp["hs"], sp["tp"]))
                if r.get("ws_hs"):
                    lines.append("wind chop up to %.1f ft" % r["ws_hs"])
                cells.append('<div class="cell %s%s" tabindex="0" data-tip="%s">%.0f</div>' % (
                    cell_class(r["score"]), " win" if inwin else "", esc("\n".join(lines)), r["score"]))
            else:
                cells.append('<div class="cell nodata%s" data-tip="%s no data">–</div>' % (" win" if inwin else "", r["label"]))
        strips.append('<div class="strip-row"><div class="strip-label">%s</div><div class="strip">%s</div></div>' % (fmt_day(d), "".join(cells)))
        if s:
            rows.append("<tr><td>%s</td><td>%.1f</td><td>%s</td><td>%s</td><td>%s mph (%s kt)</td><td>%s ft</td><td>%s s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                fmt_day(d), s["score"], tier_for(s["score"])[0], esc(meta.get(d, {}).get("best", "")),
                rng(mph(s["wind_min"]), mph(s["wind_max"])), rng(s["wind_min"], s["wind_max"]),
                rng(s["hs_min"], s["hs_max"], 1), rng(s["tp_min"], s["tp_max"]), esc(spots_line or "–"),
                ("models differ by %.0f kt" % s["spread_max"]) if s["spread_max"] >= 3 else "agree",
                esc("; ".join(s["notes"]) or (s["limiter"] or "–"))))

    hours_hdr = "".join('<div class="cell hdr">%d%s</div>' % ((h % 12) or 12, "a" if h < 12 else "p") for h in STRIP_HOURS)

    # ---------- right now
    tiles = []
    b = ctx.get("buoys", {}).get("51202")
    if b:
        split = ""
        if b.get("sw_hs") is not None:
            split = "<br>swell %.1f ft @ %s s %s · wind waves %.1f ft @ %s s %s" % (
                b["sw_hs"], fmt0(b.get("sw_tp")), esc(b.get("sw_dir") or ""), b["ww_hs"], fmt0(b.get("ww_tp")), esc(b.get("ww_dir") or ""))
        label = (b.get("steep_label") or "").replace("_", " ").lower()
        tiles.append('<div class="tile"><div class="k">Mokapu buoy · %s</div><div class="v">%.1f ft @ %s s</div>'
                     '<div class="s">NDBC calls it <b>%s</b>%s%s</div></div>' % (
                         age_txt(b["t"]), b["hs"], fmt0(b.get("tp")), esc(label or "n/a"),
                         (" · steepness %.3f" % b["steep"]) if b.get("steep") else "", split))
    for key, title in (("airport", "Kaneohe airport wind"), ("himb", "Coconut Island wind (in the bay)")):
        a = ctx.get(key)
        if a:
            tiles.append('<div class="tile"><div class="k">%s · %s</div><div class="v">%.0f mph (%.0f kt)</div><div class="s">from the %s%s</div></div>' % (
                title, age_txt(a["t"]), mph(a["kt"]), a["kt"], compass(a.get("dir")), (" · gusts %.0f mph" % mph(a["gust"])) if a.get("gust") else ""))
    for m in ctx.get("mesonet", []) or []:
        if m.get("kt") is not None:
            tiles.append('<div class="tile"><div class="k">Mesonet · %s</div><div class="v">%.0f mph (%.0f kt)</div><div class="s">from the %s%s</div></div>' % (
                esc(m["name"] or m["id"]), mph(m["kt"]), m["kt"], compass(m.get("dir")), (" · gusts %.0f mph" % mph(m["gust"])) if m.get("gust") else ""))
    lg = ctx.get("lifeguards") or []
    if lg:
        items = "".join("<li><b>%s</b> %s at %s%s</li>" % (esc(r["name"]), esc(r["surf"]), esc(r["when"]), (" – " + esc(r["desc"])) if r["desc"] else "") for r in lg[:6])
        tiles.append('<div class="tile wide"><div class="k">Lifeguard surf reports (Oahu)</div><ul class="lg">%s</ul></div>' % items)
    now_html = "".join(tiles) or '<p class="muted">No live readings came through on this run.</p>'

    up_rows = []
    for sid, label in UPSTREAM:
        u = ctx.get("buoys", {}).get(sid)
        if not u:
            up_rows.append("<tr><td>%s</td><td colspan=\"5\" class=\"muted\">not reporting</td></tr>" % esc(label))
            continue
        stale = (datetime.now(UTC) - u["t"]).total_seconds() > STALE_HOURS * 3600
        trend = "" if u.get("trend") is None else ("rising" if u["trend"] > 0.5 else "dropping" if u["trend"] < -0.5 else "steady")
        up_rows.append("<tr%s><td>%s</td><td>%s</td><td>%.1f ft</td><td>%s s</td><td>%s</td><td>%s</td></tr>" % (
            ' class="stale"' if stale else "", esc(label), age_txt(u["t"]) + (" (stale)" if stale else ""), u["hs"], fmt0(u.get("tp")),
            compass(u.get("dir")), trend))

    heads = ctx.get("cwf_heads") or []
    banner = "".join('<div class="banner">⚠ NWS: %s</div>' % esc(h) for h in heads)
    cwf_html = ""
    if cwf_days:
        items = []
        for d in sorted(cwf_days):
            for part in ("day", "night"):
                if cwf_days[d].get(part):
                    items.append("<p><b>%s%s</b> %s</p>" % (d.strftime("%a %b %-d"), " night" if part == "night" else "", esc(cwf_days[d][part])))
        cwf_html = '<div class="cwf"><div class="muted">%s</div>%s</div>' % (esc(ctx.get("cwf_issued", "")), "".join(items))

    status_html = "".join('<li><span class="%s">%s</span> %s</li>' % ("ok" if ok else "bad", "✓" if ok else "✕", esc(msg)) for ok, msg in status)
    win_txt = "%d %s–%d %s" % ((WINDOW[0] % 12) or 12, "AM" if WINDOW[0] < 12 else "PM", (WINDOW[1] % 12) or 12, "AM" if WINDOW[1] < 12 else "PM")
    rep = {"%%LIGHT%%": ramp_rules(BLUE), "%%DARK%%": ramp_rules(BLUE[::-1]),
           "%%UPDATED%%": datetime.now(HST).strftime("%a %b %-d, %-I:%M %p HST"),
           "%%DEMO%%": '<p class="demo">DEMO DATA – made-up numbers, just to show the layout.</p>' if demo else "",
           "%%BANNER%%": banner, "%%HERO_K%%": esc(hero[0]), "%%HERO_V%%": esc(hero[1]), "%%HERO_S%%": esc(hero[2]),
           "%%CARDS%%": "".join(cards), "%%HOURS_HDR%%": hours_hdr, "%%STRIPS%%": "".join(strips), "%%NOW%%": now_html,
           "%%UP%%": "".join(up_rows), "%%CWF%%": cwf_html or '<p class="muted">NWS text forecast not available this run.</p>',
           "%%ROWS%%": "".join(rows), "%%STATUS%%": status_html, "%%WIN%%": win_txt,
           "%%SPOTS%%": esc(" / ".join(rn for rn, _ in ROUTES)), "%%PENGUIN%%": penguin_section(pbc)}
    page = PAGE
    for k, v in rep.items():
        page = page.replace(k, v)
    return page


def penguin_section(pbc):
    try:
        import penguin as pb_mod
    except Exception:  # noqa
        return ""
    return pb_mod.check_table(pbc, esc, os.path.exists(os.path.join(pb_mod.SAVED_DIR, pb_mod.PAGE_NAME)))


def fmt0(x):
    return "--" if x is None else "%.0f" % x


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Windward Go Score</title><style>
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--ring:rgba(11,11,11,.10);
--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;--okt:#006300;--badt:#b32d2d;--demo:#fff3cd;--warnbg:#fde8e8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--okt:#0ca30c;--badt:#ec835a;--demo:#4a3d10;--warnbg:#4a1d1d}}
%%LIGHT%%
@media (prefers-color-scheme:dark){%%DARK%%}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:28px 0 10px;color:var(--ink2);font-weight:600}
.sub,.muted{color:var(--ink2);font-size:13px}.demo{background:var(--demo);padding:8px 12px;border-radius:8px;font-weight:600}
.banner{background:var(--warnbg);border:1px solid var(--critical);padding:10px 12px;border-radius:8px;font-weight:600;margin:10px 0}
.hero{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:18px 20px;margin:16px 0}.hero .k{color:var(--ink2);font-size:13px}.hero .v{font-size:40px;font-weight:700;line-height:1.1}.hero .s{color:var(--ink2)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.card{background:var(--surface);border:1px solid var(--ring);border-left:5px solid var(--muted);border-radius:10px;padding:12px}.card h3{margin:0;font-size:13px;color:var(--ink2);font-weight:600}
.card.good{border-left-color:var(--good)}.card.warning{border-left-color:var(--warning)}.card.critical{border-left-color:var(--critical)}.big{font-size:34px;font-weight:700;line-height:1.1}
.tier{font-weight:600}.dot{display:inline-block;width:1.3em;text-align:center;border-radius:50%;color:#fff;font-size:12px;line-height:1.3em}.dot.good{background:var(--good)}.dot.warning{background:var(--warning);color:#000}.dot.critical{background:var(--critical)}
.meta{color:var(--ink2);font-size:13px;margin:8px 0 4px}.spots{font-size:12px;color:var(--ink2);margin:0 0 4px;font-variant-numeric:tabular-nums}.nws{font-size:12px;margin:0 0 4px;color:var(--ink)}.route{font-size:12px;margin:0 0 4px;color:var(--ink2)}.flag{font-size:12px;margin:0 0 4px;font-weight:600;color:var(--okt)}.flag.settle,.flag.lee{font-weight:500;color:var(--ink2)}.flag.penguin{color:var(--ink)}.flag.penguin a{color:inherit}.pb-yes td{font-weight:600}.note{color:var(--muted);font-size:12px;margin:0}
.strip-row{display:grid;grid-template-columns:78px 1fr;gap:8px;align-items:center;margin:4px 0}.strip-label{font-size:12px;color:var(--ink2)}
.strip,.strip-head{display:grid;grid-template-columns:repeat(13,minmax(0,1fr));gap:2px}.cell{position:relative;text-align:center;font-size:12px;padding:6px 0;border-radius:4px;font-variant-numeric:tabular-nums}
.cell.hdr{background:none;color:var(--muted);font-size:11px;padding:2px 0}.cell.nodata{background:var(--grid);color:var(--muted)}.cell.win{box-shadow:0 0 0 2px var(--ink2) inset}
.cell[data-tip]:hover::after,.cell[data-tip]:focus::after{content:attr(data-tip);position:absolute;z-index:5;left:50%;bottom:calc(100% + 6px);transform:translateX(-50%);white-space:pre;text-align:left;
background:var(--ink);color:var(--surface);padding:8px 10px;border-radius:6px;font-size:12px;line-height:1.35;min-width:170px;pointer-events:none}
.strip .cell:nth-child(-n+3)[data-tip]:hover::after,.strip .cell:nth-child(-n+3)[data-tip]:focus::after{left:0;transform:none}
.strip .cell:nth-last-child(-n+3)[data-tip]:hover::after,.strip .cell:nth-last-child(-n+3)[data-tip]:focus::after{left:auto;right:0;transform:none}
.legend{font-size:12px;color:var(--ink2);margin:8px 0 0}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px}.tile.wide{grid-column:1/-1}.tile .k{color:var(--ink2);font-size:13px}.tile .v{font-size:22px;font-weight:700}.tile .s{color:var(--ink2);font-size:13px}
ul.lg{margin:6px 0 0;padding-left:18px;font-size:13px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface);border:1px solid var(--ring);border-radius:10px}th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--grid);white-space:nowrap}th{color:var(--ink2);font-weight:600}
tr.stale td{color:var(--muted)}.cwf{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px;font-size:13px}.cwf p{margin:6px 0}
ul.status{list-style:none;padding:0;margin:0;font-size:13px}ul.status li{margin:3px 0}.ok{color:var(--okt);font-weight:700}.bad{color:var(--badt);font-weight:700}
.fine{font-size:12px;color:var(--ink2);margin-top:8px}
@media (max-width:600px){.cell.hdr:nth-child(even){visibility:hidden}.cell{font-size:11px}}
</style></head><body><main>
<h1>Windward Go Score</h1><div class="sub">Heeia Kea · 15' RIB · routes: %%SPOTS%% · scored over %%WIN%% · updated %%UPDATED%%</div>%%DEMO%%%%BANNER%%
<section class="hero"><div class="k">%%HERO_K%%</div><div class="v">%%HERO_V%%</div><div class="s">%%HERO_S%%</div></section>
<h2>Next 7 days</h2><div class="cards">%%CARDS%%</div>
<h2>Hour by hour (best route)</h2><div class="strip-row"><div></div><div class="strip-head">%%HOURS_HDR%%</div></div>%%STRIPS%%
<p class="legend">Each cell is that hour's score out of 10 for that day's best route, averaged over its spots (the number is in the cell). Outlined cells are your trip window. Hover or tap a cell for each spot.</p>
<h2>Right now</h2><div class="tiles">%%NOW%%</div>
<h2>Buoys, nearest first then upstream</h2><div class="wrap"><table><thead><tr><th>Buoy</th><th>Reading</th><th>Waves</th><th>Period</th><th>From</th><th>Last 6 h</th></tr></thead><tbody>%%UP%%</tbody></table></div>
<h2>NWS forecast – Oahu Windward Waters</h2>%%CWF%%
<h2>Details</h2><div class="wrap"><table><thead><tr><th>Day</th><th>Score</th><th>Call</th><th>Best route</th><th>Wind</th><th>Waves</th><th>Period</th><th>By spot</th><th>Wind models</th><th>Held back by</th></tr></thead><tbody>%%ROWS%%</tbody></table></div>
<h2>Penguin Bank check</h2>%%PENGUIN%%
<h2>Data sources this run</h2><ul class="status">%%STATUS%%</ul>
<p class="fine">How to read this: the score takes points off 10 for sustained wind (3-hour average), short-period chop and big waves, at each spot on the route, then averages the spots. Long, smooth swell costs almost nothing; small waves with a short period cost a lot. Epic needs 9+ with no rough hour; the day is capped at 3 if the route-average wind holds at 12 kt or more for 2+ hours, or any spot hits 15 kt. WW3 wind chop over 2.5 ft costs points even when the swell period looks long. Offshore spots use the WW3 model, which separates wind chop from swell. Fitted to five of your days, so treat the tiers as a starting point.</p>
</main></body></html>"""


# =============================================================== demo
def demo_ctx():
    shapes = [(3, 9, 4.0, 9.5), (1, 7, 5.3, 10.5), (6, 12, 4.6, 6.0), (4, 10, 4.0, 7.5), (2, 8, 3.0, 8.0), (1, 6, 2.8, 8.5), (8, 12, 5.0, 6.8)]
    today = datetime.now(HST).replace(hour=0, minute=0, second=0, microsecond=0)
    sw, sv = {}, {}
    for k, (name, *_r) in enumerate(SPOTS):
        wind, wave = {}, {}
        for d, (w0, w1, hs, tp) in enumerate(shapes):
            for h in range(24):
                ut = hour_floor(today + timedelta(days=d, hours=h))
                frac = max(0.0, min(1.0, (h - 5) / 7.0))
                w = (w0 + (w1 - w0) * frac if h < 17 else w1) + 0.6 * k
                wind[ut] = {"wind": w, "gust": w + 3, "dir": [70, 190, 60, 75, 200, 180, 60][d]}
                wave[ut] = {"hs": hs + 0.3 * k, "tp": tp, "ws_hs": max(0.5, w / 5), "ws_tp": 4.5}
        sw[name] = {"demo": wind}
        sv[name] = {"WW3": wave}
    now = datetime.now(UTC)
    ctx = {"buoys": {"51202": {"t": now - timedelta(minutes=40), "hs": 4.3, "tp": 9.1, "dir": 60, "trend": 0.2, "sw_hs": 2.6, "sw_tp": 10.5, "sw_dir": "ENE",
                               "ww_hs": 3.3, "ww_tp": 7.1, "ww_dir": "ENE", "steep_label": "AVERAGE", "steep": steepness(4.3, 9.1)},
                     "51201": {"t": now - timedelta(hours=1), "hs": 3.1, "tp": 13.0, "dir": 330, "trend": 1.0},
                     "51205": {"t": now - timedelta(days=44), "hs": 5.2, "tp": 8.0, "dir": 70, "trend": None}},
           "airport": {"t": datetime.now(HST) - timedelta(minutes=50), "kt": 8, "dir": 70, "gust": None},
           "himb": {"t": now - timedelta(minutes=70), "kt": 6, "dir": 80, "gust": 11},
           "lifeguards": [{"name": "Makapuu", "surf": "3-5 ft", "when": "10:00 AM HST", "t": now, "desc": ""}],
           "cwf_heads": [], "cwf_issued": "315 AM HST (demo)",
           "cwf_days": {(today + timedelta(days=i)).date(): {"day": "East winds %d to %d knots. Seas 4 to 6 feet." % (10 + i, 15 + i)} for i in range(5)}}
    return sw, sv, ctx


# =============================================================== backtest
CALIBRATION = [("2026-09-08", "sloppy but manageable, nearshore, day before epic"), ("2026-09-09", "epic"), ("2026-09-13", "good"),
               ("2026-06-13", "nice"), ("2026-06-21", "nice"), ("2026-06-16", "MISERABLE")]
SEQUENCE = ("2026-09-03", "2026-09-12")    # storm -> settle -> epic run, checked against the pattern flags


def airport_day(d):
    """Observed Kaneohe airport wind for one HST day -> {hour: (kt, dir)}."""
    d1 = d + timedelta(days=1)
    q = [("station", "PHNG"), ("data", "sknt"), ("data", "drct"), ("year1", d.year), ("month1", d.month), ("day1", d.day),
         ("year2", d1.year), ("month2", d1.month), ("day2", d1.day), ("tz", "Pacific/Honolulu"), ("format", "onlycomma"),
         ("latlon", "no"), ("elev", "no"), ("missing", "M"), ("report_type", "3")]
    rows = [r for r in csv.reader(io.StringIO(http_get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q)))) if r]
    out = {}
    for r in rows[1:]:
        rec = dict(zip(rows[0], r))
        kt = fnum(rec.get("sknt"))
        if kt is None:
            continue
        t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=HST)
        if t.date() == d.date():
            out[t.hour] = (kt, fnum(rec.get("drct")) if kt > 0 else None)
    return out


def backtest_day(d, verbose=True):
    lat, lon = 21.414, -157.681
    t0, t1 = d.replace(hour=WINDOW[0] - 1), d.replace(hour=WINDOW[1] + 1)
    try:
        ww3 = run_with_timeout(lambda: fetch_ww3(lat, lon, t0, t1), 130)
    except Exception as e:  # noqa
        ww3 = {}
        print("   WW3 failed: %s" % str(e)[:120])
    swan = {}
    if verbose:
        try:
            swan = run_with_timeout(lambda: fetch_swan(lat, lon, t0, t1), 130)
        except Exception as e:  # noqa
            print("   SWAN failed: %s" % str(e)[:120])
    try:
        wind = airport_day(d)
    except Exception as e:  # noqa
        wind = {}
        print("   airport wind failed: %s" % str(e)[:120])
    if verbose:
        print("   hour  wind kt from | WW3: Hs ft  Tp s  chop ft @ s | SWAN: Hs ft  Tp s | score")
    scores, kts, dirs, chops, hss, tps = [], [], [], [], [], []
    for h in range(WINDOW[0], WINDOW[1]):
        ut = hour_floor(d.replace(hour=h))
        w, s = ww3.get(ut), swan.get(ut)
        kt, dr = wind.get(h, wind.get(h - 1, (None, None)))
        if kt is not None:
            kts.append(kt)
            if dr is not None:
                dirs.append(dr)
        sc = hour_score(kt, w["hs"], w["tp"], w.get("ws_hs"))[0] if (w and kt is not None) else None
        if w:
            hss.append(w["hs"]); tps.append(w["tp"])
            chops.append(w.get("ws_hs") or 0)
        if sc is not None:
            scores.append(sc)
        if verbose:
            print("   %2d:00  %5s  %-4s | %8s %5s  %4s @ %-4s   | %9s %5s | %s" % (
                h, fmt0(kt), compass(dr) if dr is not None else "", "%.1f" % w["hs"] if w else "--", "%.1f" % w["tp"] if w else "--",
                "%.1f" % w["ws_hs"] if w and w.get("ws_hs") is not None else "--", "%.1f" % w["ws_tp"] if w and w.get("ws_tp") else "--",
                "%.1f" % s["hs"] if s else "--", "%.1f" % s["tp"] if s else "--", "%.1f" % sc if sc is not None else "--"))
    return {"score": sum(scores) / len(scores) if scores else None, "kt": sum(kts) / len(kts) if kts else None,
            "kt_max": max(kts) if kts else None, "dir": vec_mean_dir(dirs) if dirs else None,
            "chop": max(chops) if chops else None, "hs": (sum(hss) / len(hss)) if hss else None, "tp": (sum(tps) / len(tps)) if tps else None}


def backtest():
    """Score the calibration days with model waves (WW3/SWAN at the Mokapu buoy) + observed airport wind,
    then run the SEQUENCE days through the settle/watch pattern check."""
    print("Backtest: model waves at the Mokapu buoy + observed Kaneohe airport wind, %d:00-%d:00 HST\n" % WINDOW)
    for date_str, rating in CALIBRATION:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=HST)
        print("== %s (you said: %s)" % (date_str, rating))
        r = backtest_day(d)
        print("   -> window score %s\n" % ("%.1f" % r["score"] if r["score"] is not None else "--"))
    d0 = datetime.strptime(SEQUENCE[0], "%Y-%m-%d").replace(tzinfo=HST)
    d1 = datetime.strptime(SEQUENCE[1], "%Y-%m-%d").replace(tzinfo=HST)
    print("== Pattern check %s to %s (airport wind, WW3 waves at Mokapu)" % (SEQUENCE[0], SEQUENCE[1]))
    print("   day          wind avg/max kt  from | waves ft @ s | chop ft | score | pattern")
    rows, info = [], {}
    d = d0
    while d <= d1:
        r = backtest_day(d, verbose=False)
        rows.append((d, r))
        if r["kt"] is not None:
            info[d.date()] = (r["dir"], r["kt"])
        d += timedelta(days=1)
    flags = classify_days(info)
    for d, r in rows:
        print("   %-11s  %5s / %-5s     %-4s | %4s @ %-4s  | %5s   | %5s | %s" % (
            d.strftime("%a %b %-d"), "%.0f" % r["kt"] if r["kt"] is not None else "--", "%.0f" % r["kt_max"] if r["kt_max"] is not None else "--",
            compass(r["dir"]) if r["dir"] is not None else "", "%.1f" % r["hs"] if r["hs"] else "--", "%.0f" % r["tp"] if r["tp"] else "--",
            "%.1f" % r["chop"] if r["chop"] is not None else "--", "%.1f" % r["score"] if r["score"] is not None else "--",
            "; ".join(k for k, _ in flags.get(d.date(), []))))


# =============================================================== phone push (ntfy)
def push_summary(days, meta, flags, heads, n_days=5):
    """Build (title, message, priority) for the push."""
    today = datetime.now(HST).date()
    lines, best, hot = [], None, False
    for d, hrs in days.items():
        if not (0 <= (d.date() - today).days < n_days):
            continue
        s = day_summary(window(hrs))
        if not s:
            continue
        name = tier_for(s["score"])[0]
        kinds = [k for k, _ in flags.get(d.date(), [])]
        mark = " \u2605 watch" if "watch" in kinds else (" \u21BB settling" if "settle" in kinds else "")
        route = "Kahuku side" if meta.get(d, {}).get("best", "").startswith("North") else "MM & T"
        lines.append("%s: %.1f %s (%s)%s" % (d.strftime("%a %-d"), s["score"], name, route, mark))
        if best is None or s["score"] > best[1]:
            best = (d, s["score"], name)
        if (s["score"] >= TIERS[0][0] or "watch" in kinds) and (d.date() - today).days <= 3:
            hot = True
    for h in heads or []:
        lines.append("\u26A0 NWS: " + h.capitalize())
    if best:
        title = "Go Score: best %s %.1f %s" % (best[0].strftime("%a %-d"), best[1], best[2])
    else:
        title = "Go Score: no forecast this run"
    return title, "\n".join(lines) or "No scored days.", (4 if hot else 2)


def send_push(days, meta, ctx):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("push: NTFY_TOPIC not set, skipping")
        return
    title, msg, prio = push_summary(days, meta, ctx.get("flags", {}), ctx.get("cwf_heads"))
    body = {"topic": topic, "title": title, "message": msg, "priority": prio, "tags": ["ocean"]}
    if os.environ.get("PAGE_URL"):
        body["click"] = os.environ["PAGE_URL"]
    if os.environ.get("NTFY_DRY_RUN"):
        print("push (dry run):", json.dumps(body, ensure_ascii=False))
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(server, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("push sent (%s)" % r.status)


# =============================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--out", default="go_score.html")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--notify", action="store_true", help="send a phone push via ntfy (needs NTFY_TOPIC)")
    ap.add_argument("--penguin", action="store_true", help="also write the detailed Penguin Bank forecast (penguin.html)")
    a = ap.parse_args()
    if a.backtest:
        return backtest()

    status, ctx = [], {}
    if a.demo:
        spot_wind, spot_waves, ctx = demo_ctx()
        status.append((True, "DEMO mode: no live data was fetched"))
    else:
        spot_wind = {s[0]: {} for s in SPOTS}
        spot_waves = {s[0]: {} for s in SPOTS}
        t0 = hour_floor(datetime.now(UTC) - timedelta(hours=2))
        t1 = t0 + timedelta(days=DAYS_AHEAD + 1)

        def job(label, fn, secs=75):
            print("...  %s" % label, flush=True)
            try:
                v = run_with_timeout(fn, secs)
                status.append((True, label))
                print("ok   " + label)
                return v
            except Exception as e:  # noqa
                status.append((False, "%s failed: %s" % (label, str(e)[:140])))
                print("FAIL %s: %s" % (label, str(e)[:140]))
                return None

        om = job("Open-Meteo wind (all spots)", fetch_om_wind)
        for name in (om or {}):
            spot_wind[name]["Open-Meteo"] = om[name]
        omm = job("Open-Meteo Marine waves (all spots)", fetch_om_marine)
        for name in (omm or {}):
            spot_waves[name]["Open-Meteo"] = omm[name]
        for name, lat, lon, near in SPOTS:
            v = job("NWS wind forecast @ %s" % name, lambda: fetch_nws(lat, lon, t0, t1), 45)
            if v:
                spot_wind[name]["NWS"] = v
            models = [("WW3", "WW3 waves + chop", lambda: fetch_ww3(lat, lon, t0, None))]
            if in_swan_grid(lat, lon):
                models.append(("SWAN", "SWAN waves", lambda: fetch_swan(lat, lon, t0, None)))
            for key, label, fn in models:
                ck = "%s_%s" % (key, name.replace(" ", "_"))
                v = job("%s @ %s" % (label, name), fn, 130)
                if v:
                    spot_waves[name][key] = v
                    save_cache(ck, v)
                else:
                    cached, age = load_cache(ck)
                    if cached:
                        spot_waves[name][key] = cached
                        status.append((True, "%s @ %s: server slow, using saved copy from %.0f h ago" % (label, name, age)))
                        print("     using saved %s from %.0f h ago" % (label, age))
        txt = job("NWS Oahu Windward Waters forecast", fetch_cwf, 40)
        if txt:
            ctx["cwf_heads"], ctx["cwf_issued"], ctx["cwf_days"] = parse_cwf(txt, datetime.now(HST).date())
        ctx["buoys"] = {}
        for sid, label in UPSTREAM:
            v = job("buoy %s %s" % (sid, label.split(" –")[0]), lambda: fetch_buoy(sid), 40)
            if v:
                ctx["buoys"][sid] = v
        ctx["airport"] = job("Kaneohe airport wind", fetch_airport, 40)
        past = {}
        for k in (1, 2):   # observed wind for the last two days, so the settle/watch pattern knows what came before
            dd = (datetime.now(HST) - timedelta(days=k)).replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                w = run_with_timeout(lambda: airport_day(dd), 30)
                hrs = [v for h, v in w.items() if WINDOW[0] <= h < WINDOW[1]]
                if len(hrs) >= 3:
                    past[dd.date()] = (vec_mean_dir([x[1] for x in hrs if x[1] is not None]), sum(x[0] for x in hrs) / len(hrs))
            except Exception:  # noqa
                pass
        ctx["past_days"] = past
        ctx["himb"] = job("Coconut Island (HIMB) wind", fetch_himb, 40)
        ctx["lifeguards"] = job("Lifeguard surf reports", fetch_lifeguards, 40)
        tok = hcdp_token()
        if tok:
            ctx["mesonet"] = job("Hawaii Mesonet (windward stations)", lambda: fetch_mesonet(tok), 60)
            for m in ctx["mesonet"] or []:
                print("     mesonet %s %-22s %s  vars=%s" % (m["id"], m["name"][:22],
                      ("%.0f kt from %s" % (m["kt"], compass(m.get("dir")))) if m.get("kt") is not None else "no wind reading",
                      ",".join(m.get("vars", []))))
        else:
            status.append((False, "Hawaii Mesonet: no token yet - put your free API token in hcdp_token.txt"))

    per_spot = compute_spots(spot_wind, spot_waves, DAYS_AHEAD)
    days, meta = pick_best(per_spot)
    ctx.update(per_spot=per_spot, route_meta=meta, flags=pattern_flags(per_spot, ctx.get("past_days")))
    out_dir = os.path.dirname(os.path.abspath(a.out))
    pb_mod = None
    if not a.demo:
        try:
            import penguin as pb_mod
            me = sys.modules[__name__]
            print("...  Penguin Bank %s" % ("forecast" if a.penguin else "check"), flush=True)
            ctx["penguin"] = pb_mod.detail(me, out_dir, status) if a.penguin else pb_mod.check(me, status)
        except Exception as e:  # noqa
            status.append((False, "Penguin Bank check failed: %s" % str(e)[:140]))
            print("FAIL Penguin Bank: %s" % str(e)[:140])
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(render(days, ctx, status, a.demo))
    if pb_mod:
        pb_mod.publish_saved(out_dir)

    if not a.demo:
        os.makedirs("forecast_history", exist_ok=True)
        def rnd(v):
            return round(v, 2) if isinstance(v, float) else v
        snap = {n: [{"t": r["t"].isoformat(), **{k: rnd(r.get(k)) for k in ("score", "wind", "wdir", "lee", "hs", "tp", "ws_hs", "wave_src")}}
                    for r in recs if r["hour"] in STRIP_HOURS] for n, recs in per_spot.items()}
        with gzip.open(os.path.join("forecast_history", datetime.now(HST).strftime("forecast_%Y%m%d_%H%M.json.gz")), "wt") as f:
            json.dump(snap, f)

    print("\nDay-by-day (trip window %d:00-%d:00 HST):" % WINDOW)
    for d, hrs in days.items():
        win = [r for r in hrs if WINDOW[0] <= r["hour"] < WINDOW[1] and "score" in r]
        s = day_summary(win) if win else None
        print("  %-11s %s%s" % (fmt_day(d), ("%.1f  %-9s %-20s wind %s mph, waves %s ft @ %s s" % (
            s["score"], tier_for(s["score"])[0], meta[d]["best"], rng(mph(s["wind_min"]), mph(s["wind_max"])), rng(s["hs_min"], s["hs_max"], 1),
            rng(s["tp_min"], s["tp_max"]))) if s else "no forecast", ("  [" + "; ".join(t for _, t in ctx["flags"][d.date()]) + "]") if d.date() in ctx["flags"] else ""))
    pbc = ctx.get("penguin")
    if pbc:
        print("\nPenguin Bank check:")
        for d, v in sorted(pbc["days"].items()):
            print("  %s  %s" % (d.strftime("%a %b %-d"), ("WINDOW" + (" (confirmed)" if v["confirmed"] else " (1st run)")) if v["pass"] else "no: " + "; ".join(v["fails"])))
    print("\nWrote %s" % os.path.abspath(a.out))
    if a.notify and not a.demo:
        try:
            send_push(days, meta, ctx)
        except Exception as e:  # noqa
            print("push failed: %s" % str(e)[:160])
    if a.open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    sys.exit(main())
