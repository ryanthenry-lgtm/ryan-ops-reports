"""Sophie weekly labor — cloud-native, multi-week, password-gated page.

ONE file (sophie-labor.html) holds every week's report as a separate encrypted
block plus a week-picker. Everything the weekly run needs is in the repo, so it
runs fully in the cloud (no device bridge / no Mac).

Modes:
  seed                     build sophie-labor.html from labor_data.json (this+prior weeks)
  update [sophie-labor.html]  pull the last COMPLETE Mon-Sun week (+prior) from Toast,
                              render it, and add/replace it in the page (in place)

Env: LABOR_PW (required). For 'update': toast_lib.py + creds.json must be present.
"""
import json, os, base64, sys, datetime, re
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERS = 200000
POSITIONS = ["Hostess", "Server Assistant", "Barback", "Runner",
             "Line Cook", "Prep Cook", "Dishwasher", "Porter"]
SALARIED = {"Operations Manager", "General Manager", "Owner"}  # excluded from POSITIONS now; kept for safety


# ---------------- formatting ----------------
def h(x): return f"{x:.1f}"
def d0(x): return f"${x:,.0f}"

def delta_html(cur, prior, kind):
    fmt = (lambda x: f"{h(x)}h") if kind == "h" else (lambda x: d0(x))
    thresh = 0.05 if kind == "h" else 0.5
    dv = cur - prior
    if abs(dv) < thresh:
        return '<span class="delta flat">±0</span>'
    up = dv > 0
    good = not up  # for hours & cost, lower = better
    arrow = "▲" if up else "▼"
    return f'<span class="delta {"good" if good else "bad"}">{arrow} {fmt(abs(dv))}</span>'


def wk_label(mon_iso):
    a = datetime.date.fromisoformat(mon_iso); b = a + datetime.timedelta(days=6)
    if a.month == b.month:
        return f"{a.strftime('%b')} {a.day}–{b.day}"
    return f"{a.strftime('%b')} {a.day} – {b.strftime('%b')} {b.day}"


def today_str():
    t = datetime.date.today()
    return f"{t.strftime('%b')} {t.day}, {t.year}"


# ---------------- aggregation ----------------
def aggregate_days(days):
    """days: list of {entries:[{position,name,reg,ot,wage_cost}]} -> {pos:{name:{reg,ot,hours,cost}}}"""
    agg = {}
    for d in days:
        for e in d["entries"]:
            if e["position"] not in POSITIONS:
                continue
            a = agg.setdefault(e["position"], {}).setdefault(e["name"], {"reg": 0., "ot": 0., "hours": 0., "cost": 0.})
            a["reg"] += e["reg"]; a["ot"] += e["ot"]; a["hours"] += e["reg"] + e["ot"]; a["cost"] += e["wage_cost"]
    return agg


def pull_week(monday):
    """Aggregate one Mon-Sun week straight from Toast -> {pos:{name:{reg,ot,hours,cost}}}."""
    from toast_lib import RESTAURANTS, api_get, get_token
    g = RESTAURANTS["Sophie"]; tok = get_token()
    _, jobs = api_get("/labor/v1/jobs", g, None, tok)
    jm = {j.get("guid"): j.get("title") for j in (jobs or [])}
    _, emps = api_get("/labor/v1/employees", g, None, tok)

    def enm(e):
        nm = e.get("chosenName") or ", ".join(x for x in [e.get("lastName"), e.get("firstName")] if x)
        return (nm or "").strip()
    em = {e.get("guid"): enm(e) for e in (emps or [])}
    agg = {}
    for i in range(7):
        d = monday + datetime.timedelta(days=i)
        _, te = api_get("/labor/v1/timeEntries", g, {"businessDate": d.strftime("%Y%m%d")}, tok)
        for t in (te or []):
            if t.get("deleted"):
                continue
            pos = jm.get((t.get("jobReference") or {}).get("guid"), "(unknown)")
            if pos not in POSITIONS:
                continue
            name = em.get((t.get("employeeReference") or {}).get("guid"), "(unknown)")
            reg = float(t.get("regularHours") or 0); ot = float(t.get("overtimeHours") or 0)
            wage = float(t.get("hourlyWage") or 0)
            a = agg.setdefault(pos, {}).setdefault(name, {"reg": 0., "ot": 0., "hours": 0., "cost": 0.})
            a["reg"] += reg; a["ot"] += ot; a["hours"] += reg + ot; a["cost"] += round(reg * wage + ot * wage * 1.5, 2)
    return agg


# ---------------- render one week's body ----------------
def _totals(agg):
    hh = cc = 0.0
    for pos, ppl in agg.items():
        for a in ppl.values():
            hh += a["hours"]
            if pos not in SALARIED:
                cc += a["cost"]
    return hh, cc


