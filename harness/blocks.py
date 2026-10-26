#!/usr/bin/env python3
"""Log one row per imported block, and client warnings, from the journal.

data/blocks.csv splits head lag into its two causes:
  arrival_s = payload received by Reth - block timestamp   (network + CL gossip/validation)
  import_ms = Reth's own execution + state-root time         (CPU + RAM/disk)
data/events.log keeps Reth and Lighthouse WARN/ERROR lines with the current phase.
"""
import csv, json, os, re, subprocess, sys, threading, time, urllib.request
from datetime import datetime

BASE = "/home/andy/pir-ram"
DATA, STATE = f"{BASE}/data", f"{BASE}/state"
RPC = "http://127.0.0.1:8545"
FIELDS = ["number", "block_ts", "recv_ts", "added_ts", "arrival_s", "import_ms", "added_lag_s",
          "txs", "gas_mgas", "peers", "phase", "target_gb", "held_gb"]
TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
KV = re.compile(r"(\w+)=(\S+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
lock = threading.Lock()


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def epoch(s):
    return datetime.fromisoformat(s + "+00:00").timestamp()


def dur_ms(s):
    for unit, mult in (("ns", 1e-6), ("µs", 1e-3), ("us", 1e-3), ("ms", 1.0), ("s", 1000.0)):
        if s.endswith(unit):
            return float(s[: -len(unit)]) * mult
    return None


def block_ts(n):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                       "params": [hex(n), False]}).encode()
    req = urllib.request.Request(RPC, body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return int(json.load(r)["result"]["timestamp"], 16)


def event(src, line):
    with lock, open(f"{DATA}/events.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{read(f'{STATE}/phase', 'baseline')}] {src}: {line}\n")


def follow(unit, since=None, until=None):
    win = ["--since", since, "--until", until] if since else ["-f", "-n", "0"]
    return subprocess.Popen(["journalctl", "-u", unit, *win, "-o", "cat", "--no-pager"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)


def lighthouse():
    for line in follow("lighthouse").stdout:
        line = ANSI.sub("", line)
        if re.search(r"\b(WARN|ERRO|CRIT)\b", line):
            event("lighthouse", line.strip())


def main():
    """Live mode follows the journal. Replay: blocks.py SINCE UNTIL OUT.csv rebuilds a past window."""
    replay = sys.argv[1:4] if len(sys.argv) == 4 else None
    os.makedirs(DATA, exist_ok=True)
    if not replay:
        threading.Thread(target=lighthouse, daemon=True).start()
    out = replay[2] if replay else f"{DATA}/blocks.csv"
    new = not os.path.exists(out) or os.path.getsize(out) == 0
    f = open(out, "a", newline="")
    w = csv.DictWriter(f, FIELDS)
    if new:
        w.writeheader()
    recv = {}
    for line in follow("reth", *(replay[:2] if replay else ())).stdout:
        line = ANSI.sub("", line)
        m = TS.match(line)
        if not replay and (" WARN " in line or " ERROR " in line):
            event("reth", line.strip())
        if not m:
            continue
        t = epoch(m.group(1))
        kv = dict(KV.findall(line))
        if "Received new payload" in line and "number" in kv:
            recv[int(kv["number"])] = t
        elif "Block added to canonical chain" in line and "number" in kv:
            n = int(kv["number"])
            try:
                bts = block_ts(n)
            except Exception:
                bts = None
            r = recv.pop(n, None)
            for k in [k for k in recv if k < n - 64]:
                del recv[k]
            gas = kv.get("gas_used", "")
            w.writerow({
                "number": n, "block_ts": bts, "recv_ts": round(r, 3) if r else "", "added_ts": round(t, 3),
                "arrival_s": round(r - bts, 3) if r and bts else "",
                "import_ms": round(dur_ms(kv.get("elapsed", "")) or 0, 2) if kv.get("elapsed") else "",
                "added_lag_s": round(t - bts, 3) if bts else "",
                "txs": kv.get("txs", ""), "gas_mgas": gas.replace("Mgas", "") if gas.endswith("Mgas") else gas,
                "peers": kv.get("peers", ""), "phase": "reboot-catchup" if replay else read(f"{STATE}/phase", "baseline"),
                "target_gb": read(f"{STATE}/target_gb", "0"), "held_gb": read(f"{STATE}/held_gb", "0"),
            })
            f.flush()


if __name__ == "__main__":
    main()
