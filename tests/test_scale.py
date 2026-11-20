"""Scale behaviour: huge frame spans must not cause per-frame work, and
the library stays queryable after the input package is moved away."""

import os
import time
import unittest

from common import TempDirTest

from subwarehouse.library import inspect_frame, iter_segments, open_library

BIG = 3_000_000_000  # billions of frames


def _sparse_pkg(test, frames, n_cues=64):
    man = {"package": "sparse", "languages": ["zho"],
           "assets": {"A": frames}, "sequences": {"S": frames,
                                                  "R": frames}}
    clips = [
        {"id": "s-a", "seq": "S", "track": 0, "start": 0, "end": frames,
         "kind": "asset", "ref": "A", "base": frames - 1, "p": -1, "q": 1},
        {"id": "r-s", "seq": "R", "track": 0, "start": 0, "end": frames,
         "kind": "seq", "ref": "S", "base": 0, "p": 2, "q": 3},
    ]
    coverage = [{"asset": "A", "lang": "zho", "start": 0, "end": frames}]
    cues = []
    step = frames // (n_cues + 1)
    for i in range(n_cues):
        s = (i + 1) * step
        cues.append({"id": "q%d" % i, "asset": "A", "lang": "zho",
                     "start": s, "end": s + max(1, step // 4),
                     "text": "cue %d" % i})
    return test.pkg("sparse", man, clips, coverage, cues)


class ScaleTest(TempDirTest):
    def test_billion_frame_span_builds_without_frame_iteration(self):
        pkg = _sparse_pkg(self, BIG)
        t0 = time.monotonic()
        lib, stats, meta = self.build(pkg, "R", mem_budget_mb=64)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 30)  # would be hopeless if per-frame
        # work is proportional to interval endpoints, not the frame span
        self.assertLess(stats["written"]["segments"], 1000)
        rows, _ = self.segments(lib, "zho")
        pos = 0
        for r in rows:
            self.assertEqual(r[1], pos)
            pos = r[2]
        self.assertEqual(pos, BIG)
        # move the input away: library must answer on its own
        moved = pkg + ".away"
        os.rename(pkg, moved)
        conn, meta2 = open_library(lib)
        try:
            self.assertEqual(int(meta2["root_frames"]), BIG)
            segs = list(iter_segments(conn, "zho", BIG // 2 - 10,
                                      BIG // 2 + 10))
            self.assertTrue(segs)
            info = inspect_frame(conn, meta2, "zho", BIG // 2)
            self.assertIn(info["state"],
                          ("cue", "blank", "unknown", "silence"))
        finally:
            conn.close()
        os.rename(moved, pkg)

    def test_query_short_interval_after_input_removed(self):
        pkg = _sparse_pkg(self, BIG, n_cues=8)
        lib, _, _ = self.build(pkg, "R")
        os.rename(pkg, pkg + ".away")
        try:
            conn, meta = open_library(lib)
            try:
                # a short window around a known reachable cue: the root
                # maps frame f to S frame floor(2f/3) < 2e9, and S plays
                # A backwards, so only A frames > 1e9 are reachable;
                # cue q5 sits at A frame 6*step.
                step = BIG // 9
                a_frame = 6 * step
                target_s = BIG - 1 - a_frame
                f0 = None
                for cand in range((target_s * 3) // 2 - 2,
                                  (target_s * 3) // 2 + 3):
                    if (cand * 2) // 3 == target_s:
                        f0 = cand
                        break
                self.assertIsNotNone(f0)
                self.assertLess(f0, BIG)
                segs = list(iter_segments(conn, "zho", f0 - 5, f0 + 5))
                self.assertTrue(any(r[3] == "cue" for r in segs))
                info = inspect_frame(conn, meta, "zho", f0)
                self.assertEqual(info["state"], "cue")
                self.assertEqual(info["cue"], "q5")
                self.assertEqual(info["levels"][0]["child_frame"],
                                 target_s)
                self.assertEqual(info["levels"][1]["child_frame"], a_frame)
            finally:
                conn.close()
        finally:
            os.rename(pkg + ".away", pkg)


if __name__ == "__main__":
    unittest.main()
