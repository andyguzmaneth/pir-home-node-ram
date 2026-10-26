#!/usr/bin/env python3
"""Dummy PIR server: hold state/target_gb of resident anonymous memory, and nothing else.

No CPU PIR build exists yet, so a process that only pins RAM stands in
for the PIR database. It touches each page once and then sleeps, so it uses no CPU, disk or
network. Memory is held in 256 MiB chunks; state/held_gb reports what is actually resident.
oom_score_adj=1000 makes the kernel kill this process before Reth or Lighthouse.
"""
import mmap, os, time

STATE = "/home/andy/pir-ram/state"
CHUNK = 256 * 2**20
PAGE = mmap.PAGESIZE


def main():
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
    chunks = []
    while True:
        try:
            target = float(open(f"{STATE}/target_gb").read().strip() or 0)
        except (OSError, ValueError):
            target = 0.0
        want = int(round(target * 2**30 / CHUNK))
        while len(chunks) < want:
            m = mmap.mmap(-1, CHUNK, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            for off in range(0, CHUNK, PAGE):
                m[off] = 1
            chunks.append(m)
            open(f"{STATE}/held_gb", "w").write(f"{len(chunks) * CHUNK / 2**30:.2f}")
        while len(chunks) > want:
            chunks.pop().close()
        open(f"{STATE}/held_gb", "w").write(f"{len(chunks) * CHUNK / 2**30:.2f}")
        time.sleep(2)


if __name__ == "__main__":
    main()
