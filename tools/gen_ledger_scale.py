"""Generate runtime samples for the delivery ledger benchmark.

Everything here is produced at run time (nothing is committed): a sparse
film with tens of thousands of frames but only a handful of interval
endpoints, many thousands of shards, and a target carrying a long receipt
history.  Frame spans are generated from intervals; frames are never
enumerated.

Usage: python3 tools/gen_ledger_scale.py OUTDIR
       [--frames N] [--shards N] [--cues N] [--receipts N]
"""

import argparse
import json
import os
import sys


def _write_package(dest, frames, cues, languages=("zho", "eng")):
    os.makedirs(dest, exist_ok=True)
    man = {"package": os.path.basename(dest), "languages": list(languages),
           "assets": {"M": frames}, "sequences": {"R": frames}}
    clip = {"id": "c", "seq": "R", "track": 0, "start": 0,
            "end": frames, "kind": "asset", "ref": "M", "base": 0,
            "p": 1, "q": 1}
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(man, fh)
    with open(os.path.join(dest, "clips.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(clip) + "\n")
    with open(os.path.join(dest, "coverage.jsonl"), "w",
              encoding="utf-8") as fh:
        for lang in languages:
            fh.write(json.dumps({"asset": "M", "lang": lang, "start": 0,
                                 "end": frames}) + "\n")
    with open(os.path.join(dest, "cues.jsonl"), "w",
              encoding="utf-8") as fh:
        step = frames // (cues + 1)
        for i in range(cues):
            s = (i + 1) * step
            for lang in languages:
                fh.write(json.dumps({
                    "id": "%s-%d" % (lang, i), "asset": "M",
                    "lang": lang, "start": s,
                    "end": s + max(1, step // 3),
                    "text": "%s cue %d" % (lang, i)}) + "\n")
    return dest


def _write_shard_tables(dest, frames, nshards, languages=("zho", "eng")):
    """Shard tables with deliberately uneven boundaries per language."""
    out = {}
    for li, lang in enumerate(languages):
        table = []
        pos = 0
        for i in range(nshards):
            end = (frames * (i + 1 + li)) // (nshards + li)
            if i == nshards - 1:
                end = frames
            if end <= pos:
                continue
            table.append({"name": "%s-%d" % (lang, i), "start": pos,
                          "end": end})
            pos = end
        assert pos == frames
        out[lang] = table
    path = os.path.join(dest, "shards.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return path, out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--frames", type=int, default=10 ** 9)
    ap.add_argument("--shards", type=int, default=20000)
    ap.add_argument("--cues", type=int, default=4000)
    args = ap.parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)
    pkg = _write_package(os.path.join(args.outdir, "package"),
                         args.frames, args.cues)
    path, tables = _write_shard_tables(args.outdir, args.frames,
                                       args.shards)
    print(json.dumps({"package": pkg, "languages": path,
                      "frames": args.frames,
                      "shards": sum(len(v) for v in tables.values()),
                      "cues": args.cues}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
