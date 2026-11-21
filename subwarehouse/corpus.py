"""Corpus export: cut training/validation subtitle samples from finished
film windows, with per-source-frame authorization and cross-batch
train/validation grouping.

The archive team hands the retrieval-model team batches of subtitle
samples.  A broadcastable film does not imply every source frame of the
interview is cleared for this internal use, and the same interview may be
reused by different programmes under different local names.  This command
reads existing ``build`` libraries (never builds libraries, never edits
clips, never dispatches subtitles) and produces, for one batch:

* the delivered training and validation samples (winning cues of every
  requested language inside each window, window-relative, verbatim text);
* the rejected windows with locatable reasons (no subtitle text);
* the quarantined windows plus a machine-readable connectivity proof;
* a registry file that the next batch can consume directly.

Inputs (all machine-readable, see the README section for the schemas)
---------------------------------------------------------------------

* ``--libraries``: JSON object ``{"libraries": [{"alias", "path",
  "logical_version"}]}``.  Every listed library is opened read-only and
  its ``logical_version`` must match the expectation.  Relative paths
  resolve against the libraries file's directory.
* ``--samples``: JSONL, one ``{"id", "library", "start", "end",
  "languages"}`` per line.  Ids are unique within the batch and must not
  appear in the previous registry; windows are half-open film frames;
  the language set is non-empty and declared in the library.
* ``--mapping``: JSONL ``{"library", "asset", "key"}`` mapping a local
  asset of one library to the archive team's source-material key.  Two
  libraries' assets are the same material only through this table --
  equal local asset ids never imply identity.
* ``--grants``: JSONL ``{"key", "lang", "start", "end"}``: source-frame
  intervals of one (source key, language) allowed into the corpus.
  Intervals may overlap; the union is what counts.
* ``--registry`` (optional): the previous batch's registry file.  It is
  self-contained (sample ids, sides, relation keys), so the old
  libraries may be gone.

Any invalid input (duplicate sample id, conflicting mapping, illegal
interval, library version mismatch, window outside the film, undeclared
language) refuses the whole batch: nothing is published.

Rejection (the whole sample, never a partial record)
----------------------------------------------------

A sample is rejected when

1. any requested language has an ``unknown`` (pending) frame inside the
   window -- the reason names the language and the film interval;
2. no language has any cue inside the window (``no_cues``);
3. a cue's ``(library, asset)`` has no mapping (``unmapped_asset``);
4. any frame of a winning cue samples a source frame outside that
   language's grant union (``unauthorized``) -- the reason names one
   offending pair of film frame and source frame.

Confirmed-blank frames (``blank``/``silence``) need no grant.  Only
actually sampled source frames are checked: a reversed clip that skips
an unauthorized source frame is fine, and nested roundings are lifted
level by level, never folded into one rate.  The check never iterates
frames: the sampled source frames of one cue segment lie inside a
bounding box, the grant-union gaps inside the box are lifted back to
film frames in closed form (one interval per level), and only a gap
with a non-empty preimage is a violation.  Work is proportional to the
grant intervals touching the box, not to the window's frame count.

Grouping and sides
------------------

Delivered samples plus every historical registry member form one graph:
two samples are connected when they share a source-material key (from
any actually delivered cue of any language) or the same non-empty
normalised cue text of the same language (Unicode NFC, whitespace runs
collapsed to one space, ends stripped; case and punctuation kept; the
two key kinds live in different namespaces).  Connectivity is
transitive.  A component with historical members on exactly one side
sends all new members to that side; a component without history is
assigned by hashing its lexicographically smallest sample id with the
documented seed (no ratio balancing); a component bridging historical
train and validation members has all its new members quarantined --
history is inherited unchanged and quarantined samples are not added to
the new registry.  Train, validation, rejected and quarantined are
mutually exclusive: every batch sample id has exactly one destination.

For every quarantined component one shared evidence graph is emitted:
key-labelled edges spanning the component, so any quarantined new
member can be walked to historical samples of both sides.  The graph is
stored once per component, not copied per member.

Everything larger than a per-sample buffer lives in a staging SQLite
file (candidate cues, relation keys, merge edges, grant unions); the
registry is streamed in and out.  The output directory appears only
complete: all files are written to a temporary sibling directory that is
renamed at the end, and any failure removes it.
"""

import hashlib
import json
import os
import resource
import secrets
import shutil
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone

from . import engine as eng
from .builder import _budget, _connect
from .errors import CorpusError
from .library import open_library
from .model import check_id, check_lang, is_int

FORMAT_REGISTRY = "subwarehouse-corpus-registry/1"
FORMAT_BATCH = "subwarehouse-corpus-batch/1"
GENERATOR = "subwarehouse corpus 1.0"

# Documented side-assignment rule for components without history:
# sha256(SEED + b"\n" + smallest sample id of the component, UTF-8);
# first digest byte even -> "train", odd -> "validation".
SEED = b"subwarehouse-corpus/1"

REGISTRY_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE members(
  sid TEXT PRIMARY KEY,
  side TEXT NOT NULL,
  comp TEXT NOT NULL);
CREATE TABLE keys(
  comp TEXT NOT NULL, kind TEXT NOT NULL, klang TEXT NOT NULL,
  kbody TEXT NOT NULL,
  PRIMARY KEY(comp, kind, klang, kbody));
