"""Independent per-frame reference resolver.

This is the small, obviously-correct implementation used to validate the
interval-based engine: it resolves every output frame on its own, walking
tracks top to bottom and recursing into sub-sequences, and it can print
coverage, selection and path for a single frame.  It is O(frames) and
in-memory, so it is only used for small fixtures.
"""

import json
import os


class RefPackage(object):
    def __init__(self, manifest, clips, coverage, cues):
        self.manifest = manifest
        self.assets = manifest["assets"]
        self.seqs = manifest["sequences"]
        self.languages = manifest["languages"]
        self.clips = {}                       # seq -> {track: [clip,...]}
        for c in clips:
            self.clips.setdefault(c["seq"], {}).setdefault(
                c["track"], []).append(c)
        for tracks in self.clips.values():
            for lst in tracks.values():
                lst.sort(key=lambda c: c["start"])
        self.coverage = {}                    # (asset, lang) -> [(s,e)]
        for c in coverage:
            self.coverage.setdefault((c["asset"], c["lang"]), []).append(
                (c["start"], c["end"]))
        for k in self.coverage:
            self.coverage[k] = _merge(sorted(self.coverage[k]))
        self.cues = {}                        # (asset, lang) -> [cue]
        for c in cues:
            self.cues.setdefault((c["asset"], c["lang"]), []).append(c)
        for k in self.cues:
            self.cues[k].sort(key=lambda c: c["start"])

    @classmethod
    def load(cls, pkg_dir):
        def read(name):
            path = os.path.join(pkg_dir, name)
            if not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        with open(os.path.join(pkg_dir, "manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        return cls(manifest, read("clips.jsonl"), read("coverage.jsonl"),
                   read("cues.jsonl"))


def _merge(ivs):
    out = []
    for s, e in ivs:
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _find_clip(clips, frame):
    for c in clips:
        if c["start"] <= frame < c["end"]:
            return c
    return None


def _find_cue(pkg, asset, lang, frame):
    for c in pkg.cues.get((asset, lang), ()):
        if c["start"] <= frame < c["end"]:
            return c
    return None


def _covered(pkg, asset, lang, frame):
    for s, e in pkg.coverage.get((asset, lang), ()):
        if s <= frame < e:
            return True
    return False


def resolve(pkg, seq, lang, frame, path=()):
    """Resolve one frame of `seq`.  Returns a dict:
    state transparent|cue|blank|unknown, plus path (list of levels),
    and for cue: text/cue/asset; for unknown: asset/missing_frame.
    Each level: {"clip","track","child_frame"} (child_frame None=blank)."""
    tracks = pkg.clips.get(seq, {})
    for track in sorted(tracks, reverse=True):
        clip = _find_clip(tracks[track], frame)
        if clip is None:
            continue
        if clip["kind"] == "blank":
            return {"state": "blank",
                    "path": list(path) + [{"clip": clip["id"],
                                           "track": track,
                                           "child_frame": None}]}
        k = frame - clip["start"]
        cf = clip["base"] + (k * clip["p"]) // clip["q"]
        level = {"clip": clip["id"], "track": track, "child_frame": cf}
        if clip["kind"] == "asset":
            cue = _find_cue(pkg, clip["ref"], lang, cf)
            if cue is not None:
                return {"state": "cue", "path": list(path) + [level],
                        "text": cue["text"], "cue": cue["id"],
                        "asset": clip["ref"], "asset_frame": cf}
            if _covered(pkg, clip["ref"], lang, cf):
                continue
            return {"state": "unknown", "path": list(path) + [level],
                    "asset": clip["ref"], "missing_frame": cf}
        sub = resolve(pkg, clip["ref"], lang, cf, list(path) + [level])
        if sub["state"] == "transparent":
            continue
        return sub
    return {"state": "transparent", "path": list(path)}


def resolve_root(pkg, root, lang, frame):
    r = resolve(pkg, root, lang, frame)
    if r["state"] == "transparent":
        return {"state": "silence", "path": []}
    return r


def trace_lines(pkg, root, lang, frame):
    """Human-readable per-frame listing: coverage, selection, path."""
    r = resolve_root(pkg, root, lang, frame)
    lines = ["frame %d lang %s -> %s" % (frame, lang, r["state"])]
    for i, lv in enumerate(r["path"]):
        if lv["child_frame"] is None:
            lines.append("  level %d: clip %s (track %d) -> explicit blank"
                         % (i, lv["clip"], lv["track"]))
        else:
            lines.append("  level %d: clip %s (track %d) -> child frame %d"
                         % (i, lv["clip"], lv["track"], lv["child_frame"]))
    st = r["state"]
    if st == "cue":
        lines.append("  endpoint: asset %s cue %s text=%r"
                     % (r["asset"], r["cue"], r["text"]))
    elif st == "unknown":
        asset, f = r["asset"], r["missing_frame"]
        cov = pkg.coverage.get((asset, lang), [])
        lines.append("  missing: asset %s lang %s frame %d not covered "
                     "(checked coverage: %s)"
                     % (asset, lang, f,
                        ", ".join("[%d,%d)" % iv for iv in cov) or "none"))
    elif st == "blank":
        lines.append("  endpoint: explicit blank clip %s"
                     % r["path"][-1]["clip"])
    else:
        lines.append("  silence: transparent at root; no source")
    return lines
