"""Generate a review-scale package: one small "hot" subgraph (the
correction target, fixed at HOT frames) plus a configurable number of
irrelevant programs whose timelines span a configurable number of
frames.  Interval counts stay fixed while the span grows, so any
runtime difference comes from the scale, not the input size.

Usage: python3 tools/gen_review_scale.py OUTDIR [--programs N]
              [--frames F] [--seed S]
"""

import argparse
import json
import os

HOT = 1000
HOT_WINDOW = (500, 503)          # the correction window on asset AH
HOT_CUE = (500, 502)
LOW_CUE = (500, 504)


def make_package(dest, n_programs, frames, seed):
    assets = {"AH": HOT, "AL": HOT}
    seqs = {"SH": HOT, "P0": HOT}
    clips = [
        {"id": "s-ah", "seq": "SH", "track": 1, "start": 0, "end": HOT,
         "kind": "asset", "ref": "AH", "base": 0, "p": 1, "q": 1},
        {"id": "s-al", "seq": "SH", "track": 0, "start": 0, "end": HOT,
         "kind": "asset", "ref": "AL", "base": 0, "p": 1, "q": 1},
        {"id": "p0-sh", "seq": "P0", "track": 0, "start": 0, "end": HOT,
         "kind": "seq", "ref": "SH", "base": 0, "p": 1, "q": 1},
    ]
    coverage = [
        {"asset": "AH", "lang": "zho", "start": 0, "end": HOT},
        {"asset": "AL", "lang": "zho", "start": 0, "end": HOT},
    ]
    cues = [
        {"id": "hot", "asset": "AH", "lang": "zho",
         "start": HOT_CUE[0], "end": HOT_CUE[1], "text": "hot cue"},
        {"id": "low", "asset": "AL", "lang": "zho",
         "start": LOW_CUE[0], "end": LOW_CUE[1], "text": "low cue"},
    ]
    # scattered fixed cues away from the hot window
    for i in range(8):
        s = 50 + i * 100
        cues.append({"id": "ah-x%d" % i, "asset": "AH", "lang": "zho",
                     "start": s, "end": s + 10, "text": "ah %d" % i})
        cues.append({"id": "al-x%d" % i, "asset": "AL", "lang": "zho",
                     "start": s + 20, "end": s + 30, "text": "al %d" % i})
    # irrelevant programs: own asset + sequence each, long sparse
    # timelines that the correction never touches
    for i in range(n_programs):
        a, s, p = "A%d" % i, "S%d" % i, "P%d" % (i + 1)
        assets[a] = frames
        seqs[s] = frames
        seqs[p] = frames
        clips.append({"id": "%s-a" % s, "seq": s, "track": 0, "start": 0,
                      "end": frames, "kind": "asset", "ref": a,
                      "base": frames - 1, "p": -1, "q": 1})
        clips.append({"id": "%s-s" % p, "seq": p, "track": 0, "start": 0,
                      "end": frames, "kind": "seq", "ref": s, "base": 0,
                      "p": 2, "q": 3})
        coverage.append({"asset": a, "lang": "zho", "start": 0,
                         "end": frames})
        step = frames // 9
        for j in range(8):
            cs = (j + 1) * step
            cues.append({"id": "%s-q%d" % (a, j), "asset": a,
                         "lang": "zho", "start": cs,
                         "end": cs + max(1, step // 4),
                         "text": "%s cue %d" % (a, j)})
    man = {"package": os.path.basename(dest), "languages": ["zho"],
           "assets": assets, "sequences": seqs}
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    for name, rows in (("clips.jsonl", clips),
                       ("coverage.jsonl", coverage), ("cues.jsonl", cues)):
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"dir": dest, "programs": ["P0"] + ["P%d" % (i + 1)
                                               for i in range(n_programs)],
            "frames": frames, "clips": len(clips), "cues": len(cues)}


def correction_order(baseline):
    """The fixed local correction: AH's window [500,503) keeps checked
    coverage only on [500,502) and drops the hot cue."""
    s, e = HOT_WINDOW
    return {"baseline": baseline, "order": "scale-hot",
            "windows": [{"asset": "AH", "lang": "zho", "start": s,
                         "end": e,
                         "coverage": [{"start": s, "end": HOT_CUE[1]}],
                         "cues": []}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--programs", type=int, default=16,
                    help="number of irrelevant programs")
    ap.add_argument("--frames", type=int, default=100_000,
                    help="frames per irrelevant timeline")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    info = make_package(args.outdir, args.programs, args.frames,
                        args.seed)
    print("%(dir)s: programs=%(programs)d frames=%(frames)d "
          "clips=%(clips)d cues=%(cues)d"
          % dict(info, programs=len(info["programs"])))


if __name__ == "__main__":
    main()