CREATE INDEX idx_members_comp ON members(comp);
"""

STAGING_SCHEMA = """
CREATE TABLE cues(
  sid TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  text TEXT NOT NULL, cue TEXT NOT NULL, asset TEXT NOT NULL,
  skey TEXT, path TEXT NOT NULL);
CREATE INDEX idx_cues ON cues(sid, lang, start);
CREATE TABLE rel(
  kind TEXT NOT NULL, klang TEXT NOT NULL, kbody TEXT NOT NULL,
  node TEXT NOT NULL);
CREATE INDEX idx_rel ON rel(kind, klang, kbody, node);
CREATE INDEX idx_rel_node ON rel(node);
CREATE TABLE grants(
  key TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL);
CREATE INDEX idx_grants ON grants(key, lang, start);
CREATE TABLE gunion(
  key TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL);
CREATE INDEX idx_gunion ON gunion(key, lang, start);
CREATE TABLE edges(
  a TEXT NOT NULL, b TEXT NOT NULL,
  kind TEXT NOT NULL, klang TEXT NOT NULL, kbody TEXT NOT NULL);
CREATE TABLE edge2(
  root TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
  kind TEXT NOT NULL, klang TEXT NOT NULL, kbody TEXT NOT NULL);
CREATE INDEX idx_edge2 ON edge2(root, a, b, kind, klang, kbody);
CREATE TABLE dest(
  sid TEXT PRIMARY KEY, destiny TEXT NOT NULL,
  comp TEXT NOT NULL, gid TEXT NOT NULL);
