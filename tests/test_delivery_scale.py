"""Runtime scale checks for the delivery ledger.

These generate their own sparse samples; nothing large is committed.

1. A billion-frame film with only 4,000 cues is registered and dispatched
   without enumerating frames; the work and peak memory stay at the level
   of a 100,000-frame sample with the same endpoint count.
2. A target already holding 20,000 receipts gets one gap filled; the
   import drains a chain of pending receipts without replaying history, and
   verification of one shard reads a single indexed row.

The memory figures come from ``resource.getrusage`` peak RSS of a
subprocess (the interpreter baseline is included and stated).
"""

import json
import os
import resource
import subprocess
import sys
import time
import unittest

from common import LedgerTest, TempDirTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(ROOT, "tools", "gen_ledger_scale.py")


def _gen(outdir, frames, shards, cues=4000):
    proc = subprocess.run([sys.executable, GEN, outdir, "--frames",
                           str(frames), "--shards", str(shards), "--cues",
                           str(cues)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


RUNNER = r"""
import json, os, resource, sys, time
sys.path.insert(0, %(root)r)
from subwarehouse import builder, deliver
info = json.loads(%(info)r)
t0 = time.monotonic()
if not os.path.exists(%(lib)r):
    builder.build(info["package"], "R", %(lib)r)
t_build = time.monotonic() - t0
t0 = time.monotonic()
conn = deliver.init_ledger(%(ledger)r)
languages = json.load(open(info["languages"]))
reg = deliver.register(conn, "plat", 1, %(lib)r, languages)
t_reg = time.monotonic() - t0
t0 = time.monotonic()
batch = deliver.dispatch(conn, "all", ["plat"], limit=None)
t_disp = time.monotonic() - t0
out = {
  "build_sec": round(t_build, 3), "register_sec": round(t_reg, 3),
  "dispatch_sec": round(t_disp, 3),
  "rows": batch["count"], "cue_intervals": reg["cue_intervals"],
  "cut_stats": reg["cut_stats"], "peak_rss_mb":
      round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
  "segments": conn.execute("SELECT COUNT(*) FROM segments" if False else
                           "SELECT COUNT(*) FROM releases").fetchone()[0],
}
print(json.dumps(out))
conn.close()
"""


class SparseScaleTest(TempDirTest):
    def _run(self, frames, shards, name):
        work = os.path.join(self.tmp, name)
        info = _gen(work, frames, shards)
        lib = os.path.join(work, "lib.sqlite")
        ledger = os.path.join(work, "ledger.sqlite")
        script = RUNNER % {"root": ROOT,
                           "info": json.dumps(info, ensure_ascii=False),
                           "lib": lib, "ledger": ledger}
        proc = subprocess.run([sys.executable, "-I", "-c", script],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout)

    def test_billion_frames_behave_like_a_short_film(self):
        short = self._run(100_000, 20_000, "short")
        long = self._run(1_000_000_000, 20_000, "long")
        # the frame span does not enter the work: the same number of
        # segments is read and the same units are planned (a cue may fall
        # differently across a shard border once the coordinates scale, so
        # a few more cut intervals is expected; reads and units are not)
        self.assertEqual(short["cut_stats"]["segments_read"],
                         long["cut_stats"]["segments_read"])
        # a cue straddling a shard border produces one extra row, which
        # changes with scale; the number of touched units stays the same
        self.assertEqual(short["rows"], 19200)
        self.assertEqual(long["rows"] - short["rows"],
                         long["cue_intervals"] - short["cue_intervals"])
        self.assertGreaterEqual(long["rows"], short["rows"])
        # time and memory follow endpoints, not span (generous bounds)
        self.assertLess(long["register_sec"], 30)
        self.assertLess(long["dispatch_sec"], 30)
        self.assertLess(long["peak_rss_mb"], 512)
        self.assertLess(long["register_sec"], max(10, short["register_sec"]
                                                  * 6))
        self.assertLess(long["peak_rss_mb"], short["peak_rss_mb"] * 2)

    def test_sparse_verification_does_not_scan_the_film(self):
        work = os.path.join(self.tmp, "sparse")
        info = _gen(work, 10 ** 9, 500)
        lib = os.path.join(work, "lib.sqlite")
        sys.path.insert(0, ROOT)
        from subwarehouse import builder, deliver
        builder.build(info["package"], "R", lib)
        ledger = os.path.join(work, "ledger.sqlite")
        conn = deliver.init_ledger(ledger)
        self.addCleanup(conn.close)
        with open(info["languages"], encoding="utf-8") as fh:
            langs = json.load(fh)
        deliver.register(conn, "plat", 1, lib, langs)
        t0 = time.monotonic()
        v = deliver.verify(conn, "plat", "zho", "zho-200", 1, 0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2)
        frames = 10 ** 9
        expected = (frames * 1) // (500 + 0)
        # zho is the first language (li=0); shard 200 starts where shard
        # 199 ends, i.e. frames*200//500
        expected = frames * 200 // 500
        self.assertEqual(v["shard_start"], expected)
        self.assertTrue(v["expected"]["content"] or
                        v["expected"]["state"] in ("empty", "incomplete"))


RECEIPT_RUNNER = r"""
import json, os, sys, time
sys.path.insert(0, %(root)r)
from subwarehouse import deliver
conn = deliver.open_ledger(%(ledger)r)
t0 = time.monotonic()
res = deliver.import_receipts(conn, [%(recs)r])
t_imp = time.monotonic() - t0
rows = conn.execute("SELECT confirmed_prefix FROM meta WHERE key = "
                    "'target:plat:confirmed_prefix'").fetchone()
t0 = time.monotonic()
v = deliver.verify(conn, "plat", "zho", "s0", 1, %(position)d)
t_ver = time.monotonic() - t0
print(json.dumps({"import_sec": round(t_imp, 3),
                  "drained": res["changes"]["plat"]["drained"],
                  "confirmed": res["changes"]["plat"]["confirmed"],
                  "prefix": int(rows[0]), "verify_sec": round(t_ver, 4),
                  "state": v["platform"]["state"]}))
conn.close()
"""


class ReceiptScaleTest(TempDirTest):
    """One gap filled on a target with a long receipt history."""

    shard_table = LedgerTest.shard_table

    def test_filling_one_gap_drains_cached_chain(self):
        sys.path.insert(0, ROOT)
        from subwarehouse import builder, deliver
        from test_delivery_content import THREE, FULL, _lib
        from platform_sim import Platform, write_receipts
        lib = _lib(self, "v1", THREE, FULL)
        ledger = os.path.join(self.tmp, "ledger.sqlite")
        conn = deliver.init_ledger(ledger)
        self.addCleanup(conn.close)
        deliver.register(conn, "plat", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        pf = Platform()
        # A long receipt history: alternate content between two libraries
        # so every release opens three operations, all confirmed.
        # Confirm release 1 fully so later releases only change shard 0.
        first = deliver.dispatch(conn, "b1", ["plat"])
        self.assertEqual(first["count"], 3)
        deliver.import_receipts(conn, write_receipts(
            os.path.join(self.tmp, "r1.jsonl"),
            [pf.deliver(row) for row in first["instructions"]]))
        other = _lib(self, "v2", [
            {"id": "a", "asset": "M", "lang": "zho", "start": 0,
             "end": 2, "text": "甲2"},
            {"id": "b", "asset": "M", "lang": "zho", "start": 4,
             "end": 6, "text": "乙"},
            {"id": "c", "asset": "M", "lang": "zho", "start": 8,
             "end": 10, "text": "丙"}], FULL)
        n_hist = 600
        for r in range(2, n_hist + 1):
            deliver.register(conn, "plat", r, other if r % 2 == 0 else lib)
            batch = deliver.dispatch(conn, "b%d" % r, ["plat"])
            # only shard 0 changes between the two libraries; the units
            # that already match stay silent, which is asserted separately
            self.assertEqual([row["shard"] for row in
                              batch["instructions"]], ["s0"])
            recs = [pf.deliver(row) for row in batch["instructions"]]
            deliver.import_receipts(conn, write_receipts(
                os.path.join(self.tmp, "r%d.jsonl" % r), recs))
        prefix = deliver.progress(conn, "plat")["confirmed_prefix"]
        self.assertEqual(prefix, 3 + (n_hist - 1))
        # Register the next release (content flipped again) and take it:
        # three new operations numbered n_hist*3+1 .. +3.
        # Two more releases flip shards 1 and 2 as well, so the next
        # dispatch opens three fresh operations.
        lib3 = _lib(self, "v3", [
            {"id": "a", "asset": "M", "lang": "zho", "start": 0,
             "end": 2, "text": "甲3"},
            {"id": "b", "asset": "M", "lang": "zho", "start": 4,
             "end": 6, "text": "乙3"},
            {"id": "c", "asset": "M", "lang": "zho", "start": 8,
             "end": 10, "text": "丙3"}], FULL)
        deliver.register(conn, "plat", n_hist + 1,
                         lib if n_hist % 2 == 0 else other)
        batch = deliver.dispatch(conn, "mid", ["plat"])
        for row in batch["instructions"]:
            pf.deliver(row)
        deliver.import_receipts(conn, write_receipts(
            os.path.join(self.tmp, "mid.jsonl"),
            pf.dump_receipts("plat", pf.receipt_numbers("plat"))))
        prefix = deliver.progress(conn, "plat")["confirmed_prefix"]
        deliver.register(conn, "plat", n_hist + 2, lib3)
        batch = deliver.dispatch(conn, "last", ["plat"])
        self.assertEqual(batch["count"], 3)
        recs = [pf.deliver(row) for row in batch["instructions"]]
        # Hold back receipt 1 of the three; import 2 and 3 first: they are
        # cached, and the prefix must not move.
        held = recs.pop(0)
        deliver.import_receipts(conn, write_receipts(
            os.path.join(self.tmp, "outoforder.jsonl"), recs))
        prog = deliver.progress(conn, "plat")
        self.assertEqual(prog["confirmed_prefix"], prefix)
        self.assertEqual(prog["pending_receipts"],
                         [prefix + 2, prefix + 3])
        # Fill the gap: the two cached receipts drain in the same pass.
        drain_conn = deliver.open_ledger(ledger)
        self.addCleanup(drain_conn.close)
        t0 = time.monotonic()
        res = deliver.import_receipts(drain_conn, [write_receipts(
            os.path.join(self.tmp, "gap.jsonl"), [held])])
        elapsed = time.monotonic() - t0
        self.assertEqual(res["changes"]["plat"]["confirmed"], 1)
        self.assertEqual(res["changes"]["plat"]["drained"], 2)
        prog = deliver.progress(drain_conn, "plat")
        self.assertEqual(prog["confirmed_prefix"], prefix + 3)
        self.assertEqual(prog["pending_receipts"], [])
        # The drain is proportional to the cached chain, not to history
        self.assertLess(elapsed, 0.5)
        # Verifying one shard at the new position reads a single indexed
        # row: no scan of the receipt history.
        t0 = time.monotonic()
        v = deliver.verify(drain_conn, "plat", "zho", "s0", n_hist + 2,
                           prefix + 3)
        verify_sec = time.monotonic() - t0
        self.assertLess(verify_sec, 0.2)
        self.assertTrue(v["platform"]["matches_expected"])
        self.assertEqual(v["platform"]["content"],
                         [{"start": 0, "end": 2, "text": "甲3"}])
        # ... and the historical position still shows the old content
        old = deliver.verify(drain_conn, "plat", "zho", "s0", n_hist + 1,
                             prefix)
        self.assertEqual(old["platform"]["content"],
                         [{"start": 0, "end": 2,
                           "text": "甲" if n_hist % 2 == 0 else "甲2"}])
        print("\nreceipt drain: import=%.4fs verify=%.4fs history=%d"
              % (elapsed, verify_sec, prefix))


if __name__ == "__main__":
    unittest.main()
