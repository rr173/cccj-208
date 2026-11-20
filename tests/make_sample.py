"""The rerunnable handover sample from the specification.

Material M has checked coverage [0,10) and cues A [2,5), B [7,9).
Sub-sequence S plays M backwards: [0,10) base=9 p=-1 q=1.
Root bottom track references S over [2,17): base=0 p=2 q=3.
Root top track references subtitle-less N over [4,9) (N checked only
[0,1) and [3,5)) and blanks [11,12) explicitly.

Expected root result (lang zho):
  [0,4)   silence
  [4,5)   cue B        (top track transparent at N frame 0)
  [5,7)   unknown      (N frames 1..2 not checked; blocks bottom B)
  [7,10)  silence
  [10,11) cue A
  [11,12) blank        (explicit blank clip)
  [12,14) cue A
  [14,20) silence
Root frame 12 walks to S frame 6 and M frame 3.
"""

import json
import os

MANIFEST = {
    "package": "sample-handover",
    "languages": ["zho"],
    "assets": {"M": 10, "N": 5},
    "sequences": {"S": 10, "R": 20},
}

CLIPS = [
    {"id": "s-rev", "seq": "S", "track": 0, "start": 0, "end": 10,
     "kind": "asset", "ref": "M", "base": 9, "p": -1, "q": 1},
    {"id": "r-bottom", "seq": "R", "track": 0, "start": 2, "end": 17,
     "kind": "seq", "ref": "S", "base": 0, "p": 2, "q": 3},
    {"id": "r-top-n", "seq": "R", "track": 1, "start": 4, "end": 9,
     "kind": "asset", "ref": "N", "base": 0, "p": 1, "q": 1},
    {"id": "r-blank", "seq": "R", "track": 1, "start": 11, "end": 12,
     "kind": "blank"},
]

COVERAGE = [
    {"asset": "M", "lang": "zho", "start": 0, "end": 10},
    {"asset": "N", "lang": "zho", "start": 0, "end": 1},
    {"asset": "N", "lang": "zho", "start": 3, "end": 5},
]

CUES = [
    {"id": "A", "asset": "M", "lang": "zho", "start": 2, "end": 5,
     "text": "字幕A：采访开头"},
    {"id": "B", "asset": "M", "lang": "zho", "start": 7, "end": 9,
     "text": "字幕B：采访结尾"},
]

# (start, end, state, text, cue_id, [clip ids root->leaf])
EXPECTED_SEGMENTS = [
    (0, 4, "silence", None, None, []),
    (4, 5, "cue", "字幕B：采访结尾", "B", ["r-bottom", "s-rev"]),
    (5, 7, "unknown", None, None, ["r-top-n"]),
    (7, 10, "silence", None, None, []),
    (10, 11, "cue", "字幕A：采访开头", "A", ["r-bottom", "s-rev"]),
    (11, 12, "blank", None, None, ["r-blank"]),
    (12, 14, "cue", "字幕A：采访开头", "A", ["r-bottom", "s-rev"]),
    (14, 20, "silence", None, None, []),
]


def write_sample(dest):
    os.makedirs(dest, exist_ok=True)

    def dump(name, obj):
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(
                obj, ensure_ascii=False, indent=2) + "\n")

    dump("manifest.json", MANIFEST)
    for name, rows in (("clips.jsonl", CLIPS), ("coverage.jsonl", COVERAGE),
                       ("cues.jsonl", CUES)):
        with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return dest


if __name__ == "__main__":
    import sys
    write_sample(sys.argv[1] if len(sys.argv) > 1 else "fixtures/sample")
    print("sample package written")