"""

TRAIN = "train"
VALIDATION = "validation"
REJECTED = "rejected"
QUARANTINE = "quarantine"


def new_stats():
    return {
        "samples": 0,
        "grants": 0,
        "read": {"segments": 0, "grants": 0, "registry_members": 0,
                 "registry_keys": 0, "rel": 0, "cues": 0, "dest": 0,
                 "edges": 0},
        "written": {"cues": 0, "rel": 0, "edges": 0, "gunion": 0,
                    "grants": 0, "dest": 0, "registry_members": 0,
                    "registry_keys": 0},
        "grouping": {"keys": 0, "nodes": 0, "merges": 0,
                     "components": 0, "quarantined_components": 0},
        "counts": {TRAIN: 0, VALIDATION: 0, REJECTED: 0, QUARANTINE: 0},
    }


def _fetch(cur, batch, stats, key):
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        stats["read"][key] += len(rows)
        for r in rows:
            yield r


class _Writer(object):
    def __init__(self, conn, sql, batch, stats, key):
        self.conn = conn
        self.sql = sql
        self.batch = batch
        self.stats = stats
        self.key = key
        self.buf = []

    def write(self, row):
        self.buf.append(row)
        if len(self.buf) >= self.batch:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(self.sql, self.buf)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.stats["written"][self.key] += len(self.buf)
        self.buf = []


# ---------------------------------------------------------------------------
# Input parsing and validation
# ---------------------------------------------------------------------------

def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except OSError as e:
        raise CorpusError("cannot read %s %s: %s" % (what, path, e))
    except ValueError as e:
        raise CorpusError("%s %s is not valid JSON: %s" % (what, path, e))
    return obj


def _iter_jsonl(path, what):
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as e:
        raise CorpusError("cannot read %s %s: %s" % (what, path, e))
    with fh:
        for n, raw in enumerate(fh, 1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise CorpusError("%s:%d: invalid JSON: %s"
                                  % (what, n, e.msg))
            if not isinstance(obj, dict):
                raise CorpusError("%s:%d: record must be a JSON object"
                                  % (what, n))
            yield n, obj


def _check_keys(obj, allowed, required, what, line):
    extra = set(obj) - allowed
    if extra:
        raise CorpusError("%s:%s: unexpected fields: %s"
                          % (what, line, ", ".join(sorted(extra))))
    missing = required - set(obj)
    if missing:
        raise CorpusError("%s:%s: missing fields: %s"
                          % (what, line, ", ".join(sorted(missing))))


def _interval(obj, what, line, frames=None, frame_what=None):
    s, e = obj["start"], obj["end"]
    if not is_int(s) or not is_int(e):
        raise CorpusError("%s:%s: start/end must be integers"
                          % (what, line))
    if not (0 <= s < e):
        raise CorpusError("%s:%s: empty or negative interval [%s,%s) "
                          "(intervals are [start,end), start < end)"
                          % (what, line, s, e))
    if frames is not None and e > frames:
        raise CorpusError("%s:%s: interval [%d,%d) exceeds %s"
                          % (what, line, s, e, frame_what))
    return s, e


def load_libraries(path):
    """Open every listed library read-only and check its logical
    version.  Returns {alias: (conn, meta, path)}; the caller closes
    the connections."""
    obj = _load_json(path, "libraries file")
    if not isinstance(obj, dict) or set(obj) != {"libraries"}:
        raise CorpusError("libraries file must be a JSON object with "
                          "exactly the key 'libraries'")
    entries = obj["libraries"]
    if not isinstance(entries, list):
        raise CorpusError("libraries file: 'libraries' must be an array")
    base = os.path.dirname(os.path.abspath(path))
    libs = {}
    try:
        for i, ent in enumerate(entries):
            what = "libraries file entry %d" % i
            if not isinstance(ent, dict):
                raise CorpusError("%s: must be an object" % what)
            if set(ent) != {"alias", "path", "logical_version"}:
                raise CorpusError("%s: must have exactly 'alias', 'path' "
                                  "and 'logical_version'" % what)
            try:
                alias = check_id(ent["alias"])
            except ValueError as e:
                raise CorpusError("%s: alias: %s" % (what, e))
            if alias in libs:
                raise CorpusError("libraries file: duplicate alias %r"
                                  % alias)
            lp, lv = ent["path"], ent["logical_version"]
            if not isinstance(lp, str) or not lp \
                    or any(ord(c) < 0x20 for c in lp):
                raise CorpusError("%s: path must be a non-empty string "
                                  "without control characters" % what)
            if not isinstance(lv, str) or not lv:
                raise CorpusError("%s: logical_version must be a "
                                  "non-empty string" % what)
            full = lp if os.path.isabs(lp) else os.path.join(base, lp)
            try:
                conn, meta = open_library(full)
            except Exception as e:
                raise CorpusError("library %r (%s): %s" % (alias, lp, e))
            if meta["logical_version"] != lv:
                conn.close()
                raise CorpusError(
                    "library %r (%s): logical_version mismatch: the "
                    "libraries file expects %s but the library is %s"
                    % (alias, lp, lv, meta["logical_version"]))
            libs[alias] = (conn, meta, full)
    except Exception:
        for conn, _meta, _p in libs.values():
            conn.close()
        raise
    return libs


def load_mapping(path, aliases):
    """(library, asset) -> source key.  Any repeated (library, asset)
    pair is an input error, whether the keys conflict or not."""
    mapping = {}
    for line, obj in _iter_jsonl(path, "mapping"):
        _check_keys(obj, {"library", "asset", "key"},
                    {"library", "asset", "key"}, "mapping", line)
        lib, asset, key = obj["library"], obj["asset"], obj["key"]
        try:
            check_id(asset)
            check_id(key)
        except ValueError as e:
            raise CorpusError("mapping:%d: %s" % (line, e))
        if lib not in aliases:
            raise CorpusError("mapping:%d: library alias %r is not "
                              "declared in the libraries file"
                              % (line, lib))
        pair = (lib, asset)
        if pair in mapping:
            if mapping[pair] == key:
                raise CorpusError(
                    "mapping:%d: duplicate mapping for (%r, %r)"
                    % (line, lib, asset))
            raise CorpusError(
                "mapping:%d: conflicting mappings for (%r, %r): %r vs "
                "%r" % (line, lib, asset, mapping[pair], key))
        mapping[pair] = key
    return mapping


def load_grants(path, stg, stats, batch):
    """Stream the grant table into the staging database (validated)."""
    writer = _Writer(stg, "INSERT INTO grants VALUES(?,?,?,?)", batch,
                     stats, "grants")
    n = 0
    for line, obj in _iter_jsonl(path, "grants"):
        _check_keys(obj, {"key", "lang", "start", "end"},
                    {"key", "lang", "start", "end"}, "grants", line)
        try:
            key = check_id(obj["key"])
            lang = check_lang(obj["lang"])
        except ValueError as e:
            raise CorpusError("grants:%d: %s" % (line, e))
        s, e = _interval(obj, "grants", line)
        writer.write((key, lang, s, e))
        n += 1
    writer.flush()
    stats["grants"] = n


def load_samples(path, libs, registry_conn):
    """Validate the sample sheet.  Returns samples sorted by id as
    (sid, alias, ws, we, languages).  `registry_conn` is the previous
    registry (read-only) or None; a sample id already delivered by an
    earlier batch is an input error."""
    samples = {}
    for line, obj in _iter_jsonl(path, "samples"):
        _check_keys(obj, {"id", "library", "start", "end", "languages"},
                    {"id", "library", "start", "end", "languages"},
                    "samples", line)
        try:
            sid = check_id(obj["id"])
        except ValueError as e:
            raise CorpusError("samples:%d: id: %s" % (line, e))
        if sid in samples:
            raise CorpusError("samples:%d: duplicate sample id %r"
                              % (line, sid), {"sample": sid})
        if registry_conn is not None and registry_conn.execute(
                "SELECT 1 FROM members WHERE sid = ?",
                (sid,)).fetchone() is not None:
            raise CorpusError(
                "samples:%d: sample id %r was already delivered by an "
                "earlier batch (present in the previous registry)"
                % (line, sid), {"sample": sid})
        alias = obj["library"]
        if alias not in libs:
            raise CorpusError("samples:%d: library alias %r is not "
                              "declared in the libraries file"
                              % (line, alias), {"sample": sid})
        root_frames = int(libs[alias][1]["root_frames"])
        s, e = _interval(obj, "samples", line, root_frames,
                         "film length %d of library %r"
                         % (root_frames, alias))
        langs = obj["languages"]
        if not isinstance(langs, list) or not langs:
            raise CorpusError("samples:%d: 'languages' must be a "
                              "non-empty array" % line, {"sample": sid})
        declared = set(json.loads(libs[alias][1]["languages"]))
        seen = set()
        for lg in langs:
            try:
                check_lang(lg)
            except ValueError as ex:
                raise CorpusError("samples:%d: language: %s" % (line, ex),
                                  {"sample": sid})
            if lg in seen:
                raise CorpusError("samples:%d: duplicate language %r"
                                  % (line, lg), {"sample": sid})
            seen.add(lg)
            if lg not in declared:
                raise CorpusError(
                    "samples:%d: language %r is not declared in library "
                    "%r" % (line, lg, alias), {"sample": sid})
        samples[sid] = (sid, alias, s, e, sorted(seen))
    return [samples[k] for k in sorted(samples)]


def open_registry(path):
    """Open a registry read-only and validate its format."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as e:
        raise CorpusError("cannot open registry %s: %s" % (path, e))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    except sqlite3.Error as e:
        conn.close()
        raise CorpusError("%s is not a corpus registry: %s" % (path, e))
    if rows.get("format") != FORMAT_REGISTRY:
        conn.close()
        raise CorpusError("%s: unsupported registry format %r"
                          % (path, rows.get("format")))
    return conn


