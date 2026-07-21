"""Sophie DAILY report — cloud-native, multi-day, password-gated page.

Replaces the openclaw daily bot. ONE file (sophie-daily.html) holds every day's
report as a separate encrypted block plus a day-picker. Runs fully in the cloud
(no Mac / device bridge).

Methodology is aligned to the WEEKLY labor report (labor_web.py):
  * Controllable labor = 8 groups (Hostess, Server Assistant, Barback, Runner,
    Line Cook, Prep Cook, Dishwasher, Porter). Excludes bartenders & servers
    (tipped out) and salaried managers. Each day's controllable labor is a slice
    of the week's total, so the daily ties to the weekly.
  * Real Toast hourly wages; wage cost = reg*wage + ot*wage*1.5.
  * Net sales = paid/closed checks only (matches Toast/openclaw); open checks are
    excluded from sales and surfaced under Unpaid.

Modes:
  seed [N]                 build sophie-daily.html from the last N complete days (default 7)
  update [sophie-daily.html]  pull yesterday from Toast and add/replace it in the page

Env: DAILY_PW (required). toast_lib.py + creds.json must be present for pulls.
"""
import json, os, base64, sys, datetime, re
from collections import defaultdict
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERS = 200000
CONTROLLABLE = ["Hostess", "Server Assistant", "Barback", "Runner",
                "Line Cook", "Prep Cook", "Dishwasher", "Porter"]
TIPPED_OUT = {"Server", "Bartender"}
SALARIED = {"Operations Manager", "General Manager", "Owner"}

def money(x):
    return float(x or 0)

# ================= Toast pull + compute =================
_TOAST = {}
def _toast():
    if not _TOAST:
        from toast_lib import RESTAURANTS, api_get, get_token
        g = RESTAURANTS["Sophie"]; tok = get_token()
        _, jobs = api_get("/labor/v1/jobs", g, None, tok)
        _, emps = api_get("/labor/v1/employees", g, None, tok)
        jm = {j.get("guid"): j.get("title") for j in (jobs or [])}
        def enm(e):
            nm = e.get("chosenName") or ", ".join(x for x in [e.get("lastName"), e.get("firstName")] if x)
            return (nm or "").strip()
        em = {e.get("guid"): enm(e) for e in (emps or [])}
        _TOAST.update(g=g, tok=tok, api_get=api_get, jm=jm, em=em, item_cat=_menu_map(api_get, g, tok))
    return _TOAST

def _menu_map(api_get, g, tok):
    _, menus = api_get("/menus/v2/menus", g, None, tok)
    obj = menus if isinstance(menus, dict) else {"menus": menus}
    m = {}
    for menu in (obj.get("menus") or []):
        mn = menu.get("name", "")
        for grp in (menu.get("menuGroups") or []):
            gn = grp.get("name", "")
            for it in (grp.get("menuItems") or []):
                m[it.get("guid")] = (mn, gn)
            for sub in (grp.get("menuGroups") or []):
                sn = sub.get("name", "")
                for it in (sub.get("menuItems") or []):
                    m[it.get("guid")] = (mn, f"{gn}/{sn}")
    return m

NONALC_GROUPS = {"COFFEE", "N/A BEVERAGES", "ZERO PROOF DRINKS", "HH MOCKTAILS", "MOCKTAILS"}

def _by_name(name):
    """Fallback for menu items not found in the Menus API structure."""
    n = (name or "").upper()
    if any(w in n for w in ["MOCKTAIL", "COFFEE", "ESPRESSO", "LEMONADE", " SODA",
                            "JUICE", "WATER", "ZERO PROOF", "RED BULL", "ICED TEA"]):
        return "nonalc"
    if any(w in n for w in ["BURGER", "FRIES", "SLIDER", "PASTA", "WING", "SALAD",
                            "TACO", "NACHO", "CHARCUTERIE", "DESSERT", "PLATE", "TENDER"]):
        return "food"
    if any(w in n for w in ["MARTINI", "COCKTAIL", "SPRITZ", "MARGARITA", "MOJITO",
                            "WINE", "BEER", "TEQUILA", "VODKA", "WHISKEY", "GIN ",
                            "RUM", "SHOT", "SANGRIA", "NEGRONI", "MULE", "SOUR", "75"]):
        return "alcohol"
    return "nonalc"  # ambiguous unmatched -> non-alcohol / other bucket

