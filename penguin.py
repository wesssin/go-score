"""
penguin.py - Penguin Bank add-on for go_score.py

Two jobs:
  1. check(gs)        runs on every normal Go Score run. Looks at the Kaiwi Channel and Penguin Bank and
                      flags days that pass a strict "excellent for the bank" test. No push - just a badge
                      on the day card of the main page and a small table.
  2. detail(gs, ...)  runs only when asked (python go_score.py --penguin, or the "penguin-bank" option of
                      the GitHub workflow). Writes penguin.html: hour-by-hour channel conditions for both
                      starts (Hawaii Kai, Heeia Kea), with the run out, the fishing hours and the ride home
                      rated separately.

The test is deliberately strict because the trip is long (100+ miles round trip from Heeia) and there is
no real-time buoy on the bank itself. Thresholds are a starting point, checked against a year of history
by penguin_backtest.py, not against anyone's logbook yet.

`gs` is the go_score module (passed in so this file has no circular import).
"""
import json
import math
import os
import shutil
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

HST = timezone(timedelta(hours=-10))
UTC = timezone.utc

# =============================================================== SETTINGS
# Points are approximate - replace with your own waypoints. (name, lat, lon, role)
# role "channel" points are what the flag looks at; "approach" points only matter for the Heeia route.
POINTS = [("Kailua offshore", 21.420, -157.680, "approach"),
          ("Makapuu corner", 21.315, -157.625, "channel"),
          ("Hawaii Kai exit", 21.255, -157.705, "channel"),
          ("Kaiwi mid-channel", 21.150, -157.520, "channel"),
          ("Bank N ledge", 21.020, -157.500, "channel"),
          ("P FAD", 20.773, -157.812, "channel")]          # HIMB Penguin Bank FAD, 20-46.4N 157-48.7W
ROUTES = [("From Hawaii Kai", ["Hawaii Kai exit", "Kaiwi mid-channel", "Bank N ledge", "P FAD"]),
          ("From Heeia Kea", ["Kailua offshore", "Makapuu corner", "Kaiwi mid-channel", "Bank N ledge", "P FAD"])]
WINDOW = (5, 15)            # HST: leave 5 AM, back by 3 PM
LEGS = [("Run out", 5, 9), ("Fishing", 9, 12), ("Ride home", 12, 15)]

# Strict "Penguin Bank window" test. Every line must pass for the window.
MAX_WIND_KT = 11.0          # no channel point, no hour, above this (model average). Sep 9 2026 peaked at 10.1 kt mid-channel at 2 PM
MEAN_WIND_KT = 8.0          # channel average over the window
MAX_SPREAD_KT = 4.0         # models must agree: window-average wind per model within this range
MAX_CHOP_FT = 1.5           # wind-sea height (the short, steep part)
MAX_WAVE_FT = 6.0           # total significant wave height...
MAX_WAVE_LONG_FT = 8.0      # ...or up to this when the period stays at LONG_PERIOD_S or longer (Sep 9 2026: 7.7 ft @ 10 s+)
LONG_PERIOD_S = 10.0
MIN_PERIOD_S = 9.0          # dominant period, lowest hour in the window
PREV_DAY_MAX_MEAN_KT = None  # off: Sep 9 2026 was glassy the day after a 16 kt day. Shown as a note instead.
PREV_DAY_NOTE_KT = 11.0
NWS_MAX_KT = 10.0           # top of the NWS Kaiwi Channel wind range for that day, when the text covers it

WIND_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
NWS_ZONES = [("PHZ116", "Kaiwi Channel"), ("PHZ118", "Maui County Leeward Waters")]
BUOYS = [("51202", "Mokapu, Oahu windward"), ("51211", "Pearl Harbor entrance, south shore"),
         ("51212", "Kalaeloa (Barbers Pt)"), ("51205", "Pauwela, Maui - upwind trade swell")]
WIND_STATIONS = [("ndbc", "OOUH1", "Honolulu Harbor"), ("ndbc", "MOKH1", "Mokuoloe (Kaneohe Bay)"),
                 ("ndbc", "KLIH1", "Kahului Harbor, Maui"), ("asos", "PHMK", "Molokai airport (Hoolehua)")]

STATE_FILE = os.path.join("cache", "penguin_state.json")
SAVED_DIR = "penguin"       # committed copy of penguin.html so every run republishes it
PAGE_NAME = "penguin.html"
BACKTEST_HTML = os.path.join("backtest", "penguin_backtest.html")


def point_names(role=None):
    return [p[0] for p in POINTS if role is None or p[3] == role]


# =============================================================== fetchers
def _om_points():
    return ",".join("%.4f" % p[1] for p in POINTS), ",".join("%.4f" % p[2] for p in POINTS)


