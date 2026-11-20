"""Receipt sequencing, conflicts and crash safety."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest

from common import LedgerTest
from platform_sim import Platform, write_receipts

from subwarehouse import builder, deliver
from subwarehouse.errors import LedgerError, ReceiptConflict


def _mini_library(test, name, cues, coverage_end=40, assets=None):
    man = {"package": name, "languages": ["zho"],
           "assets": assets or {"M": 40}, "sequences": {"R": 12}}
    clips = [{"id": "c", "seq": "R", "track": 0, "start": 0, "end": 12,
              "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}]
    cov = [{"asset": "M", "lang": "zho", "start": 0,
            "end": coverage_end}]
    pkg = test.pkg(name, man, clips, cov, cues)
    lib = os.path.join(test.tmp, name + ".sqlite")
    builder.build(pkg, "R", lib)
    return lib


CUES = [{"id": "a", "asset": "M", "lang": "zho", "start": 0, "end": 2,
         "text": "甲"},
        {"id": "b", "asset": "M", "lang": "zho", "start": 4, "end": 6,
         "text": "乙"},
        {"id": "c", "asset": "M", "lang": "zho", "start": 8, "end": 10,
         "text": "丙"}]


class ReceiptTest(LedgerTest):
    def setUp(self):
        super().setUp()
        self.lib = _mini_library(self, "v1", CUES)
        self.conn, self.ledger = self.build_ledger()
        deliver.register(self.conn, "A", 1, self.lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        self.pf = Platform()
        self.batch = deliver.dispatch(self.conn, "b1", ["A"])
        for row in self.batch["instructions"]:
            self.pf.deliver(row)
        self.ops = {r["shard"]: r["op_id"]
                    for r in self.batch["instructions"]}

    def rec_file(self, name, recs):
        return write_receipts(os.path.join(self.tmp, name), recs)

    def test_gap_prefix_and_cached_drain(self):
        conn = self.conn
        deliver.import_receipts(conn, [self.rec_file("f2.jsonl", [
            self.pf.receipt("A", 2), self.pf.receipt("A", 3)])])
        prog = deliver.progress(conn, "A")
        self.assertEqual((prog["confirmed_prefix"], prog["first_missing"]),
                         (0, 1))
        self.assertEqual(prog["pending_receipts"], [2, 3])
        deliver.import_receipts(conn, [self.rec_file("f1.jsonl", [
            self.pf.receipt("A", 1)])])
        prog = deliver.progress(conn, "A")
        self.assertEqual((prog["confirmed_prefix"], prog["first_missing"]),
                         (3, 4))
        self.assertEqual(prog["open_operations"], 0)

    def test_identical_repeats_change_nothing(self):
        conn = self.conn
        f = self.rec_file("f.jsonl", [self.pf.receipt("A", 1)])
        deliver.import_receipts(conn, [f])
        before = deliver.progress(conn, "A")
        after = deliver.import_receipts(conn, [f])
        self.assertEqual(deliver.progress(conn, "A"), before)
        self.assertEqual(after["changes"], {})
        conn.execute("BEGIN")
        state = list(conn.execute(
            "SELECT state, receipt FROM confirmed ORDER BY shard"))
        conn.execute("COMMIT")
        deliver.import_receipts(conn, [f, f])
        conn.execute("BEGIN")
        self.assertEqual(
            list(conn.execute(
                "SELECT state, receipt FROM confirmed ORDER BY shard")),
            state)
        conn.execute("COMMIT")

    def test_cross_target_and_unknown_operation_rejected_whole(self):
        conn = self.conn
        # target B is not even registered: the whole input is refused and
        # target A's valid receipt in the same file is not committed
        bad = self.rec_file("bad.jsonl", [
            self.pf.receipt("A", 1),
            {"target": "B", "receipt": 1, "op_id": self.ops["s0"],
             "status": "applied"}])
        with self.assertRaises(ReceiptConflict) as cm:
            deliver.import_receipts(conn, [bad])
        self.assertIn("unknown target", str(cm.exception))
        self.assertEqual(deliver.progress(conn, "A")["confirmed_prefix"], 0)
        bad2 = self.rec_file("bad2.jsonl", [
            {"target": "A", "receipt": 1, "op_id": "nope",
             "status": "applied"}])
        with self.assertRaises(ReceiptConflict) as cm:
            deliver.import_receipts(conn, [bad2])
        self.assertIn("unknown operation", str(cm.exception))
        self.assertEqual(deliver.progress(conn, "A")["confirmed_prefix"], 0)
        # cross-target: a real operation of A cited for target B
        deliver.register(conn, "B", 1, self.lib,
                         {"zho": self.shard_table(12, [4, 4, 4], "b")})
        bad3 = self.rec_file("bad3.jsonl", [
            {"target": "B", "receipt": 1, "op_id": self.ops["s1"],
             "status": "applied"}])
        with self.assertRaises(ReceiptConflict) as cm:
            deliver.import_receipts(conn, [bad3])
        self.assertIn("belongs to target", str(cm.exception))
        self.assertEqual(deliver.progress(conn, "B")["confirmed_prefix"], 0)

    def test_duplicate_file_with_one_conflict_is_rejected_whole(self):
        conn = self.conn
        f = self.rec_file("f.jsonl", [self.pf.receipt("A", 1)])
        deliver.import_receipts(conn, [f])
        before = deliver.progress(conn, "A")
        clash = dict(self.pf.receipt("A", 2))
        clash["status"] = "rejected"
        clash["reason"] = "changed mind"
        bad = self.rec_file("bad.jsonl", [self.pf.receipt("A", 1),
                                          self.pf.receipt("A", 2),
                                          clash])
        with self.assertRaises(ReceiptConflict):
            deliver.import_receipts(conn, [bad])
        self.assertEqual(deliver.progress(conn, "A"), before)

    def test_same_number_different_receipt(self):
        conn = self.conn
        deliver.import_receipts(conn, [self.rec_file("f1.jsonl", [
            self.pf.receipt("A", 1)])])
        for rec in ({"target": "A", "receipt": 1, "op_id": self.ops["s1"],
                     "status": "applied"},
                    {"target": "A", "receipt": 1, "op_id": self.ops["s0"],
                     "status": "rejected", "reason": "x"}):
            with self.assertRaises(ReceiptConflict):
                deliver.import_receipts(conn, [self.rec_file(
                    "f2.jsonl", [rec])])
        self.assertEqual(deliver.progress(conn, "A")["confirmed_prefix"], 1)

    def test_one_operation_two_numbers(self):
        conn = self.conn
        deliver.import_receipts(conn, [self.rec_file("f1.jsonl", [
            self.pf.receipt("A", 1)])])
        again = dict(self.pf.receipt("A", 1), receipt=3)
        with self.assertRaises(ReceiptConflict):
            deliver.import_receipts(conn, [self.rec_file("f3.jsonl",
                                                          [again])])
        self.assertEqual(deliver.progress(conn, "A")["confirmed_prefix"], 1)
        # while still pending the same is caught before the gap closes
        pend = dict(self.pf.receipt("A", 2), receipt=4)
        deliver.import_receipts(conn, [self.rec_file("f2.jsonl", [
            self.pf.receipt("A", 2)])])
        with self.assertRaises(ReceiptConflict):
            deliver.import_receipts(conn, [self.rec_file("f4.jsonl",
                                                          [pend])])

    def test_rejected_receipt_keeps_content(self):
        conn = self.conn
        # shard 0 is applied (receipt 1); shard 1's operation is refused
        # (receipt 2) and must leave the platform's content untouched
        s0 = self.batch["instructions"][0]
        s1 = self.batch["instructions"][1]
        rej = dict(self.pf.receipt("A", 2))
        rej["status"] = "rejected"
        rej["reason"] = "no space"
        self.pf.deliver(dict(s1, op_id="ignored"), applied=False)
        deliver.import_receipts(conn, [self.rec_file("r.jsonl", [
            self.pf.receipt("A", 1), rej])])
        v = deliver.verify(conn, "A", "zho", s1["shard"], 1, 2)
        self.assertEqual(v["platform"]["state"], "initial_empty")
        self.assertEqual(v["platform"]["content"], [])
        self.assertEqual(v["receipt_at_position"]["status"], "rejected")
        self.assertFalse(v["receipt_at_position"]["evidence"])
        self.assertEqual(v["rejected_attempts"],
                         [{"receipt": 2, "op_id": s1["op_id"], "seq": 1,
                           "reason": "no space"}])
        # the rejected unit is unoccupied and can be planned again
        nxt = deliver.dispatch(conn, "again", ["A"])
        self.assertEqual([r["shard"] for r in nxt["instructions"]],
                         [s1["shard"]])
        self.assertNotEqual(nxt["instructions"][0]["op_id"],
                            s1["op_id"])
        # verified position 1 (only shard 0 applied) has no such reception
        v1 = deliver.verify(conn, "A", "zho", s0["shard"], 1, 1)
        self.assertEqual(v1["receipt_at_position"]["status"], "applied")


class CrashSafetyTest(LedgerTest):
    """A process killed mid-transaction must leave the previous state."""

    def test_interrupted_dispatch_leaves_no_partial_batch(self):
        lib = _mini_library(self, "v1", CUES)
        conn, path = self.build_ledger()
        deliver.register(conn, "A", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        script = r"""
