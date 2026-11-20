"""Correction impact inventory: the hand-calculated table from the
handover docs, independent small samples for each classification rule,
order validation, determinism, and a fuzz comparison against rebuilding
the patched package with the ordinary builder."""

import json
import os
import unittest

from common import TempDirTest, write_json

from subwarehouse import builder, review
from subwarehouse.errors import ReviewError
from subwarehouse.library import open_library


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def handcalc_package(test, name="hc"):
    """The worked example: five-frame subsequence S, upper track plays
    nine-frame asset T as source frames 8,6,4,2,0; T fully checked with
    cue A at [2,5); lower track plays fully-checked U at constant speed
    with cue B at [2,4).  Program R1 takes five frames with
    base=1,p=3,q=4; program R2 takes only subsequence frames 1 and 3."""
    man = {"package": "handcalc", "languages": ["zho"],
           "assets": {"T": 9, "U": 5},
           "sequences": {"S": 5, "R1": 5, "R2": 2}}
    clips = [
        {"id": "s-t", "seq": "S", "track": 1, "start": 0, "end": 5,
         "kind": "asset", "ref": "T", "base": 8, "p": -2, "q": 1},
        {"id": "s-u", "seq": "S", "track": 0, "start": 0, "end": 5,
         "kind": "asset", "ref": "U", "base": 0, "p": 1, "q": 1},
        {"id": "r1-s", "seq": "R1", "track": 0, "start": 0, "end": 5,
         "kind": "seq", "ref": "S", "base": 1, "p": 3, "q": 4},
        {"id": "r2-s", "seq": "R2", "track": 0, "start": 0, "end": 2,
         "kind": "seq", "ref": "S", "base": 1, "p": 2, "q": 1},
    ]
    coverage = [
        {"asset": "T", "lang": "zho", "start": 0, "end": 9},
        {"asset": "U", "lang": "zho", "start": 0, "end": 5},
    ]
    cues = [
        {"id": "A", "asset": "T", "lang": "zho", "start": 2, "end": 5,
         "text": "字幕A"},
        {"id": "B", "asset": "U", "lang": "zho", "start": 2, "end": 4,
         "text": "字幕B"},
    ]
    return test.pkg(name, man, clips, coverage, cues)


def order_json(test, baseline, order_id, windows, name="order.json"):
    return write_json(os.path.join(test.tmp, name),
                      {"baseline": baseline, "order": order_id,
                       "windows": windows})


W_U_BD = {"asset": "U", "lang": "zho", "start": 2, "end": 4,
          "coverage": [{"start": 2, "end": 4}],
          "cues": [{"id": "D", "start": 2, "end": 4, "text": "字幕D"}]}
W_T_SHRINK = {"asset": "T", "lang": "zho", "start": 2, "end": 5,
              "coverage": [{"start": 2, "end": 4}], "cues": []}


def read_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def brief(rows):
    """(program, start, end, class, before state/cue-or-asset, after ...)."""
    out = []
    for r in rows:
        b = r["before"]
        a = r["after"]
        out.append((r["program"], r["start"], r["end"], r["class"],
                    (b["state"], b.get("cue") or b.get("asset")),
                    (a["state"], a.get("cue") or a.get("asset"))))
    return out


class ReviewTest(TempDirTest):
    def init_inventory(self, pkg, programs, name="inv", **kw):
        path = os.path.join(self.tmp, name)
        stats, meta = review.init_inventory(pkg, programs, path, **kw)
        return path, stats, meta

    def trial(self, inv, order_path, name="list.jsonl", **kw):
        out = os.path.join(self.tmp, name)
        summary, stats = review.run_trial(inv, order_path, out, **kw)
        return summary, stats, read_rows(out)