def render_week(cur, prior):
    """cur/prior: {pos:{name:{reg,ot,hours,cost}}}; prior may be None (first week)."""
    has_prior = prior is not None
    P = prior if has_prior else {}
    ch, cc = _totals(cur); ph, pc = _totals(P)
    o = ['<div class="card summary"><div class="card-label">Controllable labor (these 8 groups)</div><div class="kpis">']
    hsub = f'vs {h(ph)} {delta_html(ch, ph, "h")}' if has_prior else '<span class="muted">no prior week</span>'
    csub = f'vs {d0(pc)} {delta_html(cc, pc, "$")}' if has_prior else '<span class="muted">no prior week</span>'
    o.append(f'<div class="kpi"><div class="kpi-label">Hours</div><div class="kpi-val">{h(ch)}</div><div class="kpi-sub">{hsub}</div></div>')
    o.append(f'<div class="kpi"><div class="kpi-label">Wage cost</div><div class="kpi-val">{d0(cc)}</div><div class="kpi-sub">{csub}</div></div>')
    o.append('</div><div class="note">Excludes bartenders &amp; servers (paid via tip-out) and salaried managers. '
             'Wage cost = hours × rate (+ OT×1.5).</div></div>')
    o.append('<div class="section-label">Labor by work group — ranked by cost · tap to see people</div>')

    allpos = set(cur) | set(P)

    def poscost(pos):
        return sum(a["cost"] for a in cur.get(pos, {}).values())
    for pos in sorted(allpos, key=lambda p: -poscost(p)):
        cP = cur.get(pos, {}); pP = P.get(pos, {})
        cph = sum(a["hours"] for a in cP.values()); cpc = sum(a["cost"] for a in cP.values())
        pph = sum(a["hours"] for a in pP.values()); ppc = sum(a["cost"] for a in pP.values())
        salaried = pos in SALARIED
        names = set(cP) | set(pP)
        npeople = sum(1 for nm in names if cP.get(nm, {}).get("hours", 0) > 0 or pP.get(nm, {}).get("hours", 0) > 0)
        o.append('<details class="card pos"><summary><span class="chev">▸</span>')
        o.append(f'<span class="pos-name">{pos}</span><span class="pos-count">{npeople}</span><span class="pos-stats">')
        hstat = f'<span class="muted">vs {h(pph)}h</span> {delta_html(cph, pph, "h")}' if has_prior else ''
        o.append(f'<span class="stat"><span class="stat-big">{h(cph)}h</span> {hstat}</span>')
        if salaried:
            o.append('<span class="stat"><span class="stat-big salaried">salaried</span></span>')
        else:
            cstat = f'<span class="muted">vs {d0(ppc)} {delta_html(cpc, ppc, "$")}</span>' if has_prior else ''
            o.append(f'<span class="stat"><span class="stat-big">{d0(cpc)}</span> {cstat}</span>')
        o.append('</span></summary><div class="people">')
        for nm in sorted(names, key=lambda n: (-cP.get(n, {}).get("hours", 0), -pP.get(n, {}).get("hours", 0))):
            th = cP.get(nm, {}).get("hours", 0.); ph_ = pP.get(nm, {}).get("hours", 0.)
            tc = cP.get(nm, {}).get("cost", 0.); pc_ = pP.get(nm, {}).get("cost", 0.)
            ot = cP.get(nm, {}).get("ot", 0.)
            tags = ''
            if has_prior:
                if th > 0 and ph_ == 0:
                    tags += ' <span class="tag new">new</span>'
                if th == 0 and ph_ > 0:
                    tags += ' <span class="tag gone">not in</span>'
            if ot > 0.05:
                tags += f' <span class="tag ot">{h(ot)}h OT</span>'
            o.append(f'<div class="person"><div class="pname">{nm}{tags}</div><div class="pstats">')
            hh = f'<span class="muted">vs {h(ph_)}</span> {delta_html(th, ph_, "h")}' if has_prior else ''
            o.append(f'<span class="pstat"><span class="pl">Hrs</span> {h(th)} {hh}</span>')
            if salaried:
                o.append('<span class="pstat"><span class="pl">Cost</span> <span class="muted">salaried</span></span>')
            else:
                cc2 = f'<span class="muted">vs {d0(pc_)}</span> {delta_html(tc, pc_, "$")}' if has_prior else ''
                o.append(f'<span class="pstat"><span class="pl">Cost</span> {d0(tc)} {cc2}</span>')
            o.append('</div></div>')
        o.append('</div></details>')
    o.append('<div class="foot">Sophie Cocktail + Terrace Bar · source: Toast POS (read-only)</div>')
    return "\n".join(o)


# ---------------- crypto ----------------
def _key(password, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERS).derive(password.encode())


def _enc(key, plaintext):
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv).decode(), base64.b64encode(ct).decode()


