"""The handover sample: exact expected segments and frame inspections."""

import json
import os
import unittest

from common import TempDirTest
from make_sample import (CLIPS, CUES, COVERAGE, EXPECTED_SEGMENTS,
                         MANIFEST, write_sample)
from reference import RefPackage, resolve_root, trace_lines

from subwarehouse.library import inspect_frame, open_library


class SampleTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.pkg_dir = write_sample(os.path.join(self.tmp, "sample"))
        self.lib, self.stats, self.meta = self.build(self.pkg_dir, "R")

    def test_segments_match_spec(self):
        rows, _ = self.segments(self.lib, "zho")
        got = [(s, e, st, text, cue,
                [lv["clip"] for lv in json.loads(path)])
               for (_, s, e, st, text, cue, _asset, path) in rows]
        want = [(s, e, st, text, cue, clips)
                for (s, e, st, text, cue, clips) in EXPECTED_SEGMENTS]
        self.assertEqual(got, want)

    def test_b_only_at_4_5_and_unknown_at_5_7(self):
        rows, _ = self.segments(self.lib, "zho")
        by_state = {(r[1], r[2]): (r[3], r[5]) for r in rows}
        self.assertEqual(by_state[(4, 5)], ("cue", "B"))
        self.assertEqual(by_state[(5, 7)], ("unknown", None))
        # B must not appear anywhere else
        for r in rows:
            if r[4] == "B":
                self.assertEqual((r[1], r[2]), (4, 5))

    def test_inspect_frame_12_walks_to_s6_m3(self):
        conn, meta = open_library(self.lib)
        try:
            info = inspect_frame(conn, meta, "zho", 12)
        finally:
            conn.close()
        self.assertEqual(info["state"], "cue")
        self.assertEqual(info["cue"], "A")
        self.assertEqual(info["asset"], "M")
        self.assertEqual(info["asset_frame"], 3)
        self.assertEqual([lv["clip"] for lv in info["levels"]],
                         ["r-bottom", "s-rev"])
        self.assertEqual([lv["child_frame"] for lv in info["levels"]],
                         [6, 3])
        self.assertEqual([lv["track"] for lv in info["levels"]], [0, 0])

    def test_inspect_unknown_blank_silence(self):
        conn, meta = open_library(self.lib)
        try:
            u = inspect_frame(conn, meta, "zho", 5)
            self.assertEqual(u["state"], "unknown")
            self.assertEqual((u["asset"], u["missing_frame"]), ("N", 1))
            u6 = inspect_frame(conn, meta, "zho", 6)
            self.assertEqual((u6["asset"], u6["missing_frame"]), ("N", 2))
            b = inspect_frame(conn, meta, "zho", 11)
            self.assertEqual(b["state"], "blank")
            self.assertEqual(b["blank_clip"], "r-blank")
            s = inspect_frame(conn, meta, "zho", 0)
            self.assertEqual(s["state"], "silence")
            self.assertEqual(s["levels"], [])
            self.assertNotIn("asset", s)
        finally:
            conn.close()

    def test_matches_per_frame_reference(self):
        pkg = RefPackage.load(self.pkg_dir)
        conn, meta = open_library(self.lib)
        try:
            for f in range(MANIFEST["sequences"]["R"]):
                ref = resolve_root(pkg, "R", "zho", f)
                info = inspect_frame(conn, meta, "zho", f)
                self.assertEqual(info["state"], ref["state"], "frame %d" % f)
                self.assertEqual([lv["clip"] for lv in info["levels"]],
                                 [lv["clip"] for lv in ref["path"]],
                                 "frame %d" % f)
                if ref["state"] == "cue":
                    self.assertEqual(info["text"], ref["text"])
                    self.assertEqual(info["asset_frame"],
                                     ref["asset_frame"])
                if ref["state"] == "unknown":
                    self.assertEqual(info["missing_frame"],
                                     ref["missing_frame"])
        finally:
            conn.close()

    def test_reference_trace_lists_coverage_and_path(self):
        pkg = RefPackage.load(self.pkg_dir)
        lines = trace_lines(pkg, "R", "zho", 12)
        text = "\n".join(lines)
        self.assertIn("frame 12 lang zho -> cue", text)
        self.assertIn("child frame 6", text)
        self.assertIn("child frame 3", text)
        self.assertIn("cue A", text)
        lines = trace_lines(pkg, "R", "zho", 5)
        self.assertIn("not covered", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
