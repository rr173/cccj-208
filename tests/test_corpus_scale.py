"""Corpus-export scale behaviour: extraction and authorization work is
proportional to the segments and grant intervals touched (never to the
window's frame count), and one hot relation key with tens of thousands
of members costs a linear number of stored associations (never
pairwise).  Samples are generated at runtime, not committed."""

import json
import os
import sys
import time
import unittest

from common import TempDirTest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "tools")))
from gen_corpus_scale import (hotkey_inputs, make_hotkey, make_sparse,
                              sparse_inputs)            # noqa: E402

from subwarehouse import builder, corpus                # noqa: E402

BIG = 1_000_000_000


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


class SparseWindowTest(TempDirTest):
    def run_config(self, name, frames):
        dest = os.path.join(self.tmp, name)
        pkg = make_sparse(dest, frames, n_cues=200)
        lib = os.path.join(dest, "lib.sqlite")
        _st, meta = builder.build(pkg, "R", lib, mem_budget_mb=64)
        inputs = sparse_inputs(dest, lib, meta["logical_version"], frames)
        out = os.path.join(dest, "out")
        t0 = time.monotonic()
        summary, stats = corpus.export_corpus(
            inputs["libraries"], inputs["samples"], inputs["mapping"],
            inputs["grants"], None, out, mem_budget_mb=64)
        elapsed = time.monotonic() - t0
        rows = read_jsonl(os.path.join(out, "train.jsonl")) + \
            read_jsonl(os.path.join(out, "validation.jsonl"))
        return stats, elapsed, rows

    def test_workload_is_span_independent(self):
        s_short, t_short, rows_short = self.run_config("short", 100_000)
        s_long, t_long, rows_long = self.run_config("long", BIG)
        # Identical interval-level workload for a 10,000x span.
        self.assertEqual(s_short["read"]["segments"],
                         s_long["read"]["segments"])
        self.assertEqual(s_short["read"]["grants"],
                         s_long["read"]["grants"])
        self.assertEqual(s_short["written"]["cues"],
                         s_long["written"]["cues"])
        self.assertEqual(s_short["written"]["rel"],
                         s_long["written"]["rel"])
        # Identical delivered structure: same samples, same cue counts,
        # same relation keys (texts and source keys are span-free; only
        # absolute frame coordinates legitimately differ).
        def structure(rows):
            return sorted((r["id"], len(r["cues"]),
                           json.dumps(r["keys"], sort_keys=True,
                                      ensure_ascii=False))
                          for r in rows)
        self.assertEqual(structure(rows_short), structure(rows_long))
        self.assertEqual(sum(len(r["cues"]) for r in rows_short), 300)
        # Billion-frame windows: no per-frame iteration.
        self.assertLess(t_long, 30)


class HotKeyTest(TempDirTest):
    def test_hot_key_membership_is_linear(self):
        n = 20_000
        dest = os.path.join(self.tmp, "hot")
        pkg = make_hotkey(dest, n)
        lib = os.path.join(dest, "lib.sqlite")
        _st, meta = builder.build(pkg, "R", lib, mem_budget_mb=64)
        inputs = hotkey_inputs(dest, lib, meta["logical_version"], n)
        out = os.path.join(dest, "out")
        t0 = time.monotonic()
        summary, stats = corpus.export_corpus(
            inputs["libraries"], inputs["samples"], inputs["mapping"],
            inputs["grants"], None, out, mem_budget_mb=64)
        elapsed = time.monotonic() - t0
        # Every sample delivered to one side (no history): one component.
        self.assertEqual(summary["counts"]["rejected"], 0)
        self.assertEqual(summary["counts"]["quarantine"], 0)
        self.assertEqual(summary["counts"]["train"] +
                         summary["counts"]["validation"], n)
        # Two relation rows per sample (src + txt), not n choose 2.
        self.assertEqual(stats["written"]["rel"], 2 * n)
        # One merge edge per added member; the second key merges nothing.
        self.assertEqual(stats["grouping"]["merges"], n - 1)
        self.assertEqual(stats["grouping"]["components"], 1)
        # The registry stores the hot key once, not once per pair.
        info = corpus.registry_info(os.path.join(out, "registry.sqlite"))
        self.assertEqual(info["members"], n)
        self.assertEqual(info["keys"], 2)
        self.assertLess(elapsed, 120)


if __name__ == "__main__":
    unittest.main()
