#!/usr/bin/env python3
"""
surf_backtest.py - No Can Tells surf backtest.

No Can Tells: reef break off Bellows, about a mile south of the Mokes, boat access only. It breaks on the
NE corner of the reef and catches north wrap. Wes's known sessions are in KNOWN below.

For every day in the range it collects the 6 AM - noon conditions from every source that has history,
then shows where the known days sit against all other days and lists lookalike days for Wes to rate.

Sources (each one is optional; a failure is logged and the rest still run):
  - Open-Meteo Marine archive at a point just outside the reef: primary/secondary/tertiary swell, wind sea
  - Open-Meteo historical forecast wind at the pin
  - Observed wind at Kaneohe MCBH (PHNG) from the Iowa Environmental Mesonet ASOS archive
  - NOAA CO-OPS tide predictions, Mokuoloe (1612480)
  - NDBC buoys 51202 Mokapu, 51201 Waimea, 51207 Kaneohe Bay: height, dominant/average period, direction
  - PacIOOS WW3 Hawaii and SWAN Oahu at the pin (known days and lookalikes only, ERDDAP is slow)

Writes backtest/surf_days.csv, backtest/surf_report.md. Standard library only.
"""
import csv
import gzip
import io
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import go_score as gs

HST = timezone(timedelta(hours=-10))
UTC = timezone.utc
OUT_DIR = "backtest"

SPOTS = {
    "nct": dict(
        name="No Can Tells", pin=(21.3739, -157.6929), wave_pt=(21.39, -157.64), window=(6, 12),
        known={
            date(2026, 9, 8): "epic (post-hurricane)",
            date(2026, 3, 1): "good, clean, not huge",
            date(2025, 12, 27): "good, clean, not huge",
            date(2026, 7, 29): "probably good (no photos)",
            date(2026, 11, 25): "dud: mushy, wrong direction (due N swell)",
        }),
    "manana": dict(
        name="Manana (Rabbit Island)", pin=(21.3280, -157.6605), wave_pt=(21.33, -157.63), window=(6, 17),
        known={
            date(2026, 11, 25): "good, better as tide dropped in the afternoon, near head high, some barrels",
            date(2025, 12, 25): "rideable, not great",
        }),
}
SPOT_KEY = (os.environ.get("SPOT") or "nct").strip().lower()
CFG = SPOTS[SPOT_KEY]
SPOT, WAVE_PT = CFG["pin"], CFG["wave_pt"]
W0, W1 = CFG["window"]             # window, local hours [W0, W1)
KNOWN = CFG["known"]
GOODKNOWN = {d for d, v in KNOWN.items() if not v.startswith("dud")}
START = date.fromisoformat(os.environ.get("START") or "2025-01-01")
END = date.fromisoformat(os.environ.get("END") or (datetime.now(HST).date() - timedelta(days=2)).isoformat())
BUOYS = {"51202": "Mokapu", "51201": "Waimea", "51207": "Kaneohe Bay"}

STATUS = []


def log(*a):
    print(*a, flush=True)


def source(name):
    def wrap(fn):
        def run(*a, **k):
            try:
                r = fn(*a, **k)
                STATUS.append((name, "ok"))
                log("ok  ", name)
                return r
            except Exception as e:  # noqa
                STATUS.append((name, "FAILED: %s" % str(e)[:150]))
                log("FAIL", name, e)
                return {}
        return run
    return wrap


def chunks(a, b, days=92):
    d = a
    while d <= b:
        e = min(b, d + timedelta(days=days - 1))
        yield d, e
        d = e + timedelta(days=1)


def get_json(url, q, timeout=120):
    return json.loads(gs.http_get(url + "?" + urllib.parse.urlencode(q), timeout=timeout, tries=3))