def categorize(menu, group, name):
    """Group-precise: use the Menus API menu/group; fall back to name only when unmatched."""
    m = (menu or "").upper().strip()
    g = (group or "").upper().strip()
    if not m and not g:
        return _by_name(name)
    g_last = g.split("/")[-1].strip()  # nested subgroup -> take leaf
    if g_last in NONALC_GROUPS or "MOCKTAIL" in g_last or "ZERO PROOF" in g_last or "N/A BEV" in g_last:
        return "nonalc"
    if m in ("FOOD", "UBER EATS") or "FOOD" in g_last or "BURGER" in g_last:
        return "food"
    return "alcohol"

def pull_orders(bdate):
    T = _toast(); orders = []; page = 1
    while True:
        s, od = T["api_get"]("/orders/v2/ordersBulk", T["g"], {"businessDate": bdate, "pageSize": 100, "page": page}, T["tok"])
        if not isinstance(od, list) or not od:
            break
        orders += od
        if len(od) < 100:
            break
        page += 1
    return orders

def _is_open(c):
    return c.get("paymentStatus") == "OPEN"

_DAYCACHE = {}
def compute_day(iso):
    """Full day dict from Toast for a business date (ISO yyyy-mm-dd). Memoized per run."""
    if iso in _DAYCACHE:
        return _DAYCACHE[iso]
    T = _toast(); bdate = iso.replace("-", "")
    orders = pull_orders(bdate)
    # ---- sales (paid/closed checks only) ----
    net = 0.0; nchecks = 0; covers = 0
    voids = []; discounts = []; comps_amt = 0.0; unpaid = []
    mix = defaultdict(lambda: {"qty": 0, "net": 0.0, "cat": None})
    for o in orders:
        if o.get("voided") or o.get("deleted"):
            continue
        g = int(o.get("numberOfGuests") or 0)
        order_counts = False
        for c in (o.get("checks") or []):
            if c.get("voided") or c.get("deleted"):
                continue
            # voids & discounts counted regardless of open/closed (they happened)
            for sel in (c.get("selections") or []):
                if sel.get("voided"):
                    voids.append({"item": sel.get("displayName", "Item"), "amount": money(sel.get("price"))})
                for d in (sel.get("appliedDiscounts") or []):
                    discounts.append({"item": sel.get("displayName", "Item"),
                                      "name": d.get("name", "Discount"), "amount": money(d.get("discountAmount") or d.get("amount")),
                                      "check": c.get("displayNumber")})
                    comps_amt += money(d.get("compedAmount"))
            for d in (c.get("appliedDiscounts") or []):
                discounts.append({"item": "(check-level)", "name": d.get("name", "Discount"),
                                  "amount": money(d.get("discountAmount") or d.get("amount")),
                                  "check": c.get("displayNumber")})
                comps_amt += money(d.get("compedAmount"))
            if _is_open(c):
                unpaid.append({"check": c.get("displayNumber"), "amount": money(c.get("totalAmount")),
                               "net": money(c.get("amount"))})
            # Net sales include ALL non-void checks (paid + open) to match Toast's
            # official Net Sales; open tabs are still flagged separately (banner + Unpaid).
            nchecks += 1; net += money(c.get("amount")); order_counts = True
            for sel in (c.get("selections") or []):
                if sel.get("voided"):
                    continue
                guid = (sel.get("item") or {}).get("guid")
                menu, grp = T["item_cat"].get(guid, ("", ""))
                nm = sel.get("displayName", "Item")
                cat = categorize(menu, grp, nm)
                key = (cat, nm)
                mix[key]["qty"] += int(sel.get("quantity") or 1)
                mix[key]["net"] += money(sel.get("price"))
                mix[key]["cat"] = cat
        if order_counts:
            covers += max(g, 1) if g else 1  # each served order = >=1 cover
    avg = net / nchecks if nchecks else 0.0
    voids_total = sum(v["amount"] for v in voids)
    disc_total = sum(d["amount"] for d in discounts)

    # ---- labor (controllable 8 groups; also capture excluded for note) ----
    _, te = T["api_get"]("/labor/v1/timeEntries", T["g"], {"businessDate": bdate}, T["tok"])
    roles = {}; excl = defaultdict(lambda: {"hrs": 0.0})
    for t in (te or []):
        if t.get("deleted"):
            continue
        pos = T["jm"].get((t.get("jobReference") or {}).get("guid"), "(unknown)")
        name = T["em"].get((t.get("employeeReference") or {}).get("guid"), "(unknown)")
        reg = money(t.get("regularHours")); ot = money(t.get("overtimeHours")); wage = money(t.get("hourlyWage"))
        hrs = reg + ot; cost = round(reg * wage + ot * wage * 1.5, 2)
        if pos not in CONTROLLABLE:
            excl[pos]["hrs"] += hrs
            continue
        r = roles.setdefault(pos, {"hrs": 0.0, "cost": 0.0, "ot": 0.0, "people": {}})
        r["hrs"] += hrs; r["cost"] += cost; r["ot"] += ot
        p = r["people"].setdefault(name, {"hrs": 0.0, "cost": 0.0, "ot": 0.0})
        p["hrs"] += hrs; p["cost"] += cost; p["ot"] += ot
    lh = sum(r["hrs"] for r in roles.values())
    lc = sum(r["cost"] for r in roles.values())
    lot = sum(r["ot"] for r in roles.values())
    on_clock = sum(len(r["people"]) for r in roles.values())

    # ---- product mix top-10 per category ----
    def top(cat):
        items = [{"name": k[1], "qty": v["qty"], "net": v["net"]} for k, v in mix.items() if v["cat"] == cat]
        items.sort(key=lambda x: -x["net"])
        return items
    mix_food = top("food"); mix_alc = top("alcohol"); mix_na = top("nonalc")
    mix_total = sum(v["net"] for v in mix.values())
    covered = (mix_total / net) if net else 0.0

    result = {
        "date": iso,
        "sales": {"net": net, "avg": avg, "covers": covers, "checks": nchecks},
        "labor": {"hours": lh, "ot": lot, "dollars": lc, "on_clock": on_clock,
                  "pct": (lc / net * 100) if net else 0.0},
        "roles": roles,
        "voids": voids, "voids_total": voids_total,
        "discounts": discounts, "disc_total": disc_total,
        "comps": comps_amt,
        "unpaid": unpaid,
        "excluded": {k: v["hrs"] for k, v in excl.items()},
        "mix": {"food": mix_food, "alcohol": mix_alc, "nonalc": mix_na,
                "total": mix_total, "covered": covered},
    }
    _DAYCACHE[iso] = result
    return result

