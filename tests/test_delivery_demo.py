"""The hand-over demonstration from the specification, end to end.

Three 4-frame shards on two targets.  Release 1 has subtitles in all three
shards and sends one instruction per shard.  Receipt 3 arrives first and
the confirmed prefix stays 0; receipt 1 confirms only shard 0; together
with 2 the remaining shards confirm.  Release 2 empties shard 0, lacks one
frame of shard 1 and splits shard 2's identical text into two cue sources
-- only shard 0 gets a clear instruction.  A rejected clear keeps the old
subtitles; the next dispatch uses a new operation id; after the successful
clear, verifying the old receipt position still shows the old text.  The
second target's progress is independent, and an in-flight instruction is
not papered over when a newer release arrives.
"""

import json
import os
import unittest

from common import LedgerTest
from demo import packages
from platform_sim import Platform, write_receipts

from subwarehouse import builder, deliver
from subwarehouse.errors import LedgerError


class DemoTest(LedgerTest):
    def setUp(self):
        super().setUp()
        self.p1, self.p2 = packages(self.tmp)
        self.lib1 = os.path.join(self.tmp, "v1.sqlite")
        self.lib2 = os.path.join(self.tmp, "v2.sqlite")
        builder.build(self.p1, "R", self.lib1)
        builder.build(self.p2, "R", self.lib2)
        self.conn, self.ledger = self.build_ledger()
        self.tbl_a = self.shard_table(12, [4, 4, 4], "a")
        self.tbl_b = self.shard_table(12, [4, 4, 4], "b")
        deliver.register(self.conn, "A", 1, self.lib1, {"zho": self.tbl_a})
        deliver.register(self.conn, "B", 1, self.lib1, {"zho": self.tbl_b})
        self.pf = Platform()

    # -- helpers --------------------------------------------------------

    def receipts_file(self, name, recs):
        return write_receipts(os.path.join(self.tmp, name), recs)

    def send(self, request, targets=None, limit=None):
        batch = deliver.dispatch(self.conn, request, targets or ["A", "B"],
                                 limit)
        for row in batch["instructions"]:
            self.pf.deliver(row)
        return batch

    def by_shard(self, batch, target="A"):
        return {r["shard"]: r for r in batch["instructions"]
                if r["target"] == target}

    # -- the demonstration ---------------------------------------------

    def test_full_handover(self):
        conn = self.conn
        batch1 = self.send("r1")
        a = self.by_shard(batch1)
        self.assertEqual(sorted(a), ["a0", "a1", "a2"])
        # shard 0: two adjacent identical cues from different sources
        # merge into one relative interval
        self.assertEqual(a["a0"]["action"], "replace")
        self.assertEqual(a["a0"]["items"],
                         [{"start": 0, "end": 1, "text": "字幕甲"},
                          {"start": 1, "end": 3, "text": "重复"}])
        # target B is planned from its own empty confirmed state
        b = self.by_shard(batch1, "B")
        self.assertEqual(sorted(b), ["b0", "b1", "b2"])

        # receipt 3 arrives first: cached, prefix stays 0
        deliver.import_receipts(conn, [self.receipts_file(
            "t3.jsonl", [self.pf.receipt("A", 3)])])
        prog = deliver.progress(conn, "A")
        self.assertEqual(prog["confirmed_prefix"], 0)
        self.assertEqual(prog["first_missing"], 1)
        self.assertEqual(prog["pending_receipts"], [3])

        # receipt 1: only shard 0 confirmed; shard 2 stays in flight
        deliver.import_receipts(conn, [self.receipts_file(
            "t1.jsonl", [self.pf.receipt("A", 1)])])
        prog = deliver.progress(conn, "A")
        self.assertEqual(prog["confirmed_prefix"], 1)
        self.assertEqual(prog["open_operations"], 2)
        v0 = deliver.verify(conn, "A", "zho", "a0", 1, 1)
        self.assertEqual(v0["platform"]["content"],
                         a["a0"]["items"])
        v2 = deliver.verify(conn, "A", "zho", "a2", 1, 1)
        self.assertEqual(v2["platform"]["state"], "initial_empty")

        # receipt 2 closes the gap and the cached receipt 3 is consumed
        deliver.import_receipts(conn, [self.receipts_file(
            "t2.jsonl", [self.pf.receipt("A", 2)])])
        prog = deliver.progress(conn, "A")
        self.assertEqual(prog["confirmed_prefix"], 3)
        self.assertEqual(prog["open_operations"], 0)
        self.assertEqual(prog["pending_receipts"], [])
        for shard, rel in (("a1", "a1"), ("a2", "a2")):
            v = deliver.verify(conn, "A", "zho", shard, 1, 3)
            self.assertEqual(v["platform"]["content"], a[rel]["items"])

        # release 2: only shard 0 gets a clear
        deliver.register(conn, "A", 2, self.lib2)
        batch2 = deliver.dispatch(conn, "r2", ["A"])
        self.assertEqual(len(batch2["instructions"]), 1)
        row = batch2["instructions"][0]
        self.assertEqual((row["shard"], row["action"]), ("a0", "clear"))
        old_clear = row["op_id"]
        # shard 1 waits: the release has no complete content (frame 5)
        v1 = deliver.verify(conn, "A", "zho", "a1", 2, 3)
        self.assertEqual(v1["expected"]["state"], "incomplete")
        self.assertEqual(v1["expected"]["gaps"], [{"start": 5, "end": 6}])
        # shard 2's split identical text is the same playback content
        v2 = deliver.verify(conn, "A", "zho", "a2", 2, 3)
        self.assertEqual(v2["expected"]["content"],
                         [{"start": 0, "end": 2, "text": "字幕丁"}])
        self.assertTrue(v2["platform"]["matches_expected"])

        # the clear is rejected: content unchanged, attempt ends
        rej = self.pf.deliver(row, applied=False, reason="窗口占用")
        self.assertEqual(rej["receipt"], 4)
        deliver.import_receipts(conn, [self.receipts_file("rej.jsonl",
                                                          [rej])])
        v = deliver.verify(conn, "A", "zho", "a0", 2, 4)
        self.assertEqual(v["platform"]["state"], "subtitles")
        self.assertEqual(v["platform"]["content"], a["a0"]["items"])
        self.assertEqual(v["receipt_at_position"]["status"], "rejected")
        self.assertFalse(v["receipt_at_position"]["evidence"])
        self.assertEqual(v["rejected_attempts"][0]["reason"], "窗口占用")

        # next dispatch compares again and emits a *new* operation id
        batch3 = deliver.dispatch(conn, "r3", ["A"])
        self.assertEqual(len(batch3["instructions"]), 1)
        row3 = batch3["instructions"][0]
        self.assertEqual(row3["action"], "clear")
        self.assertNotEqual(row3["op_id"], old_clear)
        ok = self.pf.deliver(row3)
        deliver.import_receipts(conn, [self.receipts_file("ok.jsonl",
                                                          [ok])])
        self.assertEqual(deliver.progress(conn, "A")["confirmed_prefix"], 5)
        # the old receipt position still shows the old subtitles
        old = deliver.verify(conn, "A", "zho", "a0", 2, 4)
        self.assertEqual(old["platform"]["state"], "subtitles")
        self.assertEqual(old["platform"]["content"], a["a0"]["items"])
        # ... while the new position shows a cleared shard
        new = deliver.verify(conn, "A", "zho", "a0", 2, 5)
        self.assertEqual(new["platform"]["state"], "cleared")
        self.assertEqual(new["platform"]["content"], [])

        # the second target was never advanced by target A's receipts
        self.assertEqual(deliver.progress(conn, "B")["confirmed_prefix"], 0)
        self.assertEqual(
            deliver.verify(conn, "B", "zho", "b0", 1, 0)["platform"]["state"],
            "initial_empty")
        # B still has its own unconfirmed operations; a fresh dispatch for
        # B alone yields nothing new (one open operation per unit)
        self.assertEqual(deliver.dispatch(conn, "r4", ["B"])["count"], 0)
        for rec in self.pf.dump_receipts("B"):
            deliver.import_receipts(conn, [self.receipts_file(
                "b%d.jsonl" % rec["receipt"], [rec])])
        self.assertEqual(deliver.progress(conn, "B")["confirmed_prefix"], 3)

        # a newer release must not hide an in-flight instruction
        deliver.register(conn, "A", 2, self.lib2)   # idempotent retry
        batch5 = deliver.dispatch(conn, "r5", ["A"], limit=1)
        self.assertEqual(batch5["count"], 0)        # nothing left to send
        # re-register the identical release is idempotent and adds no rows
        self.assertTrue(
            deliver.register(conn, "A", 2, self.lib2)["already_registered"])


