#!/usr/bin/env python3
"""Choose make parallelism for the Linux CI runner with a memory guard."""

from pathlib import Path
import subprocess
import sys

KIB_PER_GIB = 1024**2


def build_jobs(cpu_count, available_kib):
    if cpu_count < 1 or available_kib < 0:
        raise ValueError("Invalid runner resource measurements")
    # Leave 1 GiB for the runner and budget 3 GiB per make job for Go/Rust/C++.
    memory_jobs = max(1, (available_kib - KIB_PER_GIB) // (3 * KIB_PER_GIB))
    return min(cpu_count, memory_jobs)


def available_memory(meminfo):
    for line in meminfo.splitlines():
        fields = line.split()
        if fields and fields[0] == "MemAvailable:":
            if len(fields) != 3 or fields[2] != "kB":
                raise ValueError("Unexpected MemAvailable format")
            return int(fields[1])
    raise ValueError("Runner did not report MemAvailable")


if __name__ == "__main__":
    cpus = int(subprocess.check_output(["nproc"], text=True).strip())
    memory = available_memory(Path("/proc/meminfo").read_text())
    jobs = build_jobs(cpus, memory)
    print(f"Runner: {cpus} CPUs, {memory / KIB_PER_GIB:.1f} GiB available; make -j{jobs}", file=sys.stderr)
    print(jobs)
