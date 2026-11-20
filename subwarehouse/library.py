"""Library schema and read API.

A library is a self-contained SQLite database.  Auditors can answer "why
this text / why confirmed blank / what is still missing" from it alone;
the source package is never consulted by export or inspect.

Tables:
  meta(key, value)           package, root, root_frames, languages,
                             logical_version, format, generator, created_utc
  segments(lang, start, end, state, text, cue_id, asset_id, path)
                             one row per final segment; partitions
                             [0, root_frames) for every declared language;
                             path is a JSON array of levels (root first)
  sources(kind, id, file, line)
                             stable id -> original record location
                             (diagnostic; file is relative to the package)
"""

import json
import sqlite3

from .errors import LibraryError

FORMAT = "subwarehouse/1"

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE segments(
  lang TEXT NOT NULL, start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, text TEXT, cue_id TEXT, asset_id TEXT,
  path TEXT NOT NULL,
  PRIMARY KEY(lang, start)) WITHOUT ROWID;
CREATE TABLE sources(
  kind TEXT NOT NULL, id TEXT NOT NULL,
  file TEXT NOT NULL, line INTEGER NOT NULL,
  PRIMARY KEY(kind, id));
"""

SEG_COLS = "lang, start, \"end\", state, text, cue_id, asset_id, path"


def open_library(path):
    """Open read-only; raise LibraryError if it is not a valid library."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as e:
        raise LibraryError("cannot open library %s: %s" % (path, e))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    except sqlite3.Error as e:
        conn.close()
        raise LibraryError("%s is not a subwarehouse library: %s"
                           % (path, e))
    if rows.get("format") != FORMAT:
        conn.close()
        raise LibraryError("%s: unsupported library format %r"
                           % (path, rows.get("format")))
    return conn, rows


def iter_segments(conn, lang, lo, hi):
    """Stream segments of one language intersecting [lo, hi), in order."""
    cur = conn.execute(
        "SELECT %s FROM segments WHERE lang = ? AND start < ? AND \"end\" > ? "
        "ORDER BY start" % SEG_COLS, (lang, hi, lo))
    while True:
        rows = cur.fetchmany(1024)
        if not rows:
            return
        for r in rows:
            yield r


def segment_at(conn, lang, frame):
    """The single segment covering `frame` (segments partition the
    timeline, so this always finds exactly one)."""
    row = conn.execute(
        "SELECT %s FROM segments WHERE lang = ? AND start <= ? "
        "ORDER BY start DESC LIMIT 1" % SEG_COLS, (lang, frame)).fetchone()
    if row is None or row[2] <= frame:
        raise LibraryError("library does not cover frame %d of lang %s"
                           % (frame, lang))
    return row


def inspect_frame(conn, meta, lang, frame):
    """Explain one frame from the library index alone.

    Returns a dict with the final state, the owning segment, the per-level
    walk (clip id, track, actually sampled child frame) and the endpoint:
    cue id, missing (asset, lang, frame), blank clip, or silence.
    """
    languages = json.loads(meta["languages"])
    if lang not in languages:
        raise LibraryError("language %r is not declared in this library "
                           "(declared: %s)" % (lang, ", ".join(languages)))
    root_frames = int(meta["root_frames"])
    if not (0 <= frame < root_frames):
        raise LibraryError("frame %d outside [0,%d)" % (frame, root_frames))
    lang_, start, end, state, text, cue_id, asset_id, path_json = \
        segment_at(conn, lang, frame)
    path = json.loads(path_json)
    levels = []
    cur = frame
    for lv in path:
        if lv["kind"] == "blank":
            levels.append({"clip": lv["clip"], "seq": lv["seq"],
                           "track": lv["track"], "local_frame": cur,
                           "kind": "blank"})
            cur = None
            break
        k = cur - lv["start"]
        child_frame = lv["base"] + (k * lv["p"]) // lv["q"]
        levels.append({"clip": lv["clip"], "seq": lv["seq"],
                       "track": lv["track"], "local_frame": cur,
                       "kind": lv["kind"], "ref": lv["ref"],
                       "base": lv["base"], "p": lv["p"], "q": lv["q"],
                       "child_frame": child_frame})
        cur = child_frame
    result = {
        "lang": lang, "frame": frame, "state": state,
        "segment": [start, end], "levels": levels,
    }
    if state == "cue":
        result.update({"text": text, "cue": cue_id, "asset": asset_id,
                       "asset_frame": cur})
    elif state == "unknown":
        result.update({"asset": asset_id, "missing_frame": cur})
    elif state == "blank":
        result.update({"blank_clip": levels[-1]["clip"]
                       if levels else None})
    return result
