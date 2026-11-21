"""Corpus export: authorization, grouping, cross-batch registry and
input-validation tests on small hand-built samples."""

import json
import os
import sqlite3
import unittest

from common import TempDirTest, write_json

from subwarehouse import builder, corpus
from subwarehouse.errors import CorpusError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


class CorpusTest(TempDirTest):
    def make_lib(self, name, manifest, clips, coverage, cues):
        manifest = dict(manifest)
        root = manifest.pop("root")
        pkg = self.pkg(name + "-pkg", manifest, clips, coverage, cues)
        out = os.path.join(self.tmp, name + ".sqlite")
        _stats, meta = builder.build(pkg, root, out)
        return out, meta

    def libs_file(self, entries, name="libs.json"):
        """entries: (alias, lib_path, logical_version)"""
        return write_json(os.path.join(self.tmp, name), {
            "libraries": [{"alias": a, "path": p, "logical_version": v}
                          for a, p, v in entries]})

    def inputs(self, samples, mapping, grants, names=("s", "m", "g")):
        s = write_jsonl(os.path.join(self.tmp, "%s.jsonl" % names[0]),
                        samples)
        m = write_jsonl(os.path.join(self.tmp, "%s.jsonl" % names[1]),
                        mapping)
        g = write_jsonl(os.path.join(self.tmp, "%s.jsonl" % names[2]),
                        grants)
        return s, m, g

    def export(self, libs, samples, mapping, grants, registry=None,
               out="out", **kw):
        summary, stats = corpus.export_corpus(
            libs, samples, mapping, grants, registry,
            os.path.join(self.tmp, out), **kw)
        return summary, stats, os.path.join(self.tmp, out)

    def make_registry(self, name, members, keys):
        """A previous-batch registry, written directly in the documented
        format.  members: (sid, side, comp); keys: (comp, kind, klang,
        kbody)."""
        path = os.path.join(self.tmp, name)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(corpus.REGISTRY_SCHEMA)
            conn.executemany(
                "INSERT INTO meta VALUES(?,?)",
                [("format", corpus.FORMAT_REGISTRY),
                 ("generator", "test"), ("seed", corpus.SEED.decode()),
                 ("created_utc", "2026-11-20T00:00:00+00:00")])
            conn.executemany("INSERT INTO members VALUES(?,?,?)", members)
            conn.executemany("INSERT INTO keys VALUES(?,?,?,?)", keys)
            conn.commit()
        finally:
            conn.close()
        return path

    def registry_members(self, path):
        conn = sqlite3.connect(path)
        try:
            m = conn.execute("SELECT sid, side, comp FROM members "
                             "ORDER BY sid").fetchall()
            k = conn.execute("SELECT comp, kind, klang, kbody FROM keys "
                             "ORDER BY comp, kind, klang, kbody").fetchall()
            return m, k
        finally:
            conn.close()


def rev_package(test, name="rev"):
    """Four film frames sampling source frames 8, 6, 4, 2 of asset T;
    cue A covers source [2,9)."""
    man = {"package": name, "languages": ["zho"],
           "assets": {"T": 12}, "sequences": {"R": 4}, "root": "R"}
    clips = [{"id": "r-t", "seq": "R", "track": 0, "start": 0, "end": 4,
              "kind": "asset", "ref": "T", "base": 8, "p": -2, "q": 1}]
    coverage = [{"asset": "T", "lang": "zho", "start": 0, "end": 12}]
    cues = [{"id": "A", "asset": "T", "lang": "zho", "start": 2, "end": 9,
             "text": "倒放字幕"}]
    return man, clips, coverage, cues


# ---------------------------------------------------------------------------
# The reversed-window authorization demo
# ---------------------------------------------------------------------------

class ReversedWindowTest(CorpusTest):
    def setUp(self):
        super().setUp()
        man, clips, coverage, cues = rev_package(self)
        self.lib, self.meta = self.make_lib("rev", man, clips, coverage,
                                            cues)
        self.libs = self.libs_file([("main", self.lib,
                                     self.meta["logical_version"])])
        self.samples, self.mapping, _g = self.inputs(
            [{"id": "s1", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "T", "key": "K"}],
            [])

    def grants(self, starts, name="grants.jsonl"):
        return write_jsonl(os.path.join(self.tmp, name),
                           [{"key": "K", "lang": "zho", "start": s,
                             "end": s + 1} for s in starts])

    def test_grants_covering_exactly_the_four_sampled_frames_pass(self):
        g = self.grants([2, 4, 6, 8])
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, g)
        self.assertEqual(summary["counts"],
                         {"train": 0, "validation": 1, "rejected": 0,
                          "quarantine": 0})
        rows = read_jsonl(os.path.join(out, "validation.jsonl"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "s1")
        self.assertEqual(row["logical_version"],
                         self.meta["logical_version"])
        self.assertEqual(len(row["cues"]), 1)
        cue = row["cues"][0]
        self.assertEqual((cue["start"], cue["end"], cue["text"]),
                         (0, 4, "倒放字幕"))
        self.assertEqual(cue["cue"], "A")
        self.assertEqual(cue["source_key"], "K")
        # The cue spans four one-frame grants: the proof splits into four
        # pieces without splitting the text.
        self.assertEqual(
            [(p["window"], p["source_frames"], p["grant"]) for p in
             cue["proof"]],
            [([0, 1], [8, 9], [8, 9]),
             ([1, 2], [6, 7], [6, 7]),
             ([2, 3], [4, 5], [4, 5]),
             ([3, 4], [2, 3], [2, 3])])
        # Skipped source frames 3, 5, 7 are not granted and not sampled:
        # no violation.
        self.assertEqual(read_jsonl(os.path.join(out, "rejected.jsonl")),
                         [])

    def test_removing_frame_4_locates_the_third_window_frame(self):
        g = self.grants([2, 6, 8])
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, g)
        self.assertEqual(summary["counts"]["rejected"], 1)
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(len(rows), 1)
        reason = rows[0]["reason"]
        self.assertEqual(reason["reason"], "unauthorized")
        self.assertEqual(reason["window_frame"], 2)   # third frame
        self.assertEqual(reason["frame"], 2)
        self.assertEqual(reason["source_frame"], 4)
        self.assertEqual(reason["key"], "K")
        # No text anywhere in the rejection output.
        with open(os.path.join(out, "rejected.jsonl"),
                  encoding="utf-8") as fh:
            self.assertNotIn("倒放字幕", fh.read())
        for name in ("train.jsonl", "validation.jsonl"):
            self.assertEqual(read_jsonl(os.path.join(out, name)), [])
        # The rejected sample is not in the new registry.
        members, keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        self.assertEqual(members, [])

    def test_no_grant_rows_at_all_is_unauthorized(self):
        g = self.grants([])
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, g)
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(rows[0]["reason"]["reason"], "unauthorized")
        self.assertEqual(rows[0]["reason"]["source_frame"], 8)