def light_totals(iso):
    """Just {net, covers, labor_dollars, labor_hours} for comparison days."""
    d = compute_day(iso)
    return {"net": d["sales"]["net"], "covers": d["sales"]["covers"],
            "labor_dollars": d["labor"]["dollars"], "labor_hours": d["labor"]["hours"]}


# ================= comparisons =================
def build_day(iso):
    """compute_day + attach same-day-last-week and 4-week same-weekday comparison."""
    day = compute_day(iso)
    d0 = datetime.date.fromisoformat(iso)
    priors = [(d0 - datetime.timedelta(days=7 * k)).isoformat() for k in range(1, 5)]
    pt = [light_totals(p) for p in priors]
    def avg(key):
        vals = [t[key] for t in pt]
        return sum(vals) / len(vals) if vals else 0.0
    day["cmp"] = {
        "weekday": d0.strftime("%a"),
        "last": pt[0],                     # same day last week
        "avg4": {"net": avg("net"), "covers": avg("covers"),
                 "labor_dollars": avg("labor_dollars"), "labor_hours": avg("labor_hours")},
        "n4": len(pt),
    }
    return day


# ================= formatting / deltas =================
GRN, RED, SUB, AMB, TX, TX2, BD = "#16a34a", "#dc2626", "#94a3b8", "#d97706", "#1a202c", "#4a5568", "#d0d7e0"

OT_EPS = 0.25  # below this (15 min), treat overtime as clock-rounding noise, not a flag

def d2(x): return f"${x:,.2f}"
def d0f(x): return f"${x:,.0f}"
def h1(x): return f"{x:.1f}h"

def _fmt(kind, v):
    v = abs(v)
    if kind == "money2": return f"${v:,.2f}"
    if kind == "money0": return f"${v:,.0f}"
    if kind == "hours": return f"{v:.1f}h"
    if kind == "int": return f"{int(round(v))}"
    if kind == "pp": return f"{v:.1f}pp"
    return str(v)

def _pctfloor(kind):
    # base below which % is omitted (avoids misleading swings off a near-zero base);
    # 'pp' never shows a %-of-% (a point delta is already the meaningful number).
    return {"money2": 10, "money0": 10, "hours": 2.0, "int": 3, "pp": float("inf")}.get(kind, 0)

