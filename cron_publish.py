#!/usr/bin/env python3
"""Self-healing publisher for the Sophie daily dashboard (run by GitHub Actions).

Reads the existing sophie-daily.html, finds the latest day already in the page, and
rebuilds every COMPLETE business day from there through yesterday. Key properties:

  * Idempotent — if the page is already current, it writes nothing and exits 0.
  * Self-healing — if a run (or several) was missed, the next run backfills the gap,
    so the page can never silently freeze for days the way it did before.
  * Never fabricates — a day with no Toast data (closed / not yet posted) is skipped,
    not invented.

Needs: DAILY_PW in the environment (the page's gate password) and creds.json (Toast
read-only clientId/clientSecret) next to daily_web.py. Usage:
    python3 cron_publish.py [sophie-daily.html]
"""
import os, base64, datetime, sys
import daily_web as D

PAGE = sys.argv[1] if len(sys.argv) > 1 else "sophie-daily.html"
MAX_BACKFILL_DAYS = 14   # safety cap so one run never tries to rebuild months

def main():
    pw = os.environ["DAILY_PW"]
    pd = D.extract_data(open(PAGE).read())
    salt = base64.b64decode(pd["salt"]); key = D._key(pw, salt)

    present = {d["date"] for d in pd["days"]}
    latest = max(present) if present else None   # ISO dates sort chronologically
    yesterday = datetime.date.today() - datetime.timedelta(days=1)

    if latest:
        start = datetime.date.fromisoformat(latest) + datetime.timedelta(days=1)
    else:
        start = yesterday
    floor = yesterday - datetime.timedelta(days=MAX_BACKFILL_DAYS)
    if start < floor:
        print(f"WARNING: page is behind by more than {MAX_BACKFILL_DAYS} days "
              f"(latest={latest}); clamping backfill start to {floor} — older days will be skipped.")
        start = floor

    added = []
    d = start
    while d <= yesterday:
        iso = d.isoformat()
        day = D.build_day(iso)
        if day["sales"]["net"] == 0 and not day["roles"]:
            print(f"  {iso}: no data (closed / not posted) — skipping")
        else:
            pd["days"] = [x for x in pd["days"] if x["date"] != iso]
            pd["days"].insert(0, D._entry(key, iso))
            added.append(iso)
            print(f"  {iso}: net ${day['sales']['net']:,.2f} | ctrl labor ${day['labor']['dollars']:,.0f} | covers {day['sales']['covers']}")
        d += datetime.timedelta(days=1)

    if not added:
        print(f"No new complete days to add; page already current through {latest}.")
        return

    pd["days"].sort(key=lambda x: x["date"], reverse=True)
    pd["days"] = pd["days"][:92]   # keep ~3 months of history
    open(PAGE, "w").write(D.render_full_page(pd))
    print(f"Published: added {added} | latest now {pd['days'][0]['date']} | {len(pd['days'])} days on page.")

if __name__ == "__main__":
    main()