import os, signal, sys
sys.path.insert(0, %r)
from subwarehouse import deliver
conn = deliver.open_ledger(%r)
# kill this process after the operations are inserted but before COMMIT
orig = conn.execute
def boom(*a, **kw):
    if a and str(a[0]).startswith("INSERT INTO requests"):
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(*a, **kw)
conn.execute = boom
deliver.dispatch(conn, "half", ["A"])
""" % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True)
        self.assertNotEqual(proc.returncode, 0)
        conn2 = deliver.open_ledger(path)
        self.addCleanup(conn2.close)
        peers = sqlite3.connect(path)
        self.addCleanup(peers.close)
        self.assertEqual(peers.execute(
            "SELECT COUNT(*) FROM ops").fetchone()[0], 0)
        self.assertEqual(peers.execute(
            "SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        self.assertEqual(deliver.progress(conn2, "A")["confirmed_prefix"], 0)
        # the ledger remains usable and plans the full batch once
        batch = deliver.dispatch(conn2, "half", ["A"])
        self.assertEqual(batch["count"], 3)

    def test_interrupted_receipt_import_is_all_or_nothing(self):
        lib = _mini_library(self, "v1", CUES)
        conn, path = self.build_ledger()
        deliver.register(conn, "A", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        pf = Platform()
        for row in deliver.dispatch(conn, "b", ["A"])["instructions"]:
            pf.deliver(row)
        recs = os.path.join(self.tmp, "recs.jsonl")
        write_receipts(recs, [pf.receipt("A", 1), pf.receipt("A", 2),
                              pf.receipt("A", 3)])
        script = r"""
import os, signal, sys
sys.path.insert(0, %r)
from subwarehouse import deliver
conn = deliver.open_ledger(%r)
orig = conn.execute
def boom(*a, **kw):
    if a and "UPDATE confirmed" in str(a[0]):
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(*a, **kw)
conn.execute = boom
deliver.import_receipts(conn, [%r])
""" % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path,
       recs)
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True)
        self.assertNotEqual(proc.returncode, 0)
        conn2 = deliver.open_ledger(path)
        self.addCleanup(conn2.close)
        prog = deliver.progress(conn2, "A")
        self.assertEqual(prog["confirmed_prefix"], 0)
        self.assertEqual(prog["open_operations"], 3)
        deliver.import_receipts(conn2, [recs])
        self.assertEqual(deliver.progress(conn2, "A")["confirmed_prefix"], 3)


if __name__ == "__main__":
    unittest.main()