class HandCalcTest(ReviewTest):
    def setUp(self):
        super().setUp()
        self.hc_pkg = handcalc_package(self)
        self.inv, _, self.meta = self.init_inventory(self.hc_pkg,
                                                     ["R1", "R2"])
        self.bl = self.meta["baseline"]

    def test_lower_track_correction_alone_is_invisible(self):
        """Changing the lower track's B to D alone: neither program
        needs rework (the upper track's cue A hides U everywhere)."""
        path = order_json(self, self.bl, "w1", [W_U_BD])
        summary, stats, rows = self.trial(self.inv, path)
        self.assertEqual(rows, [])
        self.assertEqual(summary["counts"], {})
        self.assertEqual(summary["programs_affected"], 0)

    def test_upper_correction_alone(self):
        """Correcting T's [2,5) alone (new coverage [2,4), no cues):
        R1 [2,3) A->pending, [3,4) A->B; R2 only [1,2) A->B."""
        path = order_json(self, self.bl, "w2", [W_T_SHRINK])
        summary, stats, rows = self.trial(self.inv, path)
        self.assertEqual(
            brief(rows),
            [("R1", 2, 3, "playback", ("cue", "A"), ("unknown", "T")),
             ("R1", 3, 4, "playback", ("cue", "A"), ("cue", "B")),
             ("R2", 1, 2, "playback", ("cue", "A"), ("cue", "B"))])
        self.assertEqual(summary["counts"],
                         {"R1": {"zho": {"playback": 2}},
                          "R2": {"zho": {"playback": 1}}})
        # evidence carries the full winning chain on both sides
        r = rows[1]
        self.assertEqual([lv["clip"] for lv in r["before"]["path"]],
                         ["r1-s", "s-t"])
        self.assertEqual([lv["clip"] for lv in r["after"]["path"]],
                         ["r1-s", "s-u"])
        self.assertEqual(r["before"]["text"], "字幕A")
        self.assertEqual(r["after"]["text"], "字幕B")

    def test_combined_order_is_not_the_sum(self):
        """Both corrections on one order: the newly exposed cue is D,
        not B; the pending interval is unchanged."""
        path = order_json(self, self.bl, "w12", [W_U_BD, W_T_SHRINK])
        summary, stats, rows = self.trial(self.inv, path)
        self.assertEqual(
            brief(rows),
            [("R1", 2, 3, "playback", ("cue", "A"), ("unknown", "T")),
             ("R1", 3, 4, "playback", ("cue", "A"), ("cue", "D")),
             ("R2", 1, 2, "playback", ("cue", "A"), ("cue", "D"))])
        self.assertEqual(summary["counts"],
                         {"R1": {"zho": {"playback": 2}},
                          "R2": {"zho": {"playback": 1}}})

    def test_record_order_does_not_change_the_list(self):
        p1 = order_json(self, self.bl, "w12", [W_U_BD, W_T_SHRINK],
                        name="o1.json")
        w2 = dict(W_T_SHRINK)
        w1 = dict(W_U_BD)
        w1["coverage"] = list(reversed(w1["coverage"]))
        p2 = order_json(self, self.bl, "w12", [w2, w1], name="o2.json")
        _, _, rows1 = self.trial(self.inv, p1, name="l1.jsonl")
        _, _, rows2 = self.trial(self.inv, p2, name="l2.jsonl")
        with open(os.path.join(self.tmp, "l1.jsonl"), "rb") as fh:
            b1 = fh.read()
        with open(os.path.join(self.tmp, "l2.jsonl"), "rb") as fh:
            b2 = fh.read()
        self.assertEqual(b1, b2)
        self.assertEqual(rows1, rows2)

    def test_trials_never_stack(self):
        """A trial always compares against the same baseline: running
        the T correction twice gives the identical list, and running it
        after the U correction still shows B, not D."""
        p_u = order_json(self, self.bl, "w1", [W_U_BD], name="ou.json")
        p_t = order_json(self, self.bl, "w2", [W_T_SHRINK], name="ot.json")
        self.trial(self.inv, p_u, name="lu.jsonl")
        _, _, rows1 = self.trial(self.inv, p_t, name="lt1.jsonl")
        _, _, rows2 = self.trial(self.inv, p_t, name="lt2.jsonl")
        self.assertEqual(rows1, rows2)
        self.assertEqual(brief(rows1)[1][5], ("cue", "B"))


