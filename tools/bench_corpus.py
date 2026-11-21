"""Corpus-export workload benchmark.

Two families of generated inputs (runtime-generated, never committed):

* sparse windows: the same fixed interval structure at frame spans of
  200,000 and 3,000,000,000.  Authorization and extraction work must be
  identical -- it is proportional to the grant intervals and segments
  touched, never to the window's frame count.
* hot key: one relation key shared by 100,000 samples.  The stored and
  visited associations must stay linear in the members (relation rows,
  one merge edge per join, two registry key rows) -- never pairwise.

Usage: python3 tools/bench_corpus.py [--workdir out/corpus]
              [--mem-budget-mb M] [--hotkey N]
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

from gen_corpus_scale import (hotkey_inputs, make_hotkey, make_sparse,
                              sparse_inputs)            # noqa: E402

SPARSE_CONFIGS = [
    ("short", 200_000),
    ("long", 3_000_000_000),
]


def run_cli(argv, cwd=ROOT):
    proc = subprocess.run([PY, "-m", "subwarehouse"] + argv, cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("command failed: %s" % " ".join(argv))
    return json.loads(proc.stdout)


def build_lib(pkg, root, out, mem):
    return run_cli(["build", "--package", pkg, "--root", root,
                    "--out", out, "--mem-budget-mb", str(mem)])


def export(inputs, out, mem):
    return run_cli(["corpus", "corpus-export",
                    "--libraries", inputs["libraries"],
                    "--samples", inputs["samples"],
                    "--mapping", inputs["mapping"],
                    "--grants", inputs["grants"],
                    "--out", out,
                    "--mem-budget-mb", str(mem), "--stats"])


def bench_sparse(workdir, mem):
    rows = []
    structures = []
    for name, frames in SPARSE_CONFIGS:
        dest = os.path.join(workdir, "sparse-%s" % name)
        pkg = make_sparse(dest, frames)
        lib = os.path.join(dest, "lib.sqlite")
        t0 = time.monotonic()
        meta = build_lib(pkg, "R", lib, mem)
        build_sec = time.monotonic() - t0
        inputs = sparse_inputs(dest, lib, meta["logical_version"], frames)
        out = os.path.join(dest, "out")
        t0 = time.monotonic()
        res = export(inputs, out, mem)
        export_sec = time.monotonic() - t0
        st = res["stats"]
        rows.append((name, frames, st["read"]["segments"],
                     st["read"]["grants"], st["written"]["cues"],
                     st["written"]["rel"], st["peak_rss_mb"],
                     build_sec, export_sec))
        # Span-free delivery structure: sample ids, cue counts, relation
        # keys (absolute frame coordinates and library versions differ
        # legitimately between spans).
        delivered = []
        for fname in ("train.jsonl", "validation.jsonl"):
            with open(os.path.join(out, fname), encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    delivered.append((r["id"], len(r["cues"]),
                                      json.dumps(r["keys"],
                                                 sort_keys=True)))
        structures.append(sorted(delivered))
    print("sparse windows (fixed interval counts, growing span):")
    print("%-6s %14s %9s %7s %7s %7s %8s %9s %9s"
          % ("config", "frames", "seg_read", "grants", "cues", "rel",
             "peak_mb", "build_s", "export_s"))
    for r in rows:
        print("%-6s %14d %9d %7d %7d %7d %8.1f %9.2f %9.2f" % r)
    flat = rows[0][2:6] == rows[1][2:6]
    print("read/write counters identical across spans: %s" % flat)
    same = structures[0] == structures[1]
    print("delivered structure identical across spans: %s" % same)
    if not flat or not same:
        raise SystemExit("sparse-window workload grows with the span")


def bench_hotkey(workdir, mem, n):
    dest = os.path.join(workdir, "hotkey")
    pkg = make_hotkey(dest, n)
    lib = os.path.join(dest, "lib.sqlite")
    meta = build_lib(pkg, "R", lib, mem)
    inputs = hotkey_inputs(dest, lib, meta["logical_version"], n)
    out = os.path.join(dest, "out")
    t0 = time.monotonic()
    res = export(inputs, out, mem)
    export_sec = time.monotonic() - t0
    st = res["stats"]
    info = run_cli(["corpus", "corpus-registry-info",
                    "--registry", res["registry"]])
    print("\none hot key with %d members:" % n)
    print("%-9s %9s %9s %9s %9s %9s %9s %9s %9s"
          % ("samples", "rel_rows", "merges", "reg_memb", "reg_keys",
             "keys", "peak_mb", "export_s", "side"))
    side = "train" if st["counts"]["train"] == n else "validation"
    print("%-9d %9d %9d %9d %9d %9d %9.1f %9.2f %9s"
          % (n, st["written"]["rel"], st["grouping"]["merges"],
             info["members"], info["keys"], st["grouping"]["keys"],
             st["peak_rss_mb"], export_sec, side))
    ok = (st["written"]["rel"] == 2 * n
          and st["grouping"]["merges"] == n - 1
          and info["members"] == n and info["keys"] == 2
          and st["counts"]["train"] + st["counts"]["validation"] == n)
    print("associations linear in members (no pairwise blow-up): %s" % ok)
    if not ok:
        raise SystemExit("hot-key workload is not linear")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out",
                                                      "corpus"))
    ap.add_argument("--mem-budget-mb", type=int, default=64)
    ap.add_argument("--hotkey", type=int, default=100_000)
    args = ap.parse_args()
    args.workdir = os.path.abspath(args.workdir)
    os.makedirs(args.workdir, exist_ok=True)
    bench_sparse(args.workdir, args.mem_budget_mb)
    bench_hotkey(args.workdir, args.mem_budget_mb, args.hotkey)
    print("\nworkdir left in %s (generated data, not committed)"
          % args.workdir)


if __name__ == "__main__":
    main()