def get_bytes(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": gs.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_key(ts):
    """'2026-03-01T06:00' (local) -> (date, hour)."""
    return date.fromisoformat(ts[:10]), int(ts[11:13])


# ------------------------------------------------------------------ sources
# Every source returns {(date, hour): {field: value}} in local time.

@source("Open-Meteo Marine archive (swell partitions)")
def om_marine():
    out = {}
    v = ["wave_height", "wave_period", "wave_direction",
         "swell_wave_height", "swell_wave_period", "swell_wave_direction",
         "secondary_swell_wave_height", "secondary_swell_wave_period", "secondary_swell_wave_direction",
         "tertiary_swell_wave_height", "tertiary_swell_wave_period", "tertiary_swell_wave_direction",
         "wind_wave_height", "wind_wave_period", "wind_wave_direction"]
    for a, b in chunks(START, END):
        h = get_json("https://marine-api.open-meteo.com/v1/marine",
                     dict(latitude=WAVE_PT[0], longitude=WAVE_PT[1], timezone="Pacific/Honolulu",
                          start_date=a, end_date=b, hourly=",".join(v), length_unit="imperial"))["hourly"]
        for i, ts in enumerate(h["time"]):
            out[local_key(ts)] = {k: h.get(k, [None] * len(h["time"]))[i] for k in v}
    return out


@source("Open-Meteo historical forecast wind at the pin")
def om_wind():
    out = {}
    for a, b in chunks(START, END):
        h = get_json("https://historical-forecast-api.open-meteo.com/v1/forecast",
                     dict(latitude=SPOT[0], longitude=SPOT[1], timezone="Pacific/Honolulu", start_date=a, end_date=b,
                          hourly="wind_speed_10m,wind_direction_10m,wind_gusts_10m", wind_speed_unit="kn"))["hourly"]
        for i, ts in enumerate(h["time"]):
            out[local_key(ts)] = {"om_kt": h["wind_speed_10m"][i], "om_dir": h["wind_direction_10m"][i],
                                  "om_gust": h["wind_gusts_10m"][i]}
    return out


@source("Observed wind, Kaneohe MCBH (PHNG)")
def phng():
    e = END + timedelta(days=1)
    q = [("station", "PHNG"), ("data", "sknt"), ("data", "drct"), ("data", "gust"),
         ("year1", START.year), ("month1", START.month), ("day1", START.day),
         ("year2", e.year), ("month2", e.month), ("day2", e.day),
         ("tz", "Pacific/Honolulu"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"),
         ("missing", "M"), ("report_type", "3")]
    txt = gs.http_get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q),
                      timeout=180, tries=3)
    rows = list(csv.reader(io.StringIO(txt)))
    acc = {}
    for r in rows[1:]:
        rec = dict(zip(rows[0], r))
        try:
            t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M")
        except Exception:  # noqa
            continue
        kt = gs.fnum(rec.get("sknt"))
        if kt is None:
            continue
        a = acc.setdefault((t.date(), t.hour), {"kt": [], "dir": [], "gust": []})
        a["kt"].append(kt)
        a["dir"].append(gs.fnum(rec.get("drct")))
        a["gust"].append(gs.fnum(rec.get("gust")))
    return {k: {"obs_kt": mean(v["kt"]), "obs_dir": circmean(v["dir"], v["kt"]), "obs_gust": maxv(v["gust"])}
            for k, v in acc.items()}


@source("NOAA tide predictions, Mokuoloe 1612480")
def tides():
    out = {}
    for a, b in chunks(START, END, days=360):
        base = dict(product="predictions", station="1612480", datum="MLLW", units="english",
                    time_zone="lst_ldt", format="json", begin_date=a.strftime("%Y%m%d"), end_date=b.strftime("%Y%m%d"))
        hourly = get_json("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", dict(base, interval="h"))
        for p in hourly.get("predictions", []):
            d, h = date.fromisoformat(p["t"][:10]), int(p["t"][11:13])
            out[(d, h)] = {"tide_ft": float(p["v"])}
        hilo = get_json("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", dict(base, interval="hilo"))
        for p in hilo.get("predictions", []):
            d, h = date.fromisoformat(p["t"][:10]), int(p["t"][11:13])
            out.setdefault((d, h), {}).setdefault("hilo", []).append("%s %s %.1f" % (p["type"], p["t"][11:16], float(p["v"])))
    return out


