#!/usr/bin/env python3
"""Sample node, OS and network state every INTERVAL seconds into data/samples.csv.

One row per sample. Rates (CPU %, disk MB/s, faults/s, ...) are deltas over the interval.
Runs as root (needs /proc/<pid>/status of the clients and fincore on the Reth DB).
"""
import csv, json, os, subprocess, threading, time, urllib.request

BASE = "/home/andy/pir-ram"
DATA = f"{BASE}/data"
STATE = f"{BASE}/state"
OUT = f"{DATA}/samples.csv"
INTERVAL = 15
RPC = "http://127.0.0.1:8545"
DISK, NIC = "sda", "eth0"
MDBX = "/data/reth/db/mdbx.dat"

FIELDS = [
    "ts", "phase", "target_gb", "held_gb",
    "head", "head_lag_s", "peers",
    "mem_total_gb", "mem_available_gb", "mem_free_gb", "cached_gb", "active_file_gb",
    "inactive_file_gb", "anon_gb", "dirty_mb", "swap_used_gb",
    "el_rss_anon_gb", "el_rss_file_gb", "cl_rss_gb", "mdbx_resident_gb",
    "cpu_user_pct", "cpu_sys_pct", "cpu_iowait_pct", "cpu_steal_pct", "cpu_idle_pct",
    "disk_read_mbs", "disk_write_mbs", "disk_read_iops", "disk_write_iops",
    "disk_read_await_ms", "disk_util_pct",
    "net_rx_mbit", "net_tx_mbit", "bwcap",
    "el_majflt_s", "pgmajfault_s", "refault_file_s", "pgscan_s",
    "psi_mem_some_pct", "psi_mem_full_pct", "psi_io_some_pct", "psi_io_full_pct", "psi_cpu_some_pct",
    "cl_backfill_slots",
]


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(RPC, body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)["result"]


def pid_of(name):
    out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    return m


def proc_status(pid):
    s = {}
    for line in read(f"/proc/{pid}/status").splitlines():
        if line.startswith(("RssAnon", "RssFile", "VmRSS")):
            k, v = line.split(":", 1)
            s[k] = int(v.split()[0]) * 1024
    return s


def majflt(pid):
    st = read(f"/proc/{pid}/stat")
    return int(st.rsplit(")", 1)[1].split()[9]) if st else None


def counters():
    c = {}
    cpu = read("/proc/stat").splitlines()[0].split()[1:]
    c["cpu"] = [int(x) for x in cpu[:8]]  # user nice system idle iowait irq softirq steal
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if f[2] == DISK:
            c["disk"] = [int(f[i]) for i in (3, 5, 6, 7, 9, 12)]  # rd_ios rd_sect rd_ms wr_ios wr_sect io_ms
    for line in read("/proc/net/dev").splitlines():
        if line.strip().startswith(NIC + ":"):
            f = line.split(":", 1)[1].split()
            c["net"] = [int(f[0]), int(f[8])]
    vm = dict(l.split() for l in read("/proc/vmstat").splitlines())
    c["vm"] = [int(vm.get(k, 0)) for k in ("pgmajfault", "workingset_refault_file")] + [
        sum(int(v) for k, v in vm.items() if k.startswith("pgscan_"))]
    psi = []
    for res in ("memory", "io", "cpu"):
        lines = read(f"/proc/pressure/{res}").splitlines()
        tot = {l.split()[0]: int(l.split("total=")[1]) for l in lines}
        psi += [tot.get("some", 0), tot.get("full", 0)]
    c["psi"] = psi
    pid = pid_of("reth-binary")
    c["el_majflt"] = majflt(pid) if pid else None
    c["t"] = time.monotonic()
    return c


class Slow:
    """Values that are too slow to read every sample, refreshed in a background thread."""
    mdbx_gb = ""
    backfill = ""

    def loop(self):
        while True:
            try:
                out = subprocess.run(["fincore", "-b", "-n", "-o", "RES", MDBX],
                                     capture_output=True, text=True, timeout=120).stdout.split()
                self.mdbx_gb = round(int(out[0]) / 2**30, 2) if out else ""
            except Exception:
                self.mdbx_gb = ""
            try:
                log = subprocess.run(["journalctl", "-u", "lighthouse", "-n", "60", "-o", "cat", "--no-pager"],
                                     capture_output=True, text=True, timeout=30).stdout.splitlines()
                hist = [l for l in log if "Downloading historical blocks" in l]
                if hist:
                    self.backfill = hist[-1].split('distance: "')[1].split()[0]
                else:
                    self.backfill = 0 if log else ""
            except Exception:
                self.backfill = ""
            time.sleep(300)


