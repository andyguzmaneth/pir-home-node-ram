#!/bin/sh
# Every minute until the orchestrator restores: keep swap off and DAppNode core stopped.
# DAppNode's recreate-dappnode.timer restarts dappmanager every 6 h, and dappmanager turns swap back on.
D=/home/andy/pir-ram
[ "$(cat $D/state/phase 2>/dev/null)" = done ] && exit 0
ts=$(date -u +%FT%TZ); ph=$(cat $D/state/phase)
if [ "$(wc -l < /proc/swaps)" -gt 1 ]; then
  used=$(awk 'NR>1{s+=$4} END{print s/1048576}' /proc/swaps)
  /sbin/swapoff -a && echo "$ts [$ph] swap was on (${used} GB used), turned off" >> $D/data/guard.log
fi
run=$(docker ps --format '{{.Names}}' | grep -xF -f $D/stopped-containers.txt)
if [ -n "$run" ]; then
  docker stop $run >/dev/null && echo "$ts [$ph] stopped: $(echo $run | tr '\n' ' ')" >> $D/data/guard.log
fi