# ---------------------------------------------------------------------------
# Cross-batch grouping: old-tr / old-val quarantine
# ---------------------------------------------------------------------------

def cross_batch_lib(test):
    """One library, two assets on disjoint film intervals: A1 carries
    eng 'shared morning' on [0,4), A2 carries eng 'shared morning' plus
    zho '共享文本' on [4,8)."""
    man = {"package": "cur", "languages": ["zho", "eng"],
           "assets": {"A1": 10, "A2": 10}, "sequences": {"R": 8},
           "root": "R"}
    clips = [
        {"id": "r-a1", "seq": "R", "track": 0, "start": 0, "end": 4,
         "kind": "asset", "ref": "A1", "base": 0, "p": 1, "q": 1},
        {"id": "r-a2", "seq": "R", "track": 0, "start": 4, "end": 8,
         "kind": "asset", "ref": "A2", "base": 0, "p": 1, "q": 1},
    ]
    coverage = [
        {"asset": "A1", "lang": "eng", "start": 0, "end": 10},
        {"asset": "A2", "lang": "eng", "start": 0, "end": 10},
        {"asset": "A2", "lang": "zho", "start": 0, "end": 10},
    ]
    cues = [
        {"id": "x1", "asset": "A1", "lang": "eng", "start": 0, "end": 4,
         "text": "shared morning"},
        {"id": "y1", "asset": "A2", "lang": "eng", "start": 0, "end": 4,
         "text": "shared morning"},
        {"id": "y2", "asset": "A2", "lang": "zho", "start": 0, "end": 4,
         "text": "共享文本"},
    ]
    return test.make_lib("cur", man, clips, coverage, cues)