class InFlightAcrossReleaseTest(LedgerTest):
    def test_new_release_waits_for_in_flight_operation(self):
        p1, p2 = packages(self.tmp)
        lib1 = os.path.join(self.tmp, "v1.sqlite")
        lib2 = os.path.join(self.tmp, "v2.sqlite")
        builder.build(p1, "R", lib1)
        builder.build(p2, "R", lib2)
        conn, _ = self.build_ledger()
        deliver.register(conn, "A", 1, lib1,
                         {"zho": self.shard_table(12, [4, 4, 4], "a")})
        first = deliver.dispatch(conn, "d1", ["A"])["instructions"]
        self.assertEqual(len(first), 3)
        # release 2 arrives while all three operations are in flight
        deliver.register(conn, "A", 2, lib2)
        self.assertEqual(deliver.dispatch(conn, "d2", ["A"])["count"], 0)
        # close only shard 0 with the old (release 1) receipt: the new
        # release now plans shard 0 from *its* expectation (a clear)
        pf = Platform()
        for row in first:
            pf.deliver(row)
        deliver.import_receipts(conn, [self.receipts_file(
            "one.jsonl", [pf.receipt("A", 1)])])
        nxt = deliver.dispatch(conn, "d3", ["A"])
        self.assertEqual([(r["shard"], r["action"]) for r in
                          nxt["instructions"]], [("a0", "clear")])

    def receipts_file(self, name, recs):
        return write_receipts(os.path.join(self.tmp, name), recs)