def load_registry(conn, stg, stats, batch):
    """Stream the previous registry into the grouping seed: historical
    relation keys are staged into `rel` with the component id as node.
    Returns {comp: side}.  Every recorded component is single-sided and
    every component id is itself a delivered sample id (so a new sample
    id can never collide with a historical component node); violations
    are input errors."""
    comp_side = {}
    cur = conn.execute("SELECT sid, side, comp FROM members")
    for sid, side, comp in _fetch(cur, batch, stats, "registry_members"):
        if side not in (TRAIN, VALIDATION):
            raise CorpusError("registry: member %r has invalid side %r"
                              % (sid, side))
        if not comp:
            raise CorpusError("registry: member %r has an empty "
                              "component id" % sid)
        prev = comp_side.get(comp)
        if prev is not None and prev != side:
            raise CorpusError(
                "registry: component %r mixes sides (%r and %r); "
                "registries never record a two-sided component"
                % (comp, prev, side))
        comp_side[comp] = side
    for comp in comp_side:
        if conn.execute("SELECT 1 FROM members WHERE sid = ?",
                        (comp,)).fetchone() is None:
            raise CorpusError(
                "registry: component id %r is not itself a delivered "
                "sample id; registries name components after their "
                "smallest member" % comp)
    writer = _Writer(stg, "INSERT INTO rel VALUES(?,?,?,?)", batch,
                     stats, "rel")
    cur = conn.execute("SELECT comp, kind, klang, kbody FROM keys")
    for comp, kind, klang, kbody in _fetch(cur, batch, stats,
                                           "registry_keys"):
        if comp not in comp_side:
            raise CorpusError("registry: key of unknown component %r"
                              % comp)
        if kind not in ("src", "txt"):
            raise CorpusError("registry: invalid key kind %r" % kind)
        writer.write((kind, klang, kbody, comp))
    writer.flush()
    return comp_side


# ---------------------------------------------------------------------------
# Per-frame source mapping and the authorization check
# ---------------------------------------------------------------------------

def source_frame(path, frame):
    """The source frame one film frame actually samples, walking the
    stored path level by level (never folding nested roundings)."""
    cur = frame
    for lv in path:
        k = cur - lv["start"]
        cur = lv["base"] + (k * lv["p"]) // lv["q"]
    return cur


def lift_source_interval(path, lo, hi):
    """Film-frame interval whose samples fall in source [lo, hi), lifted
    level by level from the asset back to the root; None if empty."""
    iv = (lo, hi)
    for lv in reversed(path):
        iv = eng.lift_interval(iv[0], iv[1], lv["base"], lv["p"],
                               lv["q"], lv["end"] - lv["start"])
        if iv is None:
            return None
        iv = (lv["start"] + iv[0], lv["start"] + iv[1])
    return iv


def normalize_text(text):
    """Grouping identity of a cue's text: Unicode NFC, whitespace runs
    collapsed to one space, ends stripped.  Case and punctuation kept."""
    return " ".join(unicodedata.normalize("NFC", text).split())