def ndbc_texts(station):
    """All stdmet text NDBC has for the range: yearly history, monthly files for the current year, last 45 days."""
    texts, tried = [], []
    for y in range(START.year, END.year + 1):
        url = "https://www.ndbc.noaa.gov/data/historical/stdmet/%sh%d.txt.gz" % (station, y)
        try:
            texts.append(gzip.decompress(get_bytes(url)).decode("utf-8", "replace"))
            continue
        except Exception:  # noqa
            tried.append(url)
        for m in range(1, 13):
            if date(y, m, 1) > END or date(y, m, 1) < START.replace(day=1):
                continue
            mon = date(y, m, 1).strftime("%b")
            code = "%d" % m if m < 10 else "abc"[m - 10]
            for url in ("https://www.ndbc.noaa.gov/data/stdmet/%s/%s%s%d.txt.gz" % (mon, station, code, y),
                        "https://www.ndbc.noaa.gov/data/stdmet/%s/%s.txt.gz" % (mon, station)):
                try:
                    texts.append(gzip.decompress(get_bytes(url)).decode("utf-8", "replace"))
                    break
                except Exception:  # noqa
                    tried.append(url)
    try:
        texts.append(get_bytes("https://www.ndbc.noaa.gov/data/realtime2/%s.txt" % station).decode("utf-8", "replace"))
    except Exception:  # noqa
        pass
    if not texts:
        raise RuntimeError("no files found (tried %d urls)" % len(tried))
    return texts


def ndbc(station):
    @source("NDBC %s %s" % (station, BUOYS[station]))
    def run():
        acc = {}
        for txt in ndbc_texts(station):
            head = None
            for line in txt.splitlines():
                if line.startswith("#"):
                    if head is None:
                        head = line.lstrip("#").split()
                    continue
                p = line.split()
                if not head or len(p) < len(head):
                    continue
                rec = dict(zip(head, p))
                try:
                    yy = int(rec.get("YY") or rec.get("YYYY"))
                    t = datetime(yy, int(rec["MM"]), int(rec["DD"]), int(rec["hh"]), int(rec.get("mm", 0)), tzinfo=UTC).astimezone(HST)
                except Exception:  # noqa
                    continue
                if not (START <= t.date() <= END):
                    continue
                f = lambda k, bad: (lambda v: None if v is None or v >= bad else v)(gs.fnum(rec.get(k)))
                a = acc.setdefault((t.date(), t.hour), {"h": [], "dpd": [], "apd": [], "mwd": []})
                a["h"].append(f("WVHT", 90))
                a["dpd"].append(f("DPD", 90))
                a["apd"].append(f("APD", 90))
                a["mwd"].append(f("MWD", 900))
        s = "b%s_" % station
        return {k: {s + "ft": (lambda m: round(m * 3.281, 1) if m is not None else None)(mean(v["h"])),
                    s + "dpd": mean(v["dpd"]), s + "apd": mean(v["apd"]), s + "dir": circmean(v["mwd"])}
                for k, v in acc.items()}
    return run()