def _om_get(gs, url, q):
    d = json.loads(gs.http_get(url + "?" + urllib.parse.urlencode(q), timeout=40))
    return d if isinstance(d, list) else [d]


def parse_om_wind(gs, locs):
    """Open-Meteo multi-model answer -> {point: {utc_dt: {"models": {m: kt}, "dirs": [..], "gust": kt}}}"""
    res = {}
    for p, loc in zip(POINTS, locs):
        h = loc.get("hourly", {})
        times = [gs.om_time(ts) for ts in h.get("time", [])]
        out = {t: {"models": {}, "dirs": [], "gust": None} for t in times}
        for key, vals in h.items():
            if not key.startswith("wind_speed_10m"):
                continue
            model = key[len("wind_speed_10m"):].lstrip("_") or "best_match"
            dirs = h.get("wind_direction_10m" + key[len("wind_speed_10m"):], [])
            gusts = h.get("wind_gusts_10m" + key[len("wind_speed_10m"):], [])
            for i, t in enumerate(times):
                if i < len(vals) and vals[i] is not None:
                    out[t]["models"][model] = vals[i]
                    if i < len(dirs) and dirs[i] is not None:
                        out[t]["dirs"].append(dirs[i])
                    if i < len(gusts) and gusts[i] is not None:
                        out[t]["gust"] = max(out[t]["gust"] or 0, gusts[i])
        res[p[0]] = out
    return res


def fetch_wind(gs, past_days=2):
    lats, lons = _om_points()
    base = {"latitude": lats, "longitude": lons, "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 8, "past_days": past_days}
    try:
        locs = _om_get(gs, "https://api.open-meteo.com/v1/forecast", dict(base, models=",".join(WIND_MODELS)))
    except Exception:  # noqa  - fall back to the single best-match model
        locs = _om_get(gs, "https://api.open-meteo.com/v1/forecast", base)
    return parse_om_wind(gs, locs)


def parse_om_marine(gs, locs):
    res = {}
    for p, loc in zip(POINTS, locs):
        h = loc.get("hourly", {})
        n = len(h.get("time", []))
        g = lambda k, i: (h.get(k) or [None] * n)[i]  # noqa
        out = {}
        for i, ts in enumerate(h.get("time", [])):
            if g("wave_height", i) is None:
                continue
            ww, sw = g("wind_wave_height", i), g("swell_wave_height", i)
            out[gs.om_time(ts)] = {"hs": g("wave_height", i) * gs.M_TO_FT,
                                   "tp": g("wave_peak_period", i) or g("wave_period", i), "dir": g("wave_direction", i),
                                   "ws_hs": ww * gs.M_TO_FT if ww is not None else None, "ws_tp": g("wind_wave_period", i),
                                   "sw_hs": sw * gs.M_TO_FT if sw is not None else None, "sw_tp": g("swell_wave_period", i),
                                   "src": "Open-Meteo"}
        res[p[0]] = out
    return res


MARINE_VARS = "wave_height,wave_peak_period,wave_period,wave_direction,wind_wave_height,wind_wave_period,swell_wave_height,swell_wave_period"


def fetch_marine(gs, past_days=2):
    lats, lons = _om_points()
    q = {"latitude": lats, "longitude": lons, "hourly": MARINE_VARS, "timezone": "UTC", "forecast_days": 8, "past_days": past_days}
    try:
        locs = _om_get(gs, "https://marine-api.open-meteo.com/v1/marine", q)
    except Exception:  # noqa
        q["hourly"] = "wave_height,wave_period,wave_direction,wind_wave_height,wind_wave_period"
        locs = _om_get(gs, "https://marine-api.open-meteo.com/v1/marine", q)
    return parse_om_marine(gs, locs)


def fetch_zone_text(gs, zone):
    return gs.http_get("https://tgftp.nws.noaa.gov/data/forecasts/marine/coastal/ph/%s.txt" % zone.lower())


def fetch_ndbc_wind(gs, station):
    rows = gs.ndbc_rows(station, "txt")
    for r in rows:
        kt = gs.fnum(r.get("WSPD"))
        if kt is not None:
            g = gs.fnum(r.get("GST"))
            return {"t": r["t"], "kt": kt * gs.MS_TO_KT, "gust": g * gs.MS_TO_KT if g is not None else None,
                    "dir": gs.fnum(r.get("WDIR"))}
    raise RuntimeError("no wind in recent rows")


def fetch_asos_wind(gs, station):
    import csv
    import io
    now = datetime.now(HST)
    d0, d1 = now - timedelta(days=1), now + timedelta(days=1)
    q = [("station", station), ("data", "sknt"), ("data", "drct"), ("data", "gust"),
         ("year1", d0.year), ("month1", d0.month), ("day1", d0.day), ("year2", d1.year), ("month2", d1.month), ("day2", d1.day),
         ("tz", "Pacific/Honolulu"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"), ("missing", "M"),
         ("trace", "T"), ("direct", "no"), ("report_type", "3"), ("report_type", "4")]
    rows = [r for r in csv.reader(io.StringIO(gs.http_get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q)))) if r]
    best = None
    for r in rows[1:]:
        rec = dict(zip(rows[0], r))
        try:
            t = datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=HST)
        except Exception:  # noqa
            continue
        if gs.fnum(rec.get("sknt")) is not None and t <= now and (best is None or t > best["t"]):
            best = {"t": t, "kt": gs.fnum(rec["sknt"]), "dir": gs.fnum(rec.get("drct")), "gust": gs.fnum(rec.get("gust"))}
    if not best:
        raise RuntimeError("no recent report")
    return best