class CrossBatchTest(CorpusTest):
    """The demo from the handover notes: X reaches old-val through a
    source-material key, Y reaches X through one shared text and old-tr
    through another; X and Y are quarantined together and the evidence
    explains the whole chain."""

    def setUp(self):
        super().setUp()
        self.lib, self.meta = cross_batch_lib(self)
        self.libs = self.libs_file([("cur", self.lib,
                                     self.meta["logical_version"])])
        # Previous batch: old-tr (train) with a zho text key, old-val
        # (validation) with the source key K1.  Their libraries are
        # gone; the registry alone carries them.
        self.registry = self.make_registry(
            "prev-registry.sqlite",
            [("old-tr", "train", "old-tr"),
             ("old-val", "validation", "old-val")],
            [("old-tr", "txt", "zho", "共享文本"),
             ("old-val", "src", "", "K1")])
        self.samples, self.mapping, _g = self.inputs(
            [{"id": "X", "library": "cur", "start": 0, "end": 4,
              "languages": ["eng"]},
             {"id": "Y", "library": "cur", "start": 4, "end": 8,
              "languages": ["eng", "zho"]}],
            [{"library": "cur", "asset": "A1", "key": "K1"},
             {"library": "cur", "asset": "A2", "key": "K2"}],
            [])

    def grants(self, rows, name="grants.jsonl"):
        return write_jsonl(os.path.join(self.tmp, name), rows)

    def full_grants(self):
        return self.grants(
            [{"key": "K1", "lang": "eng", "start": 0, "end": 10},
             {"key": "K2", "lang": "eng", "start": 0, "end": 10},
             {"key": "K2", "lang": "zho", "start": 0, "end": 10}])

    def test_bridge_between_train_and_validation_quarantines(self):
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, self.full_grants(),
                                          registry=self.registry)
        self.assertEqual(summary["counts"],
                         {"train": 0, "validation": 0, "rejected": 0,
                          "quarantine": 2})
        q = read_jsonl(os.path.join(out, "quarantine.jsonl"))
        self.assertEqual(q, [{"id": "X", "group": "X"},
                             {"id": "Y", "group": "X"}])
        ev = read_jsonl(os.path.join(out, "evidence.jsonl"))
        self.assertEqual(len(ev), 1)
        ev = ev[0]
        self.assertEqual(ev["group"], "X")
        self.assertEqual(ev["members"], ["X", "Y"])
        self.assertEqual(ev["history"], {"train": ["old-tr"],
                                         "validation": ["old-val"]})
        # The spanning chain old-val -- X -- Y -- old-tr, each edge
        # labelled with the key that linked it.
        self.assertEqual(
            [(e["a"], e["b"], e["key"]) for e in ev["edges"]],
            [("X", "Y", {"kind": "txt", "lang": "eng",
                         "text": "shared morning"}),
             ("X", "old-val", {"kind": "src", "key": "K1"}),
             ("Y", "old-tr", {"kind": "txt", "lang": "zho",
                              "text": "共享文本"})])
        # Nothing delivered; history inherited unchanged, X and Y absent.
        self.assertEqual(read_jsonl(os.path.join(out, "train.jsonl")), [])
        self.assertEqual(read_jsonl(os.path.join(out, "validation.jsonl")),
                         [])
        members, keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        self.assertEqual(members, [("old-tr", "train", "old-tr"),
                                   ("old-val", "validation", "old-val")])
        self.assertEqual(keys, [("old-tr", "txt", "zho", "共享文本"),
                                ("old-val", "src", "", "K1")])

    def test_rejected_Y_lets_X_into_validation(self):
        # Y's zho cue samples source frames 0..3; grant only [0,2).
        g = self.grants(
            [{"key": "K1", "lang": "eng", "start": 0, "end": 10},
             {"key": "K2", "lang": "eng", "start": 0, "end": 10},
             {"key": "K2", "lang": "zho", "start": 0, "end": 2}])
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, g,
                                          registry=self.registry)
        self.assertEqual(summary["counts"],
                         {"train": 0, "validation": 1, "rejected": 1,
                          "quarantine": 0})
        rej = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual([r["id"] for r in rej], ["Y"])
        self.assertEqual(rej[0]["reason"]["reason"], "unauthorized")
        self.assertEqual(rej[0]["reason"]["source_frame"], 2)
        val = read_jsonl(os.path.join(out, "validation.jsonl"))
        self.assertEqual([r["id"] for r in val], ["X"])
        members, keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        self.assertEqual(members, [("X", "validation", "old-val"),
                                   ("old-tr", "train", "old-tr"),
                                   ("old-val", "validation", "old-val")])
        # X's keys joined old-val's component.
        self.assertIn(("old-val", "src", "", "K1"), keys)
        self.assertIn(("old-val", "txt", "eng", "shared morning"), keys)

    def test_one_sided_history_fixes_the_side(self):
        # Only old-val in history: X joins validation; Y follows X.
        reg = self.make_registry(
            "one-sided.sqlite",
            [("old-val", "validation", "old-val")],
            [("old-val", "src", "", "K1")])
        summary, stats, out = self.export(self.libs, self.samples,
                                          self.mapping, self.full_grants(),
                                          registry=reg)
        self.assertEqual(summary["counts"]["validation"], 2)
        val = read_jsonl(os.path.join(out, "validation.jsonl"))
        self.assertEqual([r["id"] for r in val], ["X", "Y"])
        members, _keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        self.assertEqual(members, [("X", "validation", "old-val"),
                                   ("Y", "validation", "old-val"),
                                   ("old-val", "validation", "old-val")])


# ---------------------------------------------------------------------------
# Multi-language samples and local-id / archive-key separation
# ---------------------------------------------------------------------------

class MultiLanguageTest(CorpusTest):
    def make_two_libs(self):
        """L1: asset M with zho '你好 世界' + eng 'hello'.  L2: the same
        local asset id M with zho '完全不同', plus a second asset N with
        eng 'hello'.  The mapping gives each its own archive key."""
        man1 = {"package": "l1", "languages": ["zho", "eng"],
                "assets": {"M": 10}, "sequences": {"R": 4}, "root": "R"}
        clips1 = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                   "end": 4, "kind": "asset", "ref": "M", "base": 0,
                   "p": 1, "q": 1}]
        cov1 = [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
                {"asset": "M", "lang": "eng", "start": 0, "end": 10}]
        cues1 = [{"id": "c-zho", "asset": "M", "lang": "zho", "start": 0,
                  "end": 4, "text": "你好 世界"},
                 {"id": "c-eng", "asset": "M", "lang": "eng", "start": 0,
                  "end": 4, "text": "hello"}]
        l1 = self.make_lib("l1", man1, clips1, cov1, cues1)

        man2 = {"package": "l2", "languages": ["zho", "eng"],
                "assets": {"M": 10, "N": 10}, "sequences": {"R": 8},
                "root": "R"}
        clips2 = [{"id": "r-m", "seq": "R", "track": 0, "start": 0,
                   "end": 4, "kind": "asset", "ref": "M", "base": 0,
                   "p": 1, "q": 1},
                  {"id": "r-n", "seq": "R", "track": 0, "start": 4,
                   "end": 8, "kind": "asset", "ref": "N", "base": 0,
                   "p": 1, "q": 1}]
        cov2 = [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
                {"asset": "N", "lang": "eng", "start": 0, "end": 10}]
        cues2 = [{"id": "c-zho", "asset": "M", "lang": "zho", "start": 0,
                  "end": 4, "text": "完全不同"},
                 {"id": "c-eng", "asset": "N", "lang": "eng", "start": 0,
                  "end": 4, "text": "hello"}]
        l2 = self.make_lib("l2", man2, clips2, cov2, cues2)
        return l1, l2

    def test_multilanguage_grouping_and_key_separation(self):
        (l1, m1), (l2, m2) = self.make_two_libs()
        libs = self.libs_file([("L1", l1, m1["logical_version"]),
                               ("L2", l2, m2["logical_version"])])
        # Same local asset id M in both libraries, different archive
        # keys: identity comes only from this table.
        samples, mapping, _g = self.inputs(
            [{"id": "s1", "library": "L1", "start": 0, "end": 4,
              "languages": ["zho"]},
             {"id": "s2", "library": "L2", "start": 0, "end": 4,
              "languages": ["zho"]},
             {"id": "s3", "library": "L1", "start": 0, "end": 4,
              "languages": ["zho", "eng"]},
             {"id": "s4", "library": "L2", "start": 4, "end": 8,
              "languages": ["eng"]}],
            [{"library": "L1", "asset": "M", "key": "KA"},
             {"library": "L2", "asset": "M", "key": "KB"},
             {"library": "L2", "asset": "N", "key": "KC"}],
            [])
        grants = write_jsonl(os.path.join(self.tmp, "grants.jsonl"), [
            {"key": "KA", "lang": "zho", "start": 0, "end": 10},
            {"key": "KA", "lang": "eng", "start": 0, "end": 10},
            {"key": "KB", "lang": "zho", "start": 0, "end": 10},
            {"key": "KC", "lang": "eng", "start": 0, "end": 10}])
        summary, stats, out = self.export(libs, samples, mapping, grants)
        self.assertEqual(summary["counts"]["rejected"], 0)
        members, keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        comp = {sid: c for sid, _side, c in members}
        # s1 (zho 你好世界, KA) and s3 (both languages, KA) share the zho
        # text and the source key; s4 shares s3's eng text across
        # libraries -> one component.
        self.assertEqual(comp["s1"], comp["s3"])
        self.assertEqual(comp["s3"], comp["s4"])
        # s2 shares neither key nor text: the same local asset id M does
        # not connect it to s1.
        self.assertNotEqual(comp["s1"], comp["s2"])
        self.assertNotEqual(comp["s2"], comp["s4"])
        # Multi-language sample: its keys come from every delivered
        # language, not just the first cue.
        allrows = read_jsonl(os.path.join(out, "train.jsonl")) + \
            read_jsonl(os.path.join(out, "validation.jsonl"))
        s3 = [r for r in allrows if r["id"] == "s3"][0]
        self.assertEqual(
            sorted((k for k in s3["keys"] if k["kind"] == "txt"),
                   key=lambda k: k["lang"]),
            [{"kind": "txt", "lang": "eng", "text": "hello"},
             {"kind": "txt", "lang": "zho", "text": "你好 世界"}])
        self.assertEqual([k for k in s3["keys"] if k["kind"] == "src"],
                         [{"kind": "src", "key": "KA"}])