# ---------------- page assembly ----------------
CSS = """
:root{--bg:#e8ecf0;--card:#f0f3f6;--card2:#f0f4f8;--bd:#d0d7e0;--tx:#1a202c;--tx2:#4a5568;
--grn:#16a34a;--red:#dc2626;--amb:#d97706;--live-bg:#f0fdf4;--live-bd:#bbf7d0;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
padding:20px;max-width:600px;margin:0 auto;font-size:13px;line-height:1.4;}
.header{margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid var(--bd);}
.title{font-size:20px;font-weight:700;}
.sub{font-size:12px;color:var(--tx2);margin-top:2px;}
.pickrow{margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.pickrow label{font-size:11px;font-weight:700;text-transform:uppercase;color:var(--tx2);}
#wk{font-size:14px;font-weight:700;padding:7px 10px;border:1px solid var(--bd);border-radius:8px;background:#fff;color:var(--tx);max-width:100%;}
.weekline{font-size:12px;color:var(--tx2);margin-top:8px;}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);}
.card-label{font-size:11px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:var(--tx2);margin-bottom:10px;}
.summary .kpis{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.kpi-label{font-size:11px;color:var(--tx2);font-weight:600;}
.kpi-val{font-size:24px;font-weight:700;margin-top:2px;}
.kpi-sub{font-size:11px;color:var(--tx2);margin-top:2px;}
.note{font-size:11px;color:var(--tx2);margin-top:12px;line-height:1.45;}
.section-label{font-size:11px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:var(--tx2);margin:16px 2px 8px;}
details.pos{padding:0;overflow:hidden;}
details.pos summary{list-style:none;cursor:pointer;padding:14px 16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
details.pos summary::-webkit-details-marker{display:none;}
.chev{color:var(--tx2);font-size:12px;transition:transform .15s;display:inline-block;}
details[open] .chev{transform:rotate(90deg);}
.pos-name{font-weight:700;font-size:14px;}
.pos-count{font-size:10px;font-weight:700;color:#64748b;background:#e5e9ef;border-radius:999px;padding:1px 7px;}
.pos-stats{margin-left:auto;display:flex;gap:14px;align-items:center;text-align:right;flex-wrap:wrap;justify-content:flex-end;}
.stat{font-size:12px;color:var(--tx2);white-space:nowrap;}
.stat-big{font-weight:700;color:var(--tx);font-size:13px;}
.stat-big.salaried{font-weight:600;color:#64748b;font-style:italic;font-size:12px;}
.muted{color:#94a3b8;}
.delta{font-weight:700;font-size:11px;}
.delta.good{color:var(--grn);}.delta.bad{color:var(--red);}.delta.flat{color:#94a3b8;}
.people{border-top:1px solid var(--bd);background:var(--card2);}
.person{padding:11px 16px 11px 32px;border-bottom:1px solid #e4e9ef;}
.person:last-child{border-bottom:none;}
.pname{font-weight:600;font-size:13px;margin-bottom:3px;}
.pstats{display:flex;gap:16px;flex-wrap:wrap;}
.pstat{font-size:12px;color:var(--tx);}
.pl{font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;margin-right:2px;}
.tag{font-size:9px;font-weight:700;padding:1px 6px;border-radius:999px;vertical-align:middle;}
.tag.new{background:#eff6ff;color:#1d4ed8;}.tag.gone{background:#fef2f2;color:#b91c1c;}.tag.ot{background:#fffbeb;color:#b45309;}
.foot{font-size:10px;color:#94a3b8;text-align:center;margin-top:16px;}
.gate{max-width:380px;margin:12vh auto 0;}
.gate .card{padding:22px;}
.gate h1{font-size:18px;font-weight:700;margin-bottom:4px;}
.gate p{font-size:12px;color:var(--tx2);margin-bottom:16px;}
.gate input{width:100%;padding:11px 12px;border:1px solid var(--bd);border-radius:8px;font-size:14px;margin-bottom:10px;}
.gate button{width:100%;padding:11px;border:none;border-radius:8px;background:#1e293b;color:#fff;font-size:14px;font-weight:600;cursor:pointer;}
.gate button:hover{background:#0f172a;}
.gate .err{color:var(--red);font-size:12px;margin-top:8px;min-height:16px;font-weight:600;}
.gate .lock{font-size:22px;margin-bottom:8px;}
"""

