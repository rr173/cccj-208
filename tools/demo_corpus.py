"""Replay the corpus-export demonstration: the reversed-window grant
case, the cross-batch quarantine case, and the multi-language /
same-local-id case.  Everything is generated at runtime; nothing here
is committed.

Usage: python3 tools/demo_corpus.py [--workdir out/demo-corpus]
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from subwarehouse import builder, corpus                 # noqa: E402


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def write_pkg(dest, manifest, clips, coverage, cues):
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    for name, rows in (("clips.jsonl", clips), ("coverage.jsonl", coverage),
                       ("cues.jsonl", cues)):
        write_jsonl(os.path.join(dest, name), rows)


def build(work, name, manifest, clips, coverage, cues):
    manifest = dict(manifest)
    root = manifest.pop("root")
    pkg = os.path.join(work, name + "-pkg")
    write_pkg(pkg, manifest, clips, coverage, cues)
    lib = os.path.join(work, name + ".sqlite")
    _stats, meta = builder.build(pkg, root, lib)
    return lib, meta


def libs_file(work, name, entries):
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"libraries": [
            {"alias": a, "path": p, "logical_version": v}
            for a, p, v in entries]}, fh)
    return path


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def show(title, obj):
    print("  %s: %s" % (title, json.dumps(obj, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# 1. The reversed-window grant case
# ---------------------------------------------------------------------------

def demo_grants(work):
    print("== 1. reversed window sampling source frames 8, 6, 4, 2 ==")
    lib, meta = build(
        work, "rev",
        {"package": "rev", "languages": ["zho"],
         "assets": {"T": 12}, "sequences": {"R": 4}, "root": "R"},
        [{"id": "r-t", "seq": "R", "track": 0, "start": 0, "end": 4,
          "kind": "asset", "ref": "T", "base": 8, "p": -2, "q": 1}],
        [{"asset": "T", "lang": "zho", "start": 0, "end": 12}],
        [{"id": "A", "asset": "T", "lang": "zho", "start": 2, "end": 9,
          "text": "倒放字幕"}])
    libs = libs_file(work, "libs-rev.json",
                     [("main", lib, meta["logical_version"])])
    samples = write_jsonl(os.path.join(work, "s-rev.jsonl"),
                          [{"id": "s1", "library": "main", "start": 0,
                            "end": 4, "languages": ["zho"]}])
    mapping = write_jsonl(os.path.join(work, "m-rev.jsonl"),
                          [{"library": "main", "asset": "T", "key": "K"}])

    def grants(frames, name):
        return write_jsonl(os.path.join(work, name),
                           [{"key": "K", "lang": "zho", "start": f,
                             "end": f + 1} for f in frames])

    out = os.path.join(work, "out-rev-pass")
    summary, _ = corpus.export_corpus(libs, samples, mapping,
                                      grants([2, 4, 6, 8], "g1.jsonl"),
                                      None, out)
    print(" grants exactly {2,4,6,8} (skipped frames 3,5,7 ungranted):")
    show("counts", summary["counts"])
    side = "train" if summary["counts"]["train"] else "validation"
    row = read_jsonl(os.path.join(out, "%s.jsonl" % side))[0]
    show("proof pieces", [(p["window"], p["grant"])
                          for p in row["cues"][0]["proof"]])

    out = os.path.join(work, "out-rev-fail")
    summary, _ = corpus.export_corpus(libs, samples, mapping,
                                      grants([2, 6, 8], "g2.jsonl"),
                                      None, out)
    print(" grant for source frame 4 removed:")
    show("counts", summary["counts"])
    show("reason", read_jsonl(os.path.join(out, "rejected.jsonl"))[0]
         ["reason"])


# ---------------------------------------------------------------------------
# 2. The cross-batch quarantine case
# ---------------------------------------------------------------------------

def demo_quarantine(work):
    print("\n== 2. old-tr / old-val bridged by new samples X and Y ==")
    lib, meta = build(
        work, "cur",
        {"package": "cur", "languages": ["zho", "eng"],
         "assets": {"A1": 10, "A2": 10}, "sequences": {"R": 8},
         "root": "R"},
        [{"id": "r-a1", "seq": "R", "track": 0, "start": 0, "end": 4,
          "kind": "asset", "ref": "A1", "base": 0, "p": 1, "q": 1},
         {"id": "r-a2", "seq": "R", "track": 0, "start": 4, "end": 8,
          "kind": "asset", "ref": "A2", "base": 0, "p": 1, "q": 1}],
        [{"asset": "A1", "lang": "eng", "start": 0, "end": 10},
         {"asset": "A2", "lang": "eng", "start": 0, "end": 10},
         {"asset": "A2", "lang": "zho", "start": 0, "end": 10}],
        [{"id": "x1", "asset": "A1", "lang": "eng", "start": 0, "end": 4,
          "text": "shared morning"},
         {"id": "y1", "asset": "A2", "lang": "eng", "start": 0, "end": 4,
          "text": "shared morning"},
         {"id": "y2", "asset": "A2", "lang": "zho", "start": 0, "end": 4,
          "text": "共享文本"}])
    libs = libs_file(work, "libs-cur.json",
                     [("cur", lib, meta["logical_version"])])
    samples = write_jsonl(os.path.join(work, "s-cur.jsonl"), [
        {"id": "X", "library": "cur", "start": 0, "end": 4,
         "languages": ["eng"]},
        {"id": "Y", "library": "cur", "start": 4, "end": 8,
         "languages": ["eng", "zho"]}])
    mapping = write_jsonl(os.path.join(work, "m-cur.jsonl"), [
        {"library": "cur", "asset": "A1", "key": "K1"},
        {"library": "cur", "asset": "A2", "key": "K2"}])

    # The previous batch's registry, written in the documented format;
    # the old libraries are gone.
    import sqlite3
    reg = os.path.join(work, "prev-registry.sqlite")
    conn = sqlite3.connect(reg)
    conn.executescript(corpus.REGISTRY_SCHEMA)
    conn.executemany("INSERT INTO meta VALUES(?,?)",
                     [("format", corpus.FORMAT_REGISTRY),
                      ("generator", "demo"), ("seed", corpus.SEED.decode()),
                      ("created_utc", "2026-11-20T00:00:00+00:00")])
    conn.executemany("INSERT INTO members VALUES(?,?,?)",
                     [("old-tr", "train", "old-tr"),
                      ("old-val", "validation", "old-val")])
    conn.executemany("INSERT INTO keys VALUES(?,?,?,?)",
                     [("old-tr", "txt", "zho", "共享文本"),
                      ("old-val", "src", "", "K1")])
    conn.commit()
    conn.close()
    print(" previous registry: old-tr (train, txt zho '共享文本'), "
          "old-val (validation, src K1); old libraries moved away")

    def grants(rows, name):
        return write_jsonl(os.path.join(work, name), rows)

    full = grants([{"key": "K1", "lang": "eng", "start": 0, "end": 10},
                   {"key": "K2", "lang": "eng", "start": 0, "end": 10},
                   {"key": "K2", "lang": "zho", "start": 0, "end": 10}],
                  "g-cur.jsonl")
    out = os.path.join(work, "out-quarantine")
    summary, _ = corpus.export_corpus(libs, samples, mapping, full,
                                      reg, out)
    print(" X shares src K1 with old-val; Y shares one text with X and "
          "another with old-tr:")
    show("counts", summary["counts"])
    ev = read_jsonl(os.path.join(out, "evidence.jsonl"))[0]
    show("evidence members", ev["members"])
    show("evidence history", ev["history"])
    for e in ev["edges"]:
        show("edge", (e["a"], "->", e["b"], e["key"]))

    # Variant: Y loses one zho grant frame and is rejected first.
    short = grants([{"key": "K1", "lang": "eng", "start": 0, "end": 10},
                    {"key": "K2", "lang": "eng", "start": 0, "end": 10},
                    {"key": "K2", "lang": "zho", "start": 0, "end": 2}],
                   "g-cur-short.jsonl")
    out = os.path.join(work, "out-x-validation")
    summary, _ = corpus.export_corpus(libs, samples, mapping, short,
                                      reg, out)
    print(" variant: Y's zho grant cut to [0,2) -> Y rejected, "
          "X joins validation:")
    show("counts", summary["counts"])
    show("Y reason", read_jsonl(os.path.join(out, "rejected.jsonl"))[0]
         ["reason"])
    show("validation", [r["id"] for r in
                        read_jsonl(os.path.join(out,
                                                "validation.jsonl"))])


# ---------------------------------------------------------------------------
# 3. Multi-language samples and same-local-id separation
# ---------------------------------------------------------------------------

def demo_multilang(work):
    print("\n== 3. multi-language grouping; same local id, different "
          "archive keys ==")
    lib1, meta1 = build(
        work, "l1",
        {"package": "l1", "languages": ["zho", "eng"],
         "assets": {"M": 10}, "sequences": {"R": 4}, "root": "R"},
        [{"id": "r-m", "seq": "R", "track": 0, "start": 0, "end": 4,
          "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}],
        [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
         {"asset": "M", "lang": "eng", "start": 0, "end": 10}],
        [{"id": "c-z", "asset": "M", "lang": "zho", "start": 0,
          "end": 4, "text": "你好 世界"},
         {"id": "c-e", "asset": "M", "lang": "eng", "start": 0,
          "end": 4, "text": "hello"}])
    # L2 reuses the local asset id M for unrelated content, plus a
    # second asset N on [4,8).
    lib2, meta2 = build(
        work, "l2",
        {"package": "l2", "languages": ["zho", "eng"],
         "assets": {"M": 10, "N": 10}, "sequences": {"R": 8},
         "root": "R"},
        [{"id": "r-m", "seq": "R", "track": 0, "start": 0, "end": 4,
          "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1},
         {"id": "r-n", "seq": "R", "track": 0, "start": 4, "end": 8,
          "kind": "asset", "ref": "N", "base": 0, "p": 1, "q": 1}],
        [{"asset": "M", "lang": "zho", "start": 0, "end": 10},
         {"asset": "N", "lang": "eng", "start": 0, "end": 10}],
        [{"id": "c-z", "asset": "M", "lang": "zho", "start": 0,
          "end": 4, "text": "完全不同"},
         {"id": "c-e", "asset": "N", "lang": "eng", "start": 0,
          "end": 4, "text": "hello"}])
    libs = libs_file(work, "libs-ml.json",
                     [("L1", lib1, meta1["logical_version"]),
                      ("L2", lib2, meta2["logical_version"])])
    samples = write_jsonl(os.path.join(work, "s-ml.jsonl"), [
        {"id": "s1", "library": "L1", "start": 0, "end": 4,
         "languages": ["zho"]},
        {"id": "s2", "library": "L2", "start": 0, "end": 4,
         "languages": ["zho"]},
        {"id": "s3", "library": "L1", "start": 0, "end": 4,
         "languages": ["zho", "eng"]},
        {"id": "s4", "library": "L2", "start": 4, "end": 8,
         "languages": ["eng"]}])
    mapping = write_jsonl(os.path.join(work, "m-ml.jsonl"), [
        {"library": "L1", "asset": "M", "key": "KA"},
        {"library": "L2", "asset": "M", "key": "KB"},
        {"library": "L2", "asset": "N", "key": "KC"}])
    grants = write_jsonl(os.path.join(work, "g-ml.jsonl"), [
        {"key": k, "lang": lg, "start": 0, "end": 10}
        for k in ("KA", "KB", "KC") for lg in ("zho", "eng")])
    out = os.path.join(work, "out-ml")
    summary, _ = corpus.export_corpus(libs, samples, mapping, grants,
                                      None, out)
    show("counts", summary["counts"])
    import sqlite3
    conn = sqlite3.connect(os.path.join(out, "registry.sqlite"))
    comp = dict(conn.execute("SELECT sid, comp FROM members").fetchall())
    conn.close()
    print("  components: %s" % json.dumps(comp, sort_keys=True))
    print("  s1/s3/s4 share texts across languages and libraries; "
          "s2 (same local asset id M, different key KB) stays apart: %s"
          % (comp["s1"] == comp["s3"] == comp["s4"]
             and comp["s2"] != comp["s1"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "out",
                                                      "demo-corpus"))
    args = ap.parse_args(argv)
    work = os.path.abspath(args.workdir)
    if os.path.lexists(work):
        import shutil
        shutil.rmtree(work)
    os.makedirs(work)
    demo_grants(work)
    demo_quarantine(work)
    demo_multilang(work)
    print("\nworkdir left in %s (generated data, not committed)" % work)


if __name__ == "__main__":
    main()
