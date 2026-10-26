#!/usr/bin/env python3
"""Build a self-contained HTML report. Usage: build_html.py [DATA_DIR] [OUT.html]

DATA_DIR holds samples.csv, results.json and recoveries.json (default: data/ next to this script).
OUT defaults to DATA_DIR/pir-ram-report.html.
"""
import csv, json, statistics, os, sys

R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WINDOWS = {  # run window shown in the time-series chart (UTC)
    "A": ("2026-10-24T04:10:00Z", "2026-10-24T09:10:00Z"),
    "B": ("2026-10-25T04:40:00Z", "2026-10-25T11:15:00Z"),
    "C": ("2026-10-26T04:10:00Z", "2026-10-26T07:30:00Z"),
}
SETTLED = ("2026-10-25T13:00:00Z", "2026-10-26T01:30:00Z")  # rest between B and C, Reth fully warm


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


S = list(csv.DictReader(open(f"{R}/samples.csv")))
results = json.load(open(f"{R}/results.json"))
recov = json.load(open(f"{R}/recoveries.json"))


def breakdown(rows):
    g = lambda k: med([num(r[k]) for r in rows])
    total, free, cache, anon = g("mem_total_gb"), g("mem_free_gb"), g("cached_gb"), g("anon_gb")
    held, reth, lh = g("held_gb") or 0, g("el_rss_anon_gb"), g("cl_rss_gb")
    other = max(0, round(anon - held - reth - lh, 2))
    kernel = round(total - free - cache - anon, 2)
    return {"total": total, "avail": g("mem_available_gb"), "parts": [
        ["Reth process (heap)", reth], ["Lighthouse process", lh], ["Other processes", other],
        ["Kernel, buffers, slab", kernel], ["Dummy PIR server", held],
        ["Page cache (mostly Reth DB)", cache], ["Free", free]],
        "mdbx": g("mdbx_resident_gb"), "disk": g("disk_read_mbs")}


idle = [r for r in S if r["phase"] == "rest" and SETTLED[0] <= r["ts"] <= SETTLED[1]
        and (num(r["head_lag_s"]) or 0) < 60]
top = next(s for s in results if s["run"] == "B" and s.get("gb") == 18.5)
tail = [r for r in S if r["phase"] == "B:18.5" and r["ts"] >= top["end"][:14] + "40"]
tail = tail or [r for r in S if r["phase"] == "B:18.5"]
bd = {"idle": breakdown(idle), "full": breakdown(tail), "idle_n": len(idle), "full_n": len(tail)}

series = {}
for rid, (a, z) in WINDOWS.items():
    rows = [r for r in S if a <= r["ts"] <= z]
    series[rid] = [[r["ts"][11:19], num(r["held_gb"]) or 0,
                    round((num(r["head_lag_s"]) or 0) / 12, 2), num(r["disk_read_mbs"]),
                    num(r["mem_available_gb"])] for r in rows]

steps = [{k: s.get(k) for k in ("run", "gb", "outcome", "lag_tail_median_s", "lag_max_s",
                                 "mem_available_min_gb", "import_p95_ms", "start", "end")} for s in results]
# per-step medians from samples for the cost table
for s in steps:
    if s["gb"] is None:
        continue
    rs = [r for r in S if r["phase"] == f"{s['run']}:{s['gb']:g}" and s["start"] <= r["ts"] <= (s["end"] or "z")]
    s["cache"] = med([num(r["cached_gb"]) for r in rs])
    s["mdbx"] = med([num(r["mdbx_resident_gb"]) for r in rs])
    s["disk"] = med([num(r["disk_read_mbs"]) for r in rs])
    s["iops"] = med([num(r["disk_read_iops"]) for r in rs])
    s["swap"] = max([num(r["swap_used_gb"]) or 0 for r in rs] or [0])

def is_step(ph):
    return ":" in ph and ph.split(":")[1] not in ("precheck", "ramp", "recovery")


behind = {}
for rid in "ABC":
    sq = [r for r in S if r["phase"].startswith(rid + ":") and is_step(r["phase"])]
    behind[rid] = [sum(1 for r in sq if (num(r["head_lag_s"]) or 0) > 60), len(sq)]
nosq = [r for r in S if r["phase"] in ("baseline", "rest")]
behind["idle"] = [sum(1 for r in nosq if (num(r["head_lag_s"]) or 0) > 60), len(nosq)]
data = {"bd": bd, "series": series, "steps": steps, "recov": recov, "behind": behind,
        "span": [S[0]["ts"], S[-1]["ts"], len(S)]}
tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")).read()
out = tpl.replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
OUT = sys.argv[2] if len(sys.argv) > 2 else f"{R}/pir-ram-report.html"
open(OUT, "w").write(out)
print(f"wrote {OUT} ({len(out) // 1024} KB); idle n={len(idle)} full n={len(tail)}")
print(json.dumps(bd, indent=1))
