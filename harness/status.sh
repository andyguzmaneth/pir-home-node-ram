#!/bin/sh
# Quick status of the PIR RAM test. Run on VM101: ~/pir-ram/status.sh
D=/home/andy/pir-ram
echo "phase=$(cat $D/state/phase 2>/dev/null) target=$(cat $D/state/target_gb 2>/dev/null) GB held=$(cat $D/state/held_gb 2>/dev/null) GB"
systemctl is-active reth lighthouse pir-collect pir-blocks pir-holder pir-orchestrator | paste -sd' '
tail -n 1 $D/data/samples.csv | awk -F, '{print "last sample", $1, "lag", $6 "s", "avail", $9 "GB", "cache", $11 "GB"}'
tail -n 8 $D/data/orchestrator.log 2>/dev/null