# =============================================================== hourly records + the test
def build_records(gs, wind, waves, extra_wind=None):
    """-> {point: {utc_dt: rec}} with rec: wind (model mean), spread, models, wdir, gust, hs, tp, ws_hs, sw_hs, sw_tp, src"""
    out = {}
    for name in point_names():
        recs = {}
        w, v = wind.get(name, {}), waves.get(name, {})
        xw = (extra_wind or {}).get(name, {})
        for t in sorted(set(w) | set(v)):
            rec = {}
            models = dict(w.get(t, {}).get("models", {}))
            if t in xw and xw[t].get("wind") is not None:
                models["NWS"] = xw[t]["wind"]
            if models:
                vals = list(models.values())
                rec.update(wind=sum(vals) / len(vals), spread=max(vals) - min(vals), models=models,
                           wdir=gs.vec_mean_dir(w.get(t, {}).get("dirs", []) + ([xw[t]["dir"]] if t in xw and xw[t].get("dir") is not None else [])),
                           gust=w.get(t, {}).get("gust"))
            if t in v:
                rec.update({k: v[t].get(k) for k in ("hs", "tp", "ws_hs", "ws_tp", "sw_hs", "sw_tp", "src")})
            recs[t] = rec
        out[name] = recs
    return out


def day_hours(day, lo, hi):
    """UTC hour stamps for HST hours lo..hi-1 of date `day`."""
    base = datetime(day.year, day.month, day.day, tzinfo=HST)
    return [(base + timedelta(hours=h)).astimezone(UTC) for h in range(lo, hi)]


def day_metrics(records, day, names, lo=WINDOW[0], hi=WINDOW[1]):
    hours = day_hours(day, lo, hi)
    winds, chops, hss, tps, gusts = [], [], [], [], []
    per_model = {}
    max_pt = (None, None, None)   # (kt, point, hour)
    for t in hours:
        for nm in names:
            r = records.get(nm, {}).get(t)
            if not r:
                continue
            if r.get("wind") is not None:
                winds.append(r["wind"])
                if max_pt[0] is None or r["wind"] > max_pt[0]:
                    max_pt = (r["wind"], nm, t.astimezone(HST).hour)
                for m, kt in r.get("models", {}).items():
                    per_model.setdefault(m, []).append(kt)
            if r.get("gust"):
                gusts.append(r["gust"])
            if r.get("ws_hs") is not None:
                chops.append(r["ws_hs"])
            if r.get("hs") is not None:
                hss.append(r["hs"])
            if r.get("tp"):
                tps.append(r["tp"])
    need = len(hours) * len(names) // 2
    model_means = {m: sum(v) / len(v) for m, v in per_model.items() if len(v) >= need}
    return {"n": len(winds), "need": need,
            "wind_mean": sum(winds) / len(winds) if winds else None, "wind_max": max_pt[0],
            "wind_max_at": max_pt[1:], "gust_max": max(gusts) if gusts else None,
            "spread": (max(model_means.values()) - min(model_means.values())) if len(model_means) >= 2 else None,
            "model_means": model_means,
            "chop_max": max(chops) if chops else None, "hs_max": max(hss) if hss else None,
            "hs_min": min(hss) if hss else None, "tp_min": min(tps) if tps else None, "tp_max": max(tps) if tps else None}