# ---------------------------------------------------------------------------
# Rejection reasons
# ---------------------------------------------------------------------------

class RejectionTest(CorpusTest):
    def setUp(self):
        super().setUp()
        # Film: [0,4) cue 甲 on A; [4,6) explicit blank; [6,8) unknown
        # (A's coverage ends at 4); [8,12) silence (B checked, no cues).
        man = {"package": "rej", "languages": ["zho"],
               "assets": {"A": 10, "B": 10}, "sequences": {"R": 12},
               "root": "R"}
        clips = [
            {"id": "r-a", "seq": "R", "track": 0, "start": 0, "end": 8,
             "kind": "asset", "ref": "A", "base": 0, "p": 1, "q": 1},
            {"id": "r-blank", "seq": "R", "track": 1, "start": 4,
             "end": 6, "kind": "blank"},
            {"id": "r-b", "seq": "R", "track": 0, "start": 8, "end": 12,
             "kind": "asset", "ref": "B", "base": 0, "p": 1, "q": 1},
        ]
        coverage = [{"asset": "A", "lang": "zho", "start": 0, "end": 4},
                    {"asset": "B", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "ca", "asset": "A", "lang": "zho", "start": 0,
                 "end": 4, "text": "甲"}]
        self.lib, self.meta = self.make_lib("rej", man, clips, coverage,
                                            cues)
        self.libs = self.libs_file([("main", self.lib,
                                     self.meta["logical_version"])])

    def run_batch(self, samples, mapping, grants):
        s, m, _ = self.inputs(samples, mapping, [])
        g = write_jsonl(os.path.join(self.tmp, "g.jsonl"), grants)
        return self.export(self.libs, s, m, g)

    def test_unknown_frames_reject_with_language_and_interval(self):
        # Film [5,8): blank on [5,6), then A's coverage is over ->
        # unknown on [6,8).
        summary, stats, out = self.run_batch(
            [{"id": "u1", "library": "main", "start": 5, "end": 8,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "A", "key": "K"}],
            [{"key": "K", "lang": "zho", "start": 0, "end": 10}])
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(rows[0]["reason"],
                         {"reason": "unknown", "lang": "zho",
                          "interval": [6, 8], "window_interval": [1, 3]})

    def test_no_cues_rejects(self):
        # Film [8,12): silence only (B checked, no cues).
        summary, stats, out = self.run_batch(
            [{"id": "n1", "library": "main", "start": 8, "end": 12,
              "languages": ["zho"]}], [], [])
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(rows[0]["reason"], {"reason": "no_cues"})

    def test_unmapped_asset_rejects(self):
        summary, stats, out = self.run_batch(
            [{"id": "m1", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [],     # no mapping for A
            [])
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(rows[0]["reason"],
                         {"reason": "unmapped_asset", "lang": "zho",
                          "asset": "A", "interval": [0, 4],
                          "window_interval": [0, 4]})

    def test_blank_and_silence_need_no_grant(self):
        # Window [0,6): granted cue on [0,4) plus an explicit blank on
        # [4,6); the blank frames need no grant at all.
        summary, stats, out = self.run_batch(
            [{"id": "ok1", "library": "main", "start": 0, "end": 6,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "A", "key": "K"}],
            [{"key": "K", "lang": "zho", "start": 0, "end": 4}])
        self.assertEqual(summary["counts"]["rejected"], 0)
        self.assertEqual(summary["counts"]["train"] +
                         summary["counts"]["validation"], 1)
        rows = read_jsonl(os.path.join(out, "train.jsonl")) + \
            read_jsonl(os.path.join(out, "validation.jsonl"))
        self.assertEqual(len(rows[0]["cues"]), 1)
        self.assertEqual((rows[0]["cues"][0]["start"],
                          rows[0]["cues"][0]["end"]), (0, 4))


# ---------------------------------------------------------------------------
# Text normalisation for grouping
# ---------------------------------------------------------------------------

class NormalizationTest(CorpusTest):
    def lib_with_texts(self, texts):
        """One asset per text, each on its own disjoint film interval
        [4i, 4i+4), so sample i sees exactly text i."""
        assets = {}
        clips = []
        coverage = []
        cues = []
        for i, (lang, txt) in enumerate(texts):
            a = "A%d" % i
            assets[a] = 4
            clips.append({"id": "r-%s" % a, "seq": "R", "track": 0,
                          "start": 4 * i, "end": 4 * i + 4,
                          "kind": "asset", "ref": a, "base": 0, "p": 1,
                          "q": 1})
            coverage.append({"asset": a, "lang": "zho", "start": 0,
                             "end": 4})
            coverage.append({"asset": a, "lang": "eng", "start": 0,
                             "end": 4})
            cues.append({"id": "c%d" % i, "asset": a, "lang": lang,
                         "start": 0, "end": 4, "text": txt})
        man = {"package": "norm", "languages": ["zho", "eng"],
               "assets": assets, "sequences": {"R": 4 * len(texts)},
               "root": "R"}
        return self.make_lib("norm", man, clips, coverage, cues)

    def run_batch(self, lib, meta, texts, langs):
        libs = self.libs_file([("main", lib, meta["logical_version"])])
        samples = []
        mapping = []
        grants = []
        for i, lang in enumerate(langs):
            samples.append({"id": "s%d" % i, "library": "main",
                            "start": 4 * i, "end": 4 * i + 4,
                            "languages": [lang]})
            mapping.append({"library": "main", "asset": "A%d" % i,
                            "key": "K%d" % i})
            grants.append({"key": "K%d" % i, "lang": lang, "start": 0,
                           "end": 4})
        s, m, _ = self.inputs(samples, mapping, [])
        g = write_jsonl(os.path.join(self.tmp, "g.jsonl"), grants)
        return self.export(libs, s, m, g)

    def comps(self, out):
        members, _keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        return {sid: c for sid, _side, c in members}

    def test_whitespace_and_nfc(self):
        # "你好 世界" vs "你好  世界\n" (collapsed); "café" NFC vs NFD.
        texts = [("zho", "你好 世界"), ("zho", "  你好   世界\n"),
                 ("zho", "café"), ("zho", "café")]
        lib, meta = self.lib_with_texts(texts)
        _summary, _stats, out = self.run_batch(
            lib, meta, texts, ["zho"] * 4)
        comp = self.comps(out)
        self.assertEqual(comp["s0"], comp["s1"])
        self.assertEqual(comp["s2"], comp["s3"])
        self.assertNotEqual(comp["s0"], comp["s2"])

    def test_same_text_in_different_languages_is_not_a_duplicate(self):
        texts = [("zho", "same"), ("eng", "same")]
        lib, meta = self.lib_with_texts(texts)
        _summary, _stats, out = self.run_batch(
            lib, meta, texts, ["zho", "eng"])
        comp = self.comps(out)
        self.assertNotEqual(comp["s0"], comp["s1"])

    def test_case_and_punctuation_stay_significant(self):
        texts = [("zho", "Hello, world"), ("zho", "hello world")]
        lib, meta = self.lib_with_texts(texts)
        _summary, _stats, out = self.run_batch(
            lib, meta, texts, ["zho", "zho"])
        comp = self.comps(out)
        self.assertNotEqual(comp["s0"], comp["s1"])

    def test_seed_rule_for_components_without_history(self):
        texts = [("zho", "甲"), ("zho", "乙")]
        lib, meta = self.lib_with_texts(texts)
        _summary, _stats, out = self.run_batch(
            lib, meta, texts, ["zho", "zho"])
        members, _keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        sides = {sid: side for sid, side, _c in members}
        self.assertEqual(sides["s0"], corpus.side_for("s0"))
        self.assertEqual(sides["s1"], corpus.side_for("s1"))


# ---------------------------------------------------------------------------
# Input validation: any error refuses the whole batch
# ---------------------------------------------------------------------------

class InputErrorTest(CorpusTest):
    def setUp(self):
        super().setUp()
        man, clips, coverage, cues = rev_package(self, "ie")
        self.lib, self.meta = self.make_lib("ie", man, clips, coverage,
                                            cues)
        self.libs = self.libs_file([("main", self.lib,
                                     self.meta["logical_version"])])
        self.good_samples = [{"id": "s1", "library": "main", "start": 0,
                              "end": 4, "languages": ["zho"]}]
        self.good_mapping = [{"library": "main", "asset": "T",
                              "key": "K"}]
        self.good_grants = [{"key": "K", "lang": "zho", "start": 0,
                             "end": 12}]

    def expect_refused(self, samples=None, mapping=None, grants=None,
                       libs=None, registry=None, match=""):
        s, m, _g = self.inputs(
            self.good_samples if samples is None else samples,
            self.good_mapping if mapping is None else mapping,
            [], names=("s-err", "m-err", "g-err"))
        g = write_jsonl(os.path.join(self.tmp, "g-err.jsonl"),
                        self.good_grants if grants is None else grants)
        out = os.path.join(self.tmp, "out-err")
        with self.assertRaises(CorpusError) as cm:
            corpus.export_corpus(libs or self.libs, s, m, g, registry,
                                 out)
        self.assertIn(match, str(cm.exception))
        self.assertFalse(os.path.lexists(out))

    def test_duplicate_sample_id(self):
        self.expect_refused(samples=[self.good_samples[0],
                                     self.good_samples[0]],
                            match="duplicate sample id")

    def test_sample_id_used_by_history(self):
        reg = self.make_registry("prev.sqlite",
                                 [("s1", "train", "s1")], [])
        self.expect_refused(registry=reg, match="already delivered")

    def test_conflicting_mapping(self):
        self.expect_refused(
            mapping=[{"library": "main", "asset": "T", "key": "K1"},
                     {"library": "main", "asset": "T", "key": "K2"}],
            match="conflicting mappings")

    def test_duplicate_mapping(self):
        self.expect_refused(mapping=self.good_mapping * 2,
                            match="duplicate mapping")

    def test_mapping_unknown_alias(self):
        self.expect_refused(
            mapping=[{"library": "nope", "asset": "T", "key": "K"}],
            match="not declared")

    def test_illegal_grant_interval(self):
        self.expect_refused(
            grants=[{"key": "K", "lang": "zho", "start": 5, "end": 5}],
            match="empty or negative interval")

    def test_illegal_sample_window(self):
        self.expect_refused(
            samples=[{"id": "s1", "library": "main", "start": 3,
                      "end": 3, "languages": ["zho"]}],
            match="empty or negative interval")

    def test_window_outside_film(self):
        self.expect_refused(
            samples=[{"id": "s1", "library": "main", "start": 0,
                      "end": 5, "languages": ["zho"]}],
            match="exceeds film length 4")

    def test_undeclared_language(self):
        self.expect_refused(
            samples=[{"id": "s1", "library": "main", "start": 0,
                      "end": 4, "languages": ["eng"]}],
            match="not declared in library")

    def test_undeclared_library_alias(self):
        self.expect_refused(
            samples=[{"id": "s1", "library": "ghost", "start": 0,
                      "end": 4, "languages": ["zho"]}],
            match="not declared in the libraries file")

    def test_version_mismatch(self):
        libs = write_json(os.path.join(self.tmp, "libs-bad.json"), {
            "libraries": [{"alias": "main", "path": self.lib,
                           "logical_version": "0" * 64}]})
        self.expect_refused(libs=libs, match="logical_version mismatch")

    def test_duplicate_alias(self):
        libs = write_json(os.path.join(self.tmp, "libs-dup.json"), {
            "libraries": [
                {"alias": "main", "path": self.lib,
                 "logical_version": self.meta["logical_version"]},
                {"alias": "main", "path": self.lib,
                 "logical_version": self.meta["logical_version"]}]})
        self.expect_refused(libs=libs, match="duplicate alias")

    def test_empty_languages(self):
        self.expect_refused(
            samples=[{"id": "s1", "library": "main", "start": 0,
                      "end": 4, "languages": []}],
            match="non-empty array")

    def test_two_sided_registry_is_an_input_error(self):
        reg = self.make_registry(
            "broken.sqlite",
            [("a", "train", "c1"), ("b", "validation", "c1")], [])
        self.expect_refused(registry=reg, match="mixes sides")

    def test_registry_comp_must_be_a_member(self):
        reg = self.make_registry(
            "broken2.sqlite",
            [("a", "train", "ghost-comp")], [])
        self.expect_refused(registry=reg,
                            match="not itself a delivered sample id")


# ---------------------------------------------------------------------------
# Determinism under input reordering
# ---------------------------------------------------------------------------

class DeterminismTest(CorpusTest):
    def test_shuffled_inputs_give_identical_outputs(self):
        man = {"package": "det", "languages": ["zho"],
               "assets": {"A": 20, "B": 20}, "sequences": {"R": 8},
               "root": "R"}
        clips = [
            {"id": "r-a", "seq": "R", "track": 0, "start": 0, "end": 4,
             "kind": "asset", "ref": "A", "base": 0, "p": 1, "q": 1},
            {"id": "r-b", "seq": "R", "track": 0, "start": 4, "end": 8,
             "kind": "asset", "ref": "B", "base": 0, "p": 1, "q": 1},
        ]
        coverage = [{"asset": "A", "lang": "zho", "start": 0, "end": 20},
                    {"asset": "B", "lang": "zho", "start": 0, "end": 20}]
        cues = [{"id": "ca", "asset": "A", "lang": "zho", "start": 0,
                 "end": 4, "text": "相同"},
                {"id": "cb", "asset": "B", "lang": "zho", "start": 0,
                 "end": 4, "text": "相同"}]
        lib, meta = self.make_lib("det", man, clips, coverage, cues)
        libs = self.libs_file([("main", lib, meta["logical_version"])])
        samples = [{"id": "s1", "library": "main", "start": 0, "end": 4,
                    "languages": ["zho"]},
                   {"id": "s2", "library": "main", "start": 4, "end": 8,
                    "languages": ["zho"]},
                   {"id": "s3", "library": "main", "start": 0, "end": 8,
                    "languages": ["zho"]}]
        mapping = [{"library": "main", "asset": "A", "key": "KA"},
                   {"library": "main", "asset": "B", "key": "KB"}]
        grants = [{"key": "KA", "lang": "zho", "start": 0, "end": 20},
                  {"key": "KB", "lang": "zho", "start": 0, "end": 20}]

        def run(tag, smp, mp, gr):
            s = write_jsonl(os.path.join(self.tmp, "s-%s.jsonl" % tag),
                            smp)
            m = write_jsonl(os.path.join(self.tmp, "m-%s.jsonl" % tag),
                            mp)
            g = write_jsonl(os.path.join(self.tmp, "g-%s.jsonl" % tag),
                            gr)
            out = os.path.join(self.tmp, "out-%s" % tag)
            corpus.export_corpus(libs, s, m, g, None, out)
            return out

        o1 = run("1", samples, mapping, grants)
        o2 = run("2", list(reversed(samples)), list(reversed(mapping)),
                 list(reversed(grants)))
        for name in ("train.jsonl", "validation.jsonl",
                     "rejected.jsonl", "quarantine.jsonl",
                     "evidence.jsonl"):
            with open(os.path.join(o1, name), "rb") as fh:
                b1 = fh.read()
            with open(os.path.join(o2, name), "rb") as fh:
                b2 = fh.read()
            self.assertEqual(b1, b2, name)
        # The two delivered samples share the text 相同: one component.
        members, _keys = self.registry_members(
            os.path.join(o1, "registry.sqlite"))
        comp = {sid: c for sid, _s, c in members}
        self.assertEqual(comp["s1"], comp["s2"])
        self.assertEqual(comp["s1"], comp["s3"])
        # s3's window spans both assets: its keys carry both source keys
        # (a sample is never grouped by its first cue alone).
        allrows = read_jsonl(os.path.join(o1, "train.jsonl")) + \
            read_jsonl(os.path.join(o1, "validation.jsonl"))
        s3 = [r for r in allrows if r["id"] == "s3"][0]
        self.assertEqual(
            sorted((k for k in s3["keys"] if k["kind"] == "src"),
                   key=lambda k: k["key"]),
            [{"kind": "src", "key": "KA"}, {"kind": "src", "key": "KB"}])


# ---------------------------------------------------------------------------
# Registry chaining across batches (old library moved away)
# ---------------------------------------------------------------------------

class RegistryChainTest(CorpusTest):
    def test_chain_and_old_library_moved_away(self):
        man = {"package": "chain", "languages": ["zho"],
               "assets": {"A": 10}, "sequences": {"R": 4}, "root": "R"}
        clips = [{"id": "r-a", "seq": "R", "track": 0, "start": 0,
                  "end": 4, "kind": "asset", "ref": "A", "base": 0,
                  "p": 1, "q": 1}]
        coverage = [{"asset": "A", "lang": "zho", "start": 0, "end": 10}]
        cues = [{"id": "c", "asset": "A", "lang": "zho", "start": 0,
                 "end": 4, "text": "链上文本"}]
        lib1, meta1 = self.make_lib("chain1", man, clips, coverage, cues)
        libs1 = self.libs_file([("main", lib1, meta1["logical_version"])],
                               name="libs1.json")
        s1, m1, _ = self.inputs(
            [{"id": "b1", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "A", "key": "K"}], [])
        g = write_jsonl(os.path.join(self.tmp, "g.jsonl"),
                        [{"key": "K", "lang": "zho", "start": 0,
                          "end": 10}])
        out1 = os.path.join(self.tmp, "out1")
        corpus.export_corpus(libs1, s1, m1, g, None, out1)
        reg1 = os.path.join(out1, "registry.sqlite")
        members1, _ = self.registry_members(reg1)
        side_b1 = members1[0][1]

        # Batch 2: a new library (the old one is moved away), a new
        # sample sharing b1's text -> must inherit b1's side.  The new
        # sample's asset maps to a different source key, so the link is
        # the shared text alone.
        lib2, meta2 = self.make_lib("chain2", man, clips, coverage, cues)
        libs2 = self.libs_file([("main", lib2, meta2["logical_version"])],
                               name="libs2.json")
        s2, m2, _ = self.inputs(
            [{"id": "b2", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "A", "key": "K2"}],
            [], names=("s2", "m2", "g2tmp"))
        g2 = write_jsonl(os.path.join(self.tmp, "g2.jsonl"),
                         [{"key": "K2", "lang": "zho", "start": 0,
                           "end": 10}])
        moved = lib1 + ".away"
        os.rename(lib1, moved)
        try:
            out2 = os.path.join(self.tmp, "out2")
            corpus.export_corpus(libs2, s2, m2, g2, reg1, out2)
        finally:
            os.rename(moved, lib1)
        members2, keys2 = self.registry_members(
            os.path.join(out2, "registry.sqlite"))
        sides = {sid: side for sid, side, _c in members2}
        self.assertEqual(sides["b2"], side_b1)
        # b2 joined b1's component; the new registry chains on.
        comp = {sid: c for sid, _s, c in members2}
        self.assertEqual(comp["b1"], comp["b2"])
        # And the batch-2 registry works as batch-3 input: a fresh
        # sample id on the same text inherits the side again.
        s3, m3, _ = self.inputs(
            [{"id": "b3", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "A", "key": "K2"}],
            [], names=("s3", "m3", "g3tmp"))
        out3 = os.path.join(self.tmp, "out3")
        corpus.export_corpus(libs2, s3, m3, g2,
                             os.path.join(out2, "registry.sqlite"), out3)
        members3, _ = self.registry_members(
            os.path.join(out3, "registry.sqlite"))
        sides3 = {sid: side for sid, side, _c in members3}
        self.assertEqual(sides3["b3"], side_b1)
        # b2 is now history: reusing its id is an input error.
        with self.assertRaises(CorpusError):
            corpus.export_corpus(
                libs2, s2, m2, g2,
                os.path.join(out2, "registry.sqlite"),
                os.path.join(self.tmp, "out4"))


# ---------------------------------------------------------------------------
# Nested paths: lifting must not fold two roundings into one rate
# ---------------------------------------------------------------------------

class NestedLiftTest(CorpusTest):
    """R plays S with p=2,q=3; S plays T with p=3,q=2.  Film frames
    0..3 sample S frames 0,0,1,2 and hence T frames 0,0,1,3 -- folding
    the rates would wrongly give T frames 0,1,2,3."""

    def setUp(self):
        super().setUp()
        man = {"package": "nest", "languages": ["zho"],
               "assets": {"T": 8}, "sequences": {"S": 6, "R": 4},
               "root": "R"}
        clips = [
            {"id": "r-s", "seq": "R", "track": 0, "start": 0, "end": 4,
             "kind": "seq", "ref": "S", "base": 0, "p": 2, "q": 3},
            {"id": "s-t", "seq": "S", "track": 0, "start": 0, "end": 6,
             "kind": "asset", "ref": "T", "base": 0, "p": 3, "q": 2},
        ]
        coverage = [{"asset": "T", "lang": "zho", "start": 0, "end": 8}]
        cues = [{"id": "A", "asset": "T", "lang": "zho", "start": 0,
                 "end": 8, "text": "嵌套"}]
        self.lib, self.meta = self.make_lib("nest", man, clips, coverage,
                                            cues)
        self.libs = self.libs_file([("main", self.lib,
                                     self.meta["logical_version"])])
        self.samples, self.mapping, _g = self.inputs(
            [{"id": "s1", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "T", "key": "K"}],
            [])

    def grants(self, frames):
        return write_jsonl(os.path.join(self.tmp, "grants.jsonl"),
                           [{"key": "K", "lang": "zho", "start": f,
                             "end": f + 1} for f in frames])

    def side_file(self, out):
        for name in ("train.jsonl", "validation.jsonl"):
            rows = read_jsonl(os.path.join(out, name))
            if rows:
                return rows
        return []

    def test_nested_rounding_is_not_folded(self):
        # Grants exactly {0, 1, 3}: passes.  Frame 2 of T is never
        # sampled (level rounding), so it needs no grant.  The touching
        # singleton grants merge into [0,2) in the union; the proof
        # shows film frames 0..2 sampling only source frames [0,2) --
        # a folded rate would report source frame 2 for film frame 2.
        summary, _stats, out = self.export(self.libs, self.samples,
                                           self.mapping,
                                           self.grants([0, 1, 3]))
        self.assertEqual(summary["counts"]["rejected"], 0)
        rows = self.side_file(out)
        proof = rows[0]["cues"][0]["proof"]
        self.assertEqual(
            [(p["window"], p["source_frames"], p["grant"]) for p in
             proof],
            [([0, 3], [0, 2], [0, 2]),
             ([3, 4], [3, 4], [3, 4])])

    def test_nested_violation_locates_source_frame_3(self):
        # Remove the grant for T frame 3: the last film frame samples it.
        summary, _stats, out = self.export(self.libs, self.samples,
                                           self.mapping,
                                           self.grants([0, 1]))
        rows = read_jsonl(os.path.join(out, "rejected.jsonl"))
        self.assertEqual(rows[0]["reason"]["reason"], "unauthorized")
        self.assertEqual(rows[0]["reason"]["window_frame"], 3)
        self.assertEqual(rows[0]["reason"]["source_frame"], 3)


# ---------------------------------------------------------------------------
# Empty inputs and the CLI round-trip
# ---------------------------------------------------------------------------

class EdgeAndCliTest(CorpusTest):
    def setUp(self):
        super().setUp()
        man, clips, coverage, cues = rev_package(self, "edge")
        self.lib, self.meta = self.make_lib("edge", man, clips, coverage,
                                            cues)
        self.libs = self.libs_file([("main", self.lib,
                                     self.meta["logical_version"])])

    def test_empty_batch(self):
        s, m, g = self.inputs([], [], [])
        summary, stats, out = self.export(self.libs, s, m, g)
        self.assertEqual(summary["counts"],
                         {"train": 0, "validation": 0, "rejected": 0,
                          "quarantine": 0})
        for name in ("train.jsonl", "validation.jsonl",
                     "rejected.jsonl", "quarantine.jsonl",
                     "evidence.jsonl"):
            self.assertEqual(read_jsonl(os.path.join(out, name)), [])
        members, keys = self.registry_members(
            os.path.join(out, "registry.sqlite"))
        self.assertEqual((members, keys), ([], []))

    def test_cli_round_trip_and_refusal(self):
        from subwarehouse.cli import main
        s, m, _ = self.inputs(
            [{"id": "s1", "library": "main", "start": 0, "end": 4,
              "languages": ["zho"]}],
            [{"library": "main", "asset": "T", "key": "K"}], [])
        g = write_jsonl(os.path.join(self.tmp, "g.jsonl"),
                        [{"key": "K", "lang": "zho", "start": 0,
                          "end": 12}])
        out = os.path.join(self.tmp, "cli-out")
        rc = main(["corpus", "corpus-export", "--libraries", self.libs,
                   "--samples", s, "--mapping", m, "--grants", g,
                   "--out", out])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(out,
                                                    "registry.sqlite")))
        # Second run over the same output directory refuses.
        rc = main(["corpus", "corpus-export", "--libraries", self.libs,
                   "--samples", s, "--mapping", m, "--grants", g,
                   "--out", out])
        self.assertEqual(rc, 2)
        # registry-info on the produced registry.
        rc = main(["corpus", "corpus-registry-info", "--registry",
                   os.path.join(out, "registry.sqlite")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
