#!/usr/bin/env python3
"""
penguin_backtest.py - run the Penguin Bank window test over the past year.

For every day it uses what the models said at the time (Open-Meteo historical forecast archive: ECMWF, GFS,
ICON wind at the channel points; Open-Meteo Marine history for waves and wind chop), Small Craft Advisories
the NWS actually issued for the Kaiwi Channel (Iowa Environmental Mesonet VTEC archive), and observed wind
at Molokai airport as a reality check.

Writes backtest/penguin_backtest.csv and backtest/penguin_backtest.html. Meant to run on GitHub Actions
(the "Penguin Bank backtest" workflow). Needs only the Python standard library.
"""
import csv
import io
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import go_score as gs
import penguin as pb

HST, UTC = pb.HST, pb.UTC
OUT_DIR = "backtest"
KNOWN = {date(2026, 9, 9): "Wes: dead calm everywhere, would have been a great bank day",
         date(2026, 9, 8): "Wes: sloppy but manageable nearshore (day before the epic day)"}
VARIANTS = [("as set (strict)", {}),
            ("a bit looser: max 12 kt, avg 9 kt, chop 2 ft, period 8 s",
             {"MAX_WIND_KT": 12.0, "MEAN_WIND_KT": 9.0, "MAX_CHOP_FT": 2.0, "MIN_PERIOD_S": 8.0}),
            ("stricter: max 8 kt, avg 6 kt, chop 1 ft",
             {"MAX_WIND_KT": 8.0, "MEAN_WIND_KT": 6.0, "MAX_CHOP_FT": 1.0})]


def log(*a):
    print(*a, flush=True)


def chunks(start, end, days=92):
    d = start
    while d <= end:
        e = min(end, d + timedelta(days=days - 1))
        yield d, e
        d = e + timedelta(days=1)


def get_json(url, q, timeout=120):
    return json.loads(gs.http_get(url + "?" + urllib.parse.urlencode(q), timeout=timeout, tries=3))


def merge(dst, src):
    for k, v in src.items():
        dst.setdefault(k, {}).update(v)


