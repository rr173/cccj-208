"""The hand-over demonstration: three 4-frame shards, two targets.

Film R (12 frames) shows one asset forward; shards are four frames each.
Release 1 has subtitles in all three shards; release 2 empties shard 0
completely, leaves one frame of shard 1 unconfirmed material (unknown), and
keeps shard 2's text but splits it into two adjacent cues from different
sources, which must merge into the same playback content.
"""

import json
import os

MANIFEST = {"package": "delivery-demo", "languages": ["zho"],
            "assets": {"M": 40}, "sequences": {"R": 12}}

CLIPS = [{"id": "c", "seq": "R", "track": 0, "start": 0, "end": 12,
          "kind": "asset", "ref": "M", "base": 0, "p": 1, "q": 1}]

COVERAGE_V1 = [{"asset": "M", "lang": "zho", "start": 0, "end": 40}]

CUES_V1 = [
    {"id": "a", "asset": "M", "lang": "zho", "start": 0, "end": 1,
     "text": "字幕甲"},
    # two adjacent cues with identical text and different source ids:
    # one playback region once cut to the shard
    {"id": "b1", "asset": "M", "lang": "zho", "start": 1, "end": 2,
     "text": "重复"},
    {"id": "b2", "asset": "M", "lang": "zho", "start": 2, "end": 3,
     "text": "重复"},
    {"id": "c", "asset": "M", "lang": "zho", "start": 4, "end": 6,
     "text": "字幕乙"},
    {"id": "d", "asset": "M", "lang": "zho", "start": 6, "end": 7,
     "text": "字幕丙"},
    {"id": "e", "asset": "M", "lang": "zho", "start": 8, "end": 10,
     "text": "字幕丁"},
]

# Release 2: shard 0 is completely silent; shard 1 keeps a cue over
# frames 4..5 but frame 5 has no checked coverage (unknown); shard 2 has
# the same text as release 1 split into two cue ids.
COVERAGE_V2 = [
    {"asset": "M", "lang": "zho", "start": 0, "end": 5},
    {"asset": "M", "lang": "zho", "start": 6, "end": 40},
]

CUES_V2 = [
    {"id": "c", "asset": "M", "lang": "zho", "start": 4, "end": 5,
     "text": "字幕乙"},
    {"id": "d", "asset": "M", "lang": "zho", "start": 6, "end": 7,
     "text": "字幕丙"},
    {"id": "e1", "asset": "M", "lang": "zho", "start": 8, "end": 9,
     "text": "字幕丁"},
    {"id": "e2", "asset": "M", "lang": "zho", "start": 9, "end": 10,
     "text": "字幕丁"},
]


def packages(base):
    """Write release 1 and 2 packages under ``base``; return their paths."""
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from common import write_package
    p1 = write_package(os.path.join(base, "v1"), MANIFEST, CLIPS,
                       COVERAGE_V1, CUES_V1)
    p2 = write_package(os.path.join(base, "v2"), MANIFEST, CLIPS,
                       COVERAGE_V2, CUES_V2)
    return p1, p2
