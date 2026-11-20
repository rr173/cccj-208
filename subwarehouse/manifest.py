"""Manifest loading and validation.

The manifest is a single JSON object, ``manifest.json`` in the package
directory::

    {
      "package":   "handover-2026-11-20",        // package identifier
      "languages": ["zho", "eng"],               // declared language codes
      "assets":    {"M": 10, "N": 5},            // asset id -> frame count
      "sequences": {"S": 10, "R": 20}            // sequence id -> frame count
    }

All ids share one namespace: an asset id and a sequence id must not collide.
Frame counts are positive integers. Every asset and sequence uses the same
frame unit; all intervals are integer [start, end) on the owning object.
"""

import json
import os

from .errors import PackageError
from .model import check_frames, check_id, check_lang, canonical_json

MANIFEST_NAME = "manifest.json"
CLIPS_NAME = "clips.jsonl"
COVERAGE_NAME = "coverage.jsonl"
CUES_NAME = "cues.jsonl"

_KEYS = {"package", "languages", "assets", "sequences"}


class Manifest(object):
    def __init__(self, package, languages, assets, sequences):
        self.package = package
        self.languages = tuple(languages)      # declared languages, sorted
        self.assets = dict(assets)             # id -> frames
        self.sequences = dict(sequences)       # id -> frames

    def frames_of(self, kind, oid):
        return self.assets[oid] if kind == "asset" else self.sequences[oid]

    def canonical(self):
        return canonical_json({
            "package": self.package,
            "languages": sorted(self.languages),
            "assets": self.assets,
            "sequences": self.sequences,
        })


def _load_id_map(obj, key, fname):
    v = obj[key]
    if not isinstance(v, dict):
        raise PackageError("manifest: %r must be an object mapping ids to "
                           "frame counts" % key, file=fname)
    out = {}
    for k, frames in v.items():
        try:
            check_id(k)
            check_frames(frames)
        except ValueError as e:
            raise PackageError("manifest: %s: invalid id or frame count for "
                               "%r: %s" % (key, k, e), file=fname)
        out[k] = frames
    return out


def load_manifest(pkg_dir):
    fname = os.path.join(pkg_dir, MANIFEST_NAME)
    try:
        with open(fname, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise PackageError("cannot read manifest: %s" % e, file=fname)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise PackageError("manifest is not valid JSON: %s" % e.msg,
                           file=fname, line=e.lineno)
    if not isinstance(obj, dict):
        raise PackageError("manifest must be a JSON object", file=fname)
    extra = set(obj) - _KEYS
    if extra:
        raise PackageError("manifest: unexpected keys: %s"
                           % ", ".join(sorted(extra)), file=fname)
    missing = _KEYS - set(obj)
    if missing:
        raise PackageError("manifest: missing keys: %s"
                           % ", ".join(sorted(missing)), file=fname)
    try:
        package = check_id(obj["package"])
    except ValueError as e:
        raise PackageError("manifest: 'package': %s" % e, file=fname)
    langs = obj["languages"]
    if not isinstance(langs, list) or not langs or len(langs) > 64:
        raise PackageError("manifest: 'languages' must be a non-empty array "
                           "of at most 64 language codes", file=fname)
    seen = set()
    for lg in langs:
        try:
            check_lang(lg)
        except ValueError as e:
            raise PackageError("manifest: 'languages': %s" % e, file=fname)
        if lg in seen:
            raise PackageError("manifest: duplicate language %r" % lg,
                               file=fname)
        seen.add(lg)
    assets = _load_id_map(obj, "assets", fname)
    sequences = _load_id_map(obj, "sequences", fname)
    both = set(assets) & set(sequences)
    if both:
        raise PackageError("manifest: ids declared as both asset and "
                           "sequence: %s" % ", ".join(sorted(both)),
                           file=fname)
    return Manifest(package, sorted(seen), assets, sequences)