def main():
    os.makedirs(DATA, exist_ok=True)
    slow = Slow()
    threading.Thread(target=slow.loop, daemon=True).start()
    new = not os.path.exists(OUT)
    f = open(OUT, "a", newline="")
    w = csv.DictWriter(f, FIELDS)
    if new:
        w.writeheader()
    prev = counters()
    time.sleep(INTERVAL)
    while True:
        cur = counters()
        dt = cur["t"] - prev["t"]
        row = {k: "" for k in FIELDS}
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["phase"] = read(f"{STATE}/phase", "baseline")
        row["target_gb"] = read(f"{STATE}/target_gb", "0")
        row["held_gb"] = read(f"{STATE}/held_gb", "0")
        try:
            b = rpc("eth_getBlockByNumber", ["latest", False])
            row["head"] = int(b["number"], 16)
            row["head_lag_s"] = int(time.time()) - int(b["timestamp"], 16)
            row["peers"] = int(rpc("net_peerCount"), 16)
        except Exception:
            pass
        m = meminfo()
        g = lambda k: round(m.get(k, 0) / 2**30, 2)
        row.update(mem_total_gb=g("MemTotal"), mem_available_gb=g("MemAvailable"), mem_free_gb=g("MemFree"),
                   cached_gb=g("Cached"), active_file_gb=g("Active(file)"), inactive_file_gb=g("Inactive(file)"),
                   anon_gb=g("AnonPages"), dirty_mb=round(m.get("Dirty", 0) / 2**20, 1),
                   swap_used_gb=round((m.get("SwapTotal", 0) - m.get("SwapFree", 0)) / 2**30, 2))
        for name, keys in (("reth-binary", ("el_rss_anon_gb", "el_rss_file_gb")), ("lighthouse", ("cl_rss_gb",))):
            pid = pid_of(name)
            if pid:
                s = proc_status(pid)
                if name == "lighthouse":
                    row["cl_rss_gb"] = round(s.get("VmRSS", 0) / 2**30, 2)
                else:
                    row["el_rss_anon_gb"] = round(s.get("RssAnon", 0) / 2**30, 2)
                    row["el_rss_file_gb"] = round(s.get("RssFile", 0) / 2**30, 2)
        row["mdbx_resident_gb"] = slow.mdbx_gb
        row["cl_backfill_slots"] = slow.backfill
        d = [a - b for a, b in zip(cur["cpu"], prev["cpu"])]
        tot = sum(d) or 1
        row.update(cpu_user_pct=round(100 * (d[0] + d[1]) / tot, 1), cpu_sys_pct=round(100 * (d[2] + d[5] + d[6]) / tot, 1),
                   cpu_idle_pct=round(100 * d[3] / tot, 1), cpu_iowait_pct=round(100 * d[4] / tot, 1),
                   cpu_steal_pct=round(100 * d[7] / tot, 1))
        if "disk" in cur and "disk" in prev:
            rd_ios, rd_sect, rd_ms, wr_ios, wr_sect, io_ms = [a - b for a, b in zip(cur["disk"], prev["disk"])]
            row.update(disk_read_mbs=round(rd_sect * 512 / 2**20 / dt, 2), disk_write_mbs=round(wr_sect * 512 / 2**20 / dt, 2),
                       disk_read_iops=round(rd_ios / dt, 1), disk_write_iops=round(wr_ios / dt, 1),
                       disk_read_await_ms=round(rd_ms / rd_ios, 3) if rd_ios else "",
                       disk_util_pct=round(100 * io_ms / (dt * 1000), 1))
        if "net" in cur and "net" in prev:
            rx, tx = [a - b for a, b in zip(cur["net"], prev["net"])]
            row.update(net_rx_mbit=round(rx * 8 / 1e6 / dt, 2), net_tx_mbit=round(tx * 8 / 1e6 / dt, 2))
        qd = subprocess.run(["tc", "qdisc", "show", "dev", NIC], capture_output=True, text=True).stdout
        row["bwcap"] = "on" if "tbf" in qd else "off"
        if cur["el_majflt"] is not None and prev["el_majflt"] is not None and cur["el_majflt"] >= prev["el_majflt"]:
            row["el_majflt_s"] = round((cur["el_majflt"] - prev["el_majflt"]) / dt, 1)
        v = [a - b for a, b in zip(cur["vm"], prev["vm"])]
        row.update(pgmajfault_s=round(v[0] / dt, 1), refault_file_s=round(v[1] / dt, 1), pgscan_s=round(v[2] / dt, 1))
        p = [a - b for a, b in zip(cur["psi"], prev["psi"])]  # microseconds stalled
        pct = lambda us: round(100 * us / (dt * 1e6), 2)
        row.update(psi_mem_some_pct=pct(p[0]), psi_mem_full_pct=pct(p[1]), psi_io_some_pct=pct(p[2]),
                   psi_io_full_pct=pct(p[3]), psi_cpu_some_pct=pct(p[4]))
        w.writerow(row)
        f.flush()
        prev = cur
        time.sleep(max(1, INTERVAL - (time.monotonic() - cur["t"])))


if __name__ == "__main__":
    main()