class SmallSampleTest(ReviewTest):
    """Independent small samples, one per classification rule."""

    def simple_inv(self, man, clips, coverage, cues, programs=("R",)):
        pkg = self.pkg("pkg", man, clips, coverage, cues)
        inv, _, meta = self.init_inventory(pkg, list(programs))
        return inv, meta["baseline"]

    def test_same_text_new_cue_id_is_evidence_only(self):
        man = {"package": "p", "languages": ["zho"], "assets": {"M": 10},
               "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "X", "asset": "M", "lang": "zho", "start": 2,
                 "end": 4, "text": "同文"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "Y", "start": 2, "end": 4, "text": "同文"}]}])
        summary, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "evidence", ("cue", "X"),
                           ("cue", "Y"))])
        self.assertEqual(summary["counts"],
                         {"R": {"zho": {"evidence": 2}}})
        self.assertEqual(rows[0]["before"]["text"], "同文")
        self.assertEqual(rows[0]["after"]["text"], "同文")

    def test_same_text_new_path_is_evidence_only(self):
        """Upper track gains a cue with the same text the lower track
        already shows: playback identical, source changed."""
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M1": 10, "M2": 10}, "sequences": {"R": 10}}
        clips = [
            {"id": "r-top", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "asset", "ref": "M1", "base": 0, "p": 1, "q": 1},
            {"id": "r-bot", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M2", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": "M1", "lang": "zho", "start": 0, "end": 10},
               {"asset": "M2", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "X", "asset": "M2", "lang": "zho", "start": 2,
                 "end": 4, "text": "同文"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M1", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "Y", "start": 2, "end": 4, "text": "同文"}]}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "evidence", ("cue", "X"),
                           ("cue", "Y"))])
        self.assertEqual([lv["clip"] for lv in rows[0]["before"]["path"]],
                         ["r-bot"])
        self.assertEqual([lv["clip"] for lv in rows[0]["after"]["path"]],
                         ["r-top"])

    def test_pending_for_another_asset_is_evidence_only(self):
        """Both sides pending, but a different asset is missing."""
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M1": 10, "M2": 10}, "sequences": {"R": 10}}
        clips = [
            {"id": "r-top", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "asset", "ref": "M1", "base": 0, "p": 1, "q": 1},
            {"id": "r-bot", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M2", "base": 0, "p": 1, "q": 1},
        ]
        # M1: no coverage at all (not handed over); M2: nothing either.
        inv, bl = self.simple_inv(man, clips, [], [])
        path = order_json(self, bl, "o", [
            {"asset": "M1", "lang": "zho", "start": 2, "end": 6,
             "coverage": [{"start": 2, "end": 6}], "cues": []}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 6, "evidence", ("unknown", "M1"),
                           ("unknown", "M2"))])

    def test_cue_removed_exposes_lower_track(self):
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M1": 10, "M2": 10}, "sequences": {"R": 10}}
        clips = [
            {"id": "r-top", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "asset", "ref": "M1", "base": 0, "p": 1, "q": 1},
            {"id": "r-bot", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M2", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": a, "lang": "zho", "start": 0, "end": 10}
               for a in ("M1", "M2")]
        cues = [{"id": "A", "asset": "M1", "lang": "zho", "start": 2,
                 "end": 4, "text": "上轨A"},
                {"id": "B", "asset": "M2", "lang": "zho", "start": 0,
                 "end": 10, "text": "底轨B"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M1", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}], "cues": []}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "playback", ("cue", "A"),
                           ("cue", "B"))])

    def test_coverage_revoked_hides_lower_track(self):
        """Withdrawing checked coverage turns a transparent upper track
        opaque: the lower cue that used to show can no longer be
        promised."""
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M1": 10, "M2": 10}, "sequences": {"R": 10}}
        clips = [
            {"id": "r-top", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "asset", "ref": "M1", "base": 0, "p": 1, "q": 1},
            {"id": "r-bot", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M2", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": a, "lang": "zho", "start": 0, "end": 10}
               for a in ("M1", "M2")]
        cues = [{"id": "B", "asset": "M2", "lang": "zho", "start": 0,
                 "end": 10, "text": "底轨B"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M1", "lang": "zho", "start": 2, "end": 4,
             "coverage": [], "cues": []}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "playback", ("cue", "B"),
                           ("unknown", "M1"))])

    def test_correction_on_frames_skipped_by_sampling(self):
        """The clip samples every other frame; correcting only skipped
        frames changes nothing."""
        man = {"package": "p", "languages": ["zho"], "assets": {"M": 10},
               "sequences": {"R": 5}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 5, "kind": "asset", "ref": "M", "base": 0,
                  "p": 2, "q": 1}]        # samples M frames 0,2,4,6,8
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 0,
                 "end": 3, "text": "前段A"},
                {"id": "B", "asset": "M", "lang": "zho", "start": 4,
                 "end": 10, "text": "后段B"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        # window [3,4): frame 3 is never sampled; withdraw its coverage.
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 3, "end": 4,
             "coverage": [], "cues": []}])
        summary, stats, rows = self.trial(inv, path)
        self.assertEqual(rows, [])
        self.assertEqual(summary["counts"], {})
        # the reference was walked, but the unsampled frame lifts to an
        # empty candidate window: nothing had to be rejudged
        self.assertEqual(stats["refs_walked"], 1)
        self.assertEqual(stats["candidate_frames"], 0)

    def test_explicit_blank_protects_lower_correction(self):
        man = {"package": "p", "languages": ["zho"], "assets": {"M": 10},
               "sequences": {"R": 10}}
        clips = [
            {"id": "r-blank", "seq": "R", "track": 1, "start": 0,
             "end": 10, "kind": "blank"},
            {"id": "r-m", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "B", "asset": "M", "lang": "zho", "start": 2,
                 "end": 4, "text": "底B"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "D", "start": 2, "end": 4, "text": "换D"}]}])
        summary, _, rows = self.trial(inv, path)
        self.assertEqual(rows, [])
        self.assertEqual(summary["counts"], {})

    def test_lower_corrected_but_always_covered(self):
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M1": 10, "M2": 10}, "sequences": {"R": 10}}
        clips = [
            {"id": "r-top", "seq": "R", "track": 1, "start": 0, "end": 10,
             "kind": "asset", "ref": "M1", "base": 0, "p": 1, "q": 1},
            {"id": "r-bot", "seq": "R", "track": 0, "start": 0, "end": 10,
             "kind": "asset", "ref": "M2", "base": 0, "p": 1, "q": 1},
        ]
        cov = [{"asset": a, "lang": "zho", "start": 0, "end": 10}
               for a in ("M1", "M2")]
        cues = [{"id": "A", "asset": "M1", "lang": "zho", "start": 0,
                 "end": 10, "text": "上轨A"},
                {"id": "B", "asset": "M2", "lang": "zho", "start": 2,
                 "end": 4, "text": "底B"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M2", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "D", "start": 2, "end": 4, "text": "换D"}]}])
        summary, _, rows = self.trial(inv, path)
        self.assertEqual(rows, [])

    def test_unaffected_language_not_listed(self):
        man = {"package": "p", "languages": ["eng", "zho"],
               "assets": {"M": 10}, "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
               {"asset": "M", "lang": "eng", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 2,
                 "end": 4, "text": "中A"},
                {"id": "E", "asset": "M", "lang": "eng", "start": 2,
                 "end": 4, "text": "eng E"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "A2", "start": 2, "end": 4,
                       "text": "中A改"}]}])
        summary, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "playback", ("cue", "A"),
                           ("cue", "A2"))])
        self.assertEqual(set(summary["counts"]), {"R"})
        self.assertEqual(set(summary["counts"]["R"]), {"zho"})

    def test_correction_on_language_never_handed_over(self):
        """(asset, eng) has no coverage records at all: the correction
        hands over a first cue; zho is untouched."""
        man = {"package": "p", "languages": ["eng", "zho"],
               "assets": {"M": 10}, "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        inv, bl = self.simple_inv(man, clips, cov, [])
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "eng", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "E1", "start": 2, "end": 4,
                       "text": "first eng"}]}])
        summary, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "playback", ("unknown", "M"),
                           ("cue", "E1"))])
        self.assertEqual(set(summary["counts"]["R"]), {"eng"})

    def test_two_windows_same_pair_one_order(self):
        man = {"package": "p", "languages": ["zho"], "assets": {"M": 10},
               "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 1,
                 "end": 2, "text": "一"},
                {"id": "B", "asset": "M", "lang": "zho", "start": 7,
                 "end": 8, "text": "七"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 1, "end": 2,
             "coverage": [{"start": 1, "end": 2}],
             "cues": [{"id": "A2", "start": 1, "end": 2, "text": "一改"}]},
            {"asset": "M", "lang": "zho", "start": 7, "end": 8,
             "coverage": [{"start": 7, "end": 8}],
             "cues": [{"id": "B2", "start": 7, "end": 8, "text": "七改"}]}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 1, 2, "playback", ("cue", "A"),
                           ("cue", "A2")),
                          ("R", 7, 8, "playback", ("cue", "B"),
                           ("cue", "B2"))])

    def test_reused_id_of_replaced_cue_is_allowed(self):
        man = {"package": "p", "languages": ["zho"], "assets": {"M": 10},
               "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "A", "asset": "M", "lang": "zho", "start": 2,
                 "end": 4, "text": "旧文"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "M", "lang": "zho", "start": 2, "end": 4,
             "coverage": [{"start": 2, "end": 4}],
             "cues": [{"id": "A", "start": 2, "end": 4, "text": "新文"}]}])
        _, _, rows = self.trial(inv, path)
        self.assertEqual(brief(rows),
                         [("R", 2, 4, "playback", ("cue", "A"),
                           ("cue", "A"))])
        self.assertEqual(rows[0]["before"]["text"], "旧文")
        self.assertEqual(rows[0]["after"]["text"], "新文")

    def test_correction_of_unreachable_asset_is_a_no_op(self):
        man = {"package": "p", "languages": ["zho"],
               "assets": {"M": 10, "X": 5}, "sequences": {"R": 10}}
        clips = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                  "end": 10, "kind": "asset", "ref": "M", "base": 0,
                  "p": 1, "q": 1}]
        cov = [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
               {"asset": "X", "lang": "zho", "start": 0, "end": 5}]
        cues = [{"id": "X1", "asset": "X", "lang": "zho", "start": 1,
                 "end": 2, "text": "无关"}]
        inv, bl = self.simple_inv(man, clips, cov, cues)
        path = order_json(self, bl, "o", [
            {"asset": "X", "lang": "zho", "start": 1, "end": 2,
             "coverage": [{"start": 1, "end": 2}],
             "cues": [{"id": "X2", "start": 1, "end": 2, "text": "改"}]}])
        summary, stats, rows = self.trial(inv, path)
        self.assertEqual(rows, [])
        self.assertEqual(stats["sequences_touched"], 0)


