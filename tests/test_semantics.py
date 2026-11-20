"""Resolution semantics: opacity rules, cross-layer blank, hidden unknown,
same text via different paths, per-language coverage, silence merging."""

import json
import unittest

from common import TempDirTest
from reference import RefPackage, resolve_root

from subwarehouse.library import inspect_frame, open_library


def _man(assets, seqs, langs=("zho",)):
    return {"package": "t", "languages": list(langs), "assets": assets,
            "sequences": seqs}


class HiddenUnknownTest(TempDirTest):
    """An unknown on a low track hidden behind a higher effective track
    must not produce a final gap; it appears where the top track becomes
    transparent."""

    def test_hidden_unknown(self):
        man = _man({"U": 10, "T": 10}, {"R": 10})
        clips = [
            {"id": "low", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "U", "base": 0, "p": 1, "q": 1},
            {"id": "top", "seq": "R", "track": 1, "start": 2, "end": 5,
             "kind": "asset", "ref": "T", "base": 2, "p": 1, "q": 1},
        ]
        cov = [{"asset": "U", "lang": "zho", "start": 0, "end": 2},
               {"asset": "T", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "t", "asset": "T", "lang": "zho", "start": 0,
                 "end": 10, "text": "top"}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, cues), "R")
        rows, _ = self.segments(lib, "zho")
        got = [(r[1], r[2], r[3], r[5]) for r in rows]
        self.assertEqual(got, [
            (0, 2, "silence", None),     # U frames 0,1 covered, no cue
            (2, 5, "cue", "t"),          # top cue hides U's unknown 2..4
            (5, 10, "unknown", None),    # U frames 5..9 not checked
        ])


class CrossLayerBlankTest(TempDirTest):
    """A blank inside a child sequence is opaque and blocks the parent's
    lower tracks."""

    def test_blank_inside_child(self):
        man = _man({"C": 10, "D": 10}, {"S": 10, "R": 10})
        clips = [
            {"id": "s-c", "seq": "S", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "C", "base": 0, "p": 1, "q": 1},
            {"id": "s-blank", "seq": "S", "track": 1, "start": 2, "end": 4,
             "kind": "blank"},
            {"id": "r-s", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "seq", "ref": "S", "base": 0, "p": 1, "q": 1},
            {"id": "r-d", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "D", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": "C", "lang": "zho", "start": 0, "end": 10},
               {"asset": "D", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "c", "asset": "C", "lang": "zho", "start": 0,
                 "end": 10, "text": "cee"},
                {"id": "d", "asset": "D", "lang": "zho", "start": 0,
                 "end": 10, "text": "dee"}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, cues), "R")
        rows, _ = self.segments(lib, "zho")
        got = [(r[1], r[2], r[3], r[5]) for r in rows]
        self.assertEqual(got, [
            (0, 2, "cue", "c"),
            (2, 4, "blank", None),   # child's blank hides root's "dee"
            (4, 10, "cue", "c"),
        ])
        # blank path ends at the blank clip inside S
        blank = [r for r in rows if r[3] == "blank"][0]
        path = json.loads(blank[7])
        self.assertEqual([lv["clip"] for lv in path], ["r-s", "s-blank"])


class SameTextDifferentPathTest(TempDirTest):
    """The same cue reached through two different edit paths must stay
    two segments even when adjacent and textually identical."""

    def test_adjacent_same_cue_two_paths(self):
        man = _man({"M": 10}, {"R": 6})
        clips = [
            {"id": "c1", "seq": "R", "track": 0, "start": 0, "end": 3,
             "kind": "asset", "ref": "M", "base": 2, "p": 1, "q": 1},
            {"id": "c2", "seq": "R", "track": 0, "start": 3, "end": 6,
             "kind": "asset", "ref": "M", "base": 2, "p": 1, "q": 1},
        ]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 2,
                 "end": 5, "text": "hello"}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, cues), "R")
        rows, _ = self.segments(lib, "zho")
        cue_rows = [r for r in rows if r[3] == "cue"]
        self.assertEqual([(r[1], r[2]) for r in cue_rows], [(0, 3), (3, 6)])
        paths = [tuple(lv["clip"] for lv in json.loads(r[7]))
                 for r in cue_rows]
        self.assertEqual(paths, [("c1",), ("c2",)])
        self.assertEqual({r[4] for r in cue_rows}, {"hello"})


class LanguageCoverageTest(TempDirTest):
    """A declared language with no coverage records at all has not been
    handed over: every sampled asset frame is unknown, never guessed."""

    def test_per_language_coverage(self):
        man = _man({"M": 10}, {"R": 10}, langs=("zho", "eng"))
        clips = [{"id": "c", "seq": "R", "track": 0, "start": 0, "end": 10,
                  "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 2,
                 "end": 5, "text": "你好"}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, cues), "R")
        zho, _ = self.segments(lib, "zho")
        self.assertEqual([(r[1], r[2], r[3]) for r in zho],
                         [(0, 2, "silence"), (2, 5, "cue"),
                          (5, 10, "silence")])
        eng, _ = self.segments(lib, "eng")
        self.assertEqual([(r[1], r[2], r[3]) for r in eng],
                         [(0, 10, "unknown")])
        # every frame of every language has exactly one final state
        allrows, meta = self.segments(lib)
        for lg in ("zho", "eng"):
            rows = [r for r in allrows if r[0] == lg]
            pos = 0
            for r in rows:
                self.assertEqual(r[1], pos)
                pos = r[2]
            self.assertEqual(pos, 10)


class SilenceMergeTest(TempDirTest):
    def test_silence_merges_across_gaps(self):
        man = _man({"M": 100}, {"R": 1000})
        clips = [{"id": "c", "seq": "R", "track": 0, "start": 100,
                  "end": 110, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 100}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, None), "R")
        rows, _ = self.segments(lib, "zho")
        # covered-but-cueless frames and clip-less frames are both plain
        # silence and merge into one pathless segment
        self.assertEqual([(r[1], r[2], r[3]) for r in rows],
                         [(0, 1000, "silence")])


class FullCoverageInvariantTest(TempDirTest):
    """The library covers every declared language of the chosen film:
    each frame has exactly one final state."""

    def test_partition(self):
        man = _man({"M": 30}, {"R": 50}, langs=("zho", "eng"))
        clips = [{"id": "c", "seq": "R", "track": 0, "start": 5, "end": 45,
                  "kind": "asset", "ref": "M", "base": 3, "p": 2, "q": 3}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 30}]
        lib, _, _ = self.build(self.pkg("p", man, clips, cov, None), "R")
        rows, meta = self.segments(lib)
        for lg in ("eng", "zho"):
            segs = [r for r in rows if r[0] == lg]
            pos = 0
            for r in segs:
                self.assertEqual(r[1], pos)
                self.assertLess(r[1], r[2])
                pos = r[2]
            self.assertEqual(pos, 50)
            # adjacent segments must differ in full identity
            for a, b in zip(segs, segs[1:]):
                self.assertNotEqual((a[3], a[4], a[5], a[6], a[7]),
                                    (b[3], b[4], b[5], b[6], b[7]))


if __name__ == "__main__":
    unittest.main()
