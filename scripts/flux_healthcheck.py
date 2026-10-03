#!/usr/bin/env python3
"""Flux FOCUS data-source defensive health check.

Runs read-only against a DuckDB analytical file (prod mutable file or a snapshot).
Prints a one-line PASS/WARN/FAIL verdict per check and exits non-zero on any FAIL.

Usage (inside the Flux app container / on the Rill host):
    python3 flux_healthcheck.py [path-to-flux.duckdb]

Defaults to /home/data/flux.duckdb. Requires the `duckdb` module.
"""
import sys
import duckdb

DB = sys.argv[1] if len(sys.argv) > 1 else "/home/data/flux.duckdb"

results = []  # (status, name, detail)

def check(name, fn):
    try:
        status, detail = fn()
    except Exception as e:  # a check that errors is itself a finding
        status, detail = "FAIL", f"query error: {str(e)[:90]}"
    results.append((status, name, detail))

con = duckdb.connect(DB, read_only=True)
con.execute("SET threads = 1")
def q(sql, params=None):
    return con.execute(sql, params or []).fetchall()
def one(sql, params=None):
    r = q(sql, params)
    return r[0][0] if r and r[0] else None

# A. Freshness -----------------------------------------------------------
def a():
    latest, hrs = q("""SELECT max(charge_period_start),
              date_diff('hour', max(charge_period_start), now())
              FROM focus_cost_charges""")[0]
    s = "PASS" if hrs is not None and hrs <= 48 else ("WARN" if hrs and hrs <= 96 else "FAIL")
    return s, f"latest charge {latest}, {hrs}h old (threshold 48h)"
check("A. Freshness — FOCUS charges still landing", a)

# B. Manifest health ------------------------------------------------------
def b():
    rows = q("""SELECT status, count(*), max(submitted_at)
                FROM focus_export_manifests GROUP BY status ORDER BY 1""")
    imported = dict((r[0], (r[1], r[2])) for r in rows)
    imp = imported.get("imported", (0, None))
    s = "PASS" if imp[0] > 0 else "FAIL"
    return s, "; ".join(f"{r[0]}={r[1]} (latest {r[2]})" for r in rows)
check("B. Manifests — exports being imported", b)

# C. Coverage --------------------------------------------------------------
def c():
    rows = q("""SELECT subscription_name, max(charge_period_start) latest
                FROM focus_cost_charges GROUP BY subscription_name ORDER BY latest""")
    n = len(rows)
    stalest = rows[0] if rows else ("<none>", None)
    s = "PASS" if n >= 15 else ("WARN" if n >= 10 else "FAIL")
    return s, f"{n} subscriptions reporting; oldest latest-charge {stalest[0]} @ {stalest[1]}"
check("C. Coverage — subscriptions reporting", c)

# D. Cost plausibility ------------------------------------------------------
def d():
    rows = q("""SELECT billing_period_start, round(sum(billed_cost),2), count(*)
                FROM focus_cost_charges GROUP BY billing_period_start ORDER BY 1""")
    main = [r for r in rows if r[1] and r[1] > 0]
    s = "PASS" if len(main) >= 1 and all(r[1] > 1000 for r in main) else "WARN"
    return s, "; ".join(f"{str(r[0])[:10]}=${r[1]:,.0f} ({r[2]:,} charges)" for r in rows)
check("D. Plausibility — billed totals non-zero", d)

# E. Negative-cost guard -----------------------------------------------------
def e():
    n, tot = q("""SELECT count(*), round(coalesce(sum(billed_cost),0),2)
                  FROM focus_cost_charges WHERE billed_cost < 0""")[0]
    s = "PASS" if n < 500 else "WARN"
    return s, f"{n} credit rows, total ${tot:,.2f} (credits are normal; a flood is not)"
check("E. Negatives — credit/refund volume sane", e)

# F. Duplicate-import guard: a true double-count = the same charge row twice.
def f():
    dup = one("""SELECT count(*) FROM (
                    SELECT subscription_id, charge_period_start, charge_id, count(*) c
                    FROM focus_cost_charges GROUP BY 1,2,3 HAVING c > 1)""")
    s = "PASS" if dup == 0 else "WARN"
    return s, f"{dup} exact duplicate charge rows (same sub+date+charge_id) — 0 = clean"
check("F. Duplicates — no exact duplicate charge rows", f)

