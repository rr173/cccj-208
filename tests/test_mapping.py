"""Frame mapping: fractional speeds both directions, per-level rounding,
unsampled short cues, boundary checks."""

import json
import unittest

from common import TempDirTest
from reference import RefPackage, resolve_root

from subwarehouse.library import inspect_frame, open_library


def _man(assets, seqs, langs=("zho",)):
    return {"package": "t", "languages": list(langs), "assets": assets,
            "sequences": seqs}


class FractionalSpeedTest(TempDirTest):
    def _pkg(self, clips, cues, cov):
        man = _man({"P": 100}, {"R": 200})
        return self.pkg("p", man, clips, cov, cues)

    def test_forward_fractional(self):
        # floor(3k/2): samples 0,1,3,4,6,7,...
        clips = [{"id": "c1", "seq": "R", "track": 0, "start": 0,
                  "end": 60, "kind": "asset", "ref": "P", "base": 0,
                  "p": 3, "q": 2}]
        cues = [{"id": "x", "asset": "P", "lang": "zho", "start": 10,
                 "end": 12, "text": "ten"},
                {"id": "y", "asset": "P", "lang": "zho", "start": 51,
                 "end": 52, "text": "odd"}]
        cov = [{"asset": "P", "lang": "zho", "start": 0, "end": 100}]
        pkg_dir = self._pkg(clips, cues, cov)
        lib, _, _ = self.build(pkg_dir, "R")
        pkg = RefPackage.load(pkg_dir)
        conn, meta = open_library(lib)
        try:
            for f in range(200):
                ref = resolve_root(pkg, "R", "zho", f)
                info = inspect_frame(conn, meta, "zho", f)
                self.assertEqual(info["state"], ref["state"],
                                 "frame %d" % f)
                if ref["state"] == "cue":
                    self.assertEqual(info["text"], ref["text"])
        finally:
            conn.close()
        # cue x [10,12): k with floor(3k/2) in {10,11} -> k=7 (10)
        rows, _ = self.segments(lib, "zho")
        x_segs = [(r[1], r[2]) for r in rows if r[5] == "x"]
        self.assertEqual(x_segs, [(7, 8)])
        # cue y at odd frame 51: floor(3k/2) hits 51? k=34 -> 51 yes
        y_segs = [(r[1], r[2]) for r in rows if r[5] == "y"]
        self.assertEqual(y_segs, [(34, 35)])

    def test_reverse_fractional(self):
        clips = [{"id": "c1", "seq": "R", "track": 0, "start": 0,
                  "end": 60, "kind": "asset", "ref": "P", "base": 89,
                  "p": -3, "q": 2}]
        cues = [{"id": "x", "asset": "P", "lang": "zho", "start": 0,
                 "end": 2, "text": "head"}]
        cov = [{"asset": "P", "lang": "zho", "start": 0, "end": 100}]
        pkg_dir = self._pkg(clips, cues, cov)
        lib, _, _ = self.build(pkg_dir, "R")
        pkg = RefPackage.load(pkg_dir)
        conn, meta = open_library(lib)
        try:
            for f in range(200):
                ref = resolve_root(pkg, "R", "zho", f)
                info = inspect_frame(conn, meta, "zho", f)
                self.assertEqual(info["state"], ref["state"],
                                 "frame %d" % f)
        finally:
            conn.close()
        rows, _ = self.segments(lib, "zho")
        # P frames 0,1 sampled at k with 89+floor(-3k/2) in {0,1}:
        # k=59 -> 0 ; k=58 -> 2 ... so only frame 0 hit, at k=59.
        x_segs = [(r[1], r[2]) for r in rows if r[5] == "x"]
        self.assertEqual(x_segs, [(59, 60)])

    def test_unsampled_short_cue_never_appears(self):
        # p=2 q=1 samples even frames only; cue at odd frame 51 is skipped.
        clips = [{"id": "c1", "seq": "R", "track": 0, "start": 0,
                  "end": 50, "kind": "asset", "ref": "P", "base": 0,
                  "p": 2, "q": 1}]
        cues = [{"id": "even", "asset": "P", "lang": "zho", "start": 50,
                 "end": 51, "text": "even"},
                {"id": "odd", "asset": "P", "lang": "zho", "start": 51,
                 "end": 52, "text": "odd"}]
        cov = [{"asset": "P", "lang": "zho", "start": 0, "end": 100}]
        lib, _, _ = self.build(self._pkg(clips, cues, cov), "R")
        rows, _ = self.segments(lib, "zho")
        ids = [r[5] for r in rows if r[3] == "cue"]
        self.assertIn("even", ids)
        self.assertNotIn("odd", ids)
        even = [(r[1], r[2]) for r in rows if r[5] == "even"]
        self.assertEqual(even, [(25, 26)])


class TwoLevelRoundingTest(TempDirTest):
    def test_root_frame_1_lands_on_asset_frame_0(self):
        # Level 1: p/q = 2/3, level 2: p/q = 3/2.  Composing the rates
        # would give 1 and map root frame 1 to asset frame 1; independent
        # per-level floor rounding maps it to asset frame 0.
        man = _man({"Z": 20}, {"S": 10, "R": 10})
        clips = [
            {"id": "s-z", "seq": "S", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "Z", "base": 0, "p": 3, "q": 2},
            {"id": "r-s", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "seq", "ref": "S", "base": 0, "p": 2, "q": 3},
        ]
        cov = [{"asset": "Z", "lang": "zho", "start": 0, "end": 20}]
        cues = [{"id": "z0", "asset": "Z", "lang": "zho", "start": 0,
                 "end": 1, "text": "zero"},
                {"id": "z1", "asset": "Z", "lang": "zho", "start": 1,
                 "end": 2, "text": "one"}]
        pkg_dir = self.pkg("p", man, clips, cov, cues)
        lib, _, _ = self.build(pkg_dir, "R")
        conn, meta = open_library(lib)
        try:
            info = inspect_frame(conn, meta, "zho", 1)
            self.assertEqual(info["state"], "cue")
            self.assertEqual(info["cue"], "z0")
            self.assertEqual([lv["child_frame"] for lv in info["levels"]],
                             [0, 0])
            self.assertEqual(info["asset_frame"], 0)
            # full per-frame agreement with the reference
            pkg = RefPackage.load(pkg_dir)
            for f in range(10):
                ref = resolve_root(pkg, "R", "zho", f)
                got = inspect_frame(conn, meta, "zho", f)
                self.assertEqual(got["state"], ref["state"],
                                 "frame %d" % f)
                if ref["state"] == "cue":
                    self.assertEqual(got["cue"], ref["cue"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
