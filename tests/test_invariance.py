"""Order invariance (shuffled JSONL rows) and randomized agreement between
the interval engine and the per-frame reference."""

import json
import random
import unittest

from common import TempDirTest
from make_sample import CLIPS, CUES, COVERAGE, MANIFEST
from reference import RefPackage, resolve_root

from subwarehouse.library import inspect_frame, open_library


class ShuffleInvarianceTest(TempDirTest):
    def test_shuffled_rows_same_version_and_facts(self):
        pkg_a = self.pkg("a", MANIFEST, CLIPS, COVERAGE, CUES)
        lib_a, _, meta_a = self.build(pkg_a, "R", "a.sqlite")
        for seed in (1, 2, 3):
            pkg_b = self.pkg("b%d" % seed, MANIFEST, CLIPS, COVERAGE, CUES,
                             shuffle_seed=seed)
            lib_b, _, meta_b = self.build(pkg_b, "R", "b%d.sqlite" % seed)
            self.assertEqual(meta_a["logical_version"],
                             meta_b["logical_version"])
            rows_a, _ = self.segments(lib_a)
            rows_b, _ = self.segments(lib_b)
            self.assertEqual(rows_a, rows_b)


def gen_package(rng):
    """Generate a random valid small package."""
    langs = sorted(rng.sample(["zho", "eng"], k=rng.randint(1, 2)))
    n_assets = rng.randint(1, 4)
    assets = {"A%d" % i: rng.randint(3, 15) for i in range(n_assets)}
    seqs = {}
    order = []
    for i in range(rng.randint(1, 4)):
        seqs["S%d" % i] = rng.randint(5, 25)
        order.append("S%d" % i)
    man = {"package": "fuzz", "languages": langs, "assets": assets,
           "sequences": seqs}
    clips = []
    cid = 0
    for si, s in enumerate(order):
        L = seqs[s]
        tracks = sorted(rng.sample([-1, 0, 1, 2], k=rng.randint(1, 3)),
                        reverse=True)
        for t in tracks:
            pos = 0
            while pos < L and rng.random() < 0.8:
                start = pos + rng.randint(0, 2)
                if start >= L:
                    break
                end = min(L, start + rng.randint(1, 6))
                kind_pool = ["asset", "blank"] + (["seq"] if si else [])
                kind = rng.choice(kind_pool)
                cid += 1
                if kind == "blank":
                    clips.append({"id": "c%d" % cid, "seq": s, "track": t,
                                  "start": start, "end": end,
                                  "kind": "blank"})
                else:
                    ref = (rng.choice(list(assets)) if kind == "asset"
                           else rng.choice(order[:si]))
                    F = (assets if kind == "asset" else seqs)[ref]
                    p = rng.choice([1, 1, 2, 3, -1, -2, -3])
                    q = rng.choice([1, 1, 2, 3])
                    length = end - start
                    max_len = ((F - 1) * q) // abs(p) + 1
                    if length > max_len:
                        length = max_len
                        end = start + length
                    off = ((length - 1) * p) // q
                    lo = 0 if p > 0 else -off
                    hi = F - 1 - off if p > 0 else F - 1
                    base = rng.randint(lo, hi)
                    clips.append({"id": "c%d" % cid, "seq": s, "track": t,
                                  "start": start, "end": end, "kind": kind,
                                  "ref": ref, "base": base, "p": p, "q": q})
                pos = end + rng.randint(0, 2)
    coverage, cues = [], []
    cue_n = 0
    for a, F in assets.items():
        for lg in langs:
            ivs = []
            for _ in range(rng.randint(0, 3)):
                s = rng.randint(0, F - 1)
                ivs.append((s, rng.randint(s + 1, F)))
            for s, e in ivs:
                coverage.append({"asset": a, "lang": lg, "start": s,
                                 "end": e})
            # cues: non-overlapping sub-intervals of the coverage union
            union = []
            for s, e in sorted(ivs):
                if union and s <= union[-1][1]:
                    union[-1] = (union[-1][0], max(union[-1][1], e))
                else:
                    union.append((s, e))
            for s, e in union:
                if rng.random() < 0.6:
                    ce = rng.randint(s + 1, e)
                    cue_n += 1
                    cues.append({"id": "q%d" % cue_n, "asset": a,
                                 "lang": lg, "start": s, "end": ce,
                                 "text": "t%d" % cue_n})
    return man, clips, coverage, cues, order[-1]


class FuzzTest(TempDirTest):
    def test_engine_matches_reference(self):
        for seed in range(60):
            rng = random.Random(1000 + seed)
            man, clips, coverage, cues, root = gen_package(rng)
            pkg_dir = self.pkg("f%d" % seed, man, clips, coverage, cues)
            lib, _, _ = self.build(pkg_dir, root, "f%d.sqlite" % seed)
            pkg = RefPackage.load(pkg_dir)
            conn, meta = open_library(lib)
            try:
                frames = man["sequences"][root]
                for lang in man["languages"]:
                    for f in range(frames):
                        ref = resolve_root(pkg, root, lang, f)
                        info = inspect_frame(conn, meta, lang, f)
                        self.assertEqual(
                            info["state"], ref["state"],
                            "seed %d lang %s frame %d" % (seed, lang, f))
                        self.assertEqual(
                            [lv["clip"] for lv in info["levels"]],
                            [lv["clip"] for lv in ref["path"]],
                            "seed %d lang %s frame %d" % (seed, lang, f))
                        if ref["state"] == "cue":
                            self.assertEqual(info["text"], ref["text"])
                            self.assertEqual(info["cue"], ref["cue"])
                            self.assertEqual(info["asset_frame"],
                                             ref["asset_frame"])
                        elif ref["state"] == "unknown":
                            self.assertEqual(info["asset"], ref["asset"])
                            self.assertEqual(info["missing_frame"],
                                             ref["missing_frame"])
                        elif ref["state"] == "blank":
                            self.assertEqual(info["blank_clip"],
                                             ref["path"][-1]["clip"])
            finally:
                conn.close()
            # segmentation invariants: full partition, no adjacent
            # identical identities
            rows, _ = self.segments(lib)
            for lang in man["languages"]:
                segs = [r for r in rows if r[0] == lang]
                pos = 0
                for r in segs:
                    self.assertEqual(r[1], pos, "seed %d" % seed)
                    pos = r[2]
                self.assertEqual(pos, frames)
                for a, b in zip(segs, segs[1:]):
                    self.assertNotEqual(
                        (a[3], a[4], a[5], a[6], a[7]),
                        (b[3], b[4], b[5], b[6], b[7]),
                        "seed %d lang %s" % (seed, lang))


if __name__ == "__main__":
    unittest.main()