def hist_wind(start, end):
    lats, lons = pb._om_points()
    out = {}
    for a, b in chunks(start, end):
        base = {"latitude": lats, "longitude": lons, "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "wind_speed_unit": "kn", "timezone": "UTC", "start_date": a.isoformat(), "end_date": b.isoformat()}
        got = None
        try:
            got = get_json("https://historical-forecast-api.open-meteo.com/v1/forecast", dict(base, models=",".join(pb.WIND_MODELS)))
            log("ok   wind %s..%s: historical forecasts, %s" % (a, b, ", ".join(pb.WIND_MODELS)))
        except Exception as e:  # noqa
            log("FAIL wind %s..%s multi-model: %s" % (a, b, str(e)[:160]))
            parts = []
            for m in pb.WIND_MODELS:
                try:
                    part = get_json("https://historical-forecast-api.open-meteo.com/v1/forecast", dict(base, models=m))
                    part = part if isinstance(part, list) else [part]
                    for loc in part:   # single-model answers have no model suffix; add it so models stay apart
                        h = loc.get("hourly", {})
                        for k in [k for k in h if k.startswith("wind_")]:
                            h[k + "_" + m] = h.pop(k)
                    parts.append(part)
                    log("ok   wind %s..%s: %s" % (a, b, m))
                except Exception as e2:  # noqa
                    log("FAIL wind %s..%s %s: %s" % (a, b, m, str(e2)[:160]))
            if parts:
                got = parts
        if got is None:
            try:
                got = get_json("https://archive-api.open-meteo.com/v1/archive", base)
                log("ok   wind %s..%s: ERA5 reanalysis fallback (no model spread)" % (a, b))
            except Exception as e:  # noqa
                log("FAIL wind %s..%s ERA5: %s" % (a, b, str(e)[:160]))
                continue
        if isinstance(got, list) and got and isinstance(got[0], list):   # per-model fallback: list of lists
            for part in got:
                merge_points(out, pb.parse_om_wind(gs, part))
        else:
            merge_points(out, pb.parse_om_wind(gs, got if isinstance(got, list) else [got]))
    return out


def merge_points(out, parsed):
    for name, series in parsed.items():
        dst = out.setdefault(name, {})
        for t, v in series.items():
            cur = dst.setdefault(t, {"models": {}, "dirs": [], "gust": None})
            cur["models"].update(v["models"])
            cur["dirs"] += v["dirs"]
            if v.get("gust") is not None:
                cur["gust"] = max(cur["gust"] or 0, v["gust"])


def hist_marine(start, end):
    lats, lons = pb._om_points()
    out = {}
    for a, b in chunks(start, end):
        q = {"latitude": lats, "longitude": lons, "hourly": pb.MARINE_VARS, "timezone": "UTC",
             "start_date": a.isoformat(), "end_date": b.isoformat()}
        try:
            locs = get_json("https://marine-api.open-meteo.com/v1/marine", q)
            log("ok   waves %s..%s" % (a, b))
        except Exception as e:  # noqa
            log("FAIL waves %s..%s: %s" % (a, b, str(e)[:160]))
            try:
                q["hourly"] = "wave_height,wave_period,wave_direction,wind_wave_height,wind_wave_period"
                locs = get_json("https://marine-api.open-meteo.com/v1/marine", q)
                log("ok   waves %s..%s (fewer variables)" % (a, b))
            except Exception as e2:  # noqa
                log("FAIL waves %s..%s again: %s" % (a, b, str(e2)[:160]))
                continue
        merge(out, pb.parse_om_marine(gs, locs if isinstance(locs, list) else [locs]))
    return out


def _parse_t(s):
    if not s:
        return None
    s = str(s).replace("Z", "").replace("T", " ")[:16]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def sca_history(start, end):
    """Days (HST) when a Small Craft Advisory / Gale / Hazardous Seas product was in effect for PHZ116 between 5 AM and 3 PM."""
    url = "https://mesonet.agron.iastate.edu/json/vtec_events_byugc.php"
    d = get_json(url, {"ugc": "PHZ116", "sdate": (start - timedelta(days=3)).isoformat(), "edate": (end + timedelta(days=1)).isoformat()})
    events = d.get("events", d if isinstance(d, list) else [])
    days, kinds = set(), {}
    n = 0
    for ev in events:
        ph, sig = ev.get("phenomena"), ev.get("significance")
        if ph not in ("SC", "GL", "SE", "SR", "HF") or sig not in ("Y", "W"):
            continue
        t0 = _parse_t(ev.get("utc_issue") or ev.get("issue"))
        t1 = _parse_t(ev.get("utc_expire") or ev.get("expire"))
        if not t0 or not t1:
            continue
        n += 1
        dd = t0.astimezone(HST).date()
        while dd <= t1.astimezone(HST).date():
            w0 = datetime(dd.year, dd.month, dd.day, pb.WINDOW[0], tzinfo=HST)
            w1 = datetime(dd.year, dd.month, dd.day, pb.WINDOW[1], tzinfo=HST)
            if t0 < w1 and t1 > w0:
                days.add(dd)
                kinds[dd] = ph
            dd += timedelta(days=1)
    log("ok   NWS advisories for the Kaiwi Channel: %d events, %d days with one in effect during the window" % (n, len(days)))
    return days


def molokai_obs(start, end):
    """Observed Molokai airport wind -> {date: (mean_kt, max_kt)} over the window hours."""
    q = [("station", "PHMK"), ("data", "sknt"), ("year1", start.year), ("month1", start.month), ("day1", start.day),
         ("year2", (end + timedelta(days=1)).year), ("month2", (end + timedelta(days=1)).month), ("day2", (end + timedelta(days=1)).day),
         ("tz", "Pacific/Honolulu"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"), ("missing", "M"), ("report_type", "3")]
    rows = list(csv.reader(io.StringIO(gs.http_get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q), timeout=180, tries=3))))
    by = {}
    for r in rows[1:]:
        rec = dict(zip(rows[0], r))
        kt = gs.fnum(rec.get("sknt"))
        if kt is None:
            continue
        try:
            t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M")
        except Exception:  # noqa
            continue
        if pb.WINDOW[0] <= t.hour < pb.WINDOW[1]:
            by.setdefault(t.date(), []).append(kt)
    log("ok   Molokai airport observations: %d days" % len(by))
    return {d: (sum(v) / len(v), max(v)) for d, v in by.items() if len(v) >= 4}


def evaluate_all(records, days, sca, settings):
    saved = {k: getattr(pb, k) for k in settings}
    for k, v in settings.items():
        setattr(pb, k, v)
    try:
        chan = pb.point_names("channel")
        res = {}
        for d in days:
            m = pb.day_metrics(records, d, chan)
            prev = pb.day_metrics(records, d - timedelta(days=1), chan)
            ok, fails = pb.evaluate(m, prev["wind_mean"], None, d in sca)
            res[d] = (ok, fails, m, prev["wind_mean"])
        return res
    finally:
        for k, v in saved.items():
            setattr(pb, k, v)


def f1(x, fmt="%.1f"):
    return "" if x is None else fmt % x


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    end = datetime.now(HST).date() - timedelta(days=1)
    start = end - timedelta(days=364)
    log("Penguin Bank backtest %s to %s, window %d:00-%d:00 HST\n" % (start, end, pb.WINDOW[0], pb.WINDOW[1]))
    problems = []
    try:
        wind = hist_wind(start - timedelta(days=1), end)
    except Exception as e:  # noqa
        wind = {}
        problems.append("wind history: %s" % e)
    try:
        waves = hist_marine(start - timedelta(days=1), end)
    except Exception as e:  # noqa
        waves = {}
        problems.append("wave history: %s" % e)
    try:
        sca = sca_history(start, end)
    except Exception as e:  # noqa
        sca = set()
        problems.append("advisory history: %s (the test ran without it)" % str(e)[:160])
        log("FAIL advisories: %s" % str(e)[:160])
    try:
        obs = molokai_obs(start, end)
    except Exception as e:  # noqa
        obs = {}
        problems.append("Molokai observations: %s" % str(e)[:160])
        log("FAIL Molokai obs: %s" % str(e)[:160])
    if not wind:
        log("No wind history - nothing to test.")
        sys.exit(1)

    records = pb.build_records(gs, wind, waves)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    runs = [(label, evaluate_all(records, days, sca, st)) for label, st in VARIANTS]
    main_res = runs[0][1]

    # ---- CSV
    with open(os.path.join(OUT_DIR, "penguin_backtest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "window", "fails", "channel_wind_avg_kt", "channel_wind_max_kt", "models_differ_kt",
                    "wind_chop_max_ft", "waves_max_ft", "period_min_s", "day_before_avg_kt", "small_craft_advisory",
                    "molokai_obs_avg_kt", "molokai_obs_max_kt"] + ["window_" + str(i + 1) for i in range(1, len(VARIANTS))])
        for d in days:
            ok, fails, m, prev = main_res[d]
            o = obs.get(d, (None, None))
            w.writerow([d.isoformat(), d.strftime("%a"), "yes" if ok else "no", "; ".join(fails), f1(m["wind_mean"]), f1(m["wind_max"]),
                        f1(m["spread"]), f1(m["chop_max"]), f1(m["hs_max"]), f1(m["tp_min"], "%.0f"), f1(prev),
                        "yes" if d in sca else "", f1(o[0]), f1(o[1], "%.0f")] +
                       ["yes" if r[d][0] else "no" for _, r in runs[1:]])

    # ---- summary
    log("\nResults")
    for label, r in runs:
        n = sum(1 for v in r.values() if v[0])
        log("  %-60s %3d days" % (label, n))
    for d, note in sorted(KNOWN.items()):
        if d in main_res:
            ok, fails, m, prev = main_res[d]
            log("  %s %s -> %s %s" % (d, note, "WINDOW" if ok else "no", "; ".join(fails)))
    passes = [d for d in days if main_res[d][0]]
    log("\nWindow days (strict): " + (", ".join(d.strftime("%a %b %-d %Y") for d in passes) or "none"))
    near = [d for d in days if not main_res[d][0] and len(main_res[d][1]) == 1 and "not enough" not in main_res[d][1][0]]
    log("Near misses (one thing wrong): %d" % len(near))

    # ---- HTML
    esc = gs.esc
    css = gs.PAGE.split("<style>", 1)[1].split("</style>", 1)[0].replace("%%LIGHT%%", "").replace("%%DARK%%", "")

    def row(d):
        ok, fails, m, prev = main_res[d]
        o = obs.get(d, (None, None))
        note = KNOWN.get(d, "")
        return "<tr><td>%s</td><td>%s / %s</td><td>%s</td><td>%s</td><td>%s @ %s s+</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            d.strftime("%a %b %-d, %Y"), f1(m["wind_mean"]), f1(m["wind_max"], "%.0f"), f1(m["spread"], "%.0f"), f1(m["chop_max"]),
            f1(m["hs_max"]), f1(m["tp_min"], "%.0f"), ("%s / %s" % (f1(o[0]), f1(o[1], "%.0f"))) if o[0] is not None else "–",
            esc("; ".join(fails) or "all clear"), esc(note))
    hdr = ("<tr><th>Day</th><th>Channel wind avg / max (kt)</th><th>Models differ (kt)</th><th>Wind chop (ft)</th><th>Waves</th>"
           "<th>Molokai airport, observed avg / max (kt)</th><th>Test</th><th>Note</th></tr>")
    months = {}
    for d in days:
        k = d.strftime("%Y-%m")
        months.setdefault(k, [0, 0])
        months[k][1] += 1
        if main_res[d][0]:
            months[k][0] += 1
    month_rows = "".join("<tr><td>%s</td><td>%d</td><td>%d</td></tr>" % (datetime.strptime(k, "%Y-%m").strftime("%b %Y"), v[0],
                         sum(1 for d in days if d.strftime("%Y-%m") == k and d in sca)) for k, v in sorted(months.items()))
    known_rows = "".join(row(d) for d in sorted(KNOWN) if d in main_res)
    var_rows = "".join("<tr><td>%s</td><td>%d</td><td>%s</td></tr>" % (
        esc(label), sum(1 for v in r.values() if v[0]), esc(", ".join(d.strftime("%b %-d") for d in days if r[d][0])[:600] or "none"))
        for label, r in runs)
    prob = "".join("<li>%s</li>" % esc(p) for p in problems)
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Penguin Bank Backtest</title><style>%s</style></head><body><main>
<h1>Penguin Bank test: the past year</h1><div class="sub">%s to %s · %s window · run %s · <a href="./">back to the Go Score</a> · <a href="penguin.html">Penguin Bank forecast</a></div>
<p class="fine">Each day is judged on what the wind models (ECMWF, GFS, ICON) and the wave model forecast at the time for the Kaiwi Channel and Penguin Bank points, plus Small Craft Advisories the NWS actually issued for the Kaiwi Channel. Molokai airport wind is what was actually observed, as a reality check. The "day before" rule is included; the two-runs-in-a-row rule is not, since old forecast runs aren't archived run by run.</p>
%s
<h2>Your known days</h2><div class="wrap"><table><thead>%s</thead><tbody>%s</tbody></table></div>
<h2>How often it fires</h2><div class="wrap"><table><thead><tr><th>Test</th><th>Days</th><th>Which</th></tr></thead><tbody>%s</tbody></table></div>
<h2>Window days (strict test) – ask your friends about these</h2><div class="wrap"><table><thead>%s</thead><tbody>%s</tbody></table></div>
<h2>Near misses – failed on one thing</h2><div class="wrap"><table><thead>%s</thead><tbody>%s</tbody></table></div>
<h2>By month</h2><div class="wrap"><table><thead><tr><th>Month</th><th>Window days</th><th>Days with a Kaiwi Channel advisory</th></tr></thead><tbody>%s</tbody></table></div>
<p class="fine">Full day-by-day table: penguin_backtest.csv in the go-score repo (backtest folder).</p>
</main></body></html>""" % (
        css, start.strftime("%b %-d, %Y"), end.strftime("%b %-d, %Y"), esc(pb._win_txt()), datetime.now(HST).strftime("%b %-d, %Y %-I:%M %p HST"),
        ('<div class="banner">Some history was missing: <ul>%s</ul></div>' % prob) if prob else "",
        hdr, known_rows or '<tr><td colspan="8">outside the range</td></tr>', var_rows,
        hdr, "".join(row(d) for d in passes) or '<tr><td colspan="8">none</td></tr>',
        hdr, "".join(row(d) for d in near) or '<tr><td colspan="8">none</td></tr>', month_rows)
    with open(os.path.join(OUT_DIR, "penguin_backtest.html"), "w", encoding="utf-8") as f:
        f.write(page)
    log("\nWrote %s/penguin_backtest.html and .csv" % OUT_DIR)


if __name__ == "__main__":
    main()