class Authorizer(object):
    """Grant-union cache plus the closed-form authorization check.

    The union of each needed (key, lang) is materialised once into the
    staging table `gunion`; each cue then reads only the grant intervals
    overlapping its sampled bounding box, never the frames themselves.
    """

    def __init__(self, stg, stats, batch):
        self.stg = stg
        self.stats = stats
        self.batch = batch
        self.done = set()

    def _ensure_union(self, key, lang):
        if (key, lang) in self.done:
            return
        cur = self.stg.execute(
            "SELECT start, \"end\" FROM grants WHERE key = ? AND "
            "lang = ? ORDER BY start", (key, lang))
        writer = _Writer(self.stg,
                         "INSERT INTO gunion VALUES(?,?,?,?)",
                         self.batch, self.stats, "gunion")
        for s, e in eng._union(_fetch(cur, self.batch, self.stats,
                                      "grants")):
            writer.write((key, lang, s, e))
        writer.flush()
        self.done.add((key, lang))

    def _overlapping(self, key, lang, lo, hi):
        """Grant-union intervals of (key, lang) overlapping [lo, hi)."""
        self._ensure_union(key, lang)
        cur = self.stg.execute(
            "SELECT start, \"end\" FROM gunion WHERE key = ? AND "
            "lang = ? AND start < ? AND \"end\" > ? ORDER BY start",
            (key, lang, hi, lo))
        return list(_fetch(cur, self.batch, self.stats, "grants"))

    def check(self, key, lang, path, cs, ce):
        """Verify every source frame sampled by film frames [cs, ce) is
        granted for (key, lang).  Returns None, or a violation dict with
        the first offending film frame and its source frame."""
        lo = min(source_frame(path, cs), source_frame(path, ce - 1))
        hi = max(source_frame(path, cs), source_frame(path, ce - 1)) + 1
        grants = self._overlapping(key, lang, lo, hi)
        # Uncovered gaps of the bounding box [lo, hi): only a gap whose
        # preimage is non-empty is an actual violation (skipped source
        # frames are never sampled and never checked).
        best = None
        pos = lo
        for gs, ge in grants + [(hi, hi)]:
            if gs > pos:
                iv = lift_source_interval(path, pos, min(gs, hi))
                if iv is not None:
                    s = max(iv[0], cs)
                    if s < min(iv[1], ce) \
                            and (best is None or s < best):
                        best = s
            if ge > pos:
                pos = ge
            if pos >= hi:
                break
        if best is None:
            return None
        return {"frame": best, "source_frame": source_frame(path, best)}

    def proof(self, key, lang, path, cs, ce, ws):
        """Window-relative proof pieces for a delivered cue: the cue's
        film range partitioned by the grant-union interval authorising
        each piece (the cue text itself is never split)."""
        lo = min(source_frame(path, cs), source_frame(path, ce - 1))
        hi = max(source_frame(path, cs), source_frame(path, ce - 1)) + 1
        pieces = []
        for gs, ge in self._overlapping(key, lang, lo, hi):
            iv = lift_source_interval(path, gs, ge)
            if iv is None:
                continue
            s, e = max(iv[0], cs), min(iv[1], ce)
            if s >= e:
                continue
            flo = min(source_frame(path, s), source_frame(path, e - 1))
            fhi = max(source_frame(path, s), source_frame(path, e - 1))
            pieces.append({"window": [s - ws, e - ws],
                           "source_frames": [flo, fhi + 1],
                           "grant": [gs, ge]})
        pieces.sort(key=lambda p: p["window"][0])
        return pieces


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_sample(sample, libs, mapping, stg, auth, stats, batch):
    """Cut one sample's winning cues.  Returns (cue_count, reason):
    cue_count > 0 and reason None when the sample is deliverable;
    otherwise reason is a locatable rejection object.  Candidate cues
    are staged under the sample id and removed again on rejection, so a
    rejected sample never leaks text into later stages."""
    sid, alias, ws, we, langs = sample
    conn, meta, _path = libs[alias]
    writer = _Writer(stg, "INSERT INTO cues VALUES(?,?,?,?,?,?,?,?,?)",
                     batch, stats, "cues")
    n_cues = 0
    unknown = None
    for lang in langs:
        cur = conn.execute(
            "SELECT start, \"end\", state, text, cue_id, asset_id, "
            "path FROM segments WHERE lang = ? AND start < ? AND "
            "\"end\" > ? ORDER BY start", (lang, we, ws))
        for s0, e0, state, text, cue_id, asset_id, path_json in \
                _fetch(cur, batch, stats, "segments"):
            if state == "unknown":
                s, e = max(s0, ws), min(e0, we)
                unknown = (lang, s, e)
                break
            if state != "cue":
                continue        # blank and silence need no grant
            key = mapping.get((alias, asset_id))
            writer.write((sid, lang, max(s0, ws), min(e0, we), text,
                          cue_id, asset_id, key, path_json))
            n_cues += 1
        if unknown is not None:
            break
    writer.flush()

    def reject(reason):
        stg.execute("DELETE FROM cues WHERE sid = ?", (sid,))
        return 0, reason

    if unknown is not None:
        lang, s, e = unknown
        return reject({"reason": "unknown", "lang": lang,
                       "interval": [s, e],
                       "window_interval": [s - ws, e - ws]})
    if n_cues == 0:
        return reject({"reason": "no_cues"})
    cur = stg.execute(
        "SELECT lang, start, \"end\", asset FROM cues "
        "WHERE sid = ? AND skey IS NULL ORDER BY lang, start LIMIT 1",
        (sid,))
    row = cur.fetchone()
    if row is not None:
        lang, s, e, asset = row
        stats["read"]["cues"] += 1
        return reject({"reason": "unmapped_asset", "lang": lang,
                       "asset": asset, "interval": [s, e],
                       "window_interval": [s - ws, e - ws]})
    cur = stg.execute(
        "SELECT lang, start, \"end\", skey, path FROM cues "
        "WHERE sid = ? ORDER BY lang, start", (sid,))
    for lang, s, e, key, path_json in _fetch(cur, batch, stats, "cues"):
        path = json.loads(path_json)
        bad = auth.check(key, lang, path, s, e)
        if bad is not None:
            return reject({"reason": "unauthorized", "lang": lang,
                           "key": key, "frame": bad["frame"],
                           "window_frame": bad["frame"] - ws,
                           "source_frame": bad["source_frame"]})
    return n_cues, None


def stage_keys(sid, stg, stats, batch):
    """Relation keys of one delivered sample: every distinct source key
    of its actually delivered cues (all languages), plus every non-empty
    normalised cue text per language."""
    writer = _Writer(stg, "INSERT INTO rel VALUES(?,?,?,?)", batch,
                     stats, "rel")
    cur = stg.execute("SELECT DISTINCT skey FROM cues WHERE sid = ?",
                      (sid,))
    for (skey,) in _fetch(cur, batch, stats, "cues"):
        writer.write(("src", "", skey, sid))
    cur = stg.execute("SELECT DISTINCT lang, text FROM cues "
                      "WHERE sid = ?", (sid,))
    for lang, text in _fetch(cur, batch, stats, "cues"):
        norm = normalize_text(text)
        if norm:
            writer.write(("txt", lang, norm, sid))
    writer.flush()