def pacioos_days(days):
    """WW3 + SWAN at the pin for a short list of days. Returns {date: {...window averages...}}."""
    out = {}
    for name, fn in (("ww3", gs.fetch_ww3), ("swan", gs.fetch_swan)):
        ok = 0
        for d in days:
            t0 = datetime(d.year, d.month, d.day, W0, tzinfo=HST).astimezone(UTC)
            t1 = datetime(d.year, d.month, d.day, W1, tzinfo=HST).astimezone(UTC)
            try:
                r = fn(SPOT[0], SPOT[1], t0, t1)
            except Exception as e:  # noqa
                log("     PacIOOS %s %s: %s" % (name, d, str(e)[:100]))
                continue
            if not r:
                continue
            ok += 1
            vals = list(r.values())
            rec = out.setdefault(d, {})
            rec[name + "_ft"] = round(mean([v["hs"] for v in vals]), 1)
            rec[name + "_tp"] = mean([v["tp"] for v in vals])
            rec[name + "_dir"] = circmean([v["dir"] for v in vals])
            if name == "ww3":
                rec["ww3_swell_ft"] = mean([v.get("sw_hs") for v in vals])
                rec["ww3_chop_ft"] = mean([v.get("ws_hs") for v in vals])
        STATUS.append(("PacIOOS %s at the pin (%d of %d days)" % (name.upper(), ok, len(days)), "ok" if ok else "FAILED: no data for these dates"))
    return out


# ------------------------------------------------------------------ helpers
def mean(x):
    x = [v for v in x if v is not None]
    return round(sum(x) / len(x), 2) if x else None


def maxv(x):
    x = [v for v in x if v is not None]
    return max(x) if x else None


def circmean(degs, w=None):
    w = w or [1] * len(degs)
    pairs = [(d, wi or 0.1) for d, wi in zip(degs, w) if d is not None]
    if not pairs:
        return None
    s = sum(wi * math.sin(math.radians(d)) for d, wi in pairs)
    c = sum(wi * math.cos(math.radians(d)) for d, wi in pairs)
    return round(math.degrees(math.atan2(s, c)) % 360)


def cmp(d):
    return gs.compass(d) if d is not None else "--"


def north_ish(d):
    return d is not None and (d >= 300 or d <= 45)


def east_ish(d):
    return d is not None and 45 < d <= 135


