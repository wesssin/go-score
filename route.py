"""
route.py - suggests which order to run the FADs in on a good day.

Visits the FADs on the day's best-scoring route, in whichever order rides best.
Idea (from Wes): wind is usually the deciding factor, swell direction matters a lot too.
  - Put the windiest leg (usually the ride home, when the breeze builds) behind you.
  - Wind on the beam is fine if you are running with the swell at ~45 degrees.
  - Wind on the beam while heading into the swell at ~45 degrees is wet (spray).
  - Beating into wind and chop is the thing to avoid.

For each candidate order the boat leaves the bay mouth at the start of the trip window, runs each leg at
PLAN_SPEED_KT, splits the remaining time evenly between the FADs, and is back by the end of the window.
Each leg is costed at the hour it is run, from the forecast wind (speed, direction), wind chop and swell
(height, period, direction) at the leg's two ends. The order with the lowest total cost wins.

Hand-set weights, not yet fitted to trips. `gs` is the go_score module.
"""
import math
from datetime import timedelta

PLAN_SPEED_KT = 16.0        # average running speed for the 15' RIB in decent water
MIN_SCORE = 7.5             # only suggest routes on Good/Epic days
HOME = "Bay mouth"
# Candidate trips: FAD visit orders (home -> ... -> home). Reverse orders are added automatically.
TRIPS = [["LL FAD", "MM FAD", "T FAD"],
         ["MM FAD", "T FAD"],
         ["U FAD", "MM FAD", "T FAD"],
         ["U FAD", "LL FAD"],
         ["LL FAD", "MM FAD"],
         ["LL FAD", "X FAD"],
         ["U FAD", "LL FAD", "X FAD"]]
SHORT = {"Bay mouth": "the bay", "U FAD": "U", "MM FAD": "MM", "T FAD": "T", "LL FAD": "LL", "X FAD": "X"}


# =============================================================== geometry
def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return math.degrees(math.atan2(y, x)) % 360


def dist_nm(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(h))


def rel(heading, from_dir):
    """0 = coming straight at the bow (head-on), 180 = straight from behind."""
    return abs((from_dir - heading + 180) % 360 - 180)


def side(heading, from_dir):
    d = (from_dir - heading) % 360
    return "starboard" if 0 < d < 180 else "port"


def words(r):
    if r < 25:
        return "on the nose"
    if r < 65:
        return "on the bow"
    if r < 115:
        return "on the beam"
    if r < 155:
        return "on the aft quarter"
    return "behind you"


# =============================================================== leg cost
def interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


WIND_ANGLE = [(0, 1.0), (45, 0.9), (90, 0.55), (135, 0.3), (180, 0.25)]      # head-on .. following
SWELL_ANGLE = [(0, 1.0), (45, 0.85), (90, 0.6), (130, 0.15), (150, 0.15), (180, 0.35)]   # quartering-following is best


def leg_cost(hdg, cond):
    """cond: wind, wdir, hs, tp, wave_dir, ws_hs (any may be None). Returns (cost per hour, details)."""
    w, wd = cond.get("wind"), cond.get("wdir")
    hs, tp, sd = cond.get("hs"), cond.get("tp"), cond.get("wave_dir")
    chop = cond.get("ws_hs")
    cost, d = 0.0, {}
    if w is not None and wd is not None:
        a = rel(hdg, wd)
        excess = max(0.0, w - 4.0)
        cost += excess * interp(a, WIND_ANGLE)
        if chop:
            cost += 1.5 * chop * interp(a, WIND_ANGLE)
        d.update(wind_rel=a, wind_side=side(hdg, wd))
        if sd is not None and hs:
            s = rel(hdg, sd)
            if 40 <= a <= 115 and s <= 65:          # beam wind while heading into the swell: spray
                cost += 0.5 * excess
                d["spray"] = True
    if sd is not None and hs and tp:
        s = rel(hdg, sd)
        steep = 10.0 / max(tp, 5.0)
        cost += hs * steep * interp(s, SWELL_ANGLE)
        d["swell_rel"] = s
    return cost, d


# =============================================================== route planning
def cond_at(per_spot, names, t_local):
    """Average conditions of the given spots at the forecast hour nearest t_local."""
    t = t_local.replace(minute=0, second=0, microsecond=0) + (timedelta(hours=1) if t_local.minute >= 30 else timedelta())
    vals = {}
    for nm in names:
        for r in per_spot.get(nm, []):
            if r["t"] == t:
                for k in ("wind", "wdir", "hs", "tp", "wave_dir", "ws_hs"):
                    if r.get(k) is not None:
                        vals.setdefault(k, []).append(r[k])
                break
    out = {}
    for k, v in vals.items():
        if k in ("wdir", "wave_dir"):
            x = sum(math.cos(math.radians(a)) for a in v)
            y = sum(math.sin(math.radians(a)) for a in v)
            out[k] = math.degrees(math.atan2(y, x)) % 360
        else:
            out[k] = sum(v) / len(v)
    return out


