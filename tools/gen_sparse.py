"""Generate two sparse packages with identical endpoint counts but frame
spans differing by at least 10_000x (the long one reaches billions of
frames).  Only the positions scale; the record counts are identical, so
any runtime difference comes from the span, not the size of the input.

Usage: python3 tools/gen_sparse.py OUTDIR [--endpoints N]
              [--short-frames F] [--long-frames F] [--seed S]
"""

import argparse
import json
import os
import random


def make_package(dest, frames, endpoints, seed):
    rng = random.Random(seed)
    n_assets = 8
    per_asset = endpoints // n_assets
    assets = {"A%d" % i: frames for i in range(n_assets)}
    seqs = {"S": frames, "R": frames}
    man = {"package": os.path.basename(dest), "languages": ["zho", "eng"],
           "assets": assets, "sequences": seqs}

    coverage, cues = [], []
    for a in assets:
        # scattered checked-coverage intervals and cues inside them
        pos = 0
        gap = frames // (per_asset + 1)
        for i in range(per_asset):
            cov_len = max(2, gap // 3)
            s = pos + rng.randint(0, gap - cov_len - 1)
            coverage.append({"asset": a, "lang": "zho", "start": s,
                             "end": s + cov_len})
            cue_len = max(1, cov_len // 4)
            cs = s + rng.randint(0, cov_len - cue_len)
            cues.append({"id": "%s-q%d" % (a, i), "asset": a, "lang": "zho",
                         "start": cs, "end": cs + cue_len,
                         "text": "%s cue %d" % (a, i)})
            pos += gap
    # eng is declared but never handed over: no coverage at all.

    clips = []
    # S plays each asset backwards across its whole length.
    for i, a in enumerate(sorted(assets)):
        clips.append({"id": "s-%s" % a, "seq": "S", "track": i,
                      "start": 0, "end": frames, "kind": "asset",
                      "ref": a, "base": frames - 1, "p": -1, "q": 1})
    # R references S at 2/3 speed on the bottom track and blanks a few
    # windows on the top track.
    clips.append({"id": "r-s", "seq": "R", "track": 0, "start": 0,
                  "end": frames, "kind": "seq", "ref": "S", "base": 0,
                  "p": 2, "q": 3})
    for i in range(4):
        s = frames * (2 * i + 1) // 9
        clips.append({"id": "r-blank-%d" % i, "seq": "R", "track": 9,
                      "start": s, "end": s + frames // 90 + 1,
                      "kind": "blank"})

    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    for name, rows in (("clips.jsonl", clips), ("coverage.jsonl", coverage),
                       ("cues.jsonl", cues)):
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"dir": dest, "frames": frames, "clips": len(clips),
            "coverage": len(coverage), "cues": len(cues)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--endpoints", type=int, default=2000,
                    help="coverage intervals per package (cues match)")
    ap.add_argument("--short-frames", type=int, default=200_000)
    ap.add_argument("--long-frames", type=int, default=3_000_000_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    infos = []
    for name, frames in (("short", args.short_frames),
                         ("long", args.long_frames)):
        infos.append(make_package(os.path.join(args.outdir, name), frames,
                                  args.endpoints, args.seed))
    ratio = args.long_frames / args.short_frames
    for i in infos:
        print("%(dir)s: frames=%(frames)d clips=%(clips)d "
              "coverage=%(coverage)d cues=%(cues)d" % i)
    print("span ratio: %.0fx (>= 10000x required)" % ratio)
    assert ratio >= 10_000, "span ratio must be at least 10000x"


if __name__ == "__main__":
    main()
