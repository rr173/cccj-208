"""Delivery-ledger benchmark: runtime samples only, nothing committed.

Generates two sparse films with identical endpoint counts and spans
differing by 10,000x, registers both with 20,000 shards per language (two
languages), takes the full batch and measures work, memory and time.  Then
builds a long receipt history on one target, fills a single gap in front of
a cached out-of-order chain, and verifies one shard at an old and a new
position.

Usage: python3 tools/bench_ledger.py [--workdir out/ledger] [--shards N]
                                     [--cues N] [--history N]
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def _gen(outdir, frames, shards, cues):
    proc = subprocess.run(
        [PY, os.path.join(HERE, "gen_ledger_scale.py"), outdir,
         "--frames", str(frames), "--shards", str(shards), "--cues",
         str(cues)], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr)
    return json.loads(proc.stdout)


SPAN_SCRIPT = r"""
import json, os, resource, sys, time
sys.path.insert(0, %(root)r)
from subwarehouse import builder, deliver
info = json.loads(%(info)r)
t0 = time.monotonic()
if not os.path.exists(%(lib)r):
    builder.build(info["package"], "R", %(lib)r)
build_sec = time.monotonic() - t0
if os.path.exists(%(ledger)r):
    os.unlink(%(ledger)r)
t0 = time.monotonic()
conn = deliver.init_ledger(%(ledger)r)
with open(info["languages"], encoding="utf-8") as fh:
    langs = json.load(fh)
reg = deliver.register(conn, "plat", 1, %(lib)r, langs)
reg_sec = time.monotonic() - t0
t0 = time.monotonic()
batch = deliver.dispatch(conn, "all", ["plat"], limit=None)
disp_sec = time.monotonic() - t0
print(json.dumps({
    "build_sec": round(build_sec, 3), "register_sec": round(reg_sec, 3),
    "dispatch_sec": round(disp_sec, 3), "rows": batch["count"],
    "segments_read": reg["cut_stats"]["segments_read"],
    "cue_intervals": reg["cut_stats"]["cue_intervals"],
    "peak_rss_mb": round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
}))
conn.close()
"""

HISTORY_SCRIPT = r"""
import json, os, resource, sys, time
sys.path.insert(0, %(root)r)
sys.path.insert(0, os.path.join(%(root)r, "tests"))
from platform_sim import Platform, write_receipts
from subwarehouse import builder, deliver
n = %(history)d
work = %(work)r
def make(name, text):
    pkg = os.path.join(work, name)
    lib = os.path.join(work, name + ".sqlite")
    man = {"package": name, "languages": ["zho"], "assets": {"M": 12},
           "sequences": {"R": 12}}
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "manifest.json"), "w") as fh:
        json.dump(man, fh)
    with open(os.path.join(pkg, "clips.jsonl"), "w") as fh:
        fh.write(json.dumps({"id": "c", "seq": "R", "track": 0,
                             "start": 0, "end": 12, "kind": "asset",
                             "ref": "M", "base": 0, "p": 1,
                             "q": 1}) + "\n")
    with open(os.path.join(pkg, "coverage.jsonl"), "w") as fh:
        fh.write(json.dumps({"asset": "M", "lang": "zho", "start": 0,
                             "end": 12}) + "\n")
    with open(os.path.join(pkg, "cues.jsonl"), "w") as fh:
        for i in range(3):
            fh.write(json.dumps({"id": "q%%d" %% i, "asset": "M",
                                 "lang": "zho", "start": i * 4,
                                 "end": i * 4 + 2,
                                 "text": text %% i}) + "\n")
    if os.path.exists(lib):
        os.unlink(lib)
    builder.build(pkg, "R", lib)
    return lib
libs = [make("histsrc-a", "A%%d"), make("histsrc-b", "B%%d"),
        make("histsrc-c", "C%%d")]
ledger = os.path.join(work, "hist-ledger.sqlite")
if os.path.exists(ledger):
    os.unlink(ledger)
conn = deliver.init_ledger(ledger)
table = [{"name": "h%%d" %% i, "start": i * 4, "end": i * 4 + 4}
         for i in range(3)]
deliver.register(conn, "plat", 1, libs[0], {"zho": table})
pf = Platform()
t0 = time.monotonic()
rels = 0
for r in range(2, n + 2):
    deliver.register(conn, "plat", r, libs[(r - 1) %% 3])
    batch = deliver.dispatch(conn, "h%%d" %% r, ["plat"])
    recs = [pf.deliver(row) for row in batch["instructions"]]
    if recs:
        deliver.import_receipts(conn, write_receipts(
            os.path.join(work, "rec.tmp.jsonl"), recs))
        rels += 1