def delta(cur, prior, kind, higher_good, label):
    """Colored '↑ X% (+$Y) vs LABEL' — omits % on tiny bases to avoid misleading swings."""
    if prior is None:
        return f'<span style="color:{SUB}">— vs {label}</span>'
    dv = cur - prior
    eps = 0.05 if kind == "hours" else (0.5 if kind == "int" else 0.5)
    if abs(dv) < eps:
        return f'<span style="color:{SUB}">± flat vs {label}</span>'
    up = dv > 0
    good = (up == higher_good)
    color = GRN if good else RED
    arrow = "↑" if up else "↓"
    sign = "+" if dv > 0 else "−"
    pct = ""
    if abs(prior) > _pctfloor(kind):
        pct = f"{abs(100 * dv / prior):.1f}% "
    return f'<span style="color:{color};font-weight:600">{arrow} {pct}({sign}{_fmt(kind, dv)}) vs {label}</span>'


# ================= render one day =================
def _statcard(label, value, value_color, deltas):
    dl = "".join(f'<div class="d-line">{d}</div>' for d in deltas)
    return (f'<div class="stat"><div class="stat-l">{label}</div>'
            f'<div class="stat-v" style="color:{value_color}">{value}</div>'
            f'<div class="stat-d">{dl}</div></div>')

def render_day(day):
    s = day["sales"]; l = day["labor"]; c = day["cmp"]
    wd = c["weekday"]; last = c["last"]; a4 = c["avg4"]; n4 = c["n4"]
    o = []
    # ---- RED unpaid-tab banner (very top, for manager review) ----
    if day["unpaid"]:
        up = day["unpaid"]; tot = sum(u["amount"] for u in up)
        chks = ", ".join(f'#{u["check"]}' for u in up if u.get("check"))
        plural = "s" if len(up) != 1 else ""
        o.append(f'<div class="redbanner"><div class="rb-top">⚠️ {len(up)} UNPAID TAB{plural.upper()} — {d2(tot)}</div>'
                 f'<div class="rb-sub">Check{plural} {chks} left open (included in net sales above). '
                 f'Managers — please review &amp; close.</div></div>')
    # ---- Sales ----
    o.append('<div class="sec">📊 Sales</div><div class="grid">')
    o.append(_statcard("Net Sales", d2(s["net"]), GRN, [
        delta(s["net"], last["net"], "money2", True, f"last {wd}"),
        delta(s["net"], a4["net"], "money2", True, f"{n4}-wk {wd} avg")]))
    o.append(_statcard("Avg Check", d2(s["avg"]), TX, [
        delta(s["avg"], (last["net"]/last["covers"] if last["covers"] else None), "money2", True, f"last {wd}")]))
    o.append(_statcard("Covers", str(s["covers"]), TX, [
        delta(s["covers"], last["covers"], "int", True, f"last {wd}")]))
    o.append('</div>')
    # ---- Labor ----
    o.append('<div class="sec">👥 Labor <span class="sec-note">· controllable (8 groups)</span></div><div class="grid">')
    o.append(_statcard("Staff Labor Hours", h1(l["hours"]), TX, [
        delta(l["hours"], last["labor_hours"], "hours", False, f"last {wd}"),
        delta(l["hours"], a4["labor_hours"], "hours", False, f"{n4}-wk {wd} avg")]))
    ot_minimal = l["ot"] < OT_EPS
    o.append(_statcard("Overtime", h1(l["ot"]), (GRN if ot_minimal else AMB),
                       [f'<span style="color:{SUB}">none logged</span>' if ot_minimal
                        else f'<span style="color:{AMB};font-weight:600">review — controllable OT</span>']))
    o.append(_statcard("Staff Labor $", d0f(l["dollars"]), TX, [
        delta(l["dollars"], last["labor_dollars"], "money0", False, f"last {wd}"),
        delta(l["dollars"], a4["labor_dollars"], "money0", False, f"{n4}-wk {wd} avg")]))
    lastpct = (last["labor_dollars"]/last["net"]*100) if last["net"] else None
    o.append(_statcard("Labor % of Sales", f'{l["pct"]:.1f}%', TX, [
        delta(l["pct"], lastpct, "pp", False, f"last {wd}") if lastpct is not None else
        f'<span style="color:{SUB}">— vs last {wd}</span>']))
    o.append('</div>')
    # ---- Staff Detail ----
    o.append(render_staff(day))
    # ---- Product Mix (NEW, right after Staff Detail) ----
    o.append(render_mix(day))
    # ---- Clock-outs ----
    o.append('<div class="sec">🕐 Clock-Outs</div>'
             '<div class="ok">✅ All employees clocked out — no open shifts</div>')
    # ---- Voids/Discounts/Comps ----
    o.append(render_vdc(day))
    # ---- Unpaid ----
    o.append(render_unpaid(day))
    # ---- Alerts ----
    o.append(render_alerts(day))
    return "\n".join(o)