def plan(gs, per_spot, day, order):
    pos = {s[0]: (s[1], s[2]) for s in gs.SPOTS}
    stops = [HOME] + order + [HOME]
    legs = [(a, b, dist_nm(pos[a], pos[b]), bearing(pos[a], pos[b])) for a, b in zip(stops, stops[1:])]
    run_h = sum(l[2] for l in legs) / PLAN_SPEED_KT
    total_h = gs.WINDOW[1] - gs.WINDOW[0]
    fish_h = max(0.5, (total_h - run_h) / len(order))
    t = day.replace(hour=gs.WINDOW[0], minute=0)
    out, cost = [], 0.0
    for i, (a, b, dnm, hdg) in enumerate(legs):
        dur = dnm / PLAN_SPEED_KT
        mid = t + timedelta(hours=dur / 2)
        c = cond_at(per_spot, [x for x in (a, b) if x in per_spot], mid)
        lc, det = leg_cost(hdg, c)
        cost += lc * dur * (1.2 if i == len(legs) - 1 else 1.0)    # the ride home counts a bit more
        out.append({"from": a, "to": b, "nm": dnm, "hdg": hdg, "start": t, "dur": dur, "cond": c, "cost": lc, "det": det})
        t += timedelta(hours=dur)
        if b != HOME:
            t += timedelta(hours=fish_h)
    return {"order": order, "legs": out, "cost": cost, "back": t, "run_h": run_h}


def best_plan(gs, per_spot, day, spot_scores, prefer=None):
    """Try every visiting order of the FADs on the day's best-scoring route.
    -> (best plan, same trip in reverse) or (None, None)."""
    import itertools
    fads = [f for f in (prefer or []) if f != HOME]
    if not fads:
        return None, None
    plans = [plan(gs, per_spot, day, list(o)) for o in itertools.permutations(fads)]
    plans = [p for p in plans if all(l["cond"].get("wind") is not None for l in p["legs"])]
    if not plans:
        return None, None
    plans.sort(key=lambda p: p["cost"])
    best = plans[0]
    rev = next((p for p in plans if p["order"] == list(reversed(best["order"]))), None)
    return best, rev


def leg_text(gs, l):
    c, d = l["cond"], l["det"]
    parts = []
    if c.get("wind") is not None and "wind_rel" in d:
        parts.append("%.0f kt %s %s" % (c["wind"], gs.compass(c.get("wdir")), words(d["wind_rel"])))
    if "swell_rel" in d:
        parts.append("swell %s" % words(d["swell_rel"]))
    if d.get("spray"):
        parts.append("expect spray")
    return "%s → %s (%s, %.0f nm): %s" % (SHORT[l["from"]], SHORT[l["to"]], l["start"].strftime("%-I:%M"), l["nm"], ", ".join(parts))


def summary(gs, best, rev):
    """One line for the day card."""
    order = " \u2192 ".join(SHORT[f] for f in best["order"])
    home = best["legs"][-1]
    line = "Route: the bay \u2192 %s \u2192 home" % order
    hw = home["det"].get("wind_rel")
    if hw is not None:
        line += ", wind %s coming home" % words(hw)
    winds = [l["cond"]["wind"] for l in best["legs"] if l["cond"].get("wind") is not None]
    if rev is None or len(best["order"]) < 2:
        return line
    if rev["cost"] <= best["cost"] * 1.15 or (winds and max(winds) < 7):
        return line + " (either direction works today)"
    worst = max(rev["legs"], key=lambda l: l["cost"] * l["dur"])
    a = worst["det"].get("wind_rel")
    if a is not None and a < 65:
        return line + " (the other way puts %.0f kt %s on the %s\u2192%s leg)" % (
            worst["cond"]["wind"], words(a), SHORT[worst["from"]], SHORT[worst["to"]])
    if worst["det"].get("spray"):
        return line + " (the other way means spray on the %s\u2192%s leg)" % (SHORT[worst["from"]], SHORT[worst["to"]])
    return line + " (the other way is about %.0f%% rougher)" % ((rev["cost"] / max(best["cost"], 0.1) - 1) * 100)


def details_html(gs, items):
    """items: [(day, best, rev)] -> HTML section body."""
    esc = gs.esc
    if not items:
        return '<p class="muted">No Good or Epic days to plan a route for.</p>'
    blocks = []
    for d, best, rev in items:
        rows = "".join("<li>%s</li>" % esc(leg_text(gs, l)) for l in best["legs"])
        cmp_txt = ""
        if rev:
            cmp_txt = " Reverse order costs %.0f%% %s." % (abs(rev["cost"] - best["cost"]) / max(best["cost"], 0.1) * 100,
                                                            "more" if rev["cost"] >= best["cost"] else "less")
        blocks.append('<div class="plan"><b>%s</b> – the bay → %s → home, back about %s.%s<ul>%s</ul></div>' % (
            esc(gs.fmt_day(d)), esc(" → ".join(SHORT[f] for f in best["order"])), best["back"].strftime("%-I:%M %p").lower(),
            esc(cmp_txt), rows))
    return "".join(blocks) + ('<p class="fine">Leaves the bay at %d AM, runs at about %.0f kt, splits the rest of the window between the FADs. '
                              'Each leg is judged at the hour you would run it: wind behind you is easy, on the beam is fine when you are running with '
                              'the swell, on the beam while heading into the swell means spray, on the nose is worst. The ride home counts a bit '
                              'more. Hand-set rules from your description, not fitted to trips yet.</p>' % (gs.WINDOW[0], PLAN_SPEED_KT))