class OrderAndLimitTest(LedgerTest):
    def setUp(self):
        super().setUp()
        p1, _ = packages(self.tmp)
        self.lib1 = os.path.join(self.tmp, "v1.sqlite")
        builder.build(p1, "R", self.lib1)
        self.conn, _ = self.build_ledger()
        self.tbl = self.shard_table(12, [4, 4, 4], "a")
        deliver.register(self.conn, "B", 1, self.lib1, {"zho": self.tbl})
        deliver.register(self.conn, "A", 1, self.lib1, {"zho": self.tbl})

    def test_stable_unit_order_and_caps(self):
        b1 = deliver.dispatch(self.conn, "q1", ["B", "A"], limit=2)
        self.assertEqual([(r["target"], r["shard"])
                          for r in b1["instructions"]],
                         [("A", "a0"), ("A", "a1")])
        self.assertEqual(b1["count"], 2)
        self.assertTrue(b1["more"])
        b2 = deliver.dispatch(self.conn, "q2", ["A", "B"], limit=2)
        self.assertEqual([(r["target"], r["shard"])
                          for r in b2["instructions"]],
                         [("A", "a2"), ("B", "a0")])
        b3 = deliver.dispatch(self.conn, "q3", ["A", "B"], limit=10)
        self.assertEqual([(r["target"], r["shard"])
                          for r in b3["instructions"]],
                         [("B", "a1"), ("B", "a2")])
        self.assertFalse(b3["more"])
        self.assertEqual(deliver.dispatch(self.conn, "q4",
                                          ["A", "B"])["count"], 0)

    def test_request_conflicts_and_idempotent_retry(self):
        b1 = deliver.dispatch(self.conn, "same", ["A"], limit=1)
        self.assertEqual(deliver.dispatch(self.conn, "same", ["A"],
                                          limit=1), b1)
        with self.assertRaises(LedgerError):
            deliver.dispatch(self.conn, "same", ["A"], limit=2)
        with self.assertRaises(LedgerError):
            deliver.dispatch(self.conn, "same", ["B"], limit=1)
        empty = deliver.dispatch(self.conn, "empty", ["A"], limit=1)
        self.assertEqual(empty["count"], 1)
        store = deliver.dispatch(self.conn, "empty2", ["A"])
        self.assertEqual(store["count"], 1)  # a1, a2 still open? a0 open
        # retrying an empty batch returns the original empty batch
        b = deliver.dispatch(self.conn, "later", ["B"], limit=100)
        for row in b["instructions"]:
            self.apply(row)
        self.assertEqual(deliver.dispatch(self.conn, "later", ["B"],
                                          limit=100), b)

    def apply(self, row):
        from platform_sim import Platform
        Platform().deliver(row)


if __name__ == "__main__":
    unittest.main()
