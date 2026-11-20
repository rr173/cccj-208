"""Review-trial scale behaviour: per-trial work depends on the touched
references, time ranges and languages only -- never on the number of
irrelevant registered programs, nor on the frame span of untouched
timelines.  Samples are generated at runtime, not committed."""

import json
import os
import sys
import time
import unittest

from common import TempDirTest, write_json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "tools")))
from gen_review_scale import correction_order, make_package  # noqa: E402

from subwarehouse import review  # noqa: E402

BIG = 1_000_000_000


def read_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


class ReviewScaleTest(TempDirTest):
    def run_config(self, name, n_programs, frames):
        pkg = os.path.join(self.tmp, "pkg-%s" % name)
        info = make_package(pkg, n_programs, frames, seed=7)
        inv = os.path.join(self.tmp, "inv-%s" % name)
        review.init_inventory(pkg, info["programs"], inv,
                              mem_budget_mb=64)
        _, meta = review.open_inventory(inv)
        order = correction_order(meta["baseline"])
        opath = write_json(os.path.join(self.tmp, "o-%s.json" % name),
                           order)
        out = os.path.join(self.tmp, "l-%s.jsonl" % name)
        t0 = time.monotonic()
        summary, stats = review.run_trial(inv, opath, out,
                                          mem_budget_mb=64)
        elapsed = time.monotonic() - t0
        with open(out, "rb") as fh:
            body = fh.read()
        return summary, stats, body, elapsed, inv, pkg

    def test_trial_workload_is_local(self):
        small = self.run_config("small", 4, 100_000)
        many = self.run_config("many", 128, 100_000)
        long = self.run_config("long", 4, BIG)
        huge = self.run_config("huge", 128, BIG)
        s_small, s_many, s_long, s_huge = (c[1] for c in
                                           (small, many, long, huge))
        # identical per-trial workload in all four configurations
        keys = ["refs_walked", "sequences_touched", "sequences_rejudged",
                "programs_rejudged", "candidate_windows",
                "candidate_frames", "review_rows"]
        for k in keys:
            self.assertEqual(s_small[k], s_huge[k], k)
            self.assertEqual(s_small[k], s_many[k], k)
            self.assertEqual(s_small[k], s_long[k], k)
        self.assertEqual(s_small["read"], s_huge["read"])
        self.assertEqual(s_small["written"], s_huge["written"])
        # identical review list everywhere
        self.assertEqual(small[2], many[2])
        self.assertEqual(small[2], long[2])
        self.assertEqual(small[2], huge[2])
        # the list is the expected local change on P0 only
        rows = read_rows(os.path.join(self.tmp, "l-small.jsonl"))
        self.assertEqual(
            [(r["program"], r["start"], r["end"], r["class"],
              r["before"]["state"], r["after"]["state"]) for r in rows],
            [("P0", 500, 502, "playback", "cue", "cue"),
             ("P0", 502, 503, "playback", "cue", "unknown")])
        # billion-frame timelines: no per-frame iteration
        self.assertLess(huge[3], 30)
        self.assertLess(long[3], 30)

    def test_trial_after_package_moved_away(self):
        summary, stats, body, elapsed, inv, pkg = \
            self.run_config("away", 8, BIG)
        moved = pkg + ".away"
        os.rename(pkg, moved)
        try:
            _, meta = review.open_inventory(inv)
            opath = write_json(
                os.path.join(self.tmp, "o-away.json"),
                correction_order(meta["baseline"]))
            out = os.path.join(self.tmp, "l-away.jsonl")
            summary2, stats2 = review.run_trial(inv, opath, out,
                                                mem_budget_mb=64)
            with open(out, "rb") as fh:
                self.assertEqual(body, fh.read())
            self.assertEqual(stats["read"], stats2["read"])
        finally:
            os.rename(moved, pkg)


if __name__ == "__main__":
    unittest.main()
