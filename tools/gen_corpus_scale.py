"""Generate corpus-export scale inputs at runtime (never committed):

* ``make_sparse``: one library whose timeline spans `frames` frames with
  a fixed number of cues, plus the corpus input files for two sample
  windows proportional to the span.  Interval counts stay fixed while
  the span grows, so any runtime difference comes from the scale, not
  the input size.
* ``make_hotkey``: one small library plus `n` sample rows whose windows
  all show the same cue text, so one relation key gathers `n` members.

Usage: python3 tools/gen_corpus_scale.py OUTDIR [--frames F]
              [--cues N] [--hotkey N]
"""

import argparse
import json
import os

CUES = 2000
CUE_W = 10


def _write_package(dest, manifest, clips, coverage, cues):
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    for name, rows in (("clips.jsonl", clips), ("coverage.jsonl", coverage),
                       ("cues.jsonl", cues)):
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_sparse(dest, frames, n_cues=CUES):
    """A depth-2 library (R -> S -> A) over `frames` frames with a fixed
    number of evenly spaced cues, and corpus inputs whose two sample
    windows scale with the span."""
    pkg = os.path.join(dest, "pkg")
    cues = []
    step = frames // n_cues
    for i in range(n_cues):
        cues.append({"id": "q%d" % i, "asset": "A", "lang": "zho",
                     "start": i * step, "end": i * step + CUE_W,
                     "text": "cue %d" % i})
    _write_package(
        pkg,
        {"package": os.path.basename(dest), "languages": ["zho"],
         "assets": {"A": frames}, "sequences": {"S": frames, "R": frames}},
        [{"id": "s-a", "seq": "S", "track": 0, "start": 0, "end": frames,
          "kind": "asset", "ref": "A", "base": 0, "p": 1, "q": 1},
         {"id": "r-s", "seq": "R", "track": 0, "start": 0, "end": frames,
          "kind": "seq", "ref": "S", "base": 0, "p": 1, "q": 1}],
        [{"asset": "A", "lang": "zho", "start": 0, "end": frames}],
        cues)
    return pkg


def sparse_inputs(dest, lib_path, logical_version, frames):
    """Corpus input files for the sparse library: two windows
    ([0, frames) and [0, frames/2)), one mapping, one grant."""
    libs = os.path.join(dest, "libraries.json")
    with open(libs, "w", encoding="utf-8") as fh:
        json.dump({"libraries": [{"alias": "main", "path": lib_path,
                                  "logical_version": logical_version}]},
                  fh)
    samples = os.path.join(dest, "samples.jsonl")
    _write_jsonl(samples, [
        {"id": "full", "library": "main", "start": 0, "end": frames,
         "languages": ["zho"]},
        {"id": "half", "library": "main", "start": 0, "end": frames // 2,
         "languages": ["zho"]},
    ])
    mapping = os.path.join(dest, "mapping.jsonl")
    _write_jsonl(mapping, [{"library": "main", "asset": "A", "key": "K"}])
    grants = os.path.join(dest, "grants.jsonl")
    _write_jsonl(grants, [{"key": "K", "lang": "zho", "start": 0,
                           "end": frames}])
    return {"libraries": libs, "samples": samples, "mapping": mapping,
            "grants": grants}


def make_hotkey(dest, n):
    """One small library and `n` samples that all see the same cue text
    (one hot relation key with `n` members)."""
    pkg = os.path.join(dest, "pkg")
    _write_package(
        pkg,
        {"package": os.path.basename(dest), "languages": ["zho"],
         "assets": {"A": 100}, "sequences": {"R": 100}},
        [{"id": "r-a", "seq": "R", "track": 0, "start": 0, "end": 100,
          "kind": "asset", "ref": "A", "base": 0, "p": 1, "q": 1}],
        [{"asset": "A", "lang": "zho", "start": 0, "end": 100}],
        [{"id": "hot", "asset": "A", "lang": "zho", "start": 0,
          "end": 10, "text": "热门字幕"}])
    return pkg


def hotkey_inputs(dest, lib_path, logical_version, n):
    libs = os.path.join(dest, "libraries.json")
    with open(libs, "w", encoding="utf-8") as fh:
        json.dump({"libraries": [{"alias": "main", "path": lib_path,
                                  "logical_version": logical_version}]},
                  fh)
    samples = os.path.join(dest, "samples.jsonl")
    with open(samples, "w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps(
                {"id": "s%07d" % i, "library": "main", "start": 0,
                 "end": 10, "languages": ["zho"]},
                ensure_ascii=False) + "\n")
    mapping = os.path.join(dest, "mapping.jsonl")
    _write_jsonl(mapping, [{"library": "main", "asset": "A", "key": "K"}])
    grants = os.path.join(dest, "grants.jsonl")
    _write_jsonl(grants, [{"key": "K", "lang": "zho", "start": 0,
                           "end": 100}])
    return {"libraries": libs, "samples": samples, "mapping": mapping,
            "grants": grants}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--frames", type=int, default=200_000)
    ap.add_argument("--cues", type=int, default=CUES)
    ap.add_argument("--hotkey", type=int, default=0,
                    help="also generate a hot-key batch with N samples")
    args = ap.parse_args()
    pkg = make_sparse(args.outdir, args.frames, args.cues)
    print("%s: sparse package frames=%d cues=%d"
          % (pkg, args.frames, args.cues))
    if args.hotkey:
        hot = os.path.join(args.outdir, "hotkey")
        pkg = make_hotkey(hot, args.hotkey)
        print("%s: hot-key package, %d samples" % (pkg, args.hotkey))


if __name__ == "__main__":
    main()