def evaluate(m, prev_mean=None, nws_max=None, sca=False):
    """Apply the strict test to day_metrics output. -> (passed, [reasons it failed])"""
    fails = []
    if m["n"] < m["need"] or m["wind_mean"] is None:
        return False, ["not enough forecast data"]
    if m["wind_max"] > MAX_WIND_KT:
        fails.append("wind %.0f kt at %s %s" % (m["wind_max"], m["wind_max_at"][0], _hr(m["wind_max_at"][1])))
    if m["wind_mean"] > MEAN_WIND_KT:
        fails.append("channel average %.1f kt" % m["wind_mean"])
    if m["spread"] is not None and m["spread"] > MAX_SPREAD_KT:
        fails.append("models differ by %.0f kt" % m["spread"])
    if m["chop_max"] is not None and m["chop_max"] > MAX_CHOP_FT:
        fails.append("wind chop %.1f ft" % m["chop_max"])
    hs_limit = MAX_WAVE_LONG_FT if (m["tp_min"] or 0) >= LONG_PERIOD_S else MAX_WAVE_FT
    if m["hs_max"] is not None and m["hs_max"] > hs_limit:
        fails.append("waves %.1f ft" % m["hs_max"])
    if m["tp_min"] is not None and m["tp_min"] < MIN_PERIOD_S:
        fails.append("period down to %.0f s" % m["tp_min"])
    if PREV_DAY_MAX_MEAN_KT is not None and prev_mean is not None and prev_mean > PREV_DAY_MAX_MEAN_KT:
        fails.append("windy day before (%.0f kt)" % prev_mean)
    if nws_max is not None and nws_max > NWS_MAX_KT:
        fails.append("NWS Kaiwi %d kt" % nws_max)
    if sca:
        fails.append("Small Craft Advisory")
    return not fails, fails


def _hr(h):
    return "" if h is None else "%d %s" % ((h % 12) or 12, "AM" if h < 12 else "PM")


