#!/usr/bin/env python3
"""calib_east.py - what the east-swell and crossed-seas terms would have added on Wes's calibration days.
Uses archived Open-Meteo marine partitions (MeteoFrance + GFS-Wave) at the offshore FADs, 6 AM-2 PM HST.
Run by the 'Calibration check' workflow; writes diag/calib_east.txt."""
import json, os, urllib.request, urllib.parse
import go_score as g

SPOTS = [s for s in g.SPOTS if not s[3]]
HV = ("wave_height,wave_peak_period,wave_period,wind_wave_height,wind_wave_period,wind_wave_direction,swell_wave_height,swell_wave_period,"
      "swell_wave_direction,secondary_swell_wave_height,secondary_swell_wave_period,secondary_swell_wave_direction")


def fetch(day, model):
    q = {"latitude": ",".join("%.3f" % s[1] for s in SPOTS), "longitude": ",".join("%.3f" % s[2] for s in SPOTS),
         "hourly": HV, "start_date": day, "end_date": day, "timezone": "Pacific/Honolulu"}
    if model:
        q["models"] = model
    req = urllib.request.Request("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "windward-go-score/calib"})
    d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return [x["hourly"] for x in (d if isinstance(d, list) else [d])]


def parts(h, i):
    out = []
    for a, b, c in (("wind_wave_height", "wind_wave_period", "wind_wave_direction"),
                    ("swell_wave_height", "swell_wave_period", "swell_wave_direction"),
                    ("secondary_swell_wave_height", "secondary_swell_wave_period", "secondary_swell_wave_direction")):
        v = (h.get(a) or [None] * 99)[i]
        if v is not None:
            out.append((v * g.M_TO_FT, (h.get(b) or [None] * 99)[i], (h.get(c) or [None] * 99)[i]))
    return out


L = ["Calibration check: east swell (ft @ s), extra points the new east term adds over the old total-sea term, crossed seas.",
     "Averages over the offshore FADs, 6 AM-2 PM HST. Old score inputs not re-run here (wind not included).", ""]
for day, rating in g.CALIBRATION:
    try:
        data = {m: fetch(day, m) for m in ("", "ncep_gfswave016")}
    except Exception as e:  # noqa
        L.append("%s  %-40s  failed: %s" % (day, rating[:40], str(e)[:80]))
        continue
    es, adds, crs = [], [], []
    for si in range(len(SPOTS)):
        for i in range(6, 14):
            easts, others, tot = [], [], None
            for m, hh in data.items():
                h = hh[si]
                e, o = g.east_split(parts(h, i))
                others.append(o)
                if e:
                    easts.append(e)
                tp = (h.get("wave_peak_period") or [None] * 99)[i] or (h.get("wave_period") or [None] * 99)[i]
                if tot is None and h["wave_height"][i] is not None and tp:
                    tot = (h["wave_height"][i] * g.M_TO_FT, tp)
            if not easts or not tot:
                continue
            e = (sum(x[0] for x in easts) / len(easts), sum(x[1] for x in easts) / len(easts))
            old = max(g.interp(tot[1], g.PERIOD_PEN), g.interp(g.steepness(*tot), g.STEEP_PEN))
            new = max(old, g.component_pen(*e))
            cx = sum(others) / len(others)
            es.append(e)
            adds.append(new - old)
            crs.append(g.interp(cx, g.CROSS_PEN) if e[0] >= g.CROSS_MIN_EAST_FT else 0.0)
    if not es:
        L.append("%s  %-40s  no partition data" % (day, rating[:40]))
        continue
    n = len(es)
    L.append("%s  %-44s east %.1f ft @ %.1f s   east term +%.2f   crossed +%.2f   total extra -%.2f" % (
        day, rating[:44], sum(x[0] for x in es) / n, sum(x[1] for x in es) / n, sum(adds) / n, sum(crs) / n,
        (sum(adds) + sum(crs)) / n))
txt = "\n".join(L)
print(txt)
os.makedirs("diag", exist_ok=True)
open("diag/calib_east.txt", "w").write(txt + "\n")
