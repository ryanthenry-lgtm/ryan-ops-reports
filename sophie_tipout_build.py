"""Build the Sophie weekly tip-out tool: 3 sections per day (Upstairs / Downstairs /
Support), Setup + 7 day tabs + Summary. EVERY server/bartender/support person who clocked
in is auto-pulled from Toast by JOB TITLE (new hires included automatically). Bartender
venue (up/down) = where they rang sales (revenue center 'Bar' = downstairs) -> the only bar
open that night -> home roster -> default upstairs (flagged orange for the manager to verify).
Anthony Aleman = special keep-all deal: keeps 100% own svc + tips, tips out to no one.

Model: servers keep 95% tips + 73% svc (15% ->bar, 12% ->support, 5% of tips ->support);
upstairs bartenders keep 95% tips + bar pool, tip 12% svc + 5% tips ->support;
downstairs bartenders keep 100% svc + 100% tips, tip 5% of NET SALES ->support;
support pool = servers/upstairs (12% svc + 5% tips) + downstairs (5% net sales),
level-loaded across SA+barbacks+runner.
"""
import sys, datetime
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from toast_lib import RESTAURANTS, api_get, get_token, BASE as BASEDIR

# Bartenders are INCLUDED by job title (below); these sets are only a VENUE HINT (up vs down)
# for the regulars. Anyone not listed is still pulled in — their venue is inferred from where
# they rang sales, else the open bar that night, else defaulted to upstairs and flagged.
UP_BAR = {"Makhotkin, Daniel", "Miller, Jonte", "Tzoc, Alejandro",
          "Maddie", "Alexanda", "Manley, Henry", "kiya-sabri", "Chris"}
DOWN_BAR = {"white, jamie", "Ferrell, Sarah", "Natalie", "Arias, Irina", "Ohearn, Emily",
            "Hadad-Barriga, Allison", "Levina, Diana"}