def sca_days(heads, today):
    """Days covered by a Small Craft (or worse) headline in the zone text. Headline end day parsed when possible."""
    days = set()
    for h in heads or []:
        u = h.upper()
        if not any(k in u for k in ("SMALL CRAFT", "GALE", "HAZARDOUS SEAS", "STORM WARNING")):
            continue
        end = today + timedelta(days=1)
        for i, w in enumerate(["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]):
            if w in u:
                end = today + timedelta(days=(i - today.weekday()) % 7)
        if "TONIGHT" in u and not any(w in u for w in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")):
            end = today
        d = today
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
    return days


# =============================================================== persistence (passes two runs in a row)
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa
        return {"runs": []}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        state["runs"] = state["runs"][-20:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
    except Exception:  # noqa
        pass


# =============================================================== 1. the flag (every run)
def check(gs, status, days_ahead=7, wind=None, waves=None, extra_wind=None, zone_texts=None):
    """Returns {"days": {date: {...}}, "records": ..., "heads": [...]} and appends to `status`."""
    def job(label, fn, secs=60):
        try:
            v = gs.run_with_timeout(fn, secs)
            status.append((True, label))
            return v
        except Exception as e:  # noqa
            status.append((False, "%s failed: %s" % (label, str(e)[:140])))
            return None
    if wind is None:
        wind = job("Penguin Bank: channel wind, 3 models (Open-Meteo)", lambda: fetch_wind(gs)) or {}
    if waves is None:
        waves = job("Penguin Bank: channel waves + wind chop (Open-Meteo Marine)", lambda: fetch_marine(gs)) or {}
    today = datetime.now(HST).date()
    if zone_texts is None:
        zone_texts = {}
        txt = job("Penguin Bank: NWS Kaiwi Channel forecast (PHZ116)", lambda: fetch_zone_text(gs, "PHZ116"), 40)
        if txt:
            zone_texts["PHZ116"] = txt
    heads, cwf_days = [], {}
    if zone_texts.get("PHZ116"):
        heads, _issued, cwf_days = gs.parse_cwf(zone_texts["PHZ116"], today)
    sca = sca_days(heads, today)
    records = build_records(gs, wind, waves, extra_wind)
    chan = point_names("channel")

    state = load_state()
    prev_run = state["runs"][-1] if state["runs"] else None
    prev_pass = set(prev_run.get("pass", [])) if prev_run else set()
    if prev_run:
        age_h = (datetime.now(UTC) - datetime.fromisoformat(prev_run["t"])).total_seconds() / 3600
        if age_h > 14:          # too old to count as the run before
            prev_pass = set()

    out = {}
    for k in range(days_ahead):
        d = today + timedelta(days=k)
        m = day_metrics(records, d, chan)
        prev = day_metrics(records, d - timedelta(days=1), chan)
        nws = gs.nws_wind_range(cwf_days.get(d, {}).get("day"))
        ok, fails = evaluate(m, prev["wind_mean"], nws[1] if nws else None, d in sca)
        out[d] = {"pass": ok, "fails": fails, "m": m, "prev_mean": prev["wind_mean"], "nws": nws,
                  "confirmed": ok and d.isoformat() in prev_pass,
                  "note": ("windy day before (%.0f kt) \u2013 check leftover chop" % prev["wind_mean"])
                  if prev["wind_mean"] is not None and prev["wind_mean"] > PREV_DAY_NOTE_KT else ""}
    if wind or waves:
        state["runs"].append({"t": datetime.now(UTC).isoformat(), "pass": [d.isoformat() for d, v in out.items() if v["pass"]]})
        save_state(state)
    return {"days": out, "records": records, "heads": heads, "cwf_days": cwf_days}


def card_badge(info, esc):
    """HTML for the main page's day card (empty unless the day passes)."""
    if not info or not info.get("pass"):
        return ""
    if info["confirmed"]:
        txt = "⚓ Penguin Bank window – run the Penguin Bank forecast"
    else:
        txt = "⚓ Possible Penguin Bank window (first run to show it) – check again next run"
    return '<p class="flag penguin"><a href="%s">%s</a></p>' % (PAGE_NAME, esc(txt))


def check_table(check_result, esc, have_page):
    """Small section for the main page."""
    if not check_result:
        return '<p class="muted">Penguin Bank check did not run this time.</p>'
    rows = []
    for d, v in sorted(check_result["days"].items()):
        m = v["m"]
        verdict = ("⚓ window (confirmed)" if v["confirmed"] else "⚓ window (1st run)") if v["pass"] else "no"
        rows.append("<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            ' class="pb-yes"' if v["pass"] else "", d.strftime("%a %b %-d"), esc(verdict),
            "%.1f / %.0f kt" % (m["wind_mean"], m["wind_max"]) if m["wind_mean"] is not None else "–",
            "%.1f ft" % m["chop_max"] if m["chop_max"] is not None else "–",
            ("%.1f ft @ %.0f s+" % (m["hs_max"], m["tp_min"])) if m["hs_max"] is not None and m["tp_min"] else "–",
            "%.0f kt" % m["spread"] if m["spread"] is not None else "–",
            esc("; ".join(v["fails"]) or ("all clear" + ((" \u00b7 " + v["note"]) if v.get("note") else "")))))
    link = ('<a href="%s">Open the last Penguin Bank forecast</a> · ' % PAGE_NAME) if have_page else ""
    bt = ('<a href="%s">Past-year check</a> · ' % os.path.basename(BACKTEST_HTML)) if os.path.exists(BACKTEST_HTML) else ""
    return ('<div class="wrap"><table><thead><tr><th>Day</th><th>Penguin Bank</th><th>Channel wind avg / max</th><th>Wind chop</th>'
            '<th>Waves</th><th>Models differ</th><th>Why not</th></tr></thead><tbody>%s</tbody></table></div>'
            '<p class="fine">%s%sStrict test over %s, Kaiwi Channel to the P FAD: no hour above %.0f kt, average %.0f kt or less, '
            'models within %.0f kt, wind chop %.1f ft or less, waves %.0f ft or less (%.0f ft if the period stays %.0f s+), period %.0f s or longer, '
            'no Small Craft Advisory for the Kaiwi Channel. A day has to pass two runs in a row to count as confirmed. '
            'To run the detailed forecast: GitHub app → go-score → Actions → Go Score → Run workflow → mode: penguin-bank.</p>'
            % ("".join(rows), link, bt, _win_txt(), MAX_WIND_KT, MEAN_WIND_KT, MAX_SPREAD_KT, MAX_CHOP_FT, MAX_WAVE_FT,
               MAX_WAVE_LONG_FT, LONG_PERIOD_S, MIN_PERIOD_S))


def _win_txt():
    return "%s–%s HST" % (_hr(WINDOW[0]), _hr(WINDOW[1]))


# =============================================================== 2. the detailed forecast (on demand)
def detail(gs, out_dir, status_main=None):
    """Fetch everything for the channel and write penguin.html into out_dir (and the committed copy)."""
    status = []

    def job(label, fn, secs=60):
        try:
            v = gs.run_with_timeout(fn, secs)
            status.append((True, label))
            print("ok   " + label, flush=True)
            return v
        except Exception as e:  # noqa
            status.append((False, "%s failed: %s" % (label, str(e)[:140])))
            print("FAIL %s: %s" % (label, str(e)[:140]), flush=True)
            return None

    wind = job("Channel wind, 3 models (Open-Meteo)", lambda: fetch_wind(gs)) or {}
    waves = job("Channel waves + wind chop (Open-Meteo Marine)", lambda: fetch_marine(gs)) or {}

    # WW3 (5 km, swell vs wind chop) and NWS gridpoint wind, all points in parallel
    t0 = gs.hour_floor(datetime.now(UTC) - timedelta(hours=2))
    t1 = t0 + timedelta(days=8)
    ww3, nws = {}, {}

    def ww3_one(nm, lat, lon):
        ww3[nm] = job("WW3 waves @ %s" % nm, lambda: gs.fetch_ww3(lat, lon, t0, None), 150)
        if ww3[nm]:
            gs.save_cache("WW3_PB_" + nm.replace(" ", "_"), ww3[nm])
        else:
            cached, age = gs.load_cache("WW3_PB_" + nm.replace(" ", "_"))
            if cached:
                ww3[nm] = cached
                status.append((True, "WW3 @ %s: server slow, using saved copy from %.0f h ago" % (nm, age)))

    def nws_one(nm, lat, lon):
        nws[nm] = job("NWS wind forecast @ %s" % nm, lambda: gs.fetch_nws(lat, lon, t0, t1), 60)
    threads = []
    for nm, lat, lon, _role in POINTS:
        for fn in (ww3_one, nws_one):
            th = threading.Thread(target=fn, args=(nm, lat, lon), daemon=True)
            th.start()
            threads.append(th)
    for th in threads:
        th.join(200)
    for nm, data in ww3.items():          # WW3 replaces Open-Meteo where it has the hour
        for t, v in (data or {}).items():
            waves.setdefault(nm, {})[t] = dict(v, src="WW3")
    extra = {nm: v for nm, v in nws.items() if v}

    zone_texts = {}
    for zone, label in NWS_ZONES:
        txt = job("NWS %s forecast (%s)" % (label, zone), lambda z=zone: fetch_zone_text(gs, z), 40)
        if txt:
            zone_texts[zone] = txt
    chk = check(gs, status, wind=wind, waves=waves, extra_wind=extra, zone_texts=zone_texts)

    buoys = {}
    for sid, label in BUOYS:
        v = job("buoy %s %s" % (sid, label.split(",")[0]), lambda s=sid: gs.fetch_buoy(s), 40)
        if v:
            buoys[sid] = v
    obs = {}
    for kind, sid, label in WIND_STATIONS:
        fn = (lambda s=sid: fetch_ndbc_wind(gs, s)) if kind == "ndbc" else (lambda s=sid: fetch_asos_wind(gs, s))
        v = job("%s wind (%s)" % (label, sid), fn, 40)
        if v:
            obs[sid] = v

    page = render(gs, chk, zone_texts, buoys, obs, status)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, PAGE_NAME), "w", encoding="utf-8") as f:
        f.write(page)
    os.makedirs(SAVED_DIR, exist_ok=True)
    with open(os.path.join(SAVED_DIR, PAGE_NAME), "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote %s" % os.path.join(out_dir, PAGE_NAME))
    if status_main is not None:
        bad = [m for ok, m in status if not ok]
        status_main.append((not bad, "Penguin Bank forecast written (%d of %d sources ok)" % (len(status) - len(bad), len(status))))
    return chk


def publish_saved(out_dir):
    """Copy the last Penguin Bank page and the backtest page into the site folder, so every run republishes them."""
    for src in (os.path.join(SAVED_DIR, PAGE_NAME), BACKTEST_HTML):
        if os.path.exists(src):
            try:
                os.makedirs(out_dir, exist_ok=True)
                shutil.copy(src, os.path.join(out_dir, os.path.basename(src)))
            except Exception:  # noqa
                pass


# =============================================================== page
def hour_rec_score(gs, r):
    if r.get("wind") is None or r.get("hs") is None or not r.get("tp"):
        return None
    return gs.hour_score(r["wind"], r["hs"], r["tp"], r.get("ws_hs"))[0]


def leg_summary(gs, records, d, names, lo, hi):
    hours = day_hours(d, lo, hi)
    winds, chops, scores = [], [], []
    for t in hours:
        for nm in names:
            r = records.get(nm, {}).get(t)
            if not r:
                continue
            if r.get("wind") is not None:
                winds.append(r["wind"])
            if r.get("ws_hs") is not None:
                chops.append(r["ws_hs"])
            s = hour_rec_score(gs, r)
            if s is not None:
                scores.append(s)
    if not winds:
        return None
    return {"wind_max": max(winds), "wind_mean": sum(winds) / len(winds), "chop_max": max(chops) if chops else None,
            "score_min": min(scores) if scores else None}


def render(gs, chk, zone_texts, buoys, obs, status):
    esc = gs.esc
    records = chk["records"]
    css = gs.PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
    css = css.replace("%%LIGHT%%", gs.ramp_rules(gs.BLUE)).replace("%%DARK%%", gs.ramp_rules(gs.BLUE[::-1]))
    css += (".flag.penguin a{color:inherit}.verdict{font-weight:700}.legs{font-size:12px;color:var(--ink2);margin:6px 0 0;padding-left:16px}"
            ".pb-yes td{font-weight:600}.strip13{display:grid;grid-template-columns:repeat(13,minmax(0,1fr));gap:2px}")
    hours_shown = list(range(5, 18))

    cards, strips = [], []
    for d, v in sorted(chk["days"].items()):
        m = v["m"]
        role = "good" if v["pass"] else ("warning" if len(v["fails"]) <= 2 and "not enough" not in " ".join(v["fails"]) else "critical")
        legs_html = ""
        for rn, names in ROUTES:
            parts = []
            for leg, lo, hi in LEGS:
                ls = leg_summary(gs, records, d, names, lo, hi)
                if ls:
                    parts.append("%s: up to %.0f kt%s" % (leg, ls["wind_max"], (", chop %.1f ft" % ls["chop_max"]) if ls["chop_max"] is not None else ""))
            if parts:
                legs_html += "<li><b>%s</b> – %s</li>" % (esc(rn), esc(" · ".join(parts)))
        cards.append('<article class="card %s"><h3>%s</h3><div class="verdict">%s</div>'
                     '<p class="meta">channel wind %s kt<br>wind chop up to %s<br>waves %s</p><ul class="legs">%s</ul><p class="note">%s</p></article>' % (
                         role, d.strftime("%a %b %-d"),
                         esc(("⚓ Penguin Bank window" + (" (confirmed)" if v["confirmed"] else "")) if v["pass"] else "Not a bank day"),
                         ("%.1f avg, %.0f max" % (m["wind_mean"], m["wind_max"])) if m["wind_mean"] is not None else "–",
                         ("%.1f ft" % m["chop_max"]) if m["chop_max"] is not None else "–",
                         ("%.1f ft @ %.0f s+" % (m["hs_max"], m["tp_min"])) if m["hs_max"] is not None and m["tp_min"] else "–",
                         legs_html, esc("; ".join(v["fails"]) or ("all clear" + ((" \u00b7 " + v["note"]) if v.get("note") else "")))))
        for rn, names in ROUTES:
            cells = []
            base = datetime(d.year, d.month, d.day, tzinfo=HST)
            for h in hours_shown:
                t = (base + timedelta(hours=h)).astimezone(UTC)
                sc, lines = [], ["%s  %s" % (_hr(h), rn)]
                for nm in names:
                    r = records.get(nm, {}).get(t)
                    if not r:
                        continue
                    s = hour_rec_score(gs, r)
                    if s is not None:
                        sc.append(s)
                        lines.append("%s %.1f: %.0f kt %s, %.1f ft @ %.0f s%s" % (
                            nm, s, r["wind"], gs.compass(r.get("wdir")), r["hs"], r["tp"],
                            (", chop %.1f ft" % r["ws_hs"]) if r.get("ws_hs") is not None else ""))
                inwin = WINDOW[0] <= h < WINDOW[1]
                if sc:
                    avg = sum(sc) / len(sc)
                    cells.append('<div class="cell %s%s" tabindex="0" data-tip="%s">%.0f</div>' % (
                        gs.cell_class(avg), " win" if inwin else "", esc("\n".join(lines)), avg))
                else:
                    cells.append('<div class="cell nodata%s">–</div>' % (" win" if inwin else ""))
            strips.append('<div class="strip-row"><div class="strip-label">%s<br>%s</div><div class="strip13">%s</div></div>' % (
                d.strftime("%a %-d"), esc(rn.replace("From ", "")), "".join(cells)))
    hdr = "".join('<div class="cell hdr">%d%s</div>' % ((h % 12) or 12, "a" if h < 12 else "p") for h in hours_shown)

    brow = []
    for sid, label in BUOYS:
        b = buoys.get(sid)
        if not b:
            brow.append('<tr><td>%s %s</td><td colspan="5" class="muted">not reporting</td></tr>' % (sid, esc(label)))
            continue
        split = ("%.1f ft @ %s s %s" % (b["sw_hs"], gs.fmt0(b.get("sw_tp")), b.get("sw_dir") or "")) if b.get("sw_hs") is not None else "–"
        wwv = ("%.1f ft @ %s s" % (b["ww_hs"], gs.fmt0(b.get("ww_tp")))) if b.get("ww_hs") is not None else "–"
        brow.append("<tr><td>%s %s</td><td>%s</td><td>%.1f ft @ %s s %s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            sid, esc(label), gs.age_txt(b["t"]), b["hs"], gs.fmt0(b.get("tp")), gs.compass(b.get("dir")), esc(split), esc(wwv),
            esc((b.get("steep_label") or "").replace("_", " ").lower() or "–")))
    orow = []
    for kind, sid, label in WIND_STATIONS:
        o = obs.get(sid)
        if not o:
            orow.append('<tr><td>%s</td><td colspan="3" class="muted">not reporting</td></tr>' % esc(label))
            continue
        orow.append("<tr><td>%s</td><td>%s</td><td>%.0f kt from the %s</td><td>%s</td></tr>" % (
            esc(label), gs.age_txt(o["t"]), o["kt"], gs.compass(o.get("dir")), ("gusts %.0f kt" % o["gust"]) if o.get("gust") else "–"))

    today = datetime.now(HST).date()
    zone_html = ""
    for zone, label in NWS_ZONES:
        txt = zone_texts.get(zone)
        if not txt:
            zone_html += "<h3>%s</h3><p class=\"muted\">not available this run</p>" % esc(label)
            continue
        heads, issued, days = gs.parse_cwf(txt, today)
        items = "".join('<div class="banner">⚠ %s</div>' % esc(h) for h in heads)
        for d in sorted(days):
            for part in ("day", "night"):
                if days[d].get(part):
                    items += "<p><b>%s%s</b> %s</p>" % (d.strftime("%a %b %-d"), " night" if part == "night" else "", esc(days[d][part]))
        if issued.startswith("..."):
            issued = ""
        zone_html += '<h3>%s (%s)</h3><div class="cwf"><div class="muted">%s</div>%s</div>' % (esc(label), zone, esc(issued), items)

    pts = "".join("<li>%s: %.3f, %.3f%s</li>" % (esc(n), la, lo, "" if r == "channel" else " (Heeia route only)") for n, la, lo, r in POINTS)
    status_html = "".join('<li><span class="%s">%s</span> %s</li>' % ("ok" if ok else "bad", "✓" if ok else "✕", esc(msg)) for ok, msg in status)
    bt = ('<p><a href="%s">Past-year check of this test</a></p>' % os.path.basename(BACKTEST_HTML)) if os.path.exists(BACKTEST_HTML) else ""
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Penguin Bank Forecast</title><style>%s</style></head><body><main>
<h1>Penguin Bank forecast</h1><div class="sub">Kaiwi Channel and Penguin Bank · %s window · updated %s · <a href="./">back to the Go Score</a></div>
<p class="fine">Run on demand. There is no real-time buoy on the bank itself, so this leans on models, checked against the buoys and wind stations around the channel below. Points are approximate until you add your own waypoints.</p>
<h2>Next 7 days</h2><div class="cards">%s</div>
<h2>Hour by hour, each route (score out of 10, averaged over the route's points)</h2>
<div class="strip-row"><div></div><div class="strip13">%s</div></div>%s
<p class="legend">Outlined cells are the trip window. Hover or tap a cell for each point: wind, waves and wind chop.</p>
<h2>Buoys around the channel</h2><div class="wrap"><table><thead><tr><th>Buoy</th><th>Reading</th><th>Waves</th><th>Swell</th><th>Wind waves</th><th>NDBC steepness</th></tr></thead><tbody>%s</tbody></table></div>
<h2>Wind stations</h2><div class="wrap"><table><thead><tr><th>Station</th><th>Reading</th><th>Wind</th><th>Gusts</th></tr></thead><tbody>%s</tbody></table></div>
<h2>NWS text forecasts</h2>%s
<h2>Points used</h2><ul class="fine">%s</ul>%s
<h2>Data sources this run</h2><ul class="status">%s</ul>
<p class="fine">The window test: no hour above %.0f kt anywhere from the Kaiwi Channel to the P FAD, channel average %.0f kt or less, wind models within %.0f kt of each other, wind chop %.1f ft or less, waves %.0f ft or less (%.0f ft if the period stays %.0f s or longer), period %.0f s or longer, and no Small Craft Advisory for the Kaiwi Channel. Checked against one known day (Sep 9 2026 passes, Sep 8 fails) and a year of history; not yet against anyone's trips.</p>
</main></body></html>""" % (css, esc(_win_txt()), esc(datetime.now(HST).strftime("%a %b %-d, %-I:%M %p HST")),
                              "".join(cards), hdr, "".join(strips), "".join(brow), "".join(orow), zone_html, pts, bt, status_html,
                              MAX_WIND_KT, MEAN_WIND_KT, MAX_SPREAD_KT, MAX_CHOP_FT, MAX_WAVE_FT, MAX_WAVE_LONG_FT, LONG_PERIOD_S, MIN_PERIOD_S))
