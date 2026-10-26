# pir-home-node-ram

How much RAM does a 32 GB Ethereum home node (EIP-7870 memory tier) leave for a PIR server?

Report: https://andyguzmaneth.github.io/pir-home-node-ram/

## Result

Test: a Reth + Lighthouse mainnet node on a 32 GB machine, 2026-10-24 to 2026-10-26. A dummy process stood in for the PIR server and held RAM in steps.

| PIR server RAM | Node at head | Confidence | PIR DB at 4x blowup | PIR DB at 1.6x blowup |
|---|---|---|---|---|
| up to 16 GB | yes | high | 4.0 GB | 10.0 GB |
| 18 GB | yes, in 2 runs, 0.7-1.2 GB of RAM left | high | 4.5 GB | 11.2 GB |
| 18.5 GB | no: the kernel killed the PIR process 5 times in 35 min | high | | |

GB means GiB (2^30 bytes) throughout.

- Reth, Lighthouse and the kernel use about 12.1 GB. With no PIR server, the rest of the RAM fills with page cache (15.5 GB, mostly Reth's database). The PIR memory came out of that cache.
- Holding RAM never made the node fall behind head. Reth read more from disk (0.8 to 3.6 MB/s at 18 GB), and block import stayed under 350 ms (95th percentile).
- When the node fell behind, Lighthouse had stopped sending new blocks to Reth. This also happened with no PIR memory held.
- 4x is a rule of thumb for PIR memory blowup. 1.6x is the resident footprint of InsPIRe on GPU.
- `data/results.json` is the orchestrator's raw output and lists B 18.5 GB as a pass: systemd restarted the killed process and it refilled in about 20 s, so the orchestrator did not see the kills. `data/oom-kills.log` has the kernel records, and the report marks the step "oom". The orchestrator now fails a step on any restart of the PIR process.

The report has the caveats, the confidence levels and the time-series charts.

## Layout

| Path | Contents |
|---|---|
| `harness/` | Test harness: collector, block logger, dummy PIR server, orchestrator, systemd units, `plan.json` |
| `data/` | Raw data from the test: `samples.csv` (every 15 s), `blocks.csv` (one row per block), `results.json`, `recoveries.json`, `orchestrator.log`, `oom-kills.log` |
| `build_html.py`, `report_template.html` | Build the report from `data/` |
| `docs/index.html` | The built report (GitHub Pages) |

## Reproduce

Machine used: Proxmox VM, 32 GB RAM (ballooning off), 8 vCPU (Ryzen 9 9900X), 1.2 TB NVMe-backed disk, Debian 12. Reth v2.8.0 (`reth node --minimal`, Docker) and Lighthouse v8.3.0 (checkpoint sync).

1. Sync the node to head. Stop every other service. Turn swap off: `sudo swapoff -a`.
2. Copy `harness/` to `~/pir-ram` on the node. Install the units:
   `sudo cp ~/pir-ram/units/* /etc/systemd/system/ && sudo systemctl daemon-reload`
3. Edit `~/pir-ram/plan.json`: runs, step size, hold minutes, start time and deadline.
4. Start the harness:
   `sudo systemctl enable --now pir-collect pir-blocks pir-holder pir-orchestrator pir-guard.timer`
5. When the last run ends, the orchestrator turns swap back on and writes `data/report.md`. Build the HTML report:
   `python3 build_html.py ~/pir-ram/data docs/index.html`

The scripts expect the harness at `/home/andy/pir-ram`, the disk at `sda`, the NIC at `eth0` and Reth's DB at `/data/reth/db`. Change the constants at the top of each script for another machine. `guard.sh` stops DAppNode containers by name; remove that part on a node without DAppNode.

Rules: a step passes if the median lag over its last 15 minutes is at most 24 s (2 slots). It fails if the lag is above that, or above 120 s for 5 minutes in a row. A failed step where Reth's block import p95 stayed under 4 s is marked "net" and repeated once. The run stops when free RAM minus the next step is below the margin.

To test a real PIR server, replace `pir-holder` with the server, load a DB sized for each step, and send it a steady query load.

## License

CC0-1.0