PAGE_JS = """
var D=JSON.parse(document.getElementById('labordata').textContent);
var KEY=null;
function b64(s){return Uint8Array.from(atob(s),function(c){return c.charCodeAt(0);});}
async function showWeek(i){
  var w=D.weeks[i];
  var pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(w.iv)},KEY,b64(w.ct));
  document.getElementById('app').innerHTML=new TextDecoder().decode(pt);
  var line='Week of <b>'+w.label+'</b>'+(w.prior?(' &nbsp;vs&nbsp; '+w.prior):' · first week, no prior')+' &nbsp;·&nbsp; updated '+D.weeks[0].built;
  document.getElementById('weekline').innerHTML=line;
  document.getElementById('wk').value=i;
}
async function unlock(){
  var pw=document.getElementById('pw').value; var err=document.getElementById('err'); err.textContent='';
  try{
    var km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
    KEY=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(D.salt),iterations:D.iter,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
    await showWeek(0);
    var sel=document.getElementById('wk'); sel.innerHTML='';
    D.weeks.forEach(function(w,i){var o=document.createElement('option');o.value=i;o.textContent=w.label;sel.appendChild(o);});
    sel.value=0;
    sel.addEventListener('change',function(){showWeek(+sel.value);});
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

GATE = ('<div id="gate" class="gate"><div class="card"><div class="lock">🔒</div>'
        '<h1>Sophie — Weekly Labor</h1>'
        '<p>This report contains staff pay and is password-protected. Enter the team password to view.</p>'
        '<input id="pw" type="password" placeholder="Team password" autocomplete="off">'
        '<button id="go">Unlock</button><div id="err" class="err"></div></div></div>')

CHROME = ('<div id="main" style="display:none"><div class="header">'
          '<div class="title">Sophie — Weekly Labor</div>'
          '<div class="sub">Support &amp; kitchen work groups · pick a week to view</div>'
          '<div class="pickrow"><label for="wk">Week</label><select id="wk"></select></div>'
          '<div class="weekline" id="weekline"></div></div><div id="app"></div></div>')


def render_full_page(pd):
    data_json = json.dumps(pd)
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Sophie — Weekly Labor</title><style>" + CSS + "</style></head><body>"
            + GATE + CHROME
            + '<script id="labordata" type="application/json">' + data_json + '</script>'
            + '<script>' + PAGE_JS + '</script></body></html>')


def extract_data(html):
    m = re.search(r'<script id="labordata"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no labordata block found in page")
    return json.loads(m.group(1))


def _entry(key, cur, prior, mon_iso, prior_iso):
    iv, ct = _enc(key, render_week(cur, prior))
    return {"week": mon_iso, "label": wk_label(mon_iso),
            "prior": wk_label(prior_iso) if prior_iso else "", "built": today_str(), "iv": iv, "ct": ct}


# ---------------- modes ----------------
def seed():
    password = os.environ["LABOR_PW"]
    DATA = json.load(open("labor_data.json"))
    cur = aggregate_days(DATA["weeks"]["this"]["days"])
    prior = aggregate_days(DATA["weeks"]["prior"]["days"])
    this_mon = DATA["weeks"]["this"]["monday"]; prior_mon = DATA["weeks"]["prior"]["monday"]
    salt = os.urandom(16); key = _key(password, salt)
    weeks = [_entry(key, cur, prior, this_mon, prior_mon),
             _entry(key, prior, None, prior_mon, None)]
    pd = {"salt": base64.b64encode(salt).decode(), "iter": PBKDF2_ITERS, "weeks": weeks}
    open("sophie-labor.html", "w").write(render_full_page(pd))
    print("seeded", len(weeks), "weeks ->", [w["label"] for w in weeks])


def update(pagefile="sophie-labor.html"):
    password = os.environ["LABOR_PW"]
    pd = extract_data(open(pagefile).read())
    salt = base64.b64decode(pd["salt"]); key = _key(password, salt)
    today = datetime.date.today()
    this_mon = today - datetime.timedelta(days=today.weekday() + 7)
    prior_mon = this_mon - datetime.timedelta(days=7)
    cur = pull_week(this_mon); prior = pull_week(prior_mon)
    if not cur:
        raise RuntimeError(f"no labor data for week {this_mon} — aborting, not publishing")
    th, tc = _totals(cur)
    print(f"TOTALS {this_mon} (9 groups): hours {th:.1f} | wage cost ${tc:,.0f}")
    entry = _entry(key, cur, prior, this_mon.isoformat(), prior_mon.isoformat())
    weeks = [w for w in pd["weeks"] if w["week"] != entry["week"]]
    weeks.insert(0, entry)
    # keep newest-first ordering
    weeks.sort(key=lambda w: w["week"], reverse=True)
    pd["weeks"] = weeks
    open(pagefile, "w").write(render_full_page(pd))
    print("updated", pagefile, "->", entry["label"], "| weeks now:", [w["label"] for w in weeks])


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if mode == "seed":
        seed()
    elif mode == "update":
        update(sys.argv[2] if len(sys.argv) > 2 else "sophie-labor.html")
    else:
        print("usage: labor_web.py [seed|update <page.html>]")
