#!/usr/bin/env python3
"""Build data/report.md from samples.csv, blocks.csv, results.json and recoveries.json (stdlib only)."""
import csv, json, statistics
from collections import defaultdict

DATA = "/home/andy/pir-ram/data"
LEFT_ANON_NOTE = "Blowup factors: 4x (rule of thumb), 1.6x (InsPIRe GPU resident footprint)."


def rows(path):
    try:
        return list(csv.DictReader(open(path)))
    except OSError:
        return []


def load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(q / 100 * len(vals)))]


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def f(v, d=1):
    return "" if v is None else f"{v:.{d}f}"


def col(rs, k):
    return [num(r.get(k)) for r in rs]


def main():
    S, B = rows(f"{DATA}/samples.csv"), rows(f"{DATA}/blocks.csv")
    for extra in sorted(__import__("glob").glob(f"{DATA}/catchup-*.csv")):
        B = rows(extra) + B
    results, recov = load(f"{DATA}/results.json", []), load(f"{DATA}/recoveries.json", [])
    out = ["# PIR home-node RAM test: results", ""]
    if S:
        out += [f"Samples {S[0]['ts']} to {S[-1]['ts']} ({len(S)} rows every 15 s), {len(B)} blocks logged.", ""]

    by_phase_s, by_phase_b = defaultdict(list), defaultdict(list)
    for r in S:
        by_phase_s[r["phase"]].append(r)
    for r in B:
        by_phase_b[r["phase"]].append(r)

    out += ["## Squeeze steps", "",
            "| Run | Dummy PIR GB | Outcome | Lag median s | Lag last-15-min median s | Lag max s | MemAvail min GB "
            "| Page cache GB | Reth DB resident GB | Disk read MB/s | Read IOPS | Read await ms | iowait % "
            "| Reth major faults/s | IO pressure % | Block import p50 / p95 ms | Arrival p50 / p95 s | Net rx Mbit/s | Swap used max GB |",
            "|" + "---|" * 19]
    for s in results:
        if "gb" not in s:
            out.append(f"| {s['run']} | | {s['outcome']} | | | | | | | | | | | | | | | | |")
            continue
        ph = f"{s['run']}:{s['gb']:g}"
        rs, bs = by_phase_s.get(ph, []), by_phase_b.get(ph, [])
        imp, arr = col(bs, "import_ms"), col(bs, "arrival_s")
        out.append(
            f"| {s['run']} | {s['gb']:g} | {s['outcome']} | {f(s.get('lag_median_s'), 0)} | {f(s.get('lag_tail_median_s'), 0)} "
            f"| {f(s.get('lag_max_s'), 0)} | {f(s.get('mem_available_min_gb'), 2)} | {f(med(col(rs, 'cached_gb')))} "
            f"| {f(med(col(rs, 'mdbx_resident_gb')))} | {f(med(col(rs, 'disk_read_mbs')))} | {f(med(col(rs, 'disk_read_iops')), 0)} "
            f"| {f(med(col(rs, 'disk_read_await_ms')), 2)} | {f(med(col(rs, 'cpu_iowait_pct')))} | {f(med(col(rs, 'el_majflt_s')), 0)} "
            f"| {f(med(col(rs, 'psi_io_some_pct')))} | {f(pct(imp, 50), 0)} / {f(pct(imp, 95), 0)} "
            f"| {f(pct(arr, 50), 2)} / {f(pct(arr, 95), 2)} | {f(med(col(rs, 'net_rx_mbit')))} "
            f"| {f(max([v for v in col(rs, 'swap_used_gb') if v is not None] or [0]), 2)} |")
    out += ["", "Steps with swap used > 0 are not valid: swap gives the node extra room. On 2026-10-24 05:20-06:47 UTC "
            "DAppNode's recreate-dappnode.timer restarted dappmanager, which turned swap back on (run A steps 6, 8, 10).",
            "", "Notes per step:", ""]
    out += [f"- {s['run']} {s.get('gb', '')}: {s.get('note')}" for s in results if s.get("note")]

    out += ["", "## Answer per run", "", "| Run | Largest passing GB | First non-pass | PIR DB at 4x GB | PIR DB at 1.6x GB |",
            "|---|---|---|---|---|"]
    for rid in sorted({s["run"] for s in results}):
        rs = [s for s in results if s["run"] == rid and "gb" in s]
        ok = [s["gb"] for s in rs if s["outcome"] == "pass"]
        bad = next((f"{s['gb']:g} GB ({s['outcome']})" for s in rs if s["outcome"] != "pass"), "none")
        top = max(ok) if ok else None
        out.append(f"| {rid} | {f(top, 0)} | {bad} | {f(top / 4 if top else None)} | {f(top / 1.6 if top else None)} |")
    out += ["", LEFT_ANON_NOTE, ""]

    out += ["## Recovery after release", "", "| Run | Released | Back at head (min) |", "|---|---|---|"]
    out += [f"| {r['run']} | {r['released']} | {r['back_at_head_min']} |" for r in recov]

    out += ["", "## No-squeeze periods, split by variable", "",
            "| Period | Samples | Lag median / p95 s | Block import p50 / p95 ms | Arrival p50 / p95 s | Net rx Mbit/s "
            "| CPU user % | iowait % | Disk read MB/s | Page cache GB |", "|" + "---|" * 10]
    idle_s = [r for r in S if r["phase"] in ("baseline", "rest")]
    idle_b = [r for r in B if r["phase"] in ("baseline", "rest")]
    groups = {
        "bandwidth cap on": lambda r: r["bwcap"] == "on",
        "bandwidth cap off": lambda r: r["bwcap"] == "off",
        "Lighthouse backfill running": lambda r: (num(r["cl_backfill_slots"]) or 0) > 0,
        "Lighthouse backfill done": lambda r: num(r["cl_backfill_slots"]) == 0,
    }
    for name, pred in groups.items():
        rs = [r for r in idle_s if pred(r)]
        ts = {r["ts"][:16] for r in rs}
        bs = [b for b in idle_b if b.get("added_ts") and _minute(b["added_ts"]) in ts]
        lag = col(rs, "head_lag_s")
        imp, arr = col(bs, "import_ms"), col(bs, "arrival_s")
        out.append(f"| {name} | {len(rs)} | {f(med(lag), 0)} / {f(pct(lag, 95), 0)} | {f(pct(imp, 50), 0)} / {f(pct(imp, 95), 0)} "
                   f"| {f(pct(arr, 50), 2)} / {f(pct(arr, 95), 2)} | {f(med(col(rs, 'net_rx_mbit')))} | {f(med(col(rs, 'cpu_user_pct')))} "
                   f"| {f(med(col(rs, 'cpu_iowait_pct')))} | {f(med(col(rs, 'disk_read_mbs')))} | {f(med(col(rs, 'cached_gb')))} |")

    out += ["", "## Episodes more than 60 s behind head", "",
            "| Start | End | Max lag s | Phase | Bandwidth cap | Block import p95 ms in window |", "|---|---|---|---|---|---|"]
    ev, cur = [], None
    for r in S:
        l = num(r["head_lag_s"]) or 0
        if l > 60:
            if cur is None:
                cur = {"start": r["ts"], "end": r["ts"], "max": l, "phase": r["phase"], "cap": r["bwcap"]}
            else:
                cur["end"], cur["max"] = r["ts"], max(cur["max"], l)
        elif cur:
            ev.append(cur)
            cur = None
    for e in ev:
        a = epoch_iso(e["start"]) - 60
        z = epoch_iso(e["end"]) + 600
        imp = [num(b["import_ms"]) for b in B if b.get("added_ts") and a <= float(b["added_ts"]) <= z]
        out.append(f"| {e['start']} | {e['end']} | {e['max']:.0f} | {e['phase']} | {e['cap']} | {f(pct(imp, 95), 0)} |")
    sq = [r for r in S if ":" in r["phase"] and r["phase"].split(":")[1] not in ("precheck", "ramp", "recovery")]
    out += ["", f"Samples more than 60 s behind: {sum(1 for r in S if (num(r['head_lag_s']) or 0) > 60 and r in sq)} of {len(sq)} "
            f"during squeeze steps, {sum(1 for r in idle_s if (num(r['head_lag_s']) or 0) > 60)} of {len(idle_s)} with no squeeze."]

    out += ["", "## Catch-up rate (blocks imported while more than 60 s behind)", ""]
    behind = [b for b in B if (num(b.get("added_lag_s")) or 0) > 60]
    if behind:
        segs, cur = [], [behind[0]]
        for b in behind[1:]:
            if float(b["added_ts"]) - float(cur[-1]["added_ts"]) > 120:
                segs.append(cur)
                cur = []
            cur.append(b)
        segs.append(cur)
        out += ["| Start | Phase | Blocks | Minutes | Blocks/min | Import p50 ms |", "|---|---|---|---|---|---|"]
        for sg in segs:
            mins = (float(sg[-1]["added_ts"]) - float(sg[0]["added_ts"])) / 60 or 1 / 60
            import time
            out.append(f"| {time.strftime('%Y-%m-%d %H:%M', time.gmtime(float(sg[0]['added_ts'])))} | {sg[0]['phase']} "
                       f"| {len(sg)} | {mins:.1f} | {len(sg) / mins:.0f} | {f(pct(col(sg, 'import_ms'), 50), 0)} |")
    else:
        out.append("None logged.")
    open(f"{DATA}/report.md", "w").write("\n".join(out) + "\n")


def epoch_iso(ts):
    import calendar, time
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _minute(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(float(epoch)))


if __name__ == "__main__":
    main()
