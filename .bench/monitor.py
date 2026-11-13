#!/usr/bin/env python3
"""Run a command and sample the resource use of its whole process tree.

Samples every --interval seconds: tree RSS/PSS, thread count, CPU time,
block-IO bytes and system-wide network bytes. Writes a CSV time series and a
JSON summary, and tees the child's output to a log file.

Usage: monitor.py --out DIR --label NAME -- <command> [args...]
"""

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
import time

HZ = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def read_stat(pid):
    """(ppid, pgrp, utime+stime, threads, rss_bytes) or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        rparen = raw.rfind(b")")
        rest = raw[rparen + 2 :].split()
        ppid = int(rest[1])
        pgrp = int(rest[2])
        ticks = int(rest[11]) + int(rest[12])
        threads = int(rest[17])
        return ppid, pgrp, ticks, threads
    except (OSError, IndexError, ValueError):
        return None


def read_rss_bytes(pid):
    try:
        with open(f"/proc/{pid}/statm", "rb") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return 0


def read_pss_bytes(pid):
    try:
        pss = 0
        with open(f"/proc/{pid}/smaps_rollup", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Pss:"):
                    pss = int(line.split()[1]) * 1024
                    break
        return pss
    except (OSError, ValueError, IndexError):
        return 0


def read_io(pid):
    try:
        vals = {}
        with open(f"/proc/{pid}/io", "r", encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition(":")
                vals[key.strip()] = int(val)
        return vals.get("read_bytes", 0), vals.get("write_bytes", 0), vals.get("rchar", 0), vals.get("wchar", 0)
    except (OSError, ValueError):
        return 0, 0, 0, 0


def read_net():
    rx = tx = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, data = line.split(":", 1)
                fields = data.split()
                if name.strip() in ("lo", "docker0", "virbr0"):
                    continue
                rx += int(fields[0])
                tx += int(fields[8])
    except (OSError, ValueError, IndexError):
        pass
    return rx, tx


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition(":")
                info[key.strip()] = val.strip()
    except OSError:
        pass
    return info


def tree_members(pgid):
    """PIDs in the child's process group."""
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        st = read_stat(pid)
        if st is not None and st[1] == pgid:
            members.append((pid, st))
    return members


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        sys.exit("no command given")

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    log_path = os.path.join(out, f"{args.label}.log")
    csv_path = os.path.join(out, f"{args.label}.csv")
    summary_path = os.path.join(out, f"{args.label}.json")

    mem_before = read_meminfo()
    net_rx0, net_tx0 = read_net()

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    t0 = time.monotonic()
    started_at = now_iso()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    pgid = proc.pid

    logf = open(log_path, "w", encoding="utf-8", errors="replace", buffering=1)

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            logf.write(raw.decode("utf-8", "replace"))
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    csvf = open(csv_path, "w", encoding="utf-8", buffering=1)
    csvf.write(
        "t,rss_mb,pss_mb,threads,nprocs,cpu_pct,cpu_pct_machine,"
        "disk_read_mb,disk_write_mb,sys_read_mb,sys_write_mb,net_rx_mb,net_tx_mb,mem_available_mb\n"
    )

    rows = []
    cpu_ticks_prev = {}
    io_prev = {}
    net_prev = (net_rx0, net_tx0)
    cpu_sec_total = 0.0
    disk_read_total = disk_write_total = 0
    sys_read_total = sys_write_total = 0
    net_rx_total = net_tx_total = 0
    peak_rss = 0
    peak_pss = 0
    peak_rss_t = 0.0
    peak_threads = 0
    stop = threading.Event()

    def on_term(signum, frame):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_term)

    last_t = time.monotonic()
    while True:
        exited = proc.poll() is not None
        tnow = time.monotonic()
        elapsed = tnow - t0
        dt_s = max(tnow - last_t, 1e-6)
        last_t = tnow

        members = tree_members(pgid)
        rss = pss = 0
        threads = 0
        delta_ticks = 0
        for pid, st in members:
            rss += read_rss_bytes(pid)
            pss += read_pss_bytes(pid)
            threads += st[3]
            # CPU time from tick deltas, accumulating across exiting PIDs.
            delta_ticks += max(st[2] - cpu_ticks_prev.get(pid, st[2]), 0)
            cpu_ticks_prev[pid] = st[2]
            io = read_io(pid)
            prev = io_prev.get(pid)
            if prev is not None:
                disk_read_total += max(io[0] - prev[0], 0)
                disk_write_total += max(io[1] - prev[1], 0)
                sys_read_total += max(io[2] - prev[2], 0)
                sys_write_total += max(io[3] - prev[3], 0)
            io_prev[pid] = io

        cpu_sec = delta_ticks / HZ
        cpu_sec_total += cpu_sec
        cpu_pct = cpu_sec / dt_s * 100
        cpu_pct_machine = cpu_pct / NCPU

        net_rx, net_tx = read_net()
        net_rx_total += max(net_rx - net_prev[0], 0)
        net_tx_total += max(net_tx - net_prev[1], 0)
        net_prev = (net_rx, net_tx)

        if rss > peak_rss:
            peak_rss, peak_pss, peak_rss_t = rss, pss, elapsed
        peak_threads = max(peak_threads, threads)

        mem = read_meminfo()
        avail = mem.get("MemAvailable", "0 kB").split()[0]
        csvf.write(
            f"{elapsed:.2f},{rss/2**20:.1f},{pss/2**20:.1f},{threads},{len(members)},"
            f"{cpu_pct:.1f},{cpu_pct_machine:.2f},"
            f"{disk_read_total/2**20:.1f},{disk_write_total/2**20:.1f},"
            f"{sys_read_total/2**20:.1f},{sys_write_total/2**20:.1f},"
            f"{net_rx_total/2**20:.1f},{net_tx_total/2**20:.1f},{int(avail)/1024:.0f}\n"
        )
        rows.append((elapsed, rss, cpu_pct, threads))

        if exited and not members:
            break
        if stop.is_set():
            break
        time.sleep(max(0.0, args.interval - (time.monotonic() - tnow)))

    exit_code = proc.wait()
    reader.join(timeout=5)
    logf.close()
    csvf.close()

    # Final settle sample for any buffered IO.
    time.sleep(0.5)

    wall = time.monotonic() - t0
    cpu_mean = (cpu_sec_total / wall * 100) if wall > 0 else 0
    peak_cpu = max((r[2] for r in rows), default=0)
    # Peak over a 3-sample moving average smooths single-tick spikes.
    win = max(1, int(3 / args.interval)) if args.interval > 0 else 3
    moving = [sum(r[2] for r in rows[i:i + win]) / len(rows[i:i + win]) for i in range(max(1, len(rows) - win + 1))]
    peak_cpu_smooth = max(moving) if moving else peak_cpu

    summary = {
        "label": args.label,
        "command": cmd,
        "started_at": started_at,
        "finished_at": now_iso(),
        "wall_seconds": round(wall, 1),
        "exit_code": exit_code,
        "cpu_seconds_total": round(cpu_sec_total, 1),
        "cpu_pct_mean_of_one_core": round(cpu_mean, 1),
        "cpu_pct_mean_of_machine": round(cpu_mean / NCPU, 1),
        "cpu_pct_peak": round(peak_cpu, 1),
        "cpu_pct_peak_smoothed": round(peak_cpu_smooth, 1),
        "peak_rss_mb": round(peak_rss / 2**20, 1),
        "peak_pss_mb": round(peak_pss / 2**20, 1),
        "peak_rss_at_seconds": round(peak_rss_t, 1),
        "peak_threads": peak_threads,
        "disk_read_mb": round(disk_read_total / 2**20, 1),
        "disk_write_mb": round(disk_write_total / 2**20, 1),
        "syscall_read_mb": round(sys_read_total / 2**20, 1),
        "syscall_write_mb": round(sys_write_total / 2**20, 1),
        "net_rx_mb": round(net_rx_total / 2**20, 1),
        "net_tx_mb": round(net_tx_total / 2**20, 1),
        "samples": len(rows),
        "mem_available_before_mb": int(mem_before.get("MemAvailable", "0 kB").split()[0]) // 1024,
        "mem_available_after_mb": int(read_meminfo().get("MemAvailable", "0 kB").split()[0]) // 1024,
        "log": log_path,
        "csv": csv_path,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
