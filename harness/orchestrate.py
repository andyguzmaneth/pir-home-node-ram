#!/usr/bin/env python3
"""Run the scheduled squeeze runs in plan.json, unattended, and survive reboots.

Per run: wait for the node to be at head, then raise the dummy PIR memory in steps. Each step
holds for hold_min minutes and ends as one of:
  pass     median head lag over the last 15 min of the hold <= 24 s (2 slots)
  fail     median lag > 24 s at the end of the hold, or lag > 120 s for 5 min straight
  ceiling  the next step does not fit in MemAvailable (minus a margin), or the holder was OOM-killed
  window   the step would run past the run deadline (bandwidth cap returns at 13:00 UTC)
  abort    reth or lighthouse is not running
Then the memory is released and the run records how long the node takes to get back to head.
Every decision goes to data/orchestrator.log; per-step results to data/results.json.
"""
import json, os, statistics, subprocess, time, urllib.request
from datetime import datetime, timezone

BASE = "/home/andy/pir-ram"
DATA, STATE = f"{BASE}/data", f"{BASE}/state"
PLAN = json.load(open(f"{BASE}/plan.json"))
RPC = "http://127.0.0.1:8545"
LAG_OK = 24          # 2 slots
LAG_BAD, BAD_MIN = 120, 5
TAIL_MIN = 15
MARGIN_GB = 0.5
TICK = 15