history_sec = time.monotonic() - t0
prefix = deliver.progress(conn, "plat")["confirmed_prefix"]
print(json.dumps({"history_sec": round(history_sec, 3),
                  "receipts": prefix, "releases": rels,
                  "peak_rss_mb": round(resource.getrusage(
                      resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)}))
# one gap in front of a cached chain: take a batch, hold receipt 1 back
last_r = r
nrel = last_r + 1
batch = None
for extra in range(3):
    deliver.register(conn, "plat", nrel + extra, libs[(r + extra) %% 3])
    batch = deliver.dispatch(conn, "extra%%d" %% extra, ["plat"])
    if batch["count"]:
        break
recs = [pf.deliver(row) for row in batch["instructions"]]
held, tail = recs[0], recs[1:]
deliver.import_receipts(conn, write_receipts(
    os.path.join(work, "tail.tmp.jsonl"), tail))
before = deliver.progress(conn, "plat")
t0 = time.monotonic()
res = deliver.import_receipts(conn, write_receipts(
    os.path.join(work, "gap.tmp.jsonl"), [held]))
drain_sec = time.monotonic() - t0
after = deliver.progress(conn, "plat")
t0 = time.monotonic()
deliver.verify(conn, "plat", "zho", "h1", deliver.progress(
    conn, "plat")["highest_release"],
    after["confirmed_prefix"])
verify_sec = time.monotonic() - t0
print(json.dumps({
    "prefix_before_gap": before["confirmed_prefix"],
    "pending_before_gap": len(before["pending_receipts"]),
    "drained": res["changes"]["plat"]["drained"],
    "prefix_after_gap": after["confirmed_prefix"],
    "drain_sec": round(drain_sec, 4),
    "verify_one_shard_sec": round(verify_sec, 4),
    "peak_rss_mb": round(resource.getrusage(
        resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)}))
conn.close()
"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out", "ledger"))
    ap.add_argument("--shards", type=int, default=20000)
    ap.add_argument("--cues", type=int, default=4000)
    ap.add_argument("--history", type=int, default=2000)
    args = ap.parse_args(argv)
    os.makedirs(args.workdir, exist_ok=True)

    rows = []
    for name, frames in (("short", 100_000), ("long", 1_000_000_000)):
        work = os.path.join(args.workdir, name)
        info = _gen(work, frames, args.shards, args.cues)
        script = SPAN_SCRIPT % {
            "root": ROOT, "info": json.dumps(info), "lib":
            os.path.join(work, "lib.sqlite"), "ledger":
            os.path.join(work, "ledger.sqlite")}
        t0 = time.monotonic()
        proc = subprocess.run([PY, "-I", "-c", script], capture_output=True,
                              text=True)
        wall = time.monotonic() - t0
        if proc.returncode != 0:
            raise SystemExit(proc.stderr)
        r = json.loads(proc.stdout)
        rows.append((name, frames, r, wall))
    print("%-6s %14s %8s %10s %12s %10s %10s %10s"
          % ("pkg", "frames", "shards", "seg_read", "cue_cuts", "rows",
             "peak_MB", "reg_sec"))
    for name, frames, r, wall in rows:
        print("%-6s %14d %8d %10d %12d %10d %10.1f %10.3f"
              % (name, frames, args.shards * 2, r["segments_read"],
                 r["cue_intervals"], r["rows"], r["peak_rss_mb"],
                 r["register_sec"]))
    print("span ratio: %d x; interpreter baseline included in peak RSS"
          % (rows[1][1] // rows[0][1]))

    hist = os.path.join(args.workdir, "history")
    info = _gen(hist, 12, 4, 3)
    script = HISTORY_SCRIPT % {"root": ROOT, "history": args.history,
                               "work": hist}
    proc = subprocess.run([PY, "-I", "-c", script], capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    h, g = json.loads(lines[0]), json.loads(lines[1])
    print("\nreceipt history: %d releases, %d receipts in %.3fs"
          % (h["releases"], h["receipts"], h["history_sec"]))
    print("gap fill: prefix %d -> %d, %d cached receipts drained in "
          "%.4fs; verify one shard %.4fs; peak RSS %.1f MiB"
          % (g["prefix_before_gap"], g["prefix_after_gap"], g["drained"],
             g["drain_sec"], g["verify_one_shard_sec"],
             g["peak_rss_mb"]))
    print("\nsamples left in %s (not committed)" % args.workdir)


if __name__ == "__main__":
    main()