UP_TERM = {"Upstairs, Bar"}; DOWN_TERM = {"Downstairs, Bar"}
MGR_JOBS = {"Operations Manager", "General Manager", "Owner"}
KEEP_ALL = {"Aleman, Anthony"}   # special deal: keeps 100% of own svc + tips, no tip-out, no pool feed
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYNAME = dict(zip(DAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
WAGE = 2.13

# fixed row layout
R_SRV_HDR = 8; R_SRV0 = 9; NS = 12; R_SRV_TOT = R_SRV0 + NS; R_MGR = R_SRV_TOT + 1   # servers 9-20, tot 21, mgr 22
R_UB_HDR = R_SRV_TOT + 2; R_UB_COLS = R_UB_HDR + 1; R_UB0 = R_UB_COLS + 1  # 23,24,25
NUB = 12; R_UB_TOT = R_UB0 + NUB                                           # up bart rows (auto + manual add), tot after
R_DN_HDR = R_UB_TOT + 2; R_DN_AUTO = R_DN_HDR + 1; R_DN_COLS = R_DN_HDR + 2; R_DN0 = R_DN_COLS + 1  # 33,34,35,36
ND = 10; R_DN_TOT = R_DN0 + ND                                            # down bart rows (auto + manual add), tot after
R_SP_HDR = R_DN_TOT + 2; R_SP_POOL = R_SP_HDR + 1; R_SP_COLS = R_SP_POOL + 1; R_SP0 = R_SP_COLS + 1
NSP = 10; R_SP_TOT = R_SP0 + NSP                                          # support 49-58, tot 59
R_CHECK = R_SP_TOT + 1

CUR = '$#,##0.00'; H2 = '0.00'
ARIAL = "Arial"
HF = Font(name=ARIAL, size=10, bold=True, color="FFFFFF")
HFILL = PatternFill("solid", fgColor="1F3864")
SEC = Font(name=ARIAL, size=11, bold=True, color="FFFFFF")
SECFILL = PatternFill("solid", fgColor="2E5496")
BF = Font(name=ARIAL, size=10); BOLD = Font(name=ARIAL, size=10, bold=True)
NOTE = Font(name=ARIAL, size=9, italic=True, color="595959")
YEL = PatternFill("solid", fgColor="FFF2CC")
FLAG = PatternFill("solid", fgColor="FCE4D6")   # orange = bartender auto-added, venue not confirmed
GRAY = PatternFill("solid", fgColor="F2F2F2")
thin = Side(style="thin", color="D9D9D9"); BORD = Border(thin, thin, thin, thin)


def money(x): return float(x or 0)
def norm(s): return (s or "").strip()


def role_of(title, name):
    # Classify by JOB TITLE so every tipped worker is pulled in — no name roster needed for
    # inclusion. Bartenders get a generic "BAR" role; upstairs/downstairs is resolved in pull().
    if name in UP_TERM: return "UP_TERM"
    if name in DOWN_TERM: return "DOWN_TERM"
    if title in ("Bartender", "Default Bar", "Bar Lead"): return "BAR"
    if title == "Server": return "SERVER"
    if title == "Server Assistant": return "SA"
    if title in ("Barback", "Bar Back Prep"): return "BARBACK"
    if title == "Runner": return "RUNNER"
    return None


def pull(monday, tok, g, jm, em, down_rc=None):
    down_rc = down_rc or set()
    week = []
    for i in range(7):
        d = monday + datetime.timedelta(days=i)
        s, te = api_get("/labor/v1/timeEntries", g, {"businessDate": d.strftime("%Y%m%d")}, tok)
        agg = defaultdict(lambda: {"role": None, "hrs": 0.0, "tips": 0.0, "svc": 0.0})
        keepall = defaultdict(lambda: {"hrs": 0.0, "tips": 0.0, "svc": 0.0})
        mgr_svc = 0.0
        for t in (te or []):
            if t.get("deleted"): continue
            jr = t.get("jobReference"); jg = jr.get("guid") if isinstance(jr, dict) else jr
            er = t.get("employeeReference"); eg = er.get("guid") if isinstance(er, dict) else er
            title = jm.get(jg); nm = em.get(eg, norm(eg))
            hrs = money(t.get("regularHours")) + money(t.get("overtimeHours"))
            tips = money(t.get("nonCashTips")) + money(t.get("declaredCashTips"))
            svc = money(t.get("nonCashGratuityServiceCharges")) + money(t.get("cashGratuityServiceCharges"))
            if nm in KEEP_ALL:                 # special deal: keeps 100% of own svc + tips, tips out to no one
                k = keepall[nm]; k["hrs"] += hrs; k["tips"] += tips; k["svc"] += svc
                continue
            role = role_of(title, nm)
            if role is None:
                if title == "Hostess" and svc > 0:
                    role = "SERVER"            # server clocked as hostess -> treat sales as server sales
                elif title in MGR_JOBS and svc > 0:
                    mgr_svc += svc              # other-manager sales (rare): no longer fed to the pool
                    continue
                else:
                    continue
            a = agg[(nm, role)]; a["role"] = role
            a["hrs"] += hrs; a["tips"] += tips; a["svc"] += svc
        # split labor: servers / support / bartenders (venue TBD) / shared bar terminals
        servers, support = [], []
        bar_people = {}
        term_up_svc = term_up_tips = term_dn_svc = term_dn_tips = 0.0
        srv_svc_sum = 0.0
        for (nm, role), a in agg.items():
            if role == "SERVER":
                servers.append((nm, a["hrs"], a["tips"], a["svc"])); srv_svc_sum += a["svc"]
            elif role == "BAR":
                bar_people[nm] = a
            elif role == "UP_TERM":
                term_up_svc += a["svc"]; term_up_tips += a["tips"]
            elif role == "DOWN_TERM":
                term_dn_svc += a["svc"]; term_dn_tips += a["tips"]
            elif role in ("SA", "BARBACK", "RUNNER"):
                support.append((nm, {"SA": "Server Assistant", "BARBACK": "Barback", "RUNNER": "Runner"}[role], a["hrs"]))
        # NET SALES by venue + per-ringer venue, from orders, by REVENUE CENTER ("Bar" = downstairs)
        up_net = dn_net = 0.0
        net_by_name = defaultdict(lambda: {"U": 0.0, "D": 0.0})
        opage = 1
        while True:
            so, od = api_get("/orders/v2/ordersBulk", g, {"businessDate": d.strftime("%Y%m%d"), "pageSize": 100, "page": opage}, tok)
            if not isinstance(od, list) or not od:
                break
            for o in od:
                if o.get("voided") or o.get("deleted"):
                    continue
                srv = o.get("server"); sg = srv.get("guid") if isinstance(srv, dict) else srv
                snm = (em.get(sg, "") or "").strip()
                rcv = o.get("revenueCenter"); rcg = rcv.get("guid") if isinstance(rcv, dict) else rcv
                netv = sum(money(c.get("amount")) for c in (o.get("checks") or []) if not c.get("voided"))
                is_down = (rcg in down_rc) if down_rc else (snm in DOWN_BAR or snm in DOWN_TERM)
                if is_down:
                    dn_net += netv; net_by_name[snm]["D"] += netv
                else:
                    up_net += netv; net_by_name[snm]["U"] += netv
            if len(od) < 100:
                break
            opage += 1
        # which venues were open that day -> default for hours-only unknown bartenders
        up_active = (term_up_svc > 0) or (srv_svc_sum > 0) or (up_net > 0)
        dn_active = (term_dn_svc > 0) or (dn_net > 0)
        # resolve each bartender's venue: known roster -> where they rang sales -> the open bar -> default upstairs (flag)
        up_svc, up_tips = term_up_svc, term_up_tips
        dn_svc, dn_tips = term_dn_svc, term_dn_tips
        upb, downb, bar_flags = [], [], []
        for nm, a in bar_people.items():
            nb = net_by_name.get(nm, {"U": 0.0, "D": 0.0})
            rostered = nm in UP_BAR or nm in DOWN_BAR
            if (a["svc"] > 0 or a["tips"] > 0) and (nb["U"] > 0 or nb["D"] > 0):
                # (1) they rang their OWN sales -> the venue they actually worked that night
                v = "D" if nb["D"] > nb["U"] else "U"
                flag = "" if rostered else "auto"
            elif up_active and not dn_active:
                # (2) only upstairs was open that night -> they worked upstairs (beats home roster)
                v, flag = "U", ("" if rostered else "auto")
            elif dn_active and not up_active:
                v, flag = "D", ("" if rostered else "auto")
            elif nm in UP_BAR:
                # (3) both bars open, hours only -> home venue (roster)
                v, flag = "U", ""
            elif nm in DOWN_BAR:
                v, flag = "D", ""
            else:
                # (4) both open, unrostered, no sales -> default upstairs, flag for the manager
                v, flag = "U", "new"
            if v == "U":
                upb.append((nm, a["hrs"], flag)); up_svc += a["svc"]; up_tips += a["tips"]
            else:
                downb.append((nm, a["hrs"], flag)); dn_svc += a["svc"]; dn_tips += a["tips"]
            if flag:
                bar_flags.append((nm, v, flag))
        servers.sort(key=lambda x: -x[1]); upb.sort(key=lambda x: -x[1])
        downb.sort(key=lambda x: -x[1]); support.sort(key=lambda x: (x[1], -x[2]))
        keepall_list = sorted([(nm, round(v["hrs"], 2), round(v["svc"], 2), round(v["tips"], 2))
                               for nm, v in keepall.items() if v["svc"] > 0 or v["tips"] > 0])
        week.append({"date": d, "servers": servers, "upb": upb, "downb": downb, "support": support,
                     "up_svc": round(up_svc, 2), "up_tips": round(up_tips, 2),
                     "dn_svc": round(dn_svc, 2), "dn_tips": round(dn_tips, 2),
                     "mgr_svc": round(mgr_svc, 2), "bar_flags": bar_flags, "keepall": keepall_list,
                     "up_net": round(up_net, 2), "dn_net": round(dn_net, 2)})
    return week


def section(ws, r, text):
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=14)
    c = ws.cell(r, 1, text); c.font = SEC; c.fill = SECFILL
    c.alignment = Alignment(horizontal="left", vertical="center")


def colhdr(ws, r, headers):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(r, i, h); c.font = HF; c.fill = HFILL
        c.alignment = Alignment(horizontal="center", wrap_text=True); c.border = BORD


def build_day(ws, dd, dname, withhold=False):
    ws.cell(1, 1, "Day").font = BOLD; ws.cell(1, 2, DAYNAME[dname]).font = BOLD
    ws.cell(1, 4, "Date").font = BOLD; c = ws.cell(1, 5, dd["date"]); c.number_format = "yyyy-mm-dd"
    if dd.get("ds_only"):
        ws.merge_cells("A2:M2")
        fc = ws.cell(2, 1, "⚠ DOWNSTAIRS-ONLY NIGHT — upstairs was closed. The downstairs sales & tip-out below are for this night (manager: enter the downstairs bartenders + hours).")
        fc.font = Font(name=ARIAL, size=10, bold=True, color="9C5700")
        fc.fill = PatternFill("solid", fgColor="FFEB9C")
        fc.alignment = Alignment(horizontal="left", vertical="center")
    # Daily pools box (references computed cells)
    ws.cell(3, 1, "DAILY →").font = BOLD
    labels = [("Upstairs NET sales", dd.get("up_net", 0)), ("Downstairs NET sales", dd.get("dn_net", 0)),
              ("Upstairs pool", f"=K{R_UB_HDR}"), ("Downstairs pool", f"=K{R_DN_AUTO}"),
              ("Support pool", f"=E{R_SP_POOL}"), ("Support $/hr", f"=IF(SUM(D{R_SP0}:D{R_SP_TOT-1})=0,0,{WAGE}+E{R_SP_POOL}/SUM(D{R_SP0}:D{R_SP_TOT-1}))")]
    cc = 2
    for idx, (lab, f) in enumerate(labels):
        ws.cell(3, cc, lab).font = NOTE
        v = ws.cell(4, cc, f); v.number_format = CUR; v.font = BOLD
        v.fill = PatternFill("solid", fgColor="DDEBF7") if idx < 2 else GRAY
        cc += 2

    # ---- SECTION 1: UPSTAIRS ----
    section(ws, 6, "SECTION 1 — UPSTAIRS")
    ws.cell(7, 1, "Servers (auto from Toast — editable)").font = NOTE
    colhdr(ws, R_SRV_HDR, ["#", "Name", "Role", "Hours", "Tips", "Svc Chg", "→ Bar 15%", "→ Sup 12% (svc)", "→ Sup 5% (tips)", "", "", "", "Take-Home"])
    for k in range(NS):
        r = R_SRV0 + k; ws.cell(r, 1, k + 1)
        if k < len(dd["servers"]):
            nm, hrs, tips, svc = dd["servers"][k]
            ws.cell(r, 2, nm); ws.cell(r, 4, hrs); ws.cell(r, 5, tips); ws.cell(r, 6, svc)
        ws.cell(r, 3, "Server")
        ws.cell(r, 7, f"=0.15*F{r}"); ws.cell(r, 8, f"=0.12*F{r}"); ws.cell(r, 9, f"=0.05*E{r}")
        ws.cell(r, 13, f"=E{r}+F{r}-G{r}-H{r}-I{r}")
        for col in (4, 5, 6, 7, 8, 9, 13):
            ws.cell(r, col).number_format = CUR if col != 4 else H2
    # server totals
    ws.cell(R_SRV_TOT, 3, "TOTAL").font = BOLD
    for col in (4, 5, 6, 7, 8, 9, 13):
        ws.cell(R_SRV_TOT, col, f"=SUM({get_column_letter(col)}{R_SRV0}:{get_column_letter(col)}{R_SRV_TOT-1})").font = BOLD
        ws.cell(R_SRV_TOT, col).number_format = CUR if col != 4 else H2
    ka = dd.get("keepall", [])
    if ka:
        knm, khrs, ksvc, ktips = ka[0]        # manager on a special keep-all deal (Anthony)
        ws.cell(R_MGR, 2, knm).font = BF
        ws.cell(R_MGR, 3, "Mgr (keeps own)").font = NOTE
        ws.cell(R_MGR, 4, khrs).number_format = H2
        ws.cell(R_MGR, 5, ktips).number_format = CUR     # E = Tips
        ws.cell(R_MGR, 6, ksvc).number_format = CUR      # F = Svc chg
        ws.cell(R_MGR, 8, "special deal — keeps 100%, tips out to no one").font = NOTE
        ws.cell(R_MGR, 13, f"=E{R_MGR}+F{R_MGR}").number_format = CUR   # take-home = svc + tips, no tip-out
    else:
        ws.cell(R_MGR, 2, "Manager (no tipped shift this day)").font = NOTE

    # upstairs bartenders
    ws.cell(R_UB_HDR, 1, "Upstairs Bartenders").font = BOLD
    ws.cell(R_UB_HDR, 2, "(all auto-pulled from Toast; orange = verify U/D; yellow = add someone who didn't clock in)").font = NOTE
    ws.cell(R_UB_HDR, 4, "Bar svc (auto)").font = NOTE; ws.cell(R_UB_HDR, 5, dd["up_svc"]).number_format = CUR
    ws.cell(R_UB_HDR, 6, "Bar tips").font = NOTE; ws.cell(R_UB_HDR, 7, dd["up_tips"]).number_format = CUR
    ws.cell(R_UB_HDR, 8, "→Support 12% svc + 5% tips").font = NOTE
    ws.cell(R_UB_HDR, 9, f"=0.12*E{R_UB_HDR}+0.05*G{R_UB_HDR}").number_format = CUR
    ws.cell(R_UB_HDR, 10, "Pool→").font = NOTE
    # upstairs bar pool = 88% own svc + 15% server svc + 95% bar tips (5% tips -> support)
    ws.cell(R_UB_HDR, 11, f"=0.88*E{R_UB_HDR}+0.15*F{R_SRV_TOT}+0.95*G{R_UB_HDR}").number_format = CUR
    colhdr(ws, R_UB_COLS, ["#", "Name", "Role", "Hours", "", "", "", "", "", "", "", "Avg $/hr", "Take-Home"])
    for k in range(NUB):
        r = R_UB0 + k; ws.cell(r, 1, k + 1); ws.cell(r, 3, "Bartender (U)")
        if k < len(dd["upb"]):
            nm, hrs, flag = dd["upb"][k]; ws.cell(r, 2, nm); ws.cell(r, 4, hrs)
            if flag == "new":
                ws.cell(r, 2).fill = FLAG; ws.cell(r, 3, "Bartender (U?)")   # auto-added, verify venue
            elif flag == "auto":
                ws.cell(r, 3, "Bartender (U*)")                              # venue from their sales
        else:
            ws.cell(r, 2).fill = YEL; ws.cell(r, 4).fill = YEL   # manager: add name + hours; pool re-splits automatically
        ws.cell(r, 4).number_format = H2
        ws.cell(r, 13, f"=IF(SUM($D${R_UB0}:$D${R_UB_TOT-1})=0,0,$K${R_UB_HDR}*D{r}/SUM($D${R_UB0}:$D${R_UB_TOT-1}))").number_format = CUR
        ws.cell(r, 12, f'=IF(D{r}=0,"",{WAGE}+M{r}/D{r})').number_format = CUR
    ws.cell(R_UB_TOT, 3, "TOTAL").font = BOLD
    ws.cell(R_UB_TOT, 4, f"=SUM(D{R_UB0}:D{R_UB_TOT-1})").font = BOLD; ws.cell(R_UB_TOT, 4).number_format = H2
    ws.cell(R_UB_TOT, 12, f'=IF(D{R_UB_TOT}=0,"",{WAGE}+M{R_UB_TOT}/D{R_UB_TOT})').font = BOLD; ws.cell(R_UB_TOT, 12).number_format = CUR
    ws.cell(R_UB_TOT, 13, f"=SUM(M{R_UB0}:M{R_UB_TOT-1})").font = BOLD; ws.cell(R_UB_TOT, 13).number_format = CUR

    # ---- SECTION 2: DOWNSTAIRS ----
    section(ws, R_DN_HDR, "SECTION 2 — DOWNSTAIRS  (not included in this upstairs-only file)" if withhold
            else "SECTION 2 — DOWNSTAIRS  (auto-pulled from Toast; orange = verify U/D; yellow = add an under-clocked bartender)")
    ws.cell(R_DN_AUTO, 4, "Bar svc (auto)").font = NOTE; ws.cell(R_DN_AUTO, 5, dd["dn_svc"]).number_format = CUR
    ws.cell(R_DN_AUTO, 6, "tips").font = NOTE; ws.cell(R_DN_AUTO, 7, dd["dn_tips"]).number_format = CUR
    ws.cell(R_DN_AUTO, 8, "→Support 5% of net sales").font = NOTE
    ws.cell(R_DN_AUTO, 9, f"=MIN(0.05*D4,E{R_DN_AUTO}+G{R_DN_AUTO})").number_format = CUR   # 5% of net, capped at what downstairs earned
    ws.cell(R_DN_AUTO, 10, "Pool→").font = NOTE
    ws.cell(R_DN_AUTO, 11, f"=E{R_DN_AUTO}+G{R_DN_AUTO}-I{R_DN_AUTO}").number_format = CUR   # never negative (support cut is capped)
    colhdr(ws, R_DN_COLS, ["#", "Name", "Role", "Hours", "", "", "", "", "", "", "", "Avg $/hr", "Take-Home"])
    for k in range(ND):
        r = R_DN0 + k; ws.cell(r, 1, k + 1); ws.cell(r, 3, "Bartender (D)")
        if k < len(dd["downb"]):
            nm, hrs, flag = dd["downb"][k]; ws.cell(r, 2, nm); ws.cell(r, 4, hrs)
            if flag == "new":
                ws.cell(r, 2).fill = FLAG; ws.cell(r, 3, "Bartender (D?)")   # auto-added, verify venue
            elif flag == "auto":
                ws.cell(r, 3, "Bartender (D*)")                              # venue from their sales
        else:
            ws.cell(r, 2).fill = YEL; ws.cell(r, 4).fill = YEL              # manual add-row for under-clockers
        ws.cell(r, 4).number_format = H2
        ws.cell(r, 13, f"=IF(SUM($D${R_DN0}:$D${R_DN_TOT-1})=0,0,$K${R_DN_AUTO}*D{r}/SUM($D${R_DN0}:$D${R_DN_TOT-1}))").number_format = CUR
        ws.cell(r, 12, f'=IF(D{r}=0,"",{WAGE}+M{r}/D{r})').number_format = CUR
    ws.cell(R_DN_TOT, 3, "TOTAL").font = BOLD
    ws.cell(R_DN_TOT, 4, f"=SUM(D{R_DN0}:D{R_DN_TOT-1})").font = BOLD; ws.cell(R_DN_TOT, 4).number_format = H2
    ws.cell(R_DN_TOT, 12, f'=IF(D{R_DN_TOT}=0,"",{WAGE}+M{R_DN_TOT}/D{R_DN_TOT})').font = BOLD; ws.cell(R_DN_TOT, 12).number_format = CUR
    ws.cell(R_DN_TOT, 13, f"=SUM(M{R_DN0}:M{R_DN_TOT-1})").font = BOLD; ws.cell(R_DN_TOT, 13).number_format = CUR

    # ---- SECTION 3: SUPPORT ----
    section(ws, R_SP_HDR, "SECTION 3 — SUPPORT  (WITHHELD — already paid separately this week; the 12% is still deducted above)" if withhold
            else "SECTION 3 — SUPPORT STAFF  (level-loaded: everyone same $/hr this day;  yellow rows = add missing staff)")
    ws.cell(R_SP_POOL, 1, "Support pool = 12% of all svc + 5% of all tips →").font = BOLD
    ws.cell(R_SP_POOL, 5, f"=H{R_SRV_TOT}+I{R_SRV_TOT}+I{R_UB_HDR}+I{R_DN_AUTO}").font = BOLD
    ws.cell(R_SP_POOL, 5).number_format = CUR; ws.cell(R_SP_POOL, 5).fill = GRAY
    colhdr(ws, R_SP_COLS, ["#", "Name", "Role", "Hours", "", "", "", "", "", "", "", "$/hr today*", ""])
    for k in range(NSP):
        r = R_SP0 + k; ws.cell(r, 1, k + 1)
        if k < len(dd["support"]):
            nm, role, hrs = dd["support"][k]; ws.cell(r, 2, nm); ws.cell(r, 3, role); ws.cell(r, 4, hrs)
        else:
            ws.cell(r, 2).fill = YEL; ws.cell(r, 3).fill = YEL; ws.cell(r, 4).fill = YEL   # manager: add name + role + hours
        ws.cell(r, 4).number_format = H2
        ws.cell(r, 12, f'=IF(OR(D{r}=0,SUM($D${R_SP0}:$D${R_SP_TOT-1})=0),"",{WAGE}+$E${R_SP_POOL}/SUM($D${R_SP0}:$D${R_SP_TOT-1}))').number_format = CUR
    ws.cell(R_SP_TOT, 3, "TOTAL").font = BOLD
    ws.cell(R_SP_TOT, 4, f"=SUM(D{R_SP0}:D{R_SP_TOT-1})").font = BOLD; ws.cell(R_SP_TOT, 4).number_format = H2
    ws.cell(R_CHECK, 1, "* today's rate is a reference only — support is pooled across the whole week and paid at one level rate on the Summary tab.").font = NOTE
    # widths
    for col, w in {"A": 4, "B": 22, "C": 14, "D": 8, "E": 11, "F": 11, "G": 11, "H": 12,
                   "I": 11, "J": 7, "K": 12, "L": 12, "M": 12}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A6"


def build_summary(wb, week, withhold=False):
    sm = wb.create_sheet("Summary")
    sm["A1"] = "WEEKLY SUMMARY — take-home by person (tips + service-charge distributions)"; sm["A1"].font = Font(name=ARIAL, size=13, bold=True, color="1F3864")
    sm["A2"] = "Week of"; sm["B2"] = "=Setup!$B$3"; sm["B2"].number_format = "yyyy-mm-dd"
    # weekly support settlement (level-loaded to one rate). Support HOURS = the support-section totals only.
    sm["O1"] = ("Support WITHHELD (paid separately)" if withhold else "Support pool (week)"); sm["O1"].font = NOTE
    sm["P1"] = "=" + "+".join(f"{d}!E{R_SP_POOL}" for d in DAYS); sm["P1"].number_format = CUR
    sm["O2"] = "Support hours (week)"; sm["O2"].font = NOTE
    sm["P2"] = "=" + "+".join(f"{d}!D{R_SP_TOT}" for d in DAYS); sm["P2"].number_format = H2
    sm["O3"] = "Support $/hr (level)"; sm["O3"].font = BOLD
    sm["P3"] = f"=IF(P2=0,0,{WAGE}+P1/P2)"; sm["P3"].number_format = CUR; sm["P3"].fill = GRAY
    heads = ["Employee", "Hours", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "WEEK TAKE-HOME", "$/hr (incl base)", "Wage check"]
    colhdr(sm, 4, heads)
    names = sorted({nm for dd in week for nm, *_ in dd["servers"]}
                   | {nm for dd in week for nm, *_ in dd["upb"]}
                   | {nm for dd in week for nm, *_ in dd["downb"]}
                   | {nm for dd in week for nm, *_ in dd["support"]}
                   | {nm for dd in week for nm, *_ in dd.get("keepall", [])})
    dcol = dict(zip(DAYS, "CDEFGHI"))

    def emp_formulas(r):
        # total hours across all sections (server/bartender + support)
        sm.cell(r, 2, "=" + "+".join(f'SUMIF({d}!$B$9:$B${R_SP_TOT},$A{r},{d}!$D$9:$D${R_SP_TOT})' for d in DAYS)).number_format = H2
        # daily take = server/bartender take only (rows 9..R_DN_TOT); support settled weekly below
        for d, col in dcol.items():
            sm.cell(r, ord(col) - 64, f'=SUMIF({d}!$B$9:$B${R_DN_TOT},$A{r},{d}!$M$9:$M${R_DN_TOT})').number_format = CUR
        support_h = "+".join(f'SUMIF({d}!$B${R_SP0}:$B${R_SP_TOT-1},$A{r},{d}!$D${R_SP0}:$D${R_SP_TOT-1})' for d in DAYS)
        sm.cell(r, 10, f"=SUM(C{r}:I{r})+($P$3-{WAGE})*({support_h})").number_format = CUR
        sm.cell(r, 11, f'=IF(B{r}=0,"",{WAGE}+J{r}/B{r})').number_format = CUR
        sm.cell(r, 12, f'=IF(OR(A{r}="",B{r}=0),"",IF(({WAGE}*B{r}+J{r})/B{r}>=Setup!$B$10,"OK","SHORTFALL"))')

    r = 5
    for nm in names:
        sm.cell(r, 1, nm); emp_formulas(r); r += 1
    # manual-add rows: manager types a name hand-entered on a day tab (someone who didn't clock in)
    for _ in range(6):
        sm.cell(r, 1).fill = YEL; emp_formulas(r); r += 1
    sm.cell(r, 1, "TOTAL").font = BOLD
    for col in (2, 10):
        sm.cell(r, col, f"=SUM({get_column_letter(col)}5:{get_column_letter(col)}{r-1})").font = BOLD
        sm.cell(r, col).number_format = CUR if col == 10 else H2
    note_r = r + 2
    sm.cell(note_r, 1, "↑ Yellow name cells = add a hand-entered employee (a bartender or support who didn't clock in but is entered on a day tab). Type their EXACT name as on the day tab; their pay pulls in automatically.").font = NOTE
    sm.merge_cells(start_row=note_r, start_column=1, end_row=note_r, end_column=12)
    sm.freeze_panes = "B5"
    sm.column_dimensions["A"].width = 24
    for c in range(2, 13): sm.column_dimensions[get_column_letter(c)].width = 11
    sm.column_dimensions["O"].width = 18; sm.column_dimensions["P"].width = 11


def build_setup(wb, monday, withhold=False):
    su = wb.active; su.title = "Setup"
    su["A1"] = "Sophie — Weekly Tip-Out Tool"; su["A1"].font = Font(name=ARIAL, size=14, bold=True, color="1F3864")
    su["A3"] = "Week Starting (Monday)"; su["B3"] = monday; su["B3"].number_format = "yyyy-mm-dd"; su["B3"].fill = YEL
    rows = [("Server → Bar (% of server svc)", 0.15), ("Server → Support (% of server svc)", 0.12),
            ("Upstairs Bart → Support (% of bar svc)", 0.12), ("Server & Up-Bart → Support (% of TIPS)", 0.05),
            ("Tipped cash wage ($/hr)", 2.13), ("Min wage floor ($/hr)", 7.25)]
    for i, (lab, val) in enumerate(rows):
        r = 5 + i; su.cell(r, 1, lab); c = su.cell(r, 2, val)
        c.number_format = '0%' if val < 1 else CUR; c.fill = YEL
    notes = ["Auto-filled from Toast by JOB TITLE — EVERY server, bartender, and support person who clocked in is pulled in automatically. New hires appear with no list to maintain.",
             "Bartender venue (upstairs vs downstairs): (1) where they rang their own sales that night (Toast revenue center 'Bar' = downstairs), then (2) the only bar open that night, then (3) their home roster, then (4) default upstairs. So a bartender is put where they actually worked that night; the home roster is just the fallback for shifts where they only clocked hours. ORANGE name cells = auto-placed and unconfirmed — verify U vs D. Tell us a regular's home bar and we add them to the roster.",
             "MISSING SOMEONE? That should be rare now — but yellow rows still let the manager add anyone who never clocked in (used the shared bar login). Enter name + hours and the day's pool re-splits for everyone; then type the same name into a yellow row on the Summary tab so it rolls up for the week.",
             "Servers & UPSTAIRS bartenders tip 5% of their TIPS to support (they keep 95% of tips). DOWNSTAIRS bartenders do NOT tip out tips — instead they tip 5% of downstairs NET SALES to support and keep 100% of their service charge + tips. Service-charge tip-outs unchanged (15% server→bar, 12% server→support, 12% upstairs-bar→support).",
             "Support pool = upstairs (12% of service charges + 5% of tips) + downstairs (5% of net sales), split so SA/barbacks/runner get the same $/hr each day.",
             "Anthony Aleman (manager) — SPECIAL DEAL: on any shift where he has a service charge or tips, he keeps 100% of his own service charge + tips and tips out to NO ONE (no bar, no support). He shows on the 'Mgr (keeps own)' line under the servers; his sales do NOT feed the bar or support pools.",
             "Every auto cell is editable — override anything that looks off."]
    if withhold:
        notes = ["UPSTAIRS-ONLY CATCH-UP: pays upstairs servers + bartenders only. Downstairs was already paid — its NET SALES show for reference, but its tip-out is excluded except on flagged downstairs-only nights. Support already paid — the 12% is still deducted from staff and shown as WITHHELD."] + notes
    for i, n in enumerate(notes):
        su.cell(12 + i, 1, "• " + n).font = NOTE
    su.column_dimensions["A"].width = 40; su.column_dimensions["B"].width = 12
    # B10 = min wage floor, referenced by the Summary wage-check
    su["C10"] = "← min wage (wage-check reference)"; su["C10"].font = NOTE


def main():
    mode = "full"; date_arg = None
    for a in sys.argv[1:]:
        if a in ("full", "upstairs"):
            mode = a
        else:
            date_arg = a
    if date_arg:
        monday = datetime.date.fromisoformat(date_arg)
    else:
        t = datetime.date.today()
        monday = t - datetime.timedelta(days=t.weekday() + 7)  # last complete Mon-Sun
    withhold = (mode == "upstairs")
    tok = get_token(); g = RESTAURANTS["Sophie"]
    s, jobs = api_get("/labor/v1/jobs", g, None, tok); jm = {j.get("guid"): j.get("title") for j in (jobs or [])}
    s, emps = api_get("/labor/v1/employees", g, None, tok)
    em = {e.get("guid"): norm(e.get("chosenName") or ", ".join(x for x in [e.get("lastName"), e.get("firstName")] if x)) for e in (emps or [])}
    # revenue centers: "Bar" = downstairs; everything else (Patio, Dining Room) = upstairs
    s, rcs = api_get("/config/v2/revenueCenters", g, None, tok)
    down_rc = {r.get("guid") for r in (rcs or []) if (r.get("name") or "").strip().lower() == "bar"}
    week = pull(monday, tok, g, jm, em, down_rc)
    # surface any bartender the tool auto-placed (not on the venue roster) so nobody is silent
    flagged = [(dd["date"], nm, v, fl) for dd in week for (nm, v, fl) in dd.get("bar_flags", [])]
    if flagged:
        print(f"NOTE: {len(flagged)} bartender-day(s) auto-placed (not on the U/D roster):")
        for dt, nm, v, fl in flagged:
            basis = "inferred from their own sales" if fl == "auto" else "defaulted — VERIFY venue"
            print(f"   {dt} {nm} -> {'Upstairs' if v == 'U' else 'Downstairs'}  ({basis})")
    # Detect downstairs-only nights by MONEY (not staff presence): on slow nights
    # "upstairs" staff sometimes float to the one open (downstairs) bar and ring there.
    for dd in week:
        srv_svc = sum(s[3] for s in dd["servers"])
        upstairs_active = dd["up_svc"] > 0 or srv_svc > 0
        downstairs_active = dd["dn_svc"] > 0
        dd["ds_only"] = downstairs_active and not upstairs_active
        if dd["ds_only"]:
            # upstairs closed -> all net sales for the night belong downstairs
            # (stragglers rung under server/mgr names would otherwise show as "upstairs")
            dd["dn_net"] = dd.get("dn_net", 0.0) + dd.get("up_net", 0.0)
            dd["up_net"] = 0.0
    if withhold:
        for dd in week:
            dd["support"] = []            # support already paid separately -> withhold
            if not dd["ds_only"]:
                # both-ran (or closed) nights: downstairs already paid -> exclude its tip-out
                # (dn_net is KEPT so downstairs net sales still show for reference)
                dd["downb"] = []; dd["dn_svc"] = 0.0; dd["dn_tips"] = 0.0
            # downstairs-only nights: keep downstairs volume + tip-out, flagged in the tab
    wb = openpyxl.Workbook()
    build_setup(wb, monday, withhold)
    for i, dn in enumerate(DAYS):
        ws = wb.create_sheet(dn); build_day(ws, week[i], dn, withhold)
    build_summary(wb, week, withhold)
    import os
    suffix = "_UPSTAIRS-ONLY" if withhold else ""
    out = os.path.join(BASEDIR, "tipout", f"Sophie_TipOut_{monday.isoformat()}{suffix}.xlsx")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out); print("wrote", out)


if __name__ == "__main__":
    main()
