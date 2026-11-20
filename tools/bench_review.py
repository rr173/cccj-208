"""Correction-trial workload benchmark: the same fixed local correction
is measured while the number of irrelevant programs and the span of
untouched timelines grow.  Per-trial work must stay flat; only the
one-time directory build may grow.

Usage: python3 tools/bench_review.py [--workdir out/review]
              [--mem-budget-mb M]
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

sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from gen_review_scale import HOT_WINDOW, correction_order, make_package  # noqa: E402

CONFIGS = [
    # (name, irrelevant programs, frames per irrelevant timeline)
    ("few-short", 4, 100_000),
    ("many-short", 256, 100_000),
    ("few-long", 4, 1_000_000_000),
    ("many-long", 256, 1_000_000_000),
]


def run_cli(argv, cwd=ROOT):
    proc = subprocess.run([PY, "-m", "subwarehouse"] + argv, cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("command failed: %s" % " ".join(argv))
    return json.loads(proc.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out",
                                                      "review"))
    ap.add_argument("--mem-budget-mb", type=int, default=64)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    rows = []
    lists = {}
    for name, n_programs, frames in CONFIGS:
        pkg = os.path.join(args.workdir, "pkg-%s" % name)
        inv = os.path.join(args.workdir, "inv-%s" % name)
        info = make_package(pkg, n_programs, frames, seed=7)
        programs = info["programs"]
        t0 = time.monotonic()
        init = run_cli(["review", "review-init", "--package", pkg,
                        "--dir", inv, "--mem-budget-mb",
                        str(args.mem_budget_mb), "--stats"]
                       + sum((["--program", p] for p in programs), []))
        init_sec = time.monotonic() - t0
        order = correction_order(init["baseline"])
        order_path = os.path.join(args.workdir, "order-%s.json" % name)
        with open(order_path, "w", encoding="utf-8") as fh:
            json.dump(order, fh)
        out_path = os.path.join(args.workdir, "list-%s.jsonl" % name)
        t0 = time.monotonic()
        res = run_cli(["review", "review-trial", "--dir", inv,
                       "--order", order_path, "--out", out_path,
                       "--mem-budget-mb", str(args.mem_budget_mb),
                       "--stats"])
        trial_sec = time.monotonic() - t0
        st = res["stats"]
        read_total = sum(st["read"].values())
        rows.append((name, n_programs, frames, init_sec,
                     st["refs_walked"], st["sequences_rejudged"],
                     st["candidate_windows"], st["candidate_frames"],
                     read_total, st["review_rows"], st["peak_rss_mb"],
                     trial_sec))
        with open(out_path, "rb") as fh:
            lists[name] = fh.read()

    hdr = ("%-10s %8s %12s %9s %6s %6s %6s %7s %8s %5s %8s %9s"
           % ("config", "programs", "frames", "init_s", "refs",
              "rejud", "candw", "candfr", "int_read", "rows",
              "peak_mb", "trial_s"))
    print(hdr)
    for r in rows:
        print("%-10s %8d %12d %9.2f %6d %6d %6d %7d %8d %5d %8.1f %9.3f"
              % r)

    same = len({v for v in lists.values()}) == 1
    print("\nreview list identical across configs: %s (%d bytes)"
          % (same, len(lists[rows[0][0]])))
    if not same:
        raise SystemExit("lists differ across configs")
    flat = len({r[4:10] for r in rows}) == 1
    print("per-trial workload identical across configs: %s" % flat)
    if not flat:
        raise SystemExit("workload grows with irrelevant scale")

    # The directory answers on its own: move the biggest package away
    # and re-run the trial.
    name = rows[-1][0]
    pkg = os.path.join(args.workdir, "pkg-%s" % name)
    moved = pkg + ".away"
    if os.path.exists(moved):
        raise SystemExit("%s already exists" % moved)
    os.rename(pkg, moved)
    try:
        res = run_cli(["review", "review-trial", "--dir",
                       os.path.join(args.workdir, "inv-%s" % name),
                       "--order",
                       os.path.join(args.workdir, "order-%s.json" % name),
                       "--out",
                       os.path.join(args.workdir, "list-away.jsonl")])
        print("trial with the %s package moved away: ok=%s rows=%d"
              % (name, res["ok"], res["segments"]))
    finally:
        os.rename(moved, pkg)
    print("\nworkdir left in %s (generated data, not committed)"
          % args.workdir)


if __name__ == "__main__":
    main()