# ---------------------------------------------------------------------------
# Grouping: union-find over new sample ids and historical components
# ---------------------------------------------------------------------------

class UnionFind(object):
    """In-memory union-find over node ids (new sample ids and historical
    component ids).  Only the parent pointer is kept per node; the
    aggregates the side rules need (smallest node id, smallest historical
    component id, bitmask of historical sides train=1/validation=2) live
    on roots alone, so a component costs one entry per member plus one
    root record."""

    def __init__(self):
        self.parent = {}
        self.rank = {}       # root -> rank
        self.min_all = {}    # root -> smallest node id
        self.min_hist = {}   # root -> smallest historical comp or None
        self.sides = {}      # root -> bitmask of historical sides

    def add(self, node, hist_side=None):
        if node in self.parent:
            return
        self.parent[node] = node
        self.rank[node] = 0
        self.min_all[node] = node
        self.min_hist[node] = node if hist_side else None
        self.sides[node] = {TRAIN: 1, VALIDATION: 2}.get(hist_side, 0)

    def find(self, node):
        parent = self.parent
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, a, b):
        """Merge two nodes; returns True when two trees were merged."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        if self.min_all[rb] < self.min_all[ra]:
            self.min_all[ra] = self.min_all[rb]
        ha, hb = self.min_hist[ra], self.min_hist[rb]
        if ha is None or (hb is not None and hb < ha):
            ha = hb
        self.min_hist[ra] = ha
        self.sides[ra] |= self.sides[rb]
        del self.rank[rb], self.min_all[rb], self.min_hist[rb], \
            self.sides[rb]
        return True


def group(uf, stg, stats, batch):
    """Walk the relation table in sorted key order, unioning the members
    of each key and recording one labelled edge per actual merge.  The
    recorded edges form a spanning forest of the components and are the
    quarantine evidence."""
    writer = _Writer(stg, "INSERT INTO edges VALUES(?,?,?,?,?)", batch,
                     stats, "edges")
    cur = stg.execute("SELECT kind, klang, kbody, node FROM rel "
                      "ORDER BY kind, klang, kbody, node")
    anchor = None
    prev_key = None
    n_keys = 0
    for kind, klang, kbody, node in _fetch(cur, batch, stats, "rel"):
        key = (kind, klang, kbody)
        if key != prev_key:
            prev_key = key
            anchor = node
            n_keys += 1
            continue
        if uf.union(anchor, node):
            writer.write((anchor, node, kind, klang, kbody))
            stats["grouping"]["merges"] += 1
    writer.flush()
    stats["grouping"]["keys"] = n_keys


def side_for(min_sid):
    """Documented seed rule for components without history."""
    h = hashlib.sha256(SEED + b"\n" + min_sid.encode("utf-8"))
    return TRAIN if h.digest()[0] % 2 == 0 else VALIDATION


def _key_obj(kind, klang, kbody):
    if kind == "src":
        return {"kind": "src", "key": kbody}
    return {"kind": "txt", "lang": klang, "text": kbody}


# ---------------------------------------------------------------------------
# The export driver
# ---------------------------------------------------------------------------

def export_corpus(libraries_path, samples_path, mapping_path,
                  grants_path, registry_path, out_dir,
                  mem_budget_mb=256):
    """Run one corpus-export batch.  Returns (summary, stats).  Raises
    CorpusError (invalid input) or OSError (write failure); in either
    case nothing is published at out_dir."""
    out_dir = os.path.abspath(out_dir)
    if os.path.lexists(out_dir):
        raise CorpusError("output directory %s already exists; exports "
                          "never overwrite" % out_dir)
    cache_kb, batch = _budget(mem_budget_mb)
    stats = new_stats()
    started = time.monotonic()
    token = "%d-%s" % (os.getpid(), secrets.token_hex(4))
    tmp_dir = "%s.corpus-%s.tmp" % (out_dir, token)
    tmp_stg = os.path.join(tmp_dir, ".staging.sqlite")

    libs = load_libraries(libraries_path)
    stg = None
    try:
        mapping = load_mapping(mapping_path, set(libs))
        os.makedirs(tmp_dir)
        stg = _connect(tmp_stg, cache_kb)
        stg.executescript(STAGING_SCHEMA)
        load_grants(grants_path, stg, stats, batch)

        uf = UnionFind()
        comp_side = {}
        reg = None
        try:
            if registry_path is not None:
                reg = open_registry(registry_path)
                comp_side = load_registry(reg, stg, stats, batch)
            for comp, side in comp_side.items():
                uf.add(comp, hist_side=side)
            samples = load_samples(samples_path, libs, reg)
        finally:
            if reg is not None:
                reg.close()
        stats["samples"] = len(samples)
        sample_map = {}
        for sid, alias, ws, we, langs in samples:
            uf.add(sid)
            sample_map[sid] = (alias, ws, we, langs)

        # -- extraction: stage delivered cues, stream rejections --------
        auth = Authorizer(stg, stats, batch)
        dest_writer = _Writer(stg, "INSERT INTO dest VALUES(?,?,?,?)",
                              batch, stats, "dest")
        rejected_ids = set()
        rej_path = os.path.join(tmp_dir, "rejected.jsonl")
        with open(rej_path, "w", encoding="utf-8") as rej:
            for sample in samples:
                sid = sample[0]
                _n, reason = extract_sample(sample, libs, mapping, stg,
                                            auth, stats, batch)
                if reason is None:
                    stage_keys(sid, stg, stats, batch)
                    continue
                _sid, alias, ws, we, langs = sample
                rejected_ids.add(sid)
                stats["counts"][REJECTED] += 1
                dest_writer.write((sid, REJECTED, "", ""))
                rej.write(json.dumps({
                    "id": sid, "library": alias, "window": [ws, we],
                    "languages": list(langs), "reason": reason,
                }, ensure_ascii=False) + "\n")

        # -- grouping ----------------------------------------------------
        group(uf, stg, stats, batch)
        stats["grouping"]["nodes"] = len(uf.parent)
        root_hist = {}
        for comp, side in comp_side.items():
            root_hist.setdefault(uf.find(comp), {}).setdefault(
                side, []).append(comp)

        roots = set()
        for sid, _alias, _ws, _we, _langs in samples:
            if sid in rejected_ids:
                continue
            root = uf.find(sid)
            roots.add(root)
            sides = uf.sides[root]
            if sides == 3:
                destiny, comp, gid = QUARANTINE, "", uf.min_all[root]
            elif sides in (1, 2):
                destiny = TRAIN if sides == 1 else VALIDATION
                comp, gid = uf.min_hist[root] or uf.min_all[root], ""
            else:
                destiny = side_for(uf.min_all[root])
                comp, gid = uf.min_all[root], ""
            stats["counts"][destiny] += 1
            dest_writer.write((sid, destiny, comp, gid))
        dest_writer.flush()
        stats["grouping"]["components"] = len(roots)
        qroots = sorted((r for r in roots if uf.sides[r] == 3),
                        key=lambda r: uf.min_all[r])
        stats["grouping"]["quarantined_components"] = len(qroots)

        # Tag every merge edge with its final component root (evidence).
        ewriter = _Writer(stg, "INSERT INTO edge2 VALUES(?,?,?,?,?,?)",
                          batch, stats, "edges")
        cur = stg.execute("SELECT a, b, kind, klang, kbody FROM edges")
        for a, b, kind, klang, kbody in _fetch(cur, batch, stats,
                                               "edges"):
            ewriter.write((uf.find(a), a, b, kind, klang, kbody))
        ewriter.flush()

        # -- outputs -------------------------------------------------------
        _write_delivered(tmp_dir, libs, stg, auth, sample_map, stats,
                         batch)
        _write_quarantine(tmp_dir, qroots, uf, root_hist, stg, stats,
                          batch)
        _write_registry(os.path.join(tmp_dir, "registry.sqlite"),
                        registry_path, stg, stats, batch, cache_kb)
        manifest = {
            "ok": True,
            "format": FORMAT_BATCH,
            "generator": GENERATOR,
            "created_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "seed": SEED.decode("ascii"),
            "libraries": {a: m["logical_version"]
                          for a, (_c, m, _p) in sorted(libs.items())},
            "previous_registry": registry_path,
            "samples": stats["samples"],
            "counts": dict(stats["counts"]),
            "files": ["train.jsonl", "validation.jsonl",
                      "rejected.jsonl", "quarantine.jsonl",
                      "evidence.jsonl", "registry.sqlite"],
        }
        with open(os.path.join(tmp_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write("\n")
        stg.close()
        stg = None
        for name in os.listdir(tmp_dir):
            with open(os.path.join(tmp_dir, name), "rb") as fh:
                os.fsync(fh.fileno())
        os.rename(tmp_dir, out_dir)
    finally:
        if stg is not None:
            stg.close()
        for conn, _meta, _p in libs.values():
            conn.close()
        if os.path.lexists(tmp_dir):
            shutil.rmtree(tmp_dir, True)

    stats["elapsed_sec"] = round(time.monotonic() - started, 6)
    stats["peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    stats["mem_budget_mb"] = mem_budget_mb
    summary = {
        "ok": True,
        "out": out_dir,
        "samples": stats["samples"],
        "counts": dict(stats["counts"]),
        "registry": os.path.join(out_dir, "registry.sqlite"),
    }
    return summary, stats


def _write_delivered(tmp_dir, libs, stg, auth, sample_map, stats, batch):
    """Train and validation samples with their cues, sorted by id."""
    outs = {TRAIN: open(os.path.join(tmp_dir, "train.jsonl"), "w",
                        encoding="utf-8"),
            VALIDATION: open(os.path.join(tmp_dir, "validation.jsonl"),
                             "w", encoding="utf-8")}
    try:
        cur = stg.execute(
            "SELECT sid, destiny FROM dest WHERE destiny IN "
            "('train','validation') ORDER BY sid")
        for sid, destiny in _fetch(cur, batch, stats, "dest"):
            alias, ws, we, langs = sample_map[sid]
            rows = []
            cur2 = stg.execute(
                "SELECT lang, start, \"end\", text, cue, asset, skey, "
                "path FROM cues WHERE sid = ? ORDER BY lang, start",
                (sid,))
            for lang, s, e, text, cue, asset, skey, path_json in \
                    _fetch(cur2, batch, stats, "cues"):
                path = json.loads(path_json)
                rows.append({
                    "lang": lang, "start": s - ws, "end": e - ws,
                    "text": text, "cue": cue, "asset": asset,
                    "source_key": skey, "path": path,
                    "proof": auth.proof(skey, lang, path, s, e, ws),
                })
            keys = []
            cur3 = stg.execute(
                "SELECT kind, klang, kbody FROM rel WHERE node = ? "
                "ORDER BY kind, klang, kbody", (sid,))
            for kind, klang, kbody in _fetch(cur3, batch, stats, "rel"):
                keys.append(_key_obj(kind, klang, kbody))
            row = {
                "id": sid, "library": alias,
                "logical_version": libs[alias][1]["logical_version"],
                "window": [ws, we], "languages": list(langs),
                "cues": rows, "keys": keys,
            }
            outs[destiny].write(json.dumps(row, ensure_ascii=False)
                                + "\n")
    finally:
        for fh in outs.values():
            fh.close()


def _write_quarantine(tmp_dir, qroots, uf, root_hist, stg, stats, batch):
    """Per-sample quarantine rows (id + group, sorted by id) and, once
    per component, the shared evidence graph: key-labelled spanning
    edges plus the historical samples of both sides."""
    with open(os.path.join(tmp_dir, "quarantine.jsonl"), "w",
              encoding="utf-8") as qfh, \
            open(os.path.join(tmp_dir, "evidence.jsonl"), "w",
                 encoding="utf-8") as efh:
        cur = stg.execute(
            "SELECT sid, gid FROM dest WHERE destiny = 'quarantine' "
            "ORDER BY sid")
        for sid, gid in _fetch(cur, batch, stats, "dest"):
            qfh.write(json.dumps({"id": sid, "group": gid},
                                 ensure_ascii=False) + "\n")
        for root in qroots:
            gid = uf.min_all[root]
            cur = stg.execute(
                "SELECT sid FROM dest WHERE destiny = 'quarantine' "
                "AND gid = ? ORDER BY sid", (gid,))
            members = [r[0] for r in _fetch(cur, batch, stats, "dest")]
            hist = root_hist.get(root, {})
            edges = []
            cur = stg.execute(
                "SELECT a, b, kind, klang, kbody FROM edge2 "
                "WHERE root = ? ORDER BY a, b, kind, klang, kbody",
                (root,))
            for a, b, kind, klang, kbody in _fetch(cur, batch, stats,
                                                   "edges"):
                edges.append({"a": a, "b": b,
                              "key": _key_obj(kind, klang, kbody)})
            efh.write(json.dumps({
                "group": gid,
                "members": members,
                "history": {
                    "train": sorted(hist.get(TRAIN, [])),
                    "validation": sorted(hist.get(VALIDATION, [])),
                },
                "edges": edges,
            }, ensure_ascii=False) + "\n")


def _write_registry(path, prev_registry_path, stg, stats, batch,
                    cache_kb):
    """The new registry: every historical row inherited unchanged, plus
    this batch's delivered samples and their components' keys.  Never
    contains rejected or quarantined samples."""
    conn = _connect(path, cache_kb)
    try:
        conn.executescript(REGISTRY_SCHEMA)
        meta = {
            "format": FORMAT_REGISTRY,
            "generator": GENERATOR,
            "seed": SEED.decode("ascii"),
            "created_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
        conn.execute("BEGIN")
        conn.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
        conn.execute("COMMIT")
        members = _Writer(conn, "INSERT INTO members VALUES(?,?,?)",
                          batch, stats, "registry_members")
        keys = _Writer(conn, "INSERT OR IGNORE INTO keys "
                       "VALUES(?,?,?,?)", batch, stats, "registry_keys")
        if prev_registry_path is not None:
            old = open_registry(prev_registry_path)
            try:
                cur = old.execute("SELECT sid, side, comp FROM members "
                                  "ORDER BY sid")
                for row in _fetch(cur, batch, stats, "registry_members"):
                    members.write(row)
                cur = old.execute("SELECT comp, kind, klang, kbody "
                                  "FROM keys "
                                  "ORDER BY comp, kind, klang, kbody")
                for row in _fetch(cur, batch, stats, "registry_keys"):
                    keys.write(row)
            finally:
                old.close()
        cur = stg.execute(
            "SELECT sid, destiny, comp FROM dest WHERE destiny IN "
            "('train','validation') ORDER BY sid")
        for row in _fetch(cur, batch, stats, "dest"):
            members.write(row)
        cur = stg.execute(
            "SELECT DISTINCT d.comp, r.kind, r.klang, r.kbody "
            "FROM rel r JOIN dest d ON r.node = d.sid "
            "WHERE d.destiny IN ('train','validation') "
            "ORDER BY d.comp, r.kind, r.klang, r.kbody")
        for row in _fetch(cur, batch, stats, "rel"):
            keys.write(row)
        members.flush()
        keys.flush()
        conn.close()
        conn = None
        with open(path, "rb") as fh:
            os.fsync(fh.fileno())
    finally:
        if conn is not None:
            conn.close()


def registry_info(path):
    """Summary of a registry file for operators and tests."""
    conn = open_registry(path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        members = conn.execute("SELECT COUNT(*), side FROM members "
                               "GROUP BY side").fetchall()
        comps = conn.execute("SELECT COUNT(DISTINCT comp) "
                             "FROM members").fetchone()[0]
        keys = conn.execute("SELECT COUNT(*), kind FROM keys "
                            "GROUP BY kind").fetchall()
        return {
            "ok": True,
            "registry": path,
            "format": meta.get("format"),
            "generator": meta.get("generator"),
            "created_utc": meta.get("created_utc"),
            "members": sum(n for n, _s in members),
            "sides": {s: n for n, s in members},
            "components": comps,
            "keys": sum(n for n, _k in keys),
            "keys_by_kind": {k: n for n, k in keys},
        }
    finally:
        conn.close()