# ------------------------------------------------------------------ assemble
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    log(CFG["name"] + " backtest %s to %s, window %d-%d HST" % (START, END, W0, W1))
    hourly = {}
    for part in [om_marine(), om_wind(), phng(), tides()] + [ndbc(s) for s in BUOYS]:
        for k, v in part.items():
            hourly.setdefault(k, {}).update(v)

    days = {}
    d = START
    while d <= END:
        hrs = [hourly.get((d, h), {}) for h in range(W0, W1)]
        if not any(hrs):
            d += timedelta(days=1)
            continue
        col = lambda k: [h.get(k) for h in hrs]
        r = {"date": d.isoformat(), "rating": KNOWN.get(d, "")}
        parts = []
        for pfx, lab in (("swell_wave", "sw1"), ("secondary_swell_wave", "sw2"), ("tertiary_swell_wave", "sw3")):
            ht, tp, dr = mean(col(pfx + "_height")), mean(col(pfx + "_period")), circmean(col(pfx + "_direction"))
            r.update({lab + "_ft": ht, lab + "_s": tp, lab + "_dir": dr})
            if ht and tp:
                parts.append((ht, tp, dr))
        r["windsea_ft"] = mean(col("wind_wave_height"))
        r["windsea_s"] = mean(col("wind_wave_period"))
        # north-wrap and east energy: sum of H^2*T over partitions from each quadrant
        r["north_energy"] = round(sum(h * h * t for h, t, dr in parts if north_ish(dr)), 1)
        r["east_energy"] = round(sum(h * h * t for h, t, dr in parts if east_ish(dr)), 1)
        nparts = [p for p in parts if north_ish(p[2])]
        best_n = max(nparts, key=lambda p: p[0] * p[0] * p[1]) if nparts else None
        r["north_ft"], r["north_s"], r["north_dir"] = best_n if best_n else (None, None, None)
        r["om_kt"], r["om_dir"], r["om_gust"] = mean(col("om_kt")), circmean(col("om_dir"), col("om_kt")), maxv(col("om_gust"))
        r["obs_kt"], r["obs_dir"], r["obs_gust"] = mean(col("obs_kt")), circmean(col("obs_dir"), col("obs_kt")), maxv(col("obs_gust"))
        tl = [v for v in col("tide_ft") if v is not None]
        r["tide_min"], r["tide_max"] = (min(tl), max(tl)) if tl else (None, None)
        r["tide_trend"] = ("rising" if tl[-1] > tl[0] + 0.2 else "falling" if tl[-1] < tl[0] - 0.2 else "slack/turning") if tl else None
        hilo = []
        for h in range(0, 24):
            hilo += hourly.get((d, h), {}).get("hilo", [])
        r["tides"] = "; ".join(hilo)
        # hourly wind for known days (afternoon matters at Manana)
        r["wind_by_hour"] = " ".join("%d:%s" % (h, "%.0f%s" % (hourly[(d, h)]["obs_kt"], gs.compass(hourly[(d, h)]["obs_dir"]) if hourly[(d, h)].get("obs_dir") is not None else "")
                                         if hourly.get((d, h), {}).get("obs_kt") is not None else "-") for h in range(W0, W1))
        for s in BUOYS:
            for f in ("ft", "dpd", "apd", "dir"):
                r["b%s_%s" % (s, f)] = mean(col("b%s_%s" % (s, f))) if f != "dir" else circmean(col("b%s_dir" % s))
        days[d] = r
        d += timedelta(days=1)

    if not days:
        log("no data at all")
        write_report([], {}, [])
        sys.exit(1)

    known = [r for k, r in days.items() if k in GOODKNOWN]
    # lookalikes: wind no stronger than the windiest known day (+1 kt) and real swell energy from N or E
    wind_key = "obs_kt" if all(r["obs_kt"] is not None for r in known) and known else "om_kt"
    wmax = (max(r[wind_key] for r in known if r[wind_key] is not None) + 1) if known else 10
    emin = min((r["north_energy"] + r["east_energy"]) for r in known) * 0.7 if known else 0
    look = [r for k, r in sorted(days.items()) if k not in KNOWN and r[wind_key] is not None and r[wind_key] <= wmax
            and (r["north_energy"] + r["east_energy"]) >= emin]
    log("lookalikes: %d (wind %s <= %.1f kt, N+E energy >= %.0f)" % (len(look), wind_key, wmax, emin))

    pac_days = sorted(KNOWN) + [date.fromisoformat(r["date"]) for r in look[:40]]
    pac = pacioos_days(pac_days)
    for d, v in pac.items():
        if d in days:
            days[d].update(v)

    rows = [days[k] for k in sorted(days)]
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(os.path.join(OUT_DIR, "surf_days_%s.csv" % SPOT_KEY), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    write_report(rows, days, look, wind_key, wmax, emin)


def pctile(rows, key, v):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if v is None or not vals:
        return "--"
    return "%d" % round(100 * sum(x <= v for x in vals) / len(vals))


def fmt_sw(r, lab):
    if not r.get(lab + "_ft"):
        return "--"
    return "%.1f ft %.0f s %s" % (r[lab + "_ft"], r[lab + "_s"] or 0, cmp(r[lab + "_dir"]))


def fmt_wind(r, k):
    if r.get(k + "_kt") is None:
        return "--"
    g = r.get(k + "_gust")
    return "%.0f kt %s%s" % (r[k + "_kt"], cmp(r[k + "_dir"]), (" g%.0f" % g) if g else "")


def fmt_buoy(r, s):
    if r.get("b%s_ft" % s) is None:
        return "--"
    return "%.1f ft %.0f/%.0f s %s" % (r["b%s_ft" % s], r["b%s_dpd" % s] or 0, r["b%s_apd" % s] or 0, cmp(r["b%s_dir" % s]))


def write_report(rows, days, look, wind_key="obs_kt", wmax=0, emin=0):
    o = ["# %s surf backtest" % CFG["name"], "",
         "%s to %s, %d days, window %d:00 to %d:00 HST. Generated %s." % (START, END, len(rows), W0, W1,
                                                                       datetime.now(HST).strftime("%Y-%m-%d %H:%M HST")), "",
         "## Sources", ""]
    o += ["- %s: %s" % s for s in STATUS]
    o += ["", "## Known days", "",
          "Swell partitions are Open-Meteo Marine just outside the reef. Wind: observed at Kaneohe MCBH, then model wind at the pin.", "",
          "| Date | Wes | Swell 1 | Swell 2 | Swell 3 | Wind sea | Wind obs | Wind model | Tide in window |",
          "|---|---|---|---|---|---|---|---|---|"]
    known = [r for r in rows if r["rating"]]
    for r in known:
        o.append("| %s | %s | %s | %s | %s | %s ft | %s | %s | %s-%s ft %s |" % (
            r["date"], r["rating"], fmt_sw(r, "sw1"), fmt_sw(r, "sw2"), fmt_sw(r, "sw3"), r["windsea_ft"],
            fmt_wind(r, "obs"), fmt_wind(r, "om"), r["tide_min"], r["tide_max"], r["tide_trend"] or ""))
    o += ["", "Buoys (height, dominant/average period, dominant direction) and PacIOOS models at the pin:", "",
          "| Date | 51202 Mokapu | 51201 Waimea | 51207 Kaneohe | WW3 at pin | SWAN at pin | Tides |", "|---|---|---|---|---|---|---|"]
    for r in known:
        ww3 = ("%.1f ft %.0f s %s, chop %s" % (r["ww3_ft"], r["ww3_tp"] or 0, cmp(r["ww3_dir"]), r.get("ww3_chop_ft"))) if r.get("ww3_ft") else "--"
        swan = ("%.1f ft %.0f s %s" % (r["swan_ft"], r["swan_tp"] or 0, cmp(r["swan_dir"]))) if r.get("swan_ft") else "--"
        o.append("| %s | %s | %s | %s | %s | %s | %s |" % (r["date"], fmt_buoy(r, "51202"), fmt_buoy(r, "51201"),
                                                            fmt_buoy(r, "51207"), ww3, swan, r["tides"]))
    o += ["", "Hourly observed wind (PHNG) on known days:", ""]
    for r in known:
        o.append("- %s: %s" % (r["date"], r.get("wind_by_hour", "")))
    o += ["", "## How unusual each known day was (percentile vs all days; 90 = bigger/longer/windier than 90% of days)", "",
          "| Date | North energy | East energy | Swell 1 period | Wind obs | 51202 height | 51201 height |", "|---|---|---|---|---|---|---|"]
    for r in known:
        o.append("| %s | %s | %s | %s | %s | %s | %s |" % (r["date"], pctile(rows, "north_energy", r["north_energy"]),
                                                            pctile(rows, "east_energy", r["east_energy"]), pctile(rows, "sw1_s", r["sw1_s"]),
                                                            pctile(rows, "obs_kt", r["obs_kt"]), pctile(rows, "b51202_ft", r["b51202_ft"]),
                                                            pctile(rows, "b51201_ft", r["b51201_ft"])))
    o += ["", "## Lookalike days: %d" % len(look), "",
          "Wind (%s) at or under %.1f kt and N+E swell energy at least 70%% of the weakest known day. "
          "Wes: mark any you remember as good, flat or junk." % (wind_key, wmax), "",
          "| Date | Swell 1 | Swell 2 | Wind obs | 51201 Waimea | Tide in window |", "|---|---|---|---|---|---|"]
    for r in look:
        o.append("| %s | %s | %s | %s | %s | %s-%s %s |" % (r["date"], fmt_sw(r, "sw1"), fmt_sw(r, "sw2"), fmt_wind(r, "obs"),
                                                             fmt_buoy(r, "51201"), r["tide_min"], r["tide_max"], r["tide_trend"] or ""))
    with open(os.path.join(OUT_DIR, "surf_report_%s.md" % SPOT_KEY), "w") as f:
        f.write("\n".join(o) + "\n")
    log("\n".join(o))


if __name__ == "__main__":
    main()
