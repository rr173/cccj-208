"""Machine-readable CLI: every subcommand emits one JSON object."""

import json
import os
import subprocess
import sys
import unittest

from common import LedgerTest, write_json
from platform_sim import Platform, write_receipts

from subwarehouse import builder


def _run(*args):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, "-m", "subwarehouse"] +
                          list(args), cwd=root, capture_output=True,
                          text=True)
    return proc


class CliTest(LedgerTest):
    def setUp(self):
        super().setUp()
        from test_delivery_content import THREE, FULL, _lib
        self.lib = _lib(self, "v1", THREE, FULL)
        self.ledger = os.path.join(self.tmp, "ledger.sqlite")
        proc = _run("ledger", "ledger-init", "--ledger", self.ledger)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["ok"], True)
        self.langs = write_json(os.path.join(self.tmp, "langs.json"), {
            "zho": self.shard_table(12, [4, 4, 4], "s")})

    def cli(self, *args, ok=True):
        proc = _run("ledger", *args)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        return proc.stderr

    def test_register_dispatch_receipts_verify(self):
        reg = self.cli("ledger-register", "--ledger", self.ledger,
                       "--target", "A", "--release", "1", "--lib",
                       self.lib, "--languages", self.langs)
        self.assertEqual(reg["release"], 1)
        self.assertEqual(reg["languages"], ["zho"])

        batch = self.cli("ledger-dispatch", "--ledger", self.ledger,
                         "--request", "take-1", "--target", "A",
                         "--limit", "2")
        self.assertEqual(batch["count"], 2)
        self.assertTrue(batch["more"])
        self.assertEqual([r["shard"] for r in batch["instructions"]],
                         ["s0", "s1"])
        # retry returns the identical stored batch
        again = self.cli("ledger-dispatch", "--ledger", self.ledger,
                         "--request", "take-1", "--target", "A",
                         "--limit", "2")
        self.assertEqual(again, batch)
        stderr = self.cli("ledger-dispatch", "--ledger", self.ledger,
                          "--request", "take-1", "--target", "A",
                          "--limit", "3", ok=False)
        self.assertIn("different parameters", stderr)

        read_back = self.cli("ledger-batch", "--ledger", self.ledger,
                             "--request", "take-1")
        self.assertEqual(read_back["stored_count"], 2)
        self.assertEqual([o["status"] for o in read_back["operations"]],
                         ["open", "open"])
        self.assertEqual(read_back["progress"][0]["open_operations"], 2)

        pf = Platform()
        for row in batch["instructions"]:
            pf.deliver(row)
        rest = self.cli("ledger-dispatch", "--ledger", self.ledger,
                        "--request", "take-2", "--target", "A")
        self.assertEqual([r["shard"] for r in rest["instructions"]],
                         ["s2"])
        for row in rest["instructions"]:
            pf.deliver(row)

        rf = write_receipts(os.path.join(self.tmp, "r.jsonl"),
                            pf.dump_receipts("A"))
        imp = self.cli("ledger-receipts", "--ledger", self.ledger, rf)
        self.assertEqual(imp["progress"][0]["confirmed_prefix"], 3)
        prog = self.cli("ledger-progress", "--ledger", self.ledger,
                        "--target", "A")
        self.assertEqual(prog["confirmed_prefix"], 3)
        self.assertEqual(prog["first_missing"], 4)
        self.assertEqual(prog["open_operations"], 0)

        v = self.cli("ledger-verify", "--ledger", self.ledger,
                     "--target", "A", "--lang", "zho", "--shard", "s1")
        self.assertEqual(v["position"], 3)
        self.assertEqual(v["platform"]["content"],
                         [{"start": 0, "end": 2, "text": "乙"}])
        self.assertTrue(v["platform"]["matches_expected"])
        v0 = self.cli("ledger-verify", "--ledger", self.ledger,
                      "--target", "A", "--lang", "zho", "--shard", "s1",
                      "--upto", "0")
        self.assertEqual(v0["platform"]["state"], "initial_empty")
        v3 = self.cli("ledger-verify", "--ledger", self.ledger,
                      "--target", "A", "--lang", "zho", "--shard", "s1",
                      "--upto", "3")
        self.assertEqual(v3["receipt_at_position"]["status"], "applied")
        self.assertTrue(v3["receipt_at_position"]["evidence"])
        # a position beyond the confirmed prefix is refused, not answered
        stderr = self.cli("ledger-verify", "--ledger", self.ledger,
                          "--target", "A", "--lang", "zho", "--shard",
                          "s1", "--upto", "4", ok=False)
        self.assertIn("confirmed prefix", stderr)

    def test_cli_refusals_are_json_errors(self):
        stderr = self.cli("ledger-progress", "--ledger", self.ledger,
                          "--target", "nope", ok=False)
        self.assertIn("not registered", stderr)
        stderr = self.cli("ledger-register", "--ledger", self.ledger,
                          "--target", "A", "--release", "1", "--lib",
                          self.lib, ok=False)
        self.assertIn("must configure", stderr)
        self.cli("ledger-register", "--ledger", self.ledger, "--target",
                 "A", "--release", "1", "--lib", self.lib, "--languages",
                 self.langs)
        stderr = self.cli("ledger-verify", "--ledger", self.ledger,
                          "--target", "A", "--lang", "zho", ok=False)
        self.assertIn("needs both", stderr)


if __name__ == "__main__":
    unittest.main()