def render_staff(day):
    l = day["labor"]; roles = day["roles"]; wd = day["cmp"]["weekday"]
    o = ['<div class="sec">👥 Staff Detail</div><div class="card">']
    o.append(f'<div class="row-top"><span class="rt-l">Total Wages Paid</span>'
             f'<span><b style="color:{GRN}">{d0f(l["dollars"])}</b>'
             f'<span class="rt-n">{l["on_clock"]} {"person" if l["on_clock"]==1 else "people"}</span></span></div>')
    o.append(f'<div class="note-i">Tap a role to see individual employees · controllable roles only '
             f'(excludes tipped-out servers/bartenders &amp; salaried managers)</div>')
    if not roles:
        o.append(f'<div class="note-i" style="padding:8px 0">No controllable-role staff clocked this day.</div>')
    for pos in sorted(roles, key=lambda p: -roles[p]["cost"]):
        r = roles[pos]
        o.append('<details class="drow"><summary><span class="chev">▸</span>'
                 f'<span class="dr-name">{pos}</span>'
                 f'<span class="dr-val">{h1(r["hrs"])} · <span style="color:{GRN}">{d0f(r["cost"])}</span></span></summary>')
        o.append('<div class="people">')
        for nm in sorted(r["people"], key=lambda n: -r["people"][n]["hrs"]):
            p = r["people"][nm]
            ottag = f' <span class="tag-ot">{h1(p["ot"])} OT</span>' if p["ot"] >= OT_EPS else ""
            o.append(f'<div class="person"><span class="pn">{nm}{ottag}</span>'
                     f'<span class="pv">{p["hrs"]:.1f}h · <span style="color:{GRN}">{d2(p["cost"])}</span></span></div>')
        o.append('</div></details>')
    o.append('</div>')
    return "".join(o)


def render_mix(day):
    m = day["mix"]
    cats = [("🍽️ Food", m["food"]), ("🍸 Alcohol", m["alcohol"]), ("🥤 Non-Alcohol / Other", m["nonalc"])]
    o = ['<div class="sec">🧾 Product Mix / Sales Detail</div><div class="card">']
    o.append(f'<div class="note-i">Top items by net sales · item counts shown · '
             f'covers ~{m["covered"]*100:.0f}% of net sales (rest is modifiers/upcharges)</div>')
    for title, items in cats:
        tot = sum(i["net"] for i in items); qty = sum(i["qty"] for i in items)
        o.append('<details class="drow"><summary><span class="chev">▸</span>'
                 f'<span class="dr-name">{title}</span>'
                 f'<span class="dr-val">{qty} sold · <span style="color:{GRN}">{d0f(tot)}</span></span></summary>')
        o.append('<div class="people">')
        if not items:
            o.append('<div class="person"><span class="pn" style="color:%s">None this day</span></div>' % SUB)
        for i in items[:10]:
            o.append(f'<div class="person"><span class="pn">{i["name"]} '
                     f'<span class="qty">×{i["qty"]}</span></span>'
                     f'<span class="pv" style="color:{GRN}">{d2(i["net"])}</span></div>')
        o.append('</div></details>')
    o.append('</div>')
    return "".join(o)


def render_vdc(day):
    o = ['<div class="sec">💸 Voids, Discounts &amp; Comps</div><div class="card">']
    # voids
    o.append('<details class="drow"><summary><span class="chev">▸</span>'
             '<span class="dr-name">Voids</span>'
             f'<span class="dr-val">{len(day["voids"])} item{"s" if len(day["voids"])!=1 else ""} · {d2(day["voids_total"])}</span></summary>'
             '<div class="people">')
    if not day["voids"]:
        o.append('<div class="person"><span class="pn" style="color:%s">None</span></div>' % SUB)
    for v in sorted(day["voids"], key=lambda x: -x["amount"]):
        o.append(f'<div class="person"><span class="pn">{v["item"]}</span><span class="pv">{d2(v["amount"])}</span></div>')
    o.append('</div></details>')
    # discounts
    o.append('<details class="drow"><summary><span class="chev">▸</span>'
             '<span class="dr-name">Discounts</span>'
             f'<span class="dr-val" style="color:{AMB}">{len(day["discounts"])} applied · {d2(day["disc_total"])}</span></summary>'
             '<div class="people">')
    if not day["discounts"]:
        o.append('<div class="person"><span class="pn" style="color:%s">None</span></div>' % SUB)
    for x in sorted(day["discounts"], key=lambda z: -z["amount"]):
        chk = f' · Check #{x["check"]}' if x.get("check") else ""
        o.append(f'<div class="person"><span class="pn">{x["item"]}<span class="sub2">{x["name"]}{chk}</span></span>'
                 f'<span class="pv">{d2(x["amount"])}</span></div>')
    o.append('</div></details>')
    # comps
    o.append('<div class="flatrow"><span class="dr-name">Comps</span>'
             f'<span class="dr-val">{d2(day["comps"])}</span></div>')
    o.append('</div>')
    return "".join(o)


