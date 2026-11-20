"""Shared test helpers."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from subwarehouse import builder                       # noqa: E402
from subwarehouse.library import open_library          # noqa: E402


def write_package(dest, manifest, clips=None, coverage=None, cues=None,
                  shuffle_seed=None):
    """Write a package directory.  With shuffle_seed, JSONL rows are
    written in a shuffled (but deterministic) order."""
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    tables = {"clips.jsonl": clips, "coverage.jsonl": coverage,
              "cues.jsonl": cues}
    for name, rows in tables.items():
        if rows is None:
            continue
        rows = list(rows)
        if shuffle_seed is not None:
            import random
            random.Random(shuffle_seed).shuffle(rows)
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return dest


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subwh-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def pkg(self, name, manifest, clips=None, coverage=None, cues=None,
            shuffle_seed=None):
        return write_package(os.path.join(self.tmp, name), manifest,
                             clips, coverage, cues, shuffle_seed)

    def build(self, pkg_dir, root, name="lib.sqlite", **kw):
        out = os.path.join(self.tmp, name)
        stats, meta = builder.build(pkg_dir, root, out, **kw)
        return out, stats, meta

    def segments(self, lib_path, lang=None):
        conn, meta = open_library(lib_path)
        try:
            langs = json.loads(meta["languages"])
            rows = []
            for lg in langs:
                if lang is not None and lg != lang:
                    continue
                cur = conn.execute(
                    "SELECT lang, start, \"end\", state, text, cue_id, "
                    "asset_id, path FROM segments WHERE lang = ? "
                    "ORDER BY start", (lg,))
                rows.extend(cur.fetchall())
            return rows, meta
        finally:
            conn.close()

    def dir_listing(self):
        return sorted(os.listdir(self.tmp))