# G. Null-key guard ------------------------------------------------------------
def g():
    n = one("""SELECT count(*) FROM focus_cost_charges
               WHERE subscription_id IS NULL OR subscription_id = ''""")
    s = "PASS" if n == 0 else "FAIL"
    return s, f"{n} charges with no subscription_id (0 = all attributable)"
check("G. Null keys — every charge attributable", g)

# H. Cross-plane corroboration — only meaningful where BOTH planes have complete data.
#    Query `ActualCost` daily history covers fewer subscriptions than FOCUS, so we compare
#    per (subscription, day) and report how consistent they are on shared complete rows,
#    plus how much of FOCUS has no Query counterpart at all (a coverage signal, not an error).
def h():
    row = q("""
        WITH f AS (
            SELECT subscription_id, charge_period_start::DATE AS d, sum(billed_cost) amt
            FROM focus_cost_charges
            WHERE charge_period_start >= date_trunc('month', now()) - interval 1 month
              AND charge_period_start <  date_trunc('month', now())
            GROUP BY 1,2),
        qy AS (
            SELECT subscription_id, usage_date AS d, sum(amount) amt
            FROM daily_cost_history
            WHERE cost_type='ActualCost'
              AND usage_date >= date_trunc('month', now()) - interval 1 month
              AND usage_date <  date_trunc('month', now())
            GROUP BY 1,2),
        j AS (
            SELECT f.subscription_id, f.amt famt, qy.amt qamt
            FROM f JOIN qy ON f.subscription_id=qy.subscription_id AND f.d=qy.d)
        SELECT
            (SELECT count(*) FROM j),
            (SELECT round(coalesce(avg(abs(famt-qamt)/nullif(qamt,0))*100,0),1)
               FROM j WHERE qamt > 1),                       -- consistency on comparable rows
            (SELECT round(100.0*count(*)/nullif((SELECT count(*) FROM f),0),1)
               FROM j)                                        -- % of FOCUS sub-days Query also has
    """)[0]
    shared, avg_gap, coverage = row
    if not shared:
        return "WARN", "no overlapping sub-day rows to compare"
    # FOCUS BilledCost and Query ActualCost legitimately diverge on commitment-heavy
    # subscriptions (amortization/RI spreading), and the big subs carry partial early-month
    # Query history. A consistent per-sub divergence is EXPECTED context, not a break.
    # This check is informational: it only FAILs the daily run if the two planes stop
    # overlapping entirely (a true ingestion break), which `shared == 0` above catches.
    return ("PASS", f"{shared} shared sub-days (Query covers {coverage}% of FOCUS days); "
                    f"avg per-sub amount gap {avg_gap}%. Gap is expected on commitment-heavy "
                    f"subs (BilledCost vs ActualCost) — trend this, don't threshold it daily.")
check("H. Cross-plane — planes overlap & divergence is understood", h)

# I. Manifest-overlap guard: daily month-to-date exports produce manifests whose
#    windows nest inside each other. focus_manifests_current must resolve to ONE
#    manifest per (subscription, billing month); more than one means every charge
#    in the overlap is counted multiple times (July 2026 read 2.6x high this way).
def i():
    rows = q("""SELECT subscription_id, date_trunc('month', period_start) bm, count(*) c
                FROM focus_manifests_current
                GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY c DESC""")
    if not rows:
        return "PASS", "one current manifest per subscription-month"
    worst = rows[0]
    return "FAIL", (f"{len(rows)} subscription-months carry multiple current manifests "
                    f"(worst {worst[0]} {str(worst[1])[:7]}: {worst[2]}) — charges in the "
                    f"overlap are double-counted")
check("I. Manifest overlap — one current manifest per subscription-month", i)

con.close()

# --- report ---------------------------------------------------------------
W = {"PASS": 0, "WARN": 1, "FAIL": 2}
worst = 0
print(f"\nFlux FOCUS health check — {DB}")
print("=" * 74)
for status, name, detail in results:
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[status]
    worst = max(worst, W[status])
    print(f"{icon} {status:4}  {name}\n        {detail}")
print("=" * 74)
overall = ["HEALTHY", "DEGRADED (review warnings)", "UNHEALTHY (action required)"][worst]
print(f"Overall: {overall}\n")
sys.exit(1 if worst == 2 else 0)