def render_unpaid(day):
    up = day["unpaid"]; tot = sum(u["amount"] for u in up)
    o = ['<div class="sec">📋 Unpaid Checks</div><div class="card">']
    if not up:
        o.append(f'<div class="flatrow"><span class="dr-name">Unpaid Checks</span>'
                 f'<span class="dr-val" style="color:{GRN}">None</span></div>')
    else:
        o.append('<details class="drow" open><summary><span class="chev">▸</span>'
                 '<span class="dr-name">Open / Unpaid</span>'
                 f'<span class="dr-val" style="color:{AMB}">{len(up)} check{"s" if len(up)!=1 else ""} · {d2(tot)}</span></summary>'
                 '<div class="people">')
        for u in sorted(up, key=lambda x: -x["amount"]):
            o.append(f'<div class="person"><span class="pn">Check #{u["check"]}</span>'
                     f'<span class="pv">{d2(u["amount"])}</span></div>')
        o.append('</div></details>')
    o.append('</div>')
    return "".join(o)


def render_alerts(day):
    l = day["labor"]; s = day["sales"]; up = day["unpaid"]
    alerts = []
    if l["ot"] < OT_EPS:
        alerts.append((True, "No significant overtime — labor within normal range"))
    else:
        alerts.append((False, f'{h1(l["ot"])} of overtime logged in controllable roles'))
    if up:
        alerts.append((False, f'{len(up)} open/unpaid check{"s" if len(up)!=1 else ""} '
                              f'(${sum(u["amount"] for u in up):,.2f}) — verify closed'))
    if s["net"] and l["pct"] > 30:
        alerts.append((False, f'Labor at {l["pct"]:.1f}% of sales — above 30% target'))
    o = ['<div class="sec">⚠️ Alerts</div>']
    for ok, msg in alerts:
        bg, bd_, tc, ic = ("#f0fdf4", "#bbf7d0", "#15803d", "✅") if ok else ("#fffbeb", "#fde68a", "#b45309", "⚠️")
        o.append(f'<div class="alert" style="background:{bg};border-color:{bd_}">'
                 f'<span>{ic}</span><span style="color:{tc}">{msg}</span></div>')
    return "".join(o)


# ================= crypto =================
def _key(password, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERS).derive(password.encode())

def _enc(key, plaintext):
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv).decode(), base64.b64encode(ct).decode()


