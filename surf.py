"""
surf.py - east-side "surf check" add-on for go_score.py (No Can Tells and Manana).

Not a surf forecast. It flags days worth a look ("run down to the beach and see if there's action"),
from rules fitted by surf_backtest.py to Wes's own sessions (see SPOTS below and the project doc).

check(gs, status, buoy_now) runs on every normal Go Score run:
  - forecast swell at the Mokapu buoy position (PacIOOS WW3, saved copy if the server is slow,
    Open-Meteo Marine as backup) - the rules were fitted to buoy 51202, so this is the apples-to-apples point
  - forecast wind at each spot (Open-Meteo)
  - NOAA tide predictions, Mokuoloe (1612480)
section_html() and push_lines() turn the result into a page section and push lines.

`gs` is the go_score module (passed in so this file has no circular import).
"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

HST = timezone(timedelta(hours=-10))
UTC = timezone.utc

BUOY_PT = (21.417, -157.668)        # NDBC 51202 Mokapu
WINDOW = (6, 17)                    # daylight hours considered, HST
MIN_HOURS = 2                       # hours that must pass for a day to be flagged

# Rules fitted to very few sessions - starting points, not proof. Swell numbers are at the Mokapu buoy.
SPOTS = [
    dict(key="nct", name="No Can Tells", pin=(21.3739, -157.6929),
         min_ft=5.0, min_s=10.5, dirs=(15, 85),           # NNE-ENE; due N was a dud (Nov 25 2026)
         max_kt=9.5, south_kt=13.0,                       # light wind, or a south-quadrant wind (offshore) up to 13 kt
         note="NNE-ENE swell, light or southerly wind. Fitted to Sep 8, Mar 1, Dec 27, Jul 29 (good) and Nov 25 2026 (dud)."),
    dict(key="manana", name="Manana (Rabbit Island)", pin=(21.3280, -157.6605),
         min_ft=5.5, min_s=11.5, dirs=(340, 35),          # N-NNE
         max_kt=15.0, south_kt=15.0,                      # trades OK (Dec 25 2025 was rideable in 8-14 kt ENE)
         note="N-NNE swell, trades OK; better as the tide drops. Fitted to Nov 25 2026 (good) and Dec 25 2025 (rideable)."),
]
NEAR_FT, NEAR_S, NEAR_DEG, NEAR_KT = 0.85, 1.0, 8, 3.0   # how close counts as "maybe"


def in_dirs(d, lo, hi, pad=0):
    if d is None:
        return False
    lo, hi = (lo - pad) % 360, (hi + pad) % 360
    return lo <= d <= hi if lo <= hi else (d >= lo or d <= hi)


def swell_ok(sp, hs, tp, dr, near=False):
    if hs is None or tp is None or dr is None:
        return False
    k = NEAR_FT if near else 1.0
    return hs >= sp["min_ft"] * k and tp >= sp["min_s"] - (NEAR_S if near else 0) and in_dirs(dr, *sp["dirs"], pad=NEAR_DEG if near else 0)


def wind_ok(sp, kt, dr, near=False):
    if kt is None:
        return False
    extra = NEAR_KT if near else 0
    if kt <= sp["max_kt"] + extra:
        return True
    return dr is not None and 150 <= dr <= 260 and kt <= sp["south_kt"] + extra


# --------------------------------------------------------------- data
def fetch_swell(gs, status):
    """{hour (UTC datetime): {"hs","tp","dir","src"}} at the buoy position."""
    t0 = gs.hour_floor(datetime.now(UTC) - timedelta(hours=2))
    out = {}
    try:
        w = gs.run_with_timeout(lambda: gs.fetch_ww3(BUOY_PT[0], BUOY_PT[1], t0, None), 130)
        gs.save_cache("WW3_surf_mokapu", w)
        src = "WW3"
    except Exception as e:  # noqa
        w, age = gs.load_cache("WW3_surf_mokapu")
        src = "WW3 (saved copy, %.0f h old)" % age if w else None
        status.append((bool(w), "Surf check: WW3 at Mokapu %s" % ("timed out, using saved copy" if w else "failed: %s" % str(e)[:100])))
    for t, v in (w or {}).items():
        out[t] = {"hs": v["hs"], "tp": v["tp"], "dir": v["dir"], "src": src}
    try:
        q = {"latitude": BUOY_PT[0], "longitude": BUOY_PT[1], "timezone": "UTC", "forecast_days": 8,
             "hourly": "wave_height,wave_peak_period,wave_period,wave_direction"}
        h = json.loads(gs.http_get("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(q)))["hourly"]
        for i, ts in enumerate(h["time"]):
            t = gs.om_time(ts)
            if t in out or h["wave_height"][i] is None:
                continue
            out[t] = {"hs": h["wave_height"][i] * gs.M_TO_FT, "tp": h["wave_peak_period"][i] or h["wave_period"][i],
                      "dir": h["wave_direction"][i], "src": "Open-Meteo"}
    except Exception as e:  # noqa
        status.append((False, "Surf check: Open-Meteo swell backup failed: %s" % str(e)[:100]))
    if not out:
        raise RuntimeError("no swell forecast")
    return out


def fetch_wind(gs):
    q = {"latitude": ",".join("%.4f" % s["pin"][0] for s in SPOTS), "longitude": ",".join("%.4f" % s["pin"][1] for s in SPOTS),
         "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m", "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 8}
    d = json.loads(gs.http_get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)))
    d = d if isinstance(d, list) else [d]
    out = {}
    for sp, x in zip(SPOTS, d):
        h = x["hourly"]
        out[sp["key"]] = {gs.om_time(ts): {"kt": h["wind_speed_10m"][i], "dir": h["wind_direction_10m"][i], "gust": h["wind_gusts_10m"][i]}
                          for i, ts in enumerate(h["time"]) if h["wind_speed_10m"][i] is not None}
    return out


def fetch_tides(gs):
    a = datetime.now(HST).date()
    q = dict(product="predictions", station="1612480", datum="MLLW", units="english", time_zone="lst_ldt", format="json",
             interval="hilo", begin_date=a.strftime("%Y%m%d"), end_date=(a + timedelta(days=8)).strftime("%Y%m%d"))
    p = json.loads(gs.http_get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?" + urllib.parse.urlencode(q)))
    return [(datetime.strptime(x["t"], "%Y-%m-%d %H:%M").replace(tzinfo=HST), x["type"], float(x["v"])) for x in p.get("predictions", [])]


# --------------------------------------------------------------- evaluate
def _hr(h):
    return "%d%s" % ((h % 12) or 12, "a" if h < 12 else "p")


def _clock(t):
    return t.strftime("%-I:%M %p").lower().replace(":00", "")


def check(gs, status, buoy_now=None, days_ahead=7):
    swell = gs.run_with_timeout(lambda: fetch_swell(gs, status), 200)
    wind = gs.run_with_timeout(lambda: fetch_wind(gs), 60)
    try:
        tides = gs.run_with_timeout(lambda: fetch_tides(gs), 40)
    except Exception as e:  # noqa
        tides = []
        status.append((False, "Surf check: NOAA tides failed: %s" % str(e)[:100]))
    today = datetime.now(HST).date()
    days = {}
    for k in range(days_ahead):
        d = today + timedelta(days=k)
        for sp in SPOTS:
            hours = []
            for h in range(WINDOW[0], WINDOW[1]):
                t = datetime(d.year, d.month, d.day, h, tzinfo=HST).astimezone(UTC)
                if k == 0 and datetime(d.year, d.month, d.day, h, tzinfo=HST) < datetime.now(HST) - timedelta(hours=1):
                    continue
                s, w = swell.get(t), wind.get(sp["key"], {}).get(t)
                if not s or not w:
                    continue
                hours.append(dict(h=h, hs=s["hs"], tp=s["tp"], sdir=s["dir"], src=s["src"], kt=w["kt"], wdir=w["dir"],
                                  ok=swell_ok(sp, s["hs"], s["tp"], s["dir"]) and wind_ok(sp, w["kt"], w["dir"]),
                                  near=swell_ok(sp, s["hs"], s["tp"], s["dir"], True) and wind_ok(sp, w["kt"], w["dir"], True)))
            if not hours:
                continue
            good = [x for x in hours if x["ok"]]
            near = [x for x in hours if x["near"]]
            verdict = "look" if len(good) >= MIN_HOURS else "maybe" if len(near) >= MIN_HOURS else None
            pick = good if verdict == "look" else near if verdict == "maybe" else hours
            mid = pick[len(pick) // 2]
            lows = [(t, v) for t, kind, v in tides if kind == "L" and t.date() == d and WINDOW[0] <= t.hour < WINDOW[1] + 1]
            highs = [(t, v) for t, kind, v in tides if kind == "H" and t.date() == d and WINDOW[0] - 2 <= t.hour < WINDOW[1]]
            why = []
            if verdict != "look":
                if not any(swell_ok(sp, x["hs"], x["tp"], x["sdir"]) for x in hours):
                    best = max(hours, key=lambda x: x["hs"] * x["tp"])
                    if not in_dirs(best["sdir"], *sp["dirs"]):
                        why.append("swell from %s, wants %s-%s" % (gs.compass(best["sdir"]), gs.compass(sp["dirs"][0]), gs.compass(sp["dirs"][1])))
                    if best["hs"] < sp["min_ft"]:
                        why.append("%.1f ft, wants %.1f+" % (best["hs"], sp["min_ft"]))
                    if best["tp"] < sp["min_s"]:
                        why.append("%.0f s, wants %.1f+" % (best["tp"], sp["min_s"]))
                if not any(wind_ok(sp, x["kt"], x["wdir"]) for x in hours):
                    why.append("wind %.0f kt %s" % (min(x["kt"] for x in hours), gs.compass(hours[0]["wdir"])))
            days.setdefault(d, {})[sp["key"]] = dict(
                verdict=verdict, hours=(pick[0]["h"], pick[-1]["h"] + 1) if verdict else None,
                hs=mid["hs"], tp=mid["tp"], sdir=mid["sdir"], src=mid["src"],
                kt=(min(x["kt"] for x in pick), max(x["kt"] for x in pick)), wdir=mid["wdir"],
                lows=lows, highs=highs, why=why)
    now = None
    if buoy_now and buoy_now.get("hs") is not None:
        fits = [sp["name"] for sp in SPOTS if swell_ok(sp, buoy_now["hs"], buoy_now.get("tp"), buoy_now.get("dir"))]
        now = dict(b=buoy_now, fits=fits)
    status.append((True, "Surf check: %d day(s) flagged" % sum(1 for v in days.values() for x in v.values() if x["verdict"])))
    return {"days": days, "now": now}


def tide_note(x):
    bits = []
    for t, v in x["lows"]:
        bits.append("low %s (%.1f ft)" % (_clock(t), v))
    if not bits and x["highs"]:
        t, v = x["highs"][-1]
        bits.append("high %s, dropping after" % _clock(t))
    return ", ".join(bits)


# --------------------------------------------------------------- output
def section_html(res, gs, esc):
    if not res:
        return '<p class="muted">Surf check did not run this time.</p>'
    parts = []
    n = res.get("now")
    if n:
        b = n["b"]
        parts.append('<p>Mokapu buoy now (%s): <b>%.1f ft @ %s s %s</b> – %s</p>' % (
            gs.age_txt(b["t"]), b["hs"], gs.fmt0(b.get("tp")), gs.compass(b.get("dir")),
            esc("swell fits " + " and ".join(n["fits"])) if n["fits"] else "swell doesn't fit either spot's rule right now"))
    rows = []
    for d in sorted(res["days"]):
        for sp in SPOTS:
            x = res["days"][d].get(sp["key"])
            if not x:
                continue
            v = {"look": "\U0001F3C4 worth a look", "maybe": "maybe"}.get(x["verdict"], "no")
            when = "%s–%s" % (_hr(x["hours"][0]), _hr(x["hours"][1])) if x["hours"] else "–"
            rows.append('<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%.1f ft @ %.0f s %s</td><td>%s kt %s</td><td>%s</td><td>%s</td></tr>' % (
                ' class="pb-yes"' if x["verdict"] == "look" else "", d.strftime("%a %b %-d"), esc(sp["name"]), v, when,
                x["hs"], x["tp"] or 0, gs.compass(x["sdir"]), gs.rng(*x["kt"]), gs.compass(x["wdir"]),
                esc(tide_note(x) or "–"), esc("; ".join(x["why"]) or "")))
    parts.append('<div class="wrap"><table><thead><tr><th>Day</th><th>Spot</th><th>Call</th><th>Hours</th><th>Swell at Mokapu</th>'
                 '<th>Wind at spot</th><th>Tide (Mokuoloe)</th><th>Why not</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    parts.append('<p class="fine">A heads-up, not a surf forecast: "worth a look" means the swell and wind match the days Wes has surfed there '
                 'for at least %d hours between %s and %s. Swell is the forecast at the Mokapu buoy (PacIOOS WW3, Open-Meteo backup), '
                 'wind is Open-Meteo at each spot. Rules: %s</p>' % (
                     MIN_HOURS, _hr(WINDOW[0]), _hr(WINDOW[1]),
                     esc(" ".join("%s: %.1f+ ft, %.1f+ s, %s-%s, wind up to %.0f kt (south wind up to %.0f). %s" % (
                         sp["name"], sp["min_ft"], sp["min_s"], gs.compass(sp["dirs"][0]), gs.compass(sp["dirs"][1]),
                         sp["max_kt"], sp["south_kt"], sp["note"]) for sp in SPOTS))))
    return "".join(parts)


def push_lines(res, n_days=5):
    """(lines, hot) for the phone push: 'worth a look' days only. hot = one within 2 days."""
    if not res:
        return [], False
    today = datetime.now(HST).date()
    lines, hot = [], False
    for d in sorted(res["days"]):
        if (d - today).days >= n_days:
            continue
        for sp in SPOTS:
            x = res["days"][d].get(sp["key"])
            if not x or x["verdict"] != "look":      # "maybe" days stay on the page only, to keep the push short
                continue
            short = sp["name"].split(" (")[0]
            lines.append("\U0001F3C4 %s %s: %s %s–%s%s" % (
                short, d.strftime("%a %-d"), "worth a look" if x["verdict"] == "look" else "maybe",
                _hr(x["hours"][0]), _hr(x["hours"][1]), (", " + tide_note(x)) if tide_note(x) else ""))
            if x["verdict"] == "look" and (d - today).days <= 2:
                hot = True
    return lines, hot
