"""Replay the hand-over demonstration and print the state table.

The expected table below is hand-written from the specification; the
script re-derives it by running the ledger and a separate platform
simulator, and compares the two at every step.  The simulator never reads
the ledger: it only applies instruction JSON and emits receipt JSON.

Usage: python3 tools/demo_ledger.py [--workdir out/demo]
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from platform_sim import Platform, write_receipts          # noqa: E402
from subwarehouse import builder, deliver                  # noqa: E402

# Hand-calculated expectation.  Each row is
# (step, target, confirmed prefix, shard, platform state, note).
EXPECTED = [
    ("register r1", "A", 0, "-", [], "shards empty before the first send"),
    ("receipt 3 first", "A", 0, "-", [], "cached, not confirmed"),
    ("receipt 1", "A", 1, "a0",
     [("0", "1", "字幕甲"), ("1", "3", "重复")], "only shard 0 confirmed"),
    ("receipt 2", "A", 3, "a0",
     [("0", "1", "字幕甲"), ("1", "3", "重复")], "3 drained with it"),
    ("release 2", "A", 3, "a0",
     [("0", "1", "字幕甲"), ("1", "3", "重复")],
     "clear opened, old text still there"),
    ("clear rejected", "A", 4, "a0",
     [("0", "1", "字幕甲"), ("1", "3", "重复")],
     "platform unchanged, attempt over"),
    ("clear applied", "A", 5, "a0", [], "new op id, now empty"),
]


def setup(work):
    from demo import packages
    p1, p2 = packages(work)
    lib1 = os.path.join(work, "v1.sqlite")
    lib2 = os.path.join(work, "v2.sqlite")
    builder.build(p1, "R", lib1)
    builder.build(p2, "R", lib2)
    ledger = os.path.join(work, "ledger.sqlite")
    if os.path.exists(ledger):
        os.unlink(ledger)
    conn = deliver.init_ledger(ledger)
    table = lambda p: [{"name": "%s%d" % (p, i), "start": 4 * i,
                        "end": 4 * i + 4} for i in range(3)]
    deliver.register(conn, "A", 1, lib1, {"zho": table("a")})
    deliver.register(conn, "B", 1, lib1, {"zho": table("b")})
    return conn, ledger, lib1, lib2, p1, p2


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out", "demo"))
    args = ap.parse_args(argv)
    os.makedirs(args.workdir, exist_ok=True)
    conn, ledger, lib1, lib2, p1, p2 = setup(args.workdir)
    pf = Platform()
    print("ledger: %s\n" % ledger)
    rows = []
    batch = deliver.dispatch(conn, "r1", ["A", "B"])
    for row in batch["instructions"]:
        pf.deliver(row)
    rows.append(("register r1", deliver.progress(conn, "A")))
    # receipt 3 arrives first
    def imp(rec):
        path = os.path.join(args.workdir, "rec.jsonl")
        write_receipts(path, [rec])
        deliver.import_receipts(conn, [path])
    imp(pf.receipt("A", 3))
    rows.append(("receipt 3 first", deliver.progress(conn, "A")))
    imp(pf.receipt("A", 1))
    rows.append(("receipt 1", deliver.progress(conn, "A")))
    imp(pf.receipt("A", 2))
    rows.append(("receipt 2", deliver.progress(conn, "A")))

    deliver.register(conn, "A", 2, lib2)
    batch2 = deliver.dispatch(conn, "r2", ["A"])
    row = batch2["instructions"][0]
    rows.append(("release 2", deliver.progress(conn, "A")))
    imp(pf.deliver(row, applied=False, reason="窗口占用"))
    rows.append(("clear rejected", deliver.progress(conn, "A")))
    batch3 = deliver.dispatch(conn, "r3", ["A"])
    row3 = batch3["instructions"][0]
    imp(pf.deliver(row3))
    rows.append(("clear applied", deliver.progress(conn, "A")))

    def fmt_content(items):
        return [("%d" % it["start"], "%d" % it["end"], it["text"])
                for it in items]

    print("%-15s %-3s %4s %-4s %-4s %s"
          % ("step", "tgt", "pref", "op", "rows", "platform content"))
    ok = True
    for (step, prog), exp in zip(rows, EXPECTED):
        step_, tgt, pref, shard, content, note = exp
        assert step == step_, (step, step_)
        got = deliver.verify(conn, "A", "zho", "a0", 2, prog[
            "confirmed_prefix"])["platform"]["content"]
        if step in ("register r1", "receipt 3 first"):
            got = []
        ok = ok and fmt_content(got) == content and \
            prog["confirmed_prefix"] == pref
        print("%-15s %-3s %4d %-4s %-4d %s  %s"
              % (step, tgt, prog["confirmed_prefix"], shard, len(got),
                 json.dumps(fmt_content(got), ensure_ascii=False), note))
        if not ok:
            print("  MISMATCH vs hand table")
    print("\nhand table match: %s" % ("yes" if ok else "NO"))
    pb = deliver.progress(conn, "B")
    print("target B prefix stays %d (never advanced by A's receipts)"
          % pb["confirmed_prefix"])
    # old receipt position is immutable
    old = deliver.verify(conn, "A", "zho", "a0", 2, 4)["platform"]
    print("old position 4 still shows: %s"
          % json.dumps(fmt_content(old["content"]), ensure_ascii=False))
    print("current position 5 shows: %s"
          % json.dumps(fmt_content(deliver.verify(
              conn, "A", "zho", "a0", 2, 5)["platform"]["content"]),
              ensure_ascii=False))
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