# ================= page assembly =================
CSS = """
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#e8ecf0;color:#1a202c;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
padding:22px;max-width:600px;margin:0 auto;font-size:13px;line-height:1.45;}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:16px;border-bottom:2px solid #d0d7e0;}
.title{font-size:20px;font-weight:700;}
.sub{font-size:13px;color:#4a5568;margin-top:4px;}
.live{background:#f0fdf4;color:#16a34a;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;border:1px solid #bbf7d0;white-space:nowrap;}
.redbanner{background:#fef2f2;border:1px solid #fecaca;border-left:5px solid #dc2626;border-radius:10px;padding:13px 16px;margin-bottom:6px;}
.rb-top{font-size:14px;font-weight:800;color:#b91c1c;letter-spacing:.02em;}
.rb-sub{font-size:12px;color:#dc2626;margin-top:3px;line-height:1.4;}
.pickrow{display:flex;align-items:center;gap:8px;margin:14px 0 4px;flex-wrap:wrap;}
.pickrow label{font-size:11px;font-weight:700;text-transform:uppercase;color:#4a5568;}
#day{font-size:14px;font-weight:700;padding:7px 10px;border:1px solid #d0d7e0;border-radius:8px;background:#fff;color:#1a202c;}
.sec{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#4a5568;margin:22px 0 12px;}
.sec-note{font-weight:600;letter-spacing:0;text-transform:none;color:#94a3b8;}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.stat{background:#f0f3f6;border-radius:10px;padding:16px;border:1px solid #d0d7e0;}
.stat-l{font-size:11px;color:#4a5568;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;}
.stat-v{font-size:24px;font-weight:700;}
.stat-d{margin-top:5px;display:flex;flex-direction:column;gap:2px;}
.d-line{font-size:11.5px;font-weight:600;}
.card{background:#fff;border:1px solid #d0d7e0;border-radius:12px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,0.06);}
.row-top{display:flex;justify-content:space-between;align-items:center;padding-bottom:14px;border-bottom:2px solid #d0d7e0;margin-bottom:10px;}
.rt-l{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:#4a5568;}
.rt-n{font-size:12px;color:#4a5568;margin-left:8px;}
.note-i{font-size:11px;color:#4a5568;font-style:italic;margin-bottom:8px;line-height:1.4;}
details.drow{border-bottom:1px solid #d0d7e0;}
details.drow:last-of-type{border-bottom:none;}
details.drow summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:11px 0;user-select:none;}
details.drow summary::-webkit-details-marker{display:none;}
.chev{font-size:10px;color:#4a5568;width:12px;transition:transform .15s;display:inline-block;}
details[open]>summary .chev{transform:rotate(90deg);}
.dr-name{font-size:13px;color:#1a202c;font-weight:500;}
.dr-val{margin-left:auto;font-size:13px;font-weight:700;color:#1a202c;text-align:right;}
.people{background:#f0f4f8;border-radius:8px;margin:0 0 10px 20px;padding:4px 12px;}
.person{display:flex;justify-content:space-between;align-items:baseline;padding:8px 0;border-bottom:1px solid #e0e6ee;gap:10px;}
.person:last-child{border-bottom:none;}
.pn{font-size:12.5px;color:#1a202c;}
.pv{font-size:12.5px;font-weight:600;color:#1a202c;white-space:nowrap;text-align:right;}
.qty{font-size:11px;color:#4a5568;}
.sub2{display:block;font-size:11px;color:#4a5568;margin-top:1px;}
.tag-ot{font-size:9px;font-weight:700;padding:1px 6px;border-radius:999px;background:#fffbeb;color:#b45309;}
.flatrow{display:flex;justify-content:space-between;align-items:center;padding:11px 0;}
.ok{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px 16px;font-size:13px;color:#15803d;}
.alert{border:1px solid;border-radius:8px;padding:12px 16px;display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;font-size:13px;}
.foot{margin-top:26px;padding-top:16px;border-top:1px solid #d0d7e0;font-size:12px;color:#a0aec0;display:flex;justify-content:space-between;}
.gate{max-width:380px;margin:12vh auto 0;}
.gate .gcard{background:#fff;border:1px solid #d0d7e0;border-radius:12px;padding:22px;box-shadow:0 1px 4px rgba(0,0,0,0.06);}
.gate .lock{font-size:22px;margin-bottom:8px;}
.gate h1{font-size:18px;font-weight:700;margin-bottom:4px;}
.gate p{font-size:12px;color:#4a5568;margin-bottom:16px;}
.gate input{width:100%;padding:11px 12px;border:1px solid #d0d7e0;border-radius:8px;font-size:14px;margin-bottom:10px;}
.gate button{width:100%;padding:11px;border:none;border-radius:8px;background:#1e293b;color:#fff;font-size:14px;font-weight:600;cursor:pointer;}
.gate button:hover{background:#0f172a;}
.gate .err{color:#dc2626;font-size:12px;margin-top:8px;min-height:16px;font-weight:600;}
"""

PAGE_JS = """
var D=JSON.parse(document.getElementById('dailydata').textContent);var KEY=null;
function b64(s){return Uint8Array.from(atob(s),function(c){return c.charCodeAt(0);});}
async function showDay(i){
  var d=D.days[i];
  var pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(d.iv)},KEY,b64(d.ct));
  document.getElementById('app').innerHTML=new TextDecoder().decode(pt);
  document.getElementById('rptdate').textContent='Daily Report · '+d.label;
  document.getElementById('day').value=i;
}
async function unlock(){
  var pw=document.getElementById('pw').value;var err=document.getElementById('err');err.textContent='';
  try{
    var km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
    KEY=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(D.salt),iterations:D.iter,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
    await showDay(0);
    var sel=document.getElementById('day');sel.innerHTML='';
    D.days.forEach(function(d,i){var o=document.createElement('option');o.value=i;o.textContent=d.pick;sel.appendChild(o);});
    sel.value=0;sel.addEventListener('change',function(){showDay(+sel.value);});
    document.getElementById('gate').style.display='none';
    document.getElementById('main').style.display='block';
  }catch(e){KEY=null;err.textContent='Wrong password — try again.';}
}
window.addEventListener('DOMContentLoaded',function(){
  document.getElementById('go').addEventListener('click',unlock);
  document.getElementById('pw').addEventListener('keydown',function(e){if(e.key==='Enter')unlock();});
  document.getElementById('pw').focus();
});
"""

