"""Invalid input rejection and library state around failed builds."""

import json
import os
import unittest

from common import TempDirTest

from subwarehouse import builder
from subwarehouse.errors import PackageError, TargetExistsError


def _man(assets=None, seqs=None, langs=("zho",)):
    return {"package": "t", "languages": list(langs),
            "assets": assets or {"M": 10},
            "sequences": seqs or {"R": 10}}


def _clip(**kw):
    d = {"id": "c", "seq": "R", "track": 0, "start": 0, "end": 5,
         "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}
    d.update(kw)
    return d


def _cue(**kw):
    d = {"id": "A", "asset": "M", "lang": "zho", "start": 0, "end": 2,
         "text": "x"}
    d.update(kw)
    return d


def _cov(**kw):
    d = {"asset": "M", "lang": "zho", "start": 0, "end": 10}
    d.update(kw)
    return d


class RejectTest(TempDirTest):
    def assert_reject(self, substr, manifest=None, clips=None, coverage=None,
                      cues=None, root="R", raw=None):
        pkg = self.pkg("bad", manifest or _man(), clips, coverage, cues)
        if raw is not None:
            with open(os.path.join(pkg, raw[0]), "a",
                      encoding="utf-8") as fh:
                fh.write(raw[1])
        target = os.path.join(self.tmp, "lib.sqlite")
        before = self.dir_listing()
        with self.assertRaises(PackageError) as cm:
            builder.build(pkg, root, target)
        self.assertIn(substr, str(cm.exception))
        # failed build leaves no library and no temporary half-product
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self.dir_listing(), before)
        return str(cm.exception)

    def test_missing_reference(self):
        self.assert_reject("not declared",
                           clips=[_clip(ref="ZZ", end=5)])

    def test_missing_sequence_reference(self):
        self.assert_reject("not declared",
                           clips=[_clip(kind="seq", ref="ZZ", end=5)])

    def test_reference_cycle(self):
        man = _man(seqs={"S": 10, "T": 10, "R": 10})
        clips = [
            _clip(id="r-s", kind="seq", ref="S"),
            _clip(id="s-t", seq="S", kind="seq", ref="T"),
            _clip(id="t-s", seq="T", kind="seq", ref="S"),
        ]
        msg = self.assert_reject("cycle", manifest=man, clips=clips)
        self.assertIn("S", msg)

    def test_self_cycle(self):
        man = _man(seqs={"R": 10})
        self.assert_reject("cycle",
                           manifest=man,
                           clips=[_clip(kind="seq", ref="R")])

    def test_same_track_overlap(self):
        clips = [_clip(id="a", start=0, end=6),
                 _clip(id="b", start=5, end=9)]
        self.assert_reject("overlap", clips=clips)

    def test_adjacent_clips_allowed(self):
        clips = [_clip(id="a", start=0, end=5),
                 _clip(id="b", start=5, end=10)]
        pkg = self.pkg("ok", _man(), clips, [_cov()], [_cue()])
        out, _, _ = self.build(pkg, "R", "ok.sqlite")
        self.assertTrue(os.path.exists(out))

    def test_one_frame_out_of_bounds_forward(self):
        # M has 10 frames; clip of length 11 at p=1 samples frame 10.
        self.assert_reject("sampling leaves",
                           clips=[_clip(start=0, end=10, base=1)])

    def test_one_frame_out_of_bounds_reverse(self):
        # base=0, p=-1, length 2 samples frame -1.
        self.assert_reject("sampling leaves",
                           clips=[_clip(start=0, end=2, base=0, p=-1)])

    def test_boundary_exact_fit_allowed(self):
        clips = [_clip(start=0, end=10, base=9, p=-1)]  # samples 9..0
        pkg = self.pkg("ok", _man(), clips, [_cov()], [_cue()])
        out, _, _ = self.build(pkg, "R", "ok.sqlite")
        self.assertTrue(os.path.exists(out))

    def test_duplicate_cue_id(self):
        self.assert_reject("duplicate cue id",
                           coverage=[_cov()],
                           cues=[_cue(), _cue(id="A", start=3, end=4)])

    def test_duplicate_clip_id(self):
        self.assert_reject("duplicate clip id",
                           clips=[_clip(start=0, end=4),
                                  _clip(start=5, end=9)])

    def test_cue_overlap(self):
        self.assert_reject("overlap",
                           coverage=[_cov()],
                           cues=[_cue(start=0, end=4),
                                 _cue(id="B", start=3, end=6)])

    def test_cue_outside_coverage(self):
        self.assert_reject("not fully inside checked coverage",
                           coverage=[_cov(start=0, end=3)],
                           cues=[_cue(start=2, end=5)])

    def test_cue_without_any_coverage(self):
        self.assert_reject("not fully inside checked coverage",
                           coverage=None, cues=[_cue()])

    def test_undeclared_language(self):
        self.assert_reject("not declared",
                           coverage=[_cov()], cues=[_cue(lang="eng")])

    def test_empty_interval(self):
        self.assert_reject("empty or negative interval",
                           clips=[_clip(start=3, end=3)])

    def test_unknown_field(self):
        bad = _clip()
        bad["speed"] = 2
        self.assert_reject("unexpected fields", clips=[bad])

    def test_bad_json_line_reports_position(self):
        pkg = self.pkg("badjson", _man(), [_clip()], [_cov()], [_cue()])
        with open(os.path.join(pkg, "cues.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write('{"id": "B", oops}\n')
        with self.assertRaises(PackageError) as cm:
            builder.build(pkg, "R", os.path.join(self.tmp, "x.sqlite"))
        self.assertIn("cues.jsonl:2", str(cm.exception))

    def test_error_reports_file_and_line(self):
        pkg = self.pkg("pos", _man(),
                       [_clip(id="a", start=0, end=4),
                        _clip(id="b", start=3, end=8)])
        with self.assertRaises(PackageError) as cm:
            builder.build(pkg, "R", os.path.join(self.tmp, "x.sqlite"))
        self.assertIn("clips.jsonl:2", str(cm.exception))


class PublishSafetyTest(TempDirTest):
    def _good_pkg(self):
        return self.pkg("good", _man(), [_clip()], [_cov()], [_cue()])

    def test_existing_target_never_overwritten(self):
        pkg = self._good_pkg()
        target = os.path.join(self.tmp, "lib.sqlite")
        with open(target, "wb") as fh:
            fh.write(b"KEEP ME")
        with self.assertRaises(TargetExistsError):
            builder.build(pkg, "R", target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"KEEP ME")
        # and no temp files linger
        self.assertEqual([f for f in self.dir_listing()
                          if f != "good" and f != "lib.sqlite"], [])

    def test_rebuild_after_success_is_refused(self):
        pkg = self._good_pkg()
        target = os.path.join(self.tmp, "lib.sqlite")
        builder.build(pkg, "R", target)
        with self.assertRaises(TargetExistsError):
            builder.build(pkg, "R", target)

    def test_failed_publish_leaves_no_half_product(self):
        # os.link fails at publish time (e.g. disk full): the temporary
        # library and staging files must be removed, nothing openable left.
        from unittest import mock
        pkg = self._good_pkg()
        target = os.path.join(self.tmp, "lib.sqlite")
        before = self.dir_listing()
        with mock.patch("subwarehouse.builder.os.link",
                        side_effect=OSError("no space left on device")):
            with self.assertRaises(OSError):
                builder.build(pkg, "R", target)
        self.assertEqual(self.dir_listing(), before)
        self.assertFalse(os.path.exists(target))

    def test_target_appearing_during_build_is_not_replaced(self):
        # Target springs into existence between the initial check and the
        # atomic publish: os.link refuses to replace it.
        pkg = self._good_pkg()
        target = os.path.join(self.tmp, "lib.sqlite")
        real_link = os.link

        def racing_link(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"RACER")
            return real_link(src, dst)

        from unittest import mock
        with mock.patch("subwarehouse.builder.os.link", racing_link):
            with self.assertRaises(TargetExistsError):
                builder.build(pkg, "R", target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"RACER")

    def test_invalid_input_leaves_directory_unchanged(self):
        pkg = self.pkg("bad", _man(), clips=[_clip(ref="ZZ")])
        before = self.dir_listing()
        with self.assertRaises(PackageError):
            builder.build(pkg, "R", os.path.join(self.tmp, "lib.sqlite"))
        self.assertEqual(self.dir_listing(), before)


if __name__ == "__main__":
    unittest.main()