def now():
    return datetime.now(timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    with open(f"{DATA}/orchestrator.log", "a") as f:
        f.write(f"{iso()} {msg}\n")


def put(name, value):
    with open(f"{STATE}/{name}", "w") as f:
        f.write(str(value))


def get(name, default=""):
    try:
        return open(f"{STATE}/{name}").read().strip()
    except OSError:
        return default


def load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def lag():
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                           "params": ["latest", False]}).encode()
        req = urllib.request.Request(RPC, body, {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(time.time()) - int(json.load(r)["result"]["timestamp"], 16)
    except Exception:
        return None


def mem_available_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 2**20


def services_ok():
    return all(sh("systemctl", "is-active", "--quiet", u).returncode == 0 for u in ("reth", "lighthouse"))


def prepare():
    """Test conditions: no swap, DAppNode core containers stopped. Both come back on reboot."""
    sh("/sbin/swapoff", "-a")
    names = [n for n in open(f"{BASE}/stopped-containers.txt").read().split() if n]
    running = sh("docker", "ps", "--format", "{{.Names}}").stdout.split()
    stop = [n for n in names if n in running]
    if stop:
        sh("docker", "stop", *stop)
        log(f"prepare: stopped {len(stop)} DAppNode containers")


def restore():
    sh("/sbin/swapon", "-a")
    names = [n for n in open(f"{BASE}/stopped-containers.txt").read().split() if n]
    sh("docker", "start", *names)
    sh("systemctl", "enable", "--now", "recreate-dappnode.timer")
    sh("systemctl", "disable", "--now", "pir-guard.timer")
    log("restore: swap on, DAppNode containers started, recreate-dappnode.timer re-enabled, guard off")


def steps_for(run, results):
    p = run["params"]
    if run["mode"] == "coarse":
        return [p["start_gb"] + i * p["step_gb"] for i in range(int((p["max_gb"] - p["start_gb"]) / p["step_gb"]) + 1)]
    ref = [s for s in results if s["run"] == run["ref"]]
    passed = [s["gb"] for s in ref if s["outcome"] == "pass"]
    top = max(passed) if passed else p["step_gb"]
    start = max(p["step_gb"], top - p["below_gb"])
    if run["mode"] == "replicate":
        same = sorted({s["gb"] for s in results if s["run"] == run["ref"] and "gb" in s})
        if same:
            # Repeat the reference steps, then keep going past them (with this run's margin).
            nxt = same[-1] + p["step_gb"]
            return same + [round(nxt + i * p["step_gb"], 2) for i in range(int((p["max_gb"] - nxt) / p["step_gb"]) + 1)]
    return [round(start + i * p["step_gb"], 2) for i in range(int((p["max_gb"] - start) / p["step_gb"]) + 1)]


def wait_head(max_min):
    """Wait until lag stays <= LAG_OK for 5 min. Returns minutes waited, or None on timeout."""
    t0, ok_since = time.time(), None
    while time.time() - t0 < max_min * 60:
        l = lag()
        if l is not None and l <= LAG_OK:
            ok_since = ok_since or time.time()
            if time.time() - ok_since >= 300:
                return round((time.time() - t0) / 60, 1)
        else:
            ok_since = None
        time.sleep(TICK)
    return None


def holder_restarts():
    out = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", "pir-holder"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def hold(run_id, gb, minutes, deadline):
    """Hold one squeeze level. Returns the step record."""
    rec = {"run": run_id, "gb": gb, "start": iso(), "hold_min": minutes}
    put("target_gb", gb)
    put("phase", f"{run_id}:{gb:g}")
    lags, mins, bad_since, short_since = [], [], None, None
    t0 = time.time()
    outcome = "pass"
    restarts0 = holder_restarts()
    while time.time() - t0 < minutes * 60:
        time.sleep(TICK)
        l, m = lag(), mem_available_gb()
        lags.append((time.time(), l))
        mins.append(m)
        if not services_ok():
            outcome = "abort"
            break
        if holder_restarts() > restarts0:
            # systemd restarts the holder and it refills in ~20 s, so the shortfall check below can miss a kill
            outcome = "oom"
            rec["note"] = "holder OOM-killed by the kernel"
            break
        held = float(get("held_gb", "0") or 0)
        if held < gb - 0.3 and time.time() - t0 > 120:
            short_since = short_since or time.time()
            if time.time() - short_since > 60:
                outcome = "ceiling"
                rec["note"] = f"holder at {held} GB of {gb} GB (OOM-killed or could not allocate)"
                break
        else:
            short_since = None
        if l is not None and l > LAG_BAD:
            bad_since = bad_since or time.time()
            if time.time() - bad_since >= BAD_MIN * 60:
                outcome = "fail"
                rec["note"] = f"lag > {LAG_BAD} s for {BAD_MIN} min"
                break
        else:
            bad_since = None
    tail = [l for t, l in lags if t >= time.time() - TAIL_MIN * 60 and l is not None]
    allv = [l for _, l in lags if l is not None]
    if outcome == "pass" and tail and statistics.median(tail) > LAG_OK:
        outcome = "fail"
        rec["note"] = f"median lag over last {TAIL_MIN} min > {LAG_OK} s"
    rec.update(end=iso(), outcome=outcome,
               lag_median_s=statistics.median(allv) if allv else None,
               lag_tail_median_s=statistics.median(tail) if tail else None,
               lag_max_s=max(allv) if allv else None,
               mem_available_min_gb=round(min(mins), 2) if mins else None,
               held_gb=float(get("held_gb", "0") or 0))
    return rec


def import_p95_ms(phase, since_iso):
    """p95 of Reth block import time for blocks logged in this phase since the step start."""
    import csv
    t0 = datetime.fromisoformat(since_iso.replace("Z", "+00:00")).timestamp()
    try:
        vals = sorted(float(r["import_ms"]) for r in csv.DictReader(open(f"{DATA}/blocks.csv"))
                      if r["phase"] == phase and r["import_ms"] and float(r["added_ts"]) >= t0)
    except (OSError, ValueError):
        return None
    return vals[int(0.95 * (len(vals) - 1))] if vals else None


def recover(run_id, max_min=90):
    put("target_gb", 0)
    put("phase", f"{run_id}:recovery")
    t0 = time.time()
    waited = wait_head(max_min)
    return {"run": run_id, "released": iso(), "back_at_head_min": waited,
            "note": "minutes until lag <= 24 s held for 5 min (includes those 5 min)"}


def do_run(run, results, recoveries):
    rid, deadline = run["id"], datetime.fromisoformat(run["deadline"].replace("Z", "+00:00"))
    prepare()
    put("phase", f"{rid}:precheck")
    log(f"run {rid}: precheck, waiting for head")
    w = wait_head(45)
    log(f"run {rid}: at head after {w} min" if w is not None else f"run {rid}: not at head after 45 min, starting anyway")
    steps = steps_for(run, results)
    hold_min = run["params"]["hold_min"]
    log(f"run {rid}: steps {steps} GB, {hold_min} min each, deadline {run['deadline']}")
    last = 0.0
    # Ramp up to just below the first step in 2 GB moves, 2 min each, so the ceiling check
    # compares one small step at a time (a single 0 -> 16 GB jump fails it by construction).
    put("phase", f"{rid}:ramp")
    for g in range(2, int(steps[0]) - 1, 2):
        if mem_available_gb() - (g - last) < MARGIN_GB:
            break
        put("target_gb", g)
        time.sleep(120)
        last = float(g)
    if last:
        log(f"run {rid}: ramped to {last:g} GB (MemAvailable {mem_available_gb():.2f})")
    for gb in steps:
        if (now().timestamp() + hold_min * 60) > deadline.timestamp():
            results.append({"run": rid, "gb": gb, "start": iso(), "outcome": "window", "note": "past deadline, not run"})
            log(f"run {rid}: {gb} GB skipped, past deadline")
            break
        avail = mem_available_gb()
        margin = run["params"].get("margin_gb", MARGIN_GB)
        if avail - (gb - last) < margin:
            results.append({"run": rid, "gb": gb, "start": iso(), "outcome": "ceiling",
                            "note": f"MemAvailable {avail:.2f} GB cannot fit +{gb - last:g} GB with {margin} GB margin"})
            log(f"run {rid}: ceiling before {gb} GB (MemAvailable {avail:.2f})")
            break
        log(f"run {rid}: step {gb} GB start (MemAvailable {avail:.2f})")
        rec = hold(rid, gb, hold_min, deadline)
        if rec["outcome"] == "fail":
            p95 = import_p95_ms(f"{rid}:{gb:g}", rec["start"])
            rec["import_p95_ms"] = p95
            if p95 is not None and p95 < 4000 and not rec.get("retried"):
                # Reth kept importing fast, so the lag came from block arrival (network / CL peers),
                # not from RAM. Record it and repeat the same level once.
                rec["outcome"] = "net"
                results.append(rec)
                save(f"{DATA}/results.json", results)
                log(f"run {rid}: step {gb} GB lagged but import p95 {p95:.0f} ms -> 'net', repeating once")
                rec = hold(rid, gb, hold_min, deadline)
                rec["retried"] = True
                if rec["outcome"] == "fail":
                    rec["import_p95_ms"] = import_p95_ms(f"{rid}:{gb:g}", rec["start"])
        results.append(rec)
        save(f"{DATA}/results.json", results)
        log(f"run {rid}: step {gb} GB -> {rec['outcome']} (tail median lag {rec.get('lag_tail_median_s')} s, "
            f"max {rec.get('lag_max_s')} s, min MemAvailable {rec.get('mem_available_min_gb')} GB)")
        last = gb
        if rec["outcome"] != "pass":
            break
    rec = recover(rid)
    recoveries.append(rec)
    log(f"run {rid}: released; back at head after {rec['back_at_head_min']} min")
    save(f"{DATA}/results.json", results)
    save(f"{DATA}/recoveries.json", recoveries)


def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    st = load(f"{STATE}/orchestrator.json", {"done": [], "current": None})
    results = load(f"{DATA}/results.json", [])
    recoveries = load(f"{DATA}/recoveries.json", [])
    log(f"start; done={st['done']} current={st['current']}")
    if st["current"]:
        rid = st["current"]
        run = next(r for r in PLAN["runs"] if r["id"] == rid)
        deadline = datetime.fromisoformat(run["deadline"].replace("Z", "+00:00"))
        retry = rid not in st.setdefault("retried", []) and (deadline - now()).total_seconds() > 3 * 3600
        results.append({"run": rid, "start": iso(), "outcome": "interrupted",
                        "note": "orchestrator restarted mid-run (reboot or crash)" + ("; retrying" if retry else "")})
        if retry:
            st["retried"].append(rid)
            results[:] = [r for r in results if r["run"] != rid or r["outcome"] == "interrupted"]
        else:
            st["done"].append(rid)
        st["current"] = None
        save(f"{DATA}/results.json", results)
        save(f"{STATE}/orchestrator.json", st)
        put("target_gb", 0)
        log(f"run {rid}: marked interrupted")
    if len(st["done"]) < len(PLAN["runs"]):
        prepare()
    put("target_gb", 0)
    if not get("phase") or ":" in get("phase"):
        put("phase", "baseline")
    while True:
        pending = [r for r in PLAN["runs"] if r["id"] not in st["done"]]
        if not pending:
            if not st.get("restored"):
                put("target_gb", 0)
                put("phase", "done")
                restore()
                st["restored"] = iso()
                save(f"{STATE}/orchestrator.json", st)
                subprocess.run(["python3", f"{BASE}/report.py"])
                log("all runs done; report written to data/report.md")
            time.sleep(3600)
            continue
        run = pending[0]
        start = datetime.fromisoformat(run["start"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(run["deadline"].replace("Z", "+00:00"))
        if now() >= deadline:
            results.append({"run": run["id"], "start": iso(), "outcome": "missed", "note": "deadline passed before start"})
            save(f"{DATA}/results.json", results)
            st["done"].append(run["id"])
            save(f"{STATE}/orchestrator.json", st)
            log(f"run {run['id']}: missed")
            continue
        if now() < start:
            if time.time() - st.get("_prep", 0) > 600:
                prepare()
                st["_prep"] = time.time()
            phase = "baseline" if not st["done"] else "rest"
            if get("phase") != phase:
                put("phase", phase)
            time.sleep(min(60, (start - now()).total_seconds() + 1))
            continue
        st["current"] = run["id"]
        save(f"{STATE}/orchestrator.json", st)
        try:
            do_run(run, results, recoveries)
        finally:
            put("target_gb", 0)
        st["done"].append(run["id"])
        st["current"] = None
        save(f"{STATE}/orchestrator.json", st)
        put("phase", "rest")
        subprocess.run(["python3", f"{BASE}/report.py"])


if __name__ == "__main__":
    main()