class ValidationTest(ReviewTest):
    def setUp(self):
        super().setUp()
        self.hc_pkg = handcalc_package(self)
        self.inv, _, self.meta = self.init_inventory(self.hc_pkg,
                                                     ["R1", "R2"])
        self.bl = self.meta["baseline"]

    def refuse(self, windows, baseline=None, order="bad"):
        path = order_json(self, baseline or self.bl, order, windows,
                          name="bad-%s.json" % order)
        out = os.path.join(self.tmp, "bad-%s.jsonl" % order)
        with self.assertRaises(ReviewError) as cm:
            review.run_trial(self.inv, path, out)
        self.assertFalse(os.path.exists(out),
                         "a refused order must not leave a list behind")
        return cm.exception

    def test_baseline_mismatch(self):
        e = self.refuse([W_T_SHRINK], baseline="0" * 64)
        self.assertIn("baseline mismatch", str(e))

    def test_window_out_of_bounds(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 7,
                          "end": 10}])
        self.assertIn("exceeds", str(e))
        self.assertEqual(e.item, {"window": 0})

    def test_window_empty_interval(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 4,
                          "end": 4}])
        self.assertEqual(e.item, {"window": 0})

    def test_unknown_asset(self):
        e = self.refuse([{"asset": "ZZ", "lang": "zho", "start": 0,
                          "end": 1}])
        self.assertIn("not declared", str(e))

    def test_undeclared_language(self):
        e = self.refuse([{"asset": "T", "lang": "fra", "start": 0,
                          "end": 1}])
        self.assertIn("not declared", str(e))

    def test_straddling_baseline_cue_refused(self):
        """Cue A is [2,5); a window [3,5) intersects it without
        containing it: refused, naming the cue."""
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 3,
                          "end": 5, "coverage": [], "cues": []}])
        self.assertIn("A", str(e))
        self.assertIn("no implicit truncation", str(e))
        self.assertEqual(e.item, {"window": 0, "cue": "A"})

    def test_overlapping_windows_same_pair(self):
        w1 = {"asset": "T", "lang": "zho", "start": 2, "end": 4}
        w2 = {"asset": "T", "lang": "zho", "start": 3, "end": 5}
        e = self.refuse([w1, w2])
        self.assertIn("overlap", str(e))
        self.assertEqual(e.item, {"window": 1})

    def test_touching_windows_allowed(self):
        """Touching windows on one pair are fine as long as every
        intersecting baseline cue lies wholly inside one window."""
        man = {"package": "p", "languages": ["zho"],
               "assets": {"T": 9, "U": 5},
               "sequences": {"S": 5, "R1": 5, "R2": 2}}
        clips = [
            {"id": "s-t", "seq": "S", "track": 1, "start": 0, "end": 5,
             "kind": "asset", "ref": "T", "base": 8, "p": -2, "q": 1},
            {"id": "s-u", "seq": "S", "track": 0, "start": 0, "end": 5,
             "kind": "asset", "ref": "U", "base": 0, "p": 1, "q": 1},
            {"id": "r1-s", "seq": "R1", "track": 0, "start": 0, "end": 5,
             "kind": "seq", "ref": "S", "base": 1, "p": 3, "q": 4},
            {"id": "r2-s", "seq": "R2", "track": 0, "start": 0, "end": 2,
             "kind": "seq", "ref": "S", "base": 1, "p": 2, "q": 1},
        ]
        cov = [{"asset": "T", "lang": "zho", "start": 0, "end": 9},
               {"asset": "U", "lang": "zho", "start": 0, "end": 5}]
        cues = [{"id": "A1", "asset": "T", "lang": "zho", "start": 2,
                 "end": 3, "text": "一"},
                {"id": "A2", "asset": "T", "lang": "zho", "start": 3,
                 "end": 5, "text": "二"},
                {"id": "B", "asset": "U", "lang": "zho", "start": 2,
                 "end": 4, "text": "底B"}]
        pkg = self.pkg("touch", man, clips, cov, cues)
        inv, _, meta = self.init_inventory(pkg, ["R1", "R2"],
                                           name="touch-inv")
        w1 = {"asset": "T", "lang": "zho", "start": 2, "end": 3,
              "coverage": [{"start": 2, "end": 3}], "cues": []}
        w2 = {"asset": "T", "lang": "zho", "start": 3, "end": 5,
              "coverage": [{"start": 3, "end": 4}], "cues": []}
        path = order_json(self, meta["baseline"], "ok-touch", [w1, w2])
        out = os.path.join(self.tmp, "touch.jsonl")
        review.run_trial(inv, path, out)
        # T2 falls to U's B; T3 covered cue-less falls to B; T4 unknown
        self.assertEqual(brief(read_rows(out)),
                         [("R1", 2, 3, "playback", ("cue", "A2"),
                           ("unknown", "T")),
                          ("R1", 3, 4, "playback", ("cue", "A1"),
                           ("cue", "B")),
                          ("R2", 1, 2, "playback", ("cue", "A1"),
                           ("cue", "B"))])

    def test_new_cue_outside_declared_coverage(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 2,
                          "end": 5,
                          "coverage": [{"start": 2, "end": 3}],
                          "cues": [{"id": "N", "start": 3, "end": 4,
                                    "text": "x"}]}])
        self.assertEqual(e.item, {"window": 0, "cue": "N"})

    def test_new_cues_overlap(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 2,
                          "end": 5,
                          "coverage": [{"start": 2, "end": 5}],
                          "cues": [{"id": "N1", "start": 2, "end": 4,
                                    "text": "x"},
                                   {"id": "N2", "start": 3, "end": 5,
                                    "text": "y"}]}])
        self.assertEqual(e.item, {"window": 0, "cue": "N2"})

    def test_new_cue_id_collides_with_surviving_baseline_cue(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 2,
                          "end": 5,
                          "coverage": [{"start": 2, "end": 5}],
                          "cues": [{"id": "B", "start": 2, "end": 4,
                                    "text": "x"}]}])
        self.assertIn("already exists", str(e))
        self.assertEqual(e.item, {"window": 0, "cue": "B"})

    def test_duplicate_new_cue_id_across_windows(self):
        w = {"asset": "T", "lang": "zho", "start": 2, "end": 5,
             "coverage": [{"start": 2, "end": 5}],
             "cues": [{"id": "N", "start": 2, "end": 3, "text": "x"}]}
        w2 = {"asset": "U", "lang": "zho", "start": 2, "end": 4,
              "coverage": [{"start": 2, "end": 4}],
              "cues": [{"id": "N", "start": 2, "end": 3, "text": "y"}]}
        e = self.refuse([w, w2])
        self.assertEqual(e.item, {"window": 1, "cue": "N"})

    def test_coverage_outside_window(self):
        e = self.refuse([{"asset": "T", "lang": "zho", "start": 2,
                          "end": 5,
                          "coverage": [{"start": 1, "end": 3}]}])
        self.assertEqual(e.item, {"window": 0, "coverage": 0})

    def test_valid_trial_still_uses_original_baseline_after_refusal(self):
        self.refuse([{"asset": "T", "lang": "zho", "start": 3, "end": 5,
                      "coverage": [], "cues": []}])
        path = order_json(self, self.bl, "w2", [W_T_SHRINK])
        _, _, rows = self.trial(self.inv, path)
        self.assertEqual(len(rows), 3)

    def test_directory_untouched_by_trials(self):
        db = os.path.join(self.inv, "inventory.sqlite")
        with open(db, "rb") as fh:
            before = fh.read()
        listing_before = sorted(os.listdir(self.inv))
        path = order_json(self, self.bl, "w12", [W_U_BD, W_T_SHRINK])
        self.trial(self.inv, path)
        self.refuse([{"asset": "T", "lang": "zho", "start": 3, "end": 5,
                      "coverage": [], "cues": []}], order="bad2")
        with open(db, "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after)
        self.assertEqual(listing_before, sorted(os.listdir(self.inv)))

    def test_trial_works_after_package_moved_away(self):
        moved = self.hc_pkg + ".away"
        os.rename(self.hc_pkg, moved)
        try:
            path = order_json(self, self.bl, "w2", [W_T_SHRINK])
            _, _, rows = self.trial(self.inv, path)
            self.assertEqual(len(rows), 3)
        finally:
            os.rename(moved, self.hc_pkg)


class FuzzTest(ReviewTest):
    """The windowed trial engine against a full rebuild of the patched
    package with the ordinary builder, on random small packages."""

    def test_trial_matches_full_rebuild(self):
        import random
        from test_invariance import gen_package
        for seed in range(40):
            rng = random.Random(5000 + seed)
            man, clips, coverage, cues, root = gen_package(rng)
            # register every sequence as a program for maximum reach
            programs = sorted(man["sequences"])
            pkg = self.pkg("f%d" % seed, man, clips, coverage, cues)
            inv, _, meta = self.init_inventory(pkg, programs,
                                               name="f%d-inv" % seed)
            bl = meta["baseline"]
            windows = random_order(rng, man, cues)
            order = {"baseline": bl, "order": "fuzz", "windows": windows}
            opath = write_json(os.path.join(self.tmp, "fo.json"), order)
            out = os.path.join(self.tmp, "fl.jsonl")
            summary, stats = review.run_trial(inv, opath, out)
            got = read_rows(out)

            # Full rebuild: patch the package, build one library per
            # program with the ordinary builder, diff naively.
            new_cov, new_cues = apply_order(coverage, cues, windows)
            pkg2 = self.pkg("f%d-p" % seed, man, clips, new_cov, new_cues)
            want = naive_diff(self, pkg, pkg2, programs,
                              sorted(man["languages"]), tag="f%d" % seed)
            self.assertEqual(got, want, "seed %d" % seed)
            # the trial must not examine more programs than it reports
            self.assertLessEqual(summary["programs_affected"],
                                 len(programs))


def random_order(rng, man, cues):
    """Generate a random valid correction order (1..3 windows)."""
    by_pair = {}
    for c in cues:
        by_pair.setdefault((c["asset"], c["lang"]), []).append(c)
    assets = sorted(man["assets"])
    langs = sorted(man["languages"])
    windows = []
    used_pairs = {}
    new_id = 0
    for _ in range(rng.randint(1, 3)):
        for _try in range(30):
            asset = rng.choice(assets)
            lang = rng.choice(langs)
            frames = man["assets"][asset]
            s = rng.randint(0, frames - 1)
            e = rng.randint(s + 1, frames)
            # windows of one pair must not intersect
            if any(not (e <= ws or s >= we)
                   for ws, we in used_pairs.get((asset, lang), ())):
                continue
            # baseline cues intersecting the window must be contained
            if any(not (s <= c["start"] and c["end"] <= e)
                   for c in by_pair.get((asset, lang), ())
                   if c["start"] < e and s < c["end"]):
                continue
            break
        else:
            continue
        used_pairs.setdefault((asset, lang), []).append((s, e))
        # declared coverage: zero to two sub-intervals of the window
        cov = []
        for _ in range(rng.randint(0, 2)):
            cs = rng.randint(s, e - 1)
            ce = rng.randint(cs + 1, e)
            cov.append({"start": cs, "end": ce})
        cov.sort(key=lambda iv: iv["start"])
        # declared cues: non-overlapping, inside the coverage union
        union = []
        for iv in sorted((iv["start"], iv["end"]) for iv in cov):
            if union and iv[0] <= union[-1][1]:
                union[-1] = (union[-1][0], max(union[-1][1], iv[1]))
            else:
                union.append(iv)
        wcues = []
        for us, ue in union:
            if rng.random() < 0.7:
                new_id += 1
                wcues.append({"id": "fz%d" % new_id, "start": us,
                              "end": ue, "text": "fz%d" % new_id})
        windows.append({"asset": asset, "lang": lang, "start": s,
                        "end": e, "coverage": cov, "cues": wcues})
    return windows


def apply_order(coverage, cues, windows):
    """Materialise the patched package tables (plain Python, obviously
    correct): the reference for the fuzz comparison."""
    by_pair = {}
    for w in windows:
        by_pair.setdefault((w["asset"], w["lang"]), []).append(w)
    new_cov = list(coverage)
    new_cues = list(cues)
    for (asset, lang), lst in by_pair.items():
        wins = [(w["start"], w["end"]) for w in lst]
        kept_cov = []
        for c in new_cov:
            if c["asset"] != asset or c["lang"] != lang:
                kept_cov.append(c)
                continue
            pieces = [(c["start"], c["end"])]
            for ws, we in wins:
                nxt = []
                for s, e in pieces:
                    if we <= s or ws >= e:
                        nxt.append((s, e))
                        continue
                    if ws > s:
                        nxt.append((s, ws))
                    if we < e:
                        nxt.append((we, e))
                pieces = nxt
            kept_cov.extend({"asset": asset, "lang": lang, "start": s,
                             "end": e} for s, e in pieces)
        for w in lst:
            kept_cov.extend({"asset": asset, "lang": lang,
                             "start": iv["start"], "end": iv["end"]}
                            for iv in w.get("coverage", []))
        new_cov = kept_cov
        new_cues = [c for c in new_cues
                    if (c["asset"], c["lang"]) != (asset, lang)
                    or not any(ws < c["end"] and c["start"] < we
                               for ws, we in wins)]
        for w in lst:
            new_cues.extend({"id": c["id"], "asset": asset, "lang": lang,
                             "start": c["start"], "end": c["end"],
                             "text": c["text"]}
                            for c in w.get("cues", []))
    return new_cov, new_cues


def library_segments(test, pkg_dir, programs, languages, tag):
    """Build one library per program with the ordinary builder and
    collect {program: {lang: [segments]}}."""
    out = {}
    for i, prog in enumerate(programs):
        lib = os.path.join(test.tmp, "%s-%d.sqlite" % (tag, i))
        builder.build(pkg_dir, prog, lib)
        conn, meta = open_library(lib)
        try:
            per_lang = {}
            for lang in languages:
                rows = conn.execute(
                    "SELECT start, \"end\", state, text, cue_id, "
                    "asset_id, path FROM segments WHERE lang = ? "
                    "ORDER BY start", (lang,)).fetchall()
                per_lang[lang] = rows
        finally:
            conn.close()
        out[prog] = per_lang
    return out


def naive_diff(test, pkg_base, pkg_new, programs, languages, tag):
    """Per-frame diff of the two builds, classified and merged exactly
    like the review list."""
    base = library_segments(test, pkg_base, programs, languages,
                            tag + "-base")
    new = library_segments(test, pkg_new, programs, languages,
                           tag + "-new")
    rows = []
    for prog in programs:
        for lang in languages:
            rows.extend(_diff_program(prog, lang, base[prog][lang],
                                      new[prog][lang]))
    rows.sort(key=lambda r: (r["program"], r["lang"], r["start"]))
    return rows


def _evidence(state, text, cue_id, asset_id, path_json):
    obj = {"state": state}
    if state == "cue":
        obj["text"] = text
        obj["cue"] = cue_id
        obj["asset"] = asset_id
    elif state == "unknown":
        obj["asset"] = asset_id
    obj["path"] = json.loads(path_json)
    return obj


def _playback(state, text):
    if state == "cue":
        return ("cue", text)
    if state == "unknown":
        return ("unknown",)
    return ("none",)


def _diff_program(prog, lang, base_rows, new_rows):
    """Two-pointer merge of the two segmentations (both partition the
    same timeline), emitting maximal runs of identical
    (class, before, after)."""
    rows = []
    bi = iter(base_rows)
    ni = iter(new_rows)
    b = next(bi, None)
    n = next(ni, None)
    pos = 0
    while b is not None and n is not None:
        e = min(b[1], n[1])
        if b[2:] != n[2:]:
            cls = "playback" \
                if _playback(b[2], b[3]) != _playback(n[2], n[3]) \
                else "evidence"
            row = {"program": prog, "lang": lang, "start": pos,
                   "end": e, "class": cls,
                   "before": _evidence(*b[2:]),
                   "after": _evidence(*n[2:])}
            if rows and rows[-1]["end"] == pos \
                    and rows[-1]["class"] == cls \
                    and rows[-1]["before"] == row["before"] \
                    and rows[-1]["after"] == row["after"]:
                rows[-1]["end"] = e
            else:
                rows.append(row)
        if b[1] == e:
            b = next(bi, None)
        if n[1] == e:
            n = next(ni, None)
        pos = e
    return rows


if __name__ == "__main__":
    unittest.main()


class CliTest(ReviewTest):
    """The review subcommands through the main CLI: init -> info ->
    trial, plus a machine-readable refusal."""

    def _run(self, *args):
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "-m", "subwarehouse"] +
                              list(args), cwd=root, capture_output=True,
                              text=True)

    def test_cli_round_trip(self):
        pkg = handcalc_package(self)
        inv = os.path.join(self.tmp, "inv")
        proc = self._run("review", "review-init", "--package", pkg,
                         "--dir", inv, "--program", "R1",
                         "--program", "R2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        init = json.loads(proc.stdout)
        self.assertTrue(init["ok"])
        self.assertEqual(init["programs"], ["R1", "R2"])

        proc = self._run("review", "review-info", "--dir", inv)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertEqual(info["baseline"], init["baseline"])

        order = order_json(self, init["baseline"], "w12",
                           [W_U_BD, W_T_SHRINK])
        out = os.path.join(self.tmp, "list.jsonl")
        proc = self._run("review", "review-trial", "--dir", inv,
                         "--order", order, "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["counts"],
                         {"R1": {"zho": {"playback": 2}},
                          "R2": {"zho": {"playback": 1}}})
        rows = read_rows(out)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["program"] for r in rows],
                         ["R1", "R1", "R2"])

    def test_cli_refusal_is_machine_readable(self):
        pkg = handcalc_package(self)
        inv = os.path.join(self.tmp, "inv")
        proc = self._run("review", "review-init", "--package", pkg,
                         "--dir", inv, "--program", "R1",
                         "--program", "R2")
        baseline = json.loads(proc.stdout)["baseline"]
        order = order_json(self, baseline, "bad",
                           [{"asset": "T", "lang": "zho", "start": 3,
                             "end": 5, "coverage": [], "cues": []}])
        out = os.path.join(self.tmp, "never.jsonl")
        proc = self._run("review", "review-trial", "--dir", inv,
                         "--order", order, "--out", out)
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertFalse(err["ok"])
        self.assertEqual(err["item"], {"window": 0, "cue": "A"})
        self.assertFalse(os.path.exists(out))

    def test_cli_init_never_overwrites(self):
        pkg = handcalc_package(self)
        inv = os.path.join(self.tmp, "inv")
        for _ in range(2):
            proc = self._run("review", "review-init", "--package", pkg,
                             "--dir", inv, "--program", "R1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
