"""Build the sparse pair, report work/memory/time, and prove the library
is self-contained by querying a short interval after the input package
has been moved away.

Usage: python3 tools/bench_sparse.py [--workdir out/sparse]
              [--endpoints N] [--mem-budget-mb M]
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def run_build(pkg, out, budget):
    t0 = time.monotonic()
    proc = subprocess.run(
        [PY, "-m", "subwarehouse", "build", "--package", pkg, "--root",
         "R", "--out", out, "--mem-budget-mb", str(budget), "--stats"],
        cwd=ROOT, capture_output=True, text=True)
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("build failed for %s" % pkg)
    return json.loads(proc.stdout), wall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out",
                                                      "sparse"))
    ap.add_argument("--endpoints", type=int, default=2000)
    ap.add_argument("--mem-budget-mb", type=int, default=64)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    gen = subprocess.run(
        [PY, os.path.join(HERE, "gen_sparse.py"), args.workdir,
         "--endpoints", str(args.endpoints)], cwd=ROOT,
        capture_output=True, text=True)
    if gen.returncode != 0:
        print(gen.stdout, gen.stderr, file=sys.stderr)
        raise SystemExit("generation failed")
    print(gen.stdout.strip())

    rows = []
    libs = {}
    for name in ("short", "long"):
        pkg = os.path.join(args.workdir, name)
        lib = os.path.join(args.workdir, "%s.sqlite" % name)
        if os.path.exists(lib):
            os.unlink(lib)
        summary, wall = run_build(pkg, lib, args.mem_budget_mb)
        st = summary["stats"]
        read_total = sum(st["read"].values())
        rows.append((name, summary["root_frames"], read_total,
                     st["read"], st["written"]["aseg"] +
                     st["written"]["sseg"], st["written"]["segments"],
                     st["peak_rss_mb"], st["elapsed_sec"], wall))
        libs[name] = (pkg, lib)

    hdr = ("pkg", "frames", "intervals_read", "read_detail",
           "staged_segments", "final_segments", "peak_rss_mb",
           "build_sec", "wall_sec")
    print(("\n%-6s %12s %14s %34s %15s %14s %11s %10s %9s"
           % ("pkg", "frames", "intervals_read", "read_detail",
              "staged_segments", "final_segments", "peak_rss_mb",
              "build_sec", "wall_sec")))
    for r in rows:
        print("%-6s %12d %14d %34s %15d %14d %11.1f %10.3f %9.3f"
              % (r[0], r[1], r[2], str(r[3]), r[4], r[5], r[6], r[7],
                 r[8]))

    # Query a short interval back after moving the input away.
    pkg, lib = libs["long"]
    moved = pkg + ".away"
    if os.path.exists(moved):
        raise SystemExit("%s already exists" % moved)
    os.rename(pkg, moved)
    try:
        frames = rows[1][1]
        center = frames // 2
        t0 = time.monotonic()
        out = subprocess.run(
            [PY, "-m", "subwarehouse", "export", "--lib", lib,
             "--lang", "zho", "--start", str(center - 3), "--end",
             str(center + 3), "--format", "text"],
            cwd=ROOT, capture_output=True, text=True)
        insp = subprocess.run(
            [PY, "-m", "subwarehouse", "inspect", "--lib", lib,
             "--lang", "zho", "--frame", str(center)],
            cwd=ROOT, capture_output=True, text=True)
        dt = time.monotonic() - t0
        print("\ninput package moved away -> querying library only "
              "(%.4fs):" % dt)
        print(out.stdout.strip() or out.stderr.strip())
        print(insp.stdout.strip() or insp.stderr.strip())
    finally:
        os.rename(moved, pkg)
    print("\nlibraries left in %s (not committed)" % args.workdir)


if __name__ == "__main__":
    main()
