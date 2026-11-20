"""Shared helpers: canonical JSON, record hashing, scalar validation."""

import hashlib
import json

ID_MAX = 200
LANG_MAX = 35
MAX_FRAMES = (1 << 63) - 1  # SQLite INTEGER upper bound


def canonical_json(obj):
    """Deterministic UTF-8 encoding of a JSON-able object."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def record_hash(logical):
    """Stable hash of one normalized record's logical content.

    File name and line number are diagnostics only and never enter the hash,
    so shuffling input rows cannot change the logical version.
    """
    return hashlib.sha256(canonical_json(logical)).hexdigest()


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_id(v):
    if (not isinstance(v, str) or not v or len(v) > ID_MAX
            or any(ord(c) < 0x20 for c in v)):
        raise ValueError(
            "expected a non-empty string of at most %d chars without "
            "control characters" % ID_MAX)
    return v


def check_lang(v):
    if (not isinstance(v, str) or not v or len(v) > LANG_MAX
            or any(ord(c) < 0x20 for c in v)):
        raise ValueError(
            "expected a non-empty language code of at most %d chars without "
            "control characters" % LANG_MAX)
    return v


def check_frames(v):
    if not is_int(v) or v < 1 or v > MAX_FRAMES:
        raise ValueError("expected a positive integer frame count")
    return v
