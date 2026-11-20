"""Shard content semantics, registration rules and dispatch competition."""

import json
import multiprocessing
import os
import unittest

from common import LedgerTest
from platform_sim import Platform, write_receipts

from subwarehouse import builder, deliver
from subwarehouse.errors import LedgerError


def _lib(test, name, cues, coverage, assets=None, langs=("zho",),
         frames=12):
    man = {"package": name, "languages": list(langs),
           "assets": assets or {"M": frames * 3},
           "sequences": {"R": frames}}
    clips = [{"id": "c", "seq": "R", "track": 0, "start": 0, "end": frames,
              "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}]
    pkg = test.pkg(name, man, clips, coverage, cues)
    lib = os.path.join(test.tmp, name + ".sqlite")
    builder.build(pkg, "R", lib)
    return lib


def _cue(cid, s, e, text, lang="zho"):
    return {"id": cid, "asset": "M", "lang": lang, "start": s, "end": e,
            "text": text}


def _cov(s, e, lang="zho"):
    return {"asset": "M", "lang": lang, "start": s, "end": e}


THREE = [_cue("a", 0, 2, "甲"), _cue("b", 4, 6, "乙"),
         _cue("c", 8, 10, "丙")]
FULL = [_cov(0, 36)]


class CutSemanticsTest(LedgerTest):
    """Content is cut at shard borders; identity is relative interval +
    verbatim text, never path / cue id / diagnostic location."""

    def setUp(self):
        super().setUp()
        self.conn, _ = self.build_ledger()

    def register(self, lib, table, target="A", release=1):
        return deliver.register(self.conn, target, release, lib,
                                {"zho": table})

    def cuts(self, release=1, target="A"):
        return list(self.conn.execute(
            "SELECT shard, rel_start, rel_end, text FROM cuts WHERE "
            "target = ? AND seq = ? ORDER BY shard, rel_start",
            (target, release)))

    def test_cut_at_shard_borders_and_merge_adjacent_same_text(self):
        lib = _lib(self, "v1", [
            _cue("a", 0, 2, "same"), _cue("b", 2, 3, "same"),
            _cue("c", 3, 4, "same "), _cue("d", 4, 6, "same")], FULL)
        self.register(lib, self.shard_table(12, [4, 8], "s"))
        self.assertEqual(self.cuts(), [
            ("s0", 0, 3, "same"), ("s0", 3, 4, "same "),
            ("s1", 0, 2, "same")])

    def test_identity_ignores_cue_ids_and_paths(self):
        # Same text and relative intervals but different cue ids: the unit
        # content is identical, so no new instruction follows.
        table = self.shard_table(12, [4, 8], "s")
        lib1 = _lib(self, "p1", [_cue("x", 0, 2, "T")], FULL)
        lib2 = _lib(self, "p2", [_cue("y", 0, 1, "T"),
                                 _cue("z", 1, 2, "T")], FULL)
        self.register(lib1, table)
        pf = Platform()
        b1 = deliver.dispatch(self.conn, "d1", ["A"])
        self.assertEqual(len(b1["instructions"]), 1)
        self.assertEqual(b1["instructions"][0]["items"],
                         [{"start": 0, "end": 2, "text": "T"}])
        for row in b1["instructions"]:
            pf.deliver(row)
        deliver.import_receipts(self.conn, [write_receipts(
            os.path.join(self.tmp, "r.jsonl"), pf.dump_receipts("A"))])
        deliver.register(self.conn, "B", 1, lib2, {"zho": table})
        b2 = deliver.dispatch(self.conn, "d2", ["B"])
        self.assertEqual(b2["instructions"][0]["items"],
                         [{"start": 0, "end": 2, "text": "T"}])
        # A's release 2 built from the split-cue library is content-equal
        deliver.register(self.conn, "A", 2, lib2)
        self.assertEqual(deliver.dispatch(self.conn, "d3", ["A"])["count"],
                         0)

    def test_text_is_verbatim_and_adjacent_different_text_is_kept(self):
        lib = _lib(self, "v1", [
            _cue("a", 0, 1, " spaced "), _cue("b", 1, 2, "spaced")], FULL)
        self.register(lib, self.shard_table(12, [4, 8], "s"))
        self.assertEqual(self.cuts(), [
            ("s0", 0, 1, " spaced "), ("s0", 1, 2, "spaced")])

    def test_blank_is_empty_and_silence_is_empty(self):
        # M carries cues; N is fully checked and silent.  A blank clip
        # covers shard 0 on the highest track, N covers shard 1.
        man = {"package": "blank", "languages": ["zho"],
               "assets": {"M": 36, "N": 36}, "sequences": {"R": 12}}
        clips = [
            {"id": "c", "seq": "R", "track": 0, "start": 0, "end": 12,
             "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1},
            {"id": "n", "seq": "R", "track": 1, "start": 4, "end": 8,
             "kind": "asset", "ref": "N", "base": 0, "p": 1, "q": 1},
            {"id": "bl", "seq": "R", "track": 2, "start": 0, "end": 4,
             "kind": "blank"}]
        cues = [_cue("a", 0, 2, "被遮"), _cue("b", 4, 6, "被盖"),
                _cue("d", 8, 10, "T")]
        pkg = self.pkg("blank", man, clips, FULL, cues)
        lib = os.path.join(self.tmp, "blank.sqlite")
        builder.build(pkg, "R", lib)
        self.register(lib, self.shard_table(12, [4, 4, 4], "s"))
        self.assertEqual(self.cuts(), [("s2", 0, 2, "T")])
        b = deliver.dispatch(self.conn, "d", ["A"])
        self.assertEqual([(r["shard"], r["action"])
                          for r in b["instructions"]],
                         [("s2", "replace")])

    def test_unknown_marks_unit_incomplete_with_film_interval(self):
        lib = _lib(self, "v1", [_cue("a", 0, 2, "T")],
                   [_cov(0, 2), _cov(5, 6)])
        self.register(lib, self.shard_table(12, [4, 4, 4], "s"))
        # the only cue is in shard 0 but that shard is incomplete too
        b = deliver.dispatch(self.conn, "d", ["A"])
        self.assertEqual(b["count"], 0)
        v = deliver.verify(self.conn, "A", "zho", "s0", 1, 0)
        self.assertEqual(v["expected"]["state"], "incomplete")
        self.assertEqual(v["expected"]["content"],
                         [{"start": 0, "end": 2, "text": "T"}])
        self.assertEqual(v["expected"]["gaps"], [{"start": 2, "end": 4}])
        v = deliver.verify(self.conn, "A", "zho", "s1", 1, 0)
        self.assertEqual(v["expected"]["state"], "incomplete")
        self.assertEqual(v["expected"]["gaps"],
                         [{"start": 4, "end": 5}, {"start": 6, "end": 8}])
        lib2 = _lib(self, "v2", THREE, FULL)
        deliver.register(self.conn, "A", 2, lib2)
        b2 = deliver.dispatch(self.conn, "d2", ["A"])
        # s0 was never delivered (it had no material then); s1 now has
        # full material (乙 at 4..6 plus silence), s2 gets 丙
        self.assertEqual([(r["shard"], r["action"])
                          for r in b2["instructions"]],
                         [("s0", "replace"), ("s1", "replace"),
                          ("s2", "replace")])

    def test_incomplete_unit_keeps_confirmed_platform_content(self):
        lib = _lib(self, "v1", THREE, FULL)
        self.register(lib, self.shard_table(12, [4, 4, 4], "s"))
        pf = Platform()
        for row in deliver.dispatch(self.conn, "d1", ["A"])["instructions"]:
            pf.deliver(row)
        deliver.import_receipts(self.conn, [write_receipts(
            os.path.join(self.tmp, "r.jsonl"), pf.dump_receipts("A"))])
        # Release 2 hands shard 1 new material but drops its checked
        # coverage: the shard keeps the platform's confirmed release-1
        # subtitles even though the wanted text changed.
        # coverage covers shards 0 and 1 but not shard 2
        lib2 = _lib(self, "v2",
                    [{"id": "e", "asset": "M", "lang": "zho", "start": 6,
                      "end": 7, "text": "新"}],
                    [_cov(0, 8)])
        deliver.register(self.conn, "A", 2, lib2)
        b = deliver.dispatch(self.conn, "d2", ["A"])
        # shard 0 is now completely empty (clear it), shard 1 gains text
        # at relative 2 under changed checked coverage, shard 2 is
        # unconfirmed and therefore keeps the platform content
        self.assertEqual([(r["shard"], r["action"], r["items"])
                          for r in b["instructions"]],
                         [("s0", "clear", []),
                          ("s1", "replace",
                           [{"start": 2, "end": 3, "text": "新"}])])
        v = deliver.verify(self.conn, "A", "zho", "s1", 2, 3)
        self.assertEqual(v["expected"]["state"], "subtitles")
        self.assertEqual(v["platform"]["state"], "subtitles")
        self.assertEqual(v["platform"]["content"],
                         [{"start": 0, "end": 2, "text": "乙"}])
        self.assertFalse(v["platform"]["matches_expected"])
        # a confirmed unit is kept as-is as well
        self.assertEqual(
            deliver.verify(self.conn, "A", "zho", "s0", 2, 3)["platform"]
            ["content"], [{"start": 0, "end": 2, "text": "甲"}])
        v2 = deliver.verify(self.conn, "A", "zho", "s2", 2, 3)
        self.assertEqual(v2["expected"]["state"], "incomplete")
        self.assertEqual(v2["platform"]["state"], "subtitles")


class RegistrationTest(LedgerTest):
    def setUp(self):
        super().setUp()
        self.conn, self.ledger = self.build_ledger()

    def test_first_registration_fixes_film_length_and_shard_table(self):
        lib = _lib(self, "v1", THREE, FULL)
        table = self.shard_table(12, [4, 4, 4])
        deliver.register(self.conn, "A", 1, lib, {"zho": table})
        other = _lib(self, "short", [], [_cov(0, 30)], frames=10)
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 2, other)
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 2, lib,
                             {"zho": self.shard_table(12, [6, 6])})
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 2, lib,
                             {"zho": table, "eng": table})
        again = deliver.register(self.conn, "A", 1, lib, {"zho": table})
        self.assertTrue(again["already_registered"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM releases").fetchone()[0], 1)

    def test_one_ledger_serves_one_film(self):
        lib = _lib(self, "v1", THREE, FULL)
        table = self.shard_table(12, [4, 4, 4])
        deliver.register(self.conn, "A", 1, lib, {"zho": table})
        other = _lib(self, "other", [], [_cov(0, 30)], frames=10)
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "B", 1, other, {"zho": table})

    def test_release_numbers_increase_and_cannot_be_replaced(self):
        lib = _lib(self, "v1", [_cue("a", 0, 2, "T")], FULL)
        lib2 = _lib(self, "v2", [_cue("a", 0, 2, "U")], FULL)
        table = self.shard_table(12, [4, 4, 4])
        deliver.register(self.conn, "A", 1, lib, {"zho": table})
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 3, lib)
        deliver.register(self.conn, "A", 2, lib2)
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 2, lib)
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 1, lib2)

    def test_library_may_be_moved_after_registration(self):
        lib = _lib(self, "v1", THREE, FULL)
        deliver.register(self.conn, "A", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        os.rename(lib, lib + ".away")
        try:
            b = deliver.dispatch(self.conn, "d", ["A"])
            self.assertEqual(b["count"], 3)
            v = deliver.verify(self.conn, "A", "zho", "s0", 1, 0)
            self.assertEqual(v["expected"]["content"],
                             [{"start": 0, "end": 2, "text": "甲"}])
            s = deliver.verify(self.conn, "A", "zho", "s1", 1, 0)
            self.assertEqual(s["expected"]["content"],
                             [{"start": 0, "end": 2, "text": "乙"}])
        finally:
            os.rename(lib + ".away", lib)

    def test_shard_tables_must_tile_the_film(self):
        lib = _lib(self, "v1", [], FULL)
        for i, bad in enumerate((
                [{"name": "a", "start": 0, "end": 5}],
                [{"name": "a", "start": 0, "end": 6},
                 {"name": "b", "start": 5, "end": 12}],
                [{"name": "a", "start": 0, "end": 6},
                 {"name": "a", "start": 6, "end": 12}],
                [{"name": "a", "start": 0, "end": 6},
                 {"name": "b", "start": 6, "end": 13}],
                [{"name": "a", "start": 0, "end": 6},
                 {"name": "b", "start": 7, "end": 12}])):
            with self.assertRaises(LedgerError):
                deliver.register(self.conn, "T%d" % i, 1, lib, {"zho": bad})

    def test_multi_language_and_per_target_tables(self):
        cues = THREE + [_cue("b-en", 0, 2, "en", lang="eng"),
                        _cue("c-en", 8, 10, "en2", lang="eng")]
        cov = FULL + [_cov(0, 36, lang="eng")]
        lib = _lib(self, "v1", cues, cov, langs=("eng", "zho"))
        deliver.register(self.conn, "A", 1, lib, {
            "zho": self.shard_table(12, [4, 4, 4], "z"),
            "eng": self.shard_table(12, [6, 6], "e")})
        deliver.register(self.conn, "B", 1, lib, {
            "zho": self.shard_table(12, [5, 7], "b")})
        b = deliver.dispatch(self.conn, "d", ["A", "B"])
        self.assertEqual([(r["target"], r["lang"], r["shard"])
                          for r in b["instructions"]],
                         [("A", "eng", "e0"), ("A", "eng", "e1"),
                          ("A", "zho", "z0"), ("A", "zho", "z1"),
                          ("A", "zho", "z2"), ("B", "zho", "b0"),
                          ("B", "zho", "b1")])
        # the same film can be sharded differently per target
        self.assertEqual(
            [r["items"] for r in b["instructions"] if r["target"] == "B"],
            [[{"start": 0, "end": 2, "text": "甲"},
              {"start": 4, "end": 5, "text": "乙"}],
             [{"start": 0, "end": 1, "text": "乙"},
              {"start": 3, "end": 5, "text": "丙"}]])
        lib2 = _lib(self, "v2", THREE, FULL, langs=("zho",))
        with self.assertRaises(LedgerError):
            deliver.register(self.conn, "A", 2, lib2)


class CompetitionTest(LedgerTest):
    """Two planners racing must not open two operations on one unit."""

    def _worker(self, path, request, queue):
        try:
            conn = deliver.open_ledger(path)
            try:
                b = deliver.dispatch(conn, request, ["A"], limit=2)
                queue.put(("ok", request, b["count"],
                           [r["shard"] for r in b["instructions"]]))
            finally:
                conn.close()
        except Exception as e:      # pragma: no cover - reported to parent
            queue.put(("err", request, str(e)))

    def test_two_processes_never_double_book_a_unit(self):
        lib = _lib(self, "v1", THREE, FULL)
        self.conn, path = self.build_ledger()
        deliver.register(self.conn, "A", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        procs = [ctx.Process(target=self._worker,
                             args=(path, "req%d" % i, q))
                 for i in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        got = [q.get(timeout=10), q.get(timeout=10)]
        shards = [s for r in got if r[0] == "ok" for s in r[3]]
        rows = list(self.conn.execute(
            "SELECT lang, shard, COUNT(*) FROM ops GROUP BY lang, shard"))
        self.assertEqual(len(rows), 3)
        for lang, shard, n in rows:
            self.assertEqual(n, 1, (lang, shard, n))
        self.assertEqual(len(set(shards)), len(shards))
        self.assertLessEqual(len(shards), 3)
        # a follow-up takes exactly the units the race left unplanned
        rest = deliver.dispatch(self.conn, "after", ["A"])
        self.assertEqual(len(rows) + rest["count"], 3)

    def test_retry_same_request_after_crash_returns_full_batch(self):
        lib = _lib(self, "v1", THREE, FULL)
        self.conn, path = self.build_ledger()
        deliver.register(self.conn, "A", 1, lib,
                         {"zho": self.shard_table(12, [4, 4, 4])})
        first = deliver.dispatch(self.conn, "req", ["A"], limit=2)
        self.assertEqual(first["count"], 2)
        again = deliver.dispatch(self.conn, "req", ["A"], limit=2)
        self.assertEqual(again, first)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM ops").fetchone()[0], 2)
        with self.assertRaises(LedgerError):
            deliver.dispatch(self.conn, "req", ["A"], limit=3)


if __name__ == "__main__":
    unittest.main()