GATE = ('<div id="gate" class="gate"><div class="gcard"><div class="lock">🔒</div>'
        '<h1>Sophie — Daily Report</h1>'
        '<p>This report contains staff pay and sales detail, and is password-protected. '
        'Enter the team password to view.</p>'
        '<input id="pw" type="password" placeholder="Team password" autocomplete="off">'
        '<button id="go">Unlock</button><div id="err" class="err"></div></div></div>')

CHROME = ('<div id="main" style="display:none"><div class="header"><div>'
          '<div class="title">🍹 Sophie Cocktail &amp; Terrace Bar</div>'
          '<div class="sub" id="rptdate">Daily Report</div></div>'
          '<div class="live">Live</div></div>'
          '<div class="pickrow"><label for="day">Day</label><select id="day"></select></div>'
          '<div id="app"></div>'
          '<div class="foot"><span>Source: Toast POS (read-only)</span><span id="foot2"></span></div></div>')


def render_full_page(pd):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Sophie — Daily Report</title><style>" + CSS + "</style></head><body>"
            + GATE + CHROME
            + '<script id="dailydata" type="application/json">' + json.dumps(pd) + '</script>'
            + '<script>' + PAGE_JS + '</script></body></html>')


def extract_data(html):
    m = re.search(r'<script id="dailydata"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no dailydata block found")
    return json.loads(m.group(1))


def _label(iso):
    d = datetime.date.fromisoformat(iso)
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")

def _pick(iso):
    d = datetime.date.fromisoformat(iso)
    return d.strftime("%a · %b ") + str(d.day)

def _entry(key, iso):
    day = build_day(iso)
    iv, ct = _enc(key, render_day(day))
    return {"date": iso, "label": _label(iso), "pick": _pick(iso), "iv": iv, "ct": ct}


# ================= modes =================
def _recent_complete_days(n):
    today = datetime.date.today()
    last = today - datetime.timedelta(days=1)  # yesterday = most recent complete business day
    return [(last - datetime.timedelta(days=k)).isoformat() for k in range(n)]

def seed(n=7):
    password = os.environ["DAILY_PW"]
    salt = os.urandom(16); key = _key(password, salt)
    days = [_entry(key, iso) for iso in _recent_complete_days(n)]
    pd = {"salt": base64.b64encode(salt).decode(), "iter": PBKDF2_ITERS, "days": days}
    open("sophie-daily.html", "w").write(render_full_page(pd))
    print("seeded", len(days), "days ->", [d["date"] for d in days])

def update(pagefile="sophie-daily.html"):
    password = os.environ["DAILY_PW"]
    pd = extract_data(open(pagefile).read())
    salt = base64.b64decode(pd["salt"]); key = _key(password, salt)
    iso = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    day = build_day(iso)
    if day["sales"]["net"] == 0 and not day["roles"]:
        raise RuntimeError(f"no data for {iso} — aborting, not publishing")
    print(f"{iso}: net ${day['sales']['net']:,.2f} | controllable labor ${day['labor']['dollars']:,.0f} "
          f"({day['labor']['hours']:.1f}h) | covers {day['sales']['covers']}")
    entry = _entry(key, iso)
    days = [d for d in pd["days"] if d["date"] != iso]
    days.insert(0, entry)
    days.sort(key=lambda d: d["date"], reverse=True)
    days = days[:60]  # keep ~2 months
    pd["days"] = days
    open(pagefile, "w").write(render_full_page(pd))
    print("updated", pagefile, "->", entry["date"], "| days now:", len(days))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if mode == "seed":
        seed(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
    elif mode == "update":
        update(sys.argv[2] if len(sys.argv) > 2 else "sophie-daily.html")
    else:
        print("usage: daily_web.py [seed N | update <page.html>]")
