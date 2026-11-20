"""Incremental delivery ledger: registered subtitle versions, dispatch
batching, receipt acknowledgement and hand-over verification.

The ledger is a self-contained SQLite file for *one finished film* with a
fixed frame count.  It never re-runs the resolution engine: the official
library produced by ``subwarehouse build`` is the source of truth for what
a registered version should show, and the ledger materialises only the
per-shard cuts it needs for later dispatches and audits.

Model
-----
A *target* is a delivery platform together with its fixed shard table:
unique shard names whose relative intervals tile ``[0, film_frames)``
without gaps or overlaps (every target may use a different table).  For a
target, each language it receives is configured at registration time.  A
*delivery unit* is ``(target, language, shard)``.

``register`` records an increasing release number for one target from a
read-only library.  The library's ``cue`` segments are cut into the
shard's relative frame interval; intervals with identical text that touch
are merged.  Playback identity is the normalised relative interval plus the
verbatim text -- never the path, diagnostic line numbers or library bytes.
``blank`` and ``silence`` yield no text.  If any frame of a shard is
``unknown`` the release has no complete content for that unit: it is
flagged and the missing film interval is recorded.  Cuts and gaps are
stored in the ledger, so the source library may be moved away afterwards.

Dispatch plans the target's *current highest release* from the confirmed
platform state, never as a wholesale resend:

* confirmed content equals wanted content            -> nothing
* wanted content needs material this release lacks   -> keep platform
  content (no row; the unit is flagged as awaiting data)
* confirmed subtitles -> wanted complete empty       -> ``clear``
* never delivered and wanted empty                   -> nothing
* anything else                                      -> ``replace`` with
  the complete new content (never old and new rows spliced together)

A unit has at most one open operation.  After a receipt closes it, the
unit is planned again from the *then* latest release.  Requests carry a
caller-supplied id and a row cap; retrying an id returns the original batch
(empty ones included), reusing it with other parameters is a conflict.
Row-limited batches follow the documented unit order
``(target, language, shard start)``.  Operations and their occupancy are
written in one transaction; two planners can never open two unconfirmed
rows on one unit (write lock plus a partial unique index).

Receipts advance confirmed state per target with a contiguous sequence
starting at 1.  Each receipt answers exactly one final operation with
``applied`` or ``rejected`` (the latter carries a reason and leaves
platform content unchanged).  Files may arrive out of order and overlap;
only the prefix ``1..first-missing`` is confirmed.  Later receipts are
cached without changing content or occupancy, and are consumed
automatically once the gap closes.  Identical repeats change nothing.  A
number mapping to a different receipt, one operation occupying two
numbers, an unknown operation or a cross-target reference rejects the
whole input; data accepted earlier stays untouched.

Every mutation is a single SQLite transaction (``BEGIN IMMEDIATE``,
``synchronous=FULL``): an interrupted process leaves either the complete
batch or none of it, never progress that reflects only part of a batch.

``verify`` reads facts at the confirmed prefix only, so later dispatches or
receipts never rewrite an old position; rejected or unconfirmed records are
never evidence.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from .errors import LedgerError, LedgerStateError, ReceiptConflict
from .library import open_library
from .model import canonical_json, check_id, check_lang, is_int

FORMAT = "subwarehouse-ledger/1"
GENERATOR = "subwarehouse delivery ledger 1.0"

DEFAULT_CACHE_MB = 32
BATCH_ROWS = 2048

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE targets(
  target TEXT PRIMARY KEY, frames INTEGER NOT NULL, root TEXT NOT NULL);
CREATE TABLE shards(
  target TEXT NOT NULL, lang TEXT NOT NULL, ord INTEGER NOT NULL,
  name TEXT NOT NULL, target_start INTEGER NOT NULL,
  target_end INTEGER NOT NULL,
  PRIMARY KEY(target, lang, ord),
  UNIQUE(target, lang, name),
  UNIQUE(target, lang, target_start));
CREATE TABLE releases(
  target TEXT NOT NULL, seq INTEGER NOT NULL,
  library TEXT NOT NULL, logical_version TEXT NOT NULL,
  release_hash TEXT NOT NULL, registered_utc TEXT NOT NULL,
  PRIMARY KEY(target, seq));
CREATE TABLE release_langs(
  target TEXT NOT NULL, seq INTEGER NOT NULL, lang TEXT NOT NULL,
  PRIMARY KEY(target, seq, lang));
CREATE TABLE release_units(
  target TEXT NOT NULL, seq INTEGER NOT NULL, lang TEXT NOT NULL,
  shard TEXT NOT NULL, items INTEGER NOT NULL,
  content_hash TEXT NOT NULL, has_gap INTEGER NOT NULL,
  PRIMARY KEY(target, seq, lang, shard));
CREATE TABLE cuts(
  target TEXT NOT NULL, seq INTEGER NOT NULL, lang TEXT NOT NULL,
  shard TEXT NOT NULL, rel_start INTEGER NOT NULL,
  rel_end INTEGER NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY(target, seq, lang, shard, rel_start));
CREATE TABLE gaps(
  target TEXT NOT NULL, seq INTEGER NOT NULL, lang TEXT NOT NULL,
  shard TEXT NOT NULL, start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  PRIMARY KEY(target, seq, lang, shard, start));
CREATE TABLE ops(
  op_id TEXT PRIMARY KEY,
  target TEXT NOT NULL, lang TEXT NOT NULL, shard TEXT NOT NULL,
  request TEXT NOT NULL, batch INTEGER NOT NULL,
  seq INTEGER NOT NULL, action TEXT NOT NULL,
  payload_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
  items INTEGER NOT NULL,
  status TEXT NOT NULL, receipt INTEGER, applied INTEGER,
  reason TEXT, created_utc TEXT NOT NULL, closed_utc TEXT);
CREATE UNIQUE INDEX ops_one_open ON ops(target, lang, shard)
  WHERE status = 'open';
CREATE INDEX ops_by_unit ON ops(target, lang, shard, receipt);
CREATE INDEX ops_by_target_receipt ON ops(target, receipt);
CREATE INDEX ops_by_request ON ops(request);
CREATE TABLE op_items(
  op_id TEXT NOT NULL, rel_start INTEGER NOT NULL,
  rel_end INTEGER NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY(op_id, rel_start));
CREATE TABLE confirmed(
  target TEXT NOT NULL, lang TEXT NOT NULL, shard TEXT NOT NULL,
  state TEXT NOT NULL, action TEXT, receipt INTEGER, seq INTEGER,
  op_id TEXT, content_hash TEXT,
  PRIMARY KEY(target, lang, shard));
CREATE TABLE pending(
  target TEXT NOT NULL, receipt INTEGER NOT NULL,
  op_id TEXT NOT NULL, applied INTEGER NOT NULL, reason TEXT,
  PRIMARY KEY(target, receipt));
CREATE TABLE requests(
  request TEXT PRIMARY KEY, targets TEXT NOT NULL, limit_n INTEGER NOT NULL,
  body TEXT NOT NULL, created_utc TEXT NOT NULL);
"""

_OPEN = "open"
_APPLIED = "applied"
_REJECTED = "rejected"

_META_TARGET = "target:%s:"
_IMPORTED_AT = "receipt:last_import_utc"
_PLAN_TABLE = "plan"


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (key,)).fetchone()
    return row[0] if row else None


def _meta_put(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                 (key, str(value)))


def _target_meta(conn, target, what):
    return _meta_get(conn, _META_TARGET % target + what)


def _set_target_meta(conn, target, what, value):
    _meta_put(conn, _META_TARGET % target + what, value)


def _begin(conn):
    conn.execute("BEGIN IMMEDIATE")


def _commit(conn):
    conn.execute("COMMIT")


def _rollback(conn):
    if conn.in_transaction:
        conn.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# Opening / initialising
# ---------------------------------------------------------------------------

def init_ledger(path, cache_mb=DEFAULT_CACHE_MB):
    """Create a new ledger.  Never overwrites an existing file."""
    path = os.path.abspath(path)
    if os.path.lexists(path):
        raise LedgerError(
            "ledger %s already exists; ledgers are never overwritten" % path)
    try:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    except sqlite3.Error as e:
        raise LedgerError("cannot create ledger %s: %s" % (path, e))
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA cache_size = %d" % -(max(1024, cache_mb * 1024)))
        conn.execute("PRAGMA synchronous = FULL")
        _begin(conn)
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        _meta_put(conn, "format", FORMAT)
        _meta_put(conn, "generator", GENERATOR)
        _meta_put(conn, "created_utc", _utcnow())
        _commit(conn)
        return conn
    except Exception:
        _rollback(conn)
        conn.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def open_ledger(path, cache_mb=DEFAULT_CACHE_MB):
    """Open an existing ledger read-write."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise LedgerError("ledger %s not found" % path)
    try:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA cache_size = %d" % -(max(1024, cache_mb * 1024)))
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.Error as e:
        raise LedgerError("cannot open ledger %s: %s" % (path, e))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'format'").fetchone()
    except sqlite3.Error as e:
        conn.close()
        raise LedgerError("%s is not a delivery ledger: %s" % (path, e))
    if row is None or row[0] != FORMAT:
        conn.close()
        raise LedgerError("%s is not a %s ledger (format %r)"
                          % (path, FORMAT, row[0] if row else None))
    return conn


# ---------------------------------------------------------------------------
# Shard tables and registration
# ---------------------------------------------------------------------------

def _normalise_shards(raw, frames):
    if not isinstance(raw, list) or not raw:
        raise LedgerError("a shard table must be a non-empty array of "
                          "{name,start,end} objects")
    out = []
    pos = 0
    seen = set()
    for i, sh in enumerate(raw):
        if not isinstance(sh, dict) or set(sh) != {"name", "start", "end"}:
            raise LedgerError("shard #%d: expected exactly name, start, end"
                              % (i + 1))
        try:
            name = check_id(sh["name"])
        except ValueError as e:
            raise LedgerError("shard #%d: %s" % (i + 1, e))
        if name in seen:
            raise LedgerError("duplicate shard name %r" % name)
        seen.add(name)
        s, e = sh["start"], sh["end"]
        if not is_int(s) or not is_int(e):
            raise LedgerError("shard %r: start/end must be integers" % name)
        if s != pos:
            raise LedgerError(
                "shard %r starts at %d but the previous shard ends at %d; "
                "the shard table must tile the film without gaps"
                % (name, s, pos))
        if not (s < e <= frames):
            raise LedgerError(
                "shard %r: [%d,%d) is empty or outside the %d-frame film"
                % (name, s, e, frames))
        out.append((name, s, e))
        pos = e
    if pos != frames:
        raise LedgerError(
            "shard table ends at frame %d but the film has %d frames"
            % (pos, frames))
    return out


def _normalise_languages(spec, frames):
    if not isinstance(spec, dict):
        raise LedgerError("languages must be an object mapping each language "
                          "to its shard table")
    if isinstance(spec.get("languages"), dict):
        spec = spec["languages"]
    if not spec:
        raise LedgerError("a registration must configure at least one "
                          "language")
    out = {}
    for lang in sorted(spec):
        try:
            check_lang(lang)
        except ValueError as e:
            raise LedgerError("language %r: %s" % (lang, e))
        out[lang] = _normalise_shards(spec[lang], frames)
    return out


def _iter_shards(conn, target, lang, batch=1024):
    """Stream one language's fixed shard table in film order."""
    cur = conn.execute(
        "SELECT name, target_start, target_end FROM shards WHERE target = "
        "? AND lang = ? ORDER BY target_start", (target, lang))
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for row in rows:
            yield row


def _configured_languages(conn, target):
    return {r[0]: [(x[0], x[1], x[2]) for x in conn.execute(
        "SELECT name, target_start, target_end FROM shards WHERE target = ? "
        "AND lang = ? ORDER BY ord", (target, r[0]))]
        for r in conn.execute(
            "SELECT DISTINCT lang FROM shards WHERE target = ? "
            "ORDER BY lang", (target,))}


def _release_identity(target, seq, meta, langs):
    ident = {
        "format": FORMAT,
        "kind": "release",
        "target": target,
        "seq": seq,
        "package": meta["package"],
        "root": meta["root"],
        "root_frames": int(meta["root_frames"]),
        "logical_version": meta["logical_version"],
        "languages": sorted(langs),
    }
    return hashlib.sha256(canonical_json(ident)).hexdigest()


def _register_check_library(conn, target, lib_path):
    try:
        lconn, meta = open_library(lib_path)
    except Exception as e:
        raise LedgerError("%s is not a readable subwarehouse library: %s"
                          % (lib_path, e))
    lconn.close()
    return meta


def register(conn, target, seq, lib_path, languages=None):
    """Register release ``seq`` of ``target`` from a built library.

    The first registration of a target fixes the film (root sequence and
    frame count), the languages it receives and their shard tables.  Later
    registrations use the next release number and must match the film,
    length and configured languages.  Re-registering an existing number is
    allowed only for identical logical content (idempotent retry).
    """
    target = check_id(target)
    if not is_int(seq) or seq < 1:
        raise LedgerError("release number must be a positive integer")
    lib_path = os.path.abspath(lib_path)
    if not os.path.isfile(lib_path):
        raise LedgerError("library %s not found" % lib_path)
    meta = _register_check_library(conn, target, lib_path)
    try:
        declared = json.loads(meta["languages"])
    except (KeyError, ValueError):
        raise LedgerError("library %s has no declared languages" % lib_path)
    frames = int(meta["root_frames"])
    root = meta["root"]

    _begin(conn)
    try:
        # One ledger serves one finished film with a fixed length.
        ledger_frames = _meta_get(conn, "film:frames")
        ledger_root = _meta_get(conn, "film:root")
        if ledger_frames is not None and int(ledger_frames) != frames:
            raise LedgerError(
                "this ledger serves a %s-frame film but library %s covers "
                "%d frames" % (ledger_frames, lib_path, frames))
        if ledger_root is not None and ledger_root != root:
            raise LedgerError(
                "this ledger serves the film rooted at %r but library %s "
                "has root %r" % (ledger_root, lib_path, root))
        trow = conn.execute("SELECT frames, root FROM targets WHERE "
                            "target = ?", (target,)).fetchone()
        configured = {}
        if trow is not None:
            if int(trow[0]) != frames:
                raise LedgerError(
                    "target %r is a %d-frame film but library %s covers %d "
                    "frames" % (target, int(trow[0]), lib_path, frames))
            if trow[1] != root:
                raise LedgerError(
                    "target %r is the film rooted at %r but library %s has "
                    "root %r" % (target, trow[1], lib_path, root))
            configured = _configured_languages(conn, target)
        if languages is None:
            if trow is None:
                raise LedgerError(
                    "the first registration of target %r must configure its "
                    "languages and shard tables" % target)
        else:
            new_langs = _normalise_languages(languages, frames)
            if trow is not None:
                for lang, table in new_langs.items():
                    if lang not in configured:
                        raise LedgerError(
                            "language %r was not configured for target %r; "
                            "the configured set is fixed at first use"
                            % (lang, target))
                    if configured[lang] != table:
                        raise LedgerError(
                            "language %r of target %r already has a fixed "
                            "shard table; a new table cannot replace it"
                            % (lang, target))
            else:
                configured = new_langs
        if not configured:
            raise LedgerError("target %r has no configured languages"
                              % target)
        missing = sorted(set(configured) - set(declared))
        if missing:
            raise LedgerError(
                "library %s does not declare required language(s): %s"
                % (lib_path, ", ".join(missing)))
        rh = _release_identity(target, seq, meta, configured)

        existing = conn.execute(
            "SELECT release_hash, logical_version, library FROM releases "
            "WHERE target = ? AND seq = ?", (target, seq)).fetchone()
        if existing is not None:
            if existing[0] != rh:
                raise LedgerError(
                    "release %d of target %r is already registered with a "
                    "different logical version; a number cannot be reused "
                    "for another release" % (seq, target))
            summary = _release_summary(conn, target, seq)
            summary["already_registered"] = True
            _commit(conn)
            return summary
        highest = conn.execute(
            "SELECT seq FROM releases WHERE target = ? ORDER BY seq DESC "
            "LIMIT 1", (target,)).fetchone()
        highest = highest[0] if highest else 0
        if seq != highest + 1:
            raise LedgerError(
                "release numbers increase by one: target %r is at release "
                "%d, cannot register %d" % (target, highest, seq))

        if ledger_frames is None:
            _meta_put(conn, "film:frames", frames)
            _meta_put(conn, "film:root", root)
        if trow is None:
            conn.execute("INSERT INTO targets(target, frames, root) "
                         "VALUES(?,?,?)", (target, frames, root))
            for lang, table in configured.items():
                rows = [(target, lang, i, name, s, e)
                        for i, (name, s, e) in enumerate(table)]
                conn.executemany(
                    "INSERT INTO shards VALUES(?,?,?,?,?,?)", rows)
                conn.executemany(
                    "INSERT INTO confirmed(target, lang, shard, state) "
                    "VALUES(?,?,?,'none')",
                    [(target, lang, name) for name, _s, _e in table])
        elif languages is not None:
            # the same fixed configuration again: validated only
            pass
        conn.execute(
            "INSERT INTO releases(target, seq, library, logical_version, "
            "release_hash, registered_utc) VALUES(?,?,?,?,?,?)",
            (target, seq, lib_path, meta["logical_version"], rh, _utcnow()))
        conn.executemany(
            "INSERT INTO release_langs(target, seq, lang) VALUES(?,?,?)",
            [(target, seq, lang) for lang in sorted(configured)])
        stats = _load_cuts(conn, target, seq, lib_path, sorted(configured))
        _set_target_meta(conn, target, "batch_counter",
                         int(_target_meta(conn, target, "batch_counter")
                             or 0))
        summary = _release_summary(conn, target, seq)
        summary.update({"already_registered": False,
                        "library": lib_path,
                        "logical_version": meta["logical_version"],
                        "root": root, "root_frames": frames,
                        "languages": sorted(configured),
                        "cut_stats": stats})
        _commit(conn)
        return summary
    except Exception:
        _rollback(conn)
        raise


def _release_summary(conn, target, seq):
    row = conn.execute(
        "SELECT library, logical_version, registered_utc FROM releases "
        "WHERE target = ? AND seq = ?", (target, seq)).fetchone()
    cuts = conn.execute("SELECT COUNT(*) FROM cuts WHERE target = ? AND "
                        "seq = ?", (target, seq)).fetchone()[0]
    gaps = conn.execute("SELECT COUNT(*) FROM gaps WHERE target = ? AND "
                        "seq = ?", (target, seq)).fetchone()[0]
    units = conn.execute("SELECT COUNT(DISTINCT lang || char(31) || shard) "
                         "FROM gaps WHERE target = ? AND seq = ?",
                         (target, seq)).fetchone()[0]
    return {"target": target, "release": seq, "library": row[0],
            "logical_version": row[1], "registered_utc": row[2],
            "cue_intervals": cuts, "gap_intervals": gaps,
            "incomplete_units": units}


# ---------------------------------------------------------------------------
# Cutting a library into per-shard relative intervals
# ---------------------------------------------------------------------------

def _load_cuts(conn, target, seq, lib_path, languages, batch=BATCH_ROWS):
    """Stream the library once per language into `cuts` and `gaps`.

    Both the library segments and the target's fixed shard table are read
    as ordered streams; only shards overlapped by the current segment are
    held, and each shard's content digest is fed incrementally as its cut
    intervals are produced.  Inserts go out in bounded batches, so work and
    memory follow interval boundaries, never the frame span.
    """
    stats = {"segments_read": 0, "cue_intervals": 0, "segments_merged": 0,
             "gap_intervals": 0, "incomplete_units": 0}
    lconn, _meta = open_library(lib_path)
    try:
        for lang in sorted(languages):
            shard_iter = _iter_shards(conn, target, lang)
            window = []
            buf = []
            unit_rows = []
            state = {"cut": None, "gap": None, "idx": None,
                     "incomplete": False, "digest": None, "prev": None,
                     "last": None, "items": 0}

            def pull_until(frame):
                while not window or window[-1][2] < frame:
                    try:
                        window.append(next(shard_iter))
                    except StopIteration:
                        return

            def flush_cut():
                cut = state["cut"]
                if cut is None:
                    return
                buf.append((target, seq, lang, state["idx"][0], cut[0],
                            cut[1], cut[2]))
                stats["cue_intervals"] += 1
                state["cut"] = None

            def flush_gap():
                gap = state["gap"]
                if gap is None:
                    return
                conn.execute(
                    "INSERT INTO gaps(target, seq, lang, shard, start, "
                    "\"end\") VALUES(?,?,?,?,?,?)",
                    (target, seq, lang, state["idx"][0], gap[0], gap[1]))
                stats["gap_intervals"] += 1
                state["gap"] = None

            def close_shard():
                flush_cut()
                flush_gap()
                idx = state["idx"]
                unit_rows.append((target, seq, lang, idx[0],
                                  state["items"],
                                  state["digest"].hexdigest(),
                                  1 if state["incomplete"] else 0))
                if state["incomplete"]:
                    stats["incomplete_units"] += 1
                    state["incomplete"] = False
                state["idx"] = None
                state["digest"] = None
                state["prev"] = None
                state["last"] = None
                state["items"] = 0
                if buf:
                    conn.executemany(
                        "INSERT INTO cuts(target, seq, lang, shard, "
                        "rel_start, rel_end, text) VALUES(?,?,?,?,?,?,?)",
                        buf)
                    del buf[:]
                if len(unit_rows) >= batch:
                    conn.executemany(
                        "INSERT OR REPLACE INTO release_units VALUES"
                        "(?,?,?,?,?,?,?)", unit_rows)
                    del unit_rows[:]

            def open_shard(idx):
                state["idx"] = idx
                state["digest"] = hashlib.sha256()
                state["prev"] = None
                state["last"] = None
                state["items"] = 0

            def take_cut(idx, s0, ps, pe, text):
                rs, re = ps - s0, pe - s0
                cut = state["cut"]
                if cut is not None and cut[1] == rs and cut[2] == text:
                    # merge into the open interval: rebuild the digest from
                    # the state before that interval so the unit identity
                    # is the merged [start,end) plus text
                    merged = (cut[0], re, text)
                    digest = state["prev"].copy() if state["prev"] is not \
                        None else hashlib.sha256()
                    digest.update(canonical_json(list(merged)))
                    state["digest"] = digest
                    state["cut"] = merged
                    state["last"] = merged
                    stats["segments_merged"] += 1
                else:
                    flush_cut()
                    flush_gap()
                    state["prev"] = state["digest"].copy()
                    state["digest"].update(canonical_json([rs, re, text]))
                    state["cut"] = (rs, re, text)
                    state["last"] = (rs, re, text)
                    state["items"] += 1

            def take_gap(idx, ps, pe):
                gap = state["gap"]
                if gap is not None and gap[1] == ps:
                    state["gap"] = (gap[0], pe)
                else:
                    flush_gap()
                    flush_cut()
                    state["gap"] = (ps, pe)

            cur = lconn.execute(
                "SELECT start, \"end\", state, text FROM segments "
                "WHERE lang = ? ORDER BY start", (lang,))
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for s, e, seg_state, text in rows:
                    stats["segments_read"] += 1
                    pull_until(e)
                    window[:] = [w for w in window if w[2] > s]
                    for idx in list(window):
                        name, s0, s1 = idx
                        if s0 >= e:
                            break
                        if state["idx"] is not None and \
                                state["idx"][0] != name:
                            close_shard()
                        if state["idx"] is None:
                            open_shard(idx)
                        ps, pe = max(s, s0), min(e, s1)
                        if seg_state == "unknown":
                            state["incomplete"] = True
                            take_gap(idx, ps, pe)
                        elif seg_state == "cue":
                            take_cut(idx, s0, ps, pe, text)
                        if s1 <= e:
                            close_shard()
            if state["idx"] is not None:
                close_shard()
            if buf:
                conn.executemany(
                    "INSERT INTO cuts(target, seq, lang, shard, rel_start, "
                    "rel_end, text) VALUES(?,?,?,?,?,?,?)", buf)
                del buf[:]
            if unit_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO release_units VALUES"
                    "(?,?,?,?,?,?,?)", unit_rows)
                del unit_rows[:]
    finally:
        lconn.close()
    return stats


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _unit_join(conn, targets):
    """Stream units in the documented stable order: target, then language,
    then shard start.  The shard table, confirmed state and the release
    digest are joined on disk; cut rows are read per unit only when a row
    is actually emitted."""
    for target in targets:
        seq = _current_release(conn, target)
        cur = conn.execute(
            "SELECT s.target, s.lang, s.name, s.target_start, "
            "s.target_end, c.state, c.action, c.op_id, c.content_hash, "
            "ru.content_hash, ru.items, ru.has_gap "
            "FROM shards s "
            "LEFT JOIN confirmed c ON c.target = s.target AND c.lang = "
            "s.lang AND c.shard = s.name "
            "LEFT JOIN release_units ru ON ru.target = s.target AND "
            "ru.seq = ? AND ru.lang = s.lang AND ru.shard = s.name "
            "WHERE s.target = ? ORDER BY s.lang, s.target_start",
            (seq, target))
        while True:
            rows = cur.fetchmany(512)
            if not rows:
                break
            for row in rows:
                yield row


def _wanted_items(conn, target, seq, lang, shard):
    return tuple(conn.execute(
        "SELECT rel_start, rel_end, text FROM cuts WHERE target = ? AND "
        "seq = ? AND lang = ? AND shard = ? ORDER BY rel_start",
        (target, seq, lang, shard)))


def _wanted_digest(conn, target, seq, lang, shard):
    row = conn.execute(
        "SELECT content_hash, items, has_gap FROM release_units WHERE "
        "target = ? AND seq = ? AND lang = ? AND shard = ?",
        (target, seq, lang, shard)).fetchone()
    return row


def _has_open(conn, target, lang, shard):
    return conn.execute(
        "SELECT 1 FROM ops WHERE target = ? AND lang = ? AND shard = ? AND "
        "status = 'open' LIMIT 1",
        (target, lang, shard)).fetchone() is not None


def _current_release(conn, target):
    row = conn.execute(
        "SELECT seq FROM releases WHERE target = ? ORDER BY seq DESC "
        "LIMIT 1", (target,)).fetchone()
    return row[0] if row else None


def _confirmed_row(conn, target, lang, shard):
    return conn.execute(
        "SELECT state, action, receipt, seq, op_id, content_hash FROM "
        "confirmed WHERE target = ? AND lang = ? AND shard = ?",
        (target, lang, shard)).fetchone()


def _plan_one(conn, unit):
    """Decide one (target, language, shard) at its target's current
    highest release.  Returns a plan dict or None for no row."""
    target, lang, shard, s0, s1, cstate, caction, cop, cch, wch, witems, \
        wgap = unit
    seq = _current_release(conn, target)
    if seq is None:
        raise LedgerError("target %r has no registered release" % target)
    if wch is None:
        raise LedgerStateError(
            "release %d of target %r has no recorded expectation for unit "
            "(%s, %s)" % (seq, target, lang, shard))
    if _has_open(conn, target, lang, shard):
        return None                       # wait for the receipt
    if wgap:
        return None                       # release lacks material: keep
    if cstate == "subtitles":
        if wch == cch:
            return None                   # confirmed == wanted
        action = "clear" if witems == 0 else "replace"
    else:
        if witems == 0:
            return None                   # initial or cleared empty
        action = "replace"
    items = ()
    if action == "replace":
        items = _wanted_items(conn, target, seq, lang, shard)
    return {"target": target, "lang": lang, "shard": shard, "seq": seq,
            "action": action, "items": items, "content_hash": wch,
            "shard_start": s0}


def _plans(conn, targets):
    for unit in _unit_join(conn, targets):
        plan = _plan_one(conn, unit)
        if plan is not None:
            yield plan


def _payload_hash(plan):
    body = {"action": plan["action"], "target": plan["target"],
            "lang": plan["lang"], "shard": plan["shard"],
            "items": [{"start": it[0], "end": it[1], "text": it[2]}
                      for it in plan["items"]]}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _attempt(conn, target, lang, shard):
    return conn.execute(
        "SELECT COUNT(*) FROM ops WHERE target = ? AND lang = ? AND "
        "shard = ?", (target, lang, shard)).fetchone()[0] + 1


def dispatch(conn, request_id, targets, limit=None):
    """Plan the current releases of ``targets`` into one batch.

    ``limit`` caps the number of instruction rows (None means no cap; the
    CLI always passes one so operators take bounded batches).  The response
    is stored under ``request_id`` and returned verbatim on retry.
    """
    request_id = check_id(request_id)
    if isinstance(targets, str):
        targets = [targets]
    targets = sorted({check_id(t) for t in targets})
    if not targets:
        raise LedgerError("at least one target is required")
    if limit is not None and (not is_int(limit) or limit < 1):
        raise LedgerError("limit must be a positive integer")
    _begin(conn)
    try:
        req = conn.execute(
            "SELECT targets, limit_n, body FROM requests WHERE request = ?",
            (request_id,)).fetchone()
        if req is not None:
            if json.loads(req[0]) != targets or req[1] != \
                    (limit if limit is not None else -1):
                raise LedgerError(
                    "request %r was already used with different parameters; "
                    "a request id cannot be reused" % request_id)
            _commit(conn)
            return json.loads(req[2])
        for target in targets:
            if conn.execute("SELECT 1 FROM targets WHERE target = ?",
                            (target,)).fetchone() is None:
                raise LedgerError("target %r is not registered" % target)
            if _current_release(conn, target) is None:
                raise LedgerError("target %r has no registered release"
                                  % target)
        batch_no = int(_meta_get(conn, "batch_counter") or 0) + 1
        _meta_put(conn, "batch_counter", batch_no)
        result = {"request": request_id, "batch": batch_no,
                  "targets": targets, "limit": limit, "count": 0,
                  "more": False, "instructions": []}
        gen = _plans(conn, targets)
        for plan in gen:
            if limit is not None and result["count"] >= limit:
                # one plan is already in hand but not written; more rows
                # exist only if the next unit also differs
                result["more"] = True
                break
            result["count"] += 1
            op_id = "%s.r%d.%s.%s.%d" % (
                plan["target"], plan["seq"], plan["lang"], plan["shard"],
                _attempt(conn, plan["target"], plan["lang"],
                         plan["shard"]))
            ph = _payload_hash(plan)
            items = [{"start": it[0], "end": it[1], "text": it[2]}
                     for it in plan["items"]]
            body = {"op_id": op_id, "target": plan["target"],
                    "lang": plan["lang"], "shard": plan["shard"],
                    "shard_start": plan["shard_start"],
                    "seq": plan["seq"], "action": plan["action"],
                    "items": items if plan["action"] == "replace" else [],
                    "payload_hash": ph}
            result["instructions"].append(body)
            conn.execute(
                "INSERT INTO ops(op_id, target, lang, shard, request, "
                "batch, seq, action, payload_hash, content_hash, items, "
                "status, created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', "
                "?)",
                (op_id, plan["target"], plan["lang"], plan["shard"],
                 request_id, batch_no, plan["seq"], plan["action"], ph,
                 plan["content_hash"], len(items), _utcnow()))
            if plan["action"] == "replace":
                conn.executemany(
                    "INSERT INTO op_items(op_id, rel_start, rel_end, text) "
                    "VALUES(?,?,?,?)",
                    [(op_id, it[0], it[1], it[2]) for it in plan["items"]])
        conn.execute(
            "INSERT INTO requests(request, targets, limit_n, body, "
            "created_utc) VALUES(?,?,?,?,?)",
            (request_id, json.dumps(targets),
             limit if limit is not None else -1,
             json.dumps(result, ensure_ascii=False, sort_keys=True),
             _utcnow()))
        _commit(conn)
        return result
    except Exception:
        _rollback(conn)
        raise


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def _parse_receipt(obj, file, line):
    if not isinstance(obj, dict):
        raise ReceiptConflict("receipt must be a JSON object",
                              file=file, line=line)
    extra = set(obj) - {"target", "receipt", "op_id", "status", "reason"}
    if extra:
        raise ReceiptConflict("unexpected receipt fields: %s"
                              % ", ".join(sorted(extra)),
                              file=file, line=line)
    for k in ("target", "receipt", "op_id", "status"):
        if k not in obj:
            raise ReceiptConflict("receipt is missing %r" % k,
                                  file=file, line=line)
    try:
        target = check_id(obj["target"])
        op_id = check_id(obj["op_id"])
    except ValueError as e:
        raise ReceiptConflict(str(e), file=file, line=line)
    rno = obj["receipt"]
    if not is_int(rno) or rno < 1:
        raise ReceiptConflict("receipt number must be a positive integer",
                              file=file, line=line)
    status = obj["status"]
    if status not in ("applied", "rejected"):
        raise ReceiptConflict("receipt status must be 'applied' or "
                              "'rejected'", file=file, line=line)
    reason = obj.get("reason")
    if status == "rejected":
        if not isinstance(reason, str) or not reason:
            raise ReceiptConflict("a rejected receipt needs a non-empty "
                                  "reason", file=file, line=line)
    elif reason is not None:
        raise ReceiptConflict("an applied receipt must not carry a reason",
                              file=file, line=line)
    return {"target": target, "receipt": rno, "op_id": op_id,
            "status": status, "reason": reason, "file": file, "line": line}


def _iter_receipt_file(path):
    if not os.path.isfile(path):
        raise ReceiptConflict("receipt file %s not found" % path,
                              file=path)
    try:
        fh = open(path, "r", encoding="utf-8", errors="strict")
    except OSError as e:
        raise ReceiptConflict("cannot read receipt file: %s" % e,
                              file=path)
    with fh:
        for n, raw in enumerate(fh, 1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise ReceiptConflict("invalid JSON: %s" % e.msg,
                                      file=path, line=n)
            yield n, obj


def import_receipts(conn, paths, target=None, batch=BATCH_ROWS):
    """Import receipt files atomically; whole input is rejected on any
    conflict.  Returns per-target progress after the import.

    Records are streamed into a temporary staging table in bounded batches
    while their JSON shape is validated; only then are they folded into the
    ledger, so neither the file nor the receipt history is held in memory.
    """
    if isinstance(paths, str):
        paths = [paths]
    paths = [os.path.abspath(p) for p in paths]
    if not paths:
        raise LedgerError("no receipt files given")
    _begin(conn)
    try:
        conn.execute("DROP TABLE IF EXISTS temp.recv")
        conn.execute(
            "CREATE TEMP TABLE recv(target TEXT, receipt INTEGER, "
            "op_id TEXT, applied INTEGER, reason TEXT, file TEXT, "
            "line INTEGER, ord INTEGER)")
        n = 0
        stage = []
        for path in paths:
            for line, obj in _iter_receipt_file(path):
                rec = _parse_receipt(obj, path, line)
                if target is not None and rec["target"] != target:
                    raise ReceiptConflict(
                        "receipt for target %r in an import limited to "
                        "target %r" % (rec["target"], target),
                        file=path, line=line)
                if conn.execute(
                        "SELECT 1 FROM targets WHERE target = ?",
                        (rec["target"],)).fetchone() is None:
                    raise ReceiptConflict(
                        "receipt names unknown target %r" % rec["target"],
                        file=path, line=line)
                stage.append((rec["target"], rec["receipt"], rec["op_id"],
                              1 if rec["status"] == "applied" else 0,
                              rec["reason"], path, line, n))
                n += 1
                if len(stage) >= batch:
                    conn.executemany("INSERT INTO temp.recv VALUES"
                                     "(?,?,?,?,?,?,?,?)", stage)
                    del stage[:]
        if stage:
            conn.executemany("INSERT INTO temp.recv VALUES"
                             "(?,?,?,?,?,?,?,?)", stage)
            del stage[:]
        summary = {}
        cur = conn.execute(
            "SELECT target, receipt, op_id, applied, reason, file, line "
            "FROM temp.recv ORDER BY ord")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for row in rows:
                _accept_one(conn, {
                    "target": row[0], "receipt": row[1], "op_id": row[2],
                    "status": "applied" if row[3] else "rejected",
                    "reason": row[4], "file": row[5], "line": row[6]},
                    summary)
        conn.execute("DROP TABLE temp.recv")
        if n:
            _meta_put(conn, _IMPORTED_AT, _utcnow())
        _commit(conn)
    except Exception:
        _rollback(conn)
        try:
            conn.execute("DROP TABLE IF EXISTS temp.recv")
        except sqlite3.Error:
            pass
        raise
    out = [progress(conn, target_id) for target_id in sorted(summary)]
    return {"files": paths, "accepted": n, "changes": summary,
            "progress": out}


def _summary_slot(summary, conn, target):
    slot = summary.get(target)
    if slot is None:
        slot = {"target": target, "confirmed": 0, "pending": 0,
                "drained": 0, "prefix_before":
                int(_target_meta(conn, target, "confirmed_prefix") or 0)}
        summary[target] = slot
    return slot


def _accept_one(conn, rec, summary):
    target, rno = rec["target"], rec["receipt"]
    op_id = rec["op_id"]
    applied = 1 if rec["status"] == "applied" else 0
    reason = rec["reason"]
    file, line = rec["file"], rec["line"]

    if conn.execute("SELECT 1 FROM targets WHERE target = ?",
                    (target,)).fetchone() is None:
        raise ReceiptConflict("receipt names unknown target %r" % target,
                              file=file, line=line)
    op = conn.execute(
        "SELECT target, status, receipt FROM ops WHERE op_id = ?",
        (op_id,)).fetchone()
    if op is None:
        raise ReceiptConflict("receipt cites unknown operation %r" % op_id,
                              file=file, line=line)
    if op[0] != target:
        raise ReceiptConflict(
            "operation %r belongs to target %r, not %r"
            % (op_id, op[0], target), file=file, line=line)

    done = conn.execute(
        "SELECT op_id, applied, reason FROM ops WHERE target = ? AND "
        "receipt = ?", (target, rno)).fetchone()
    if done is not None:
        if done[0] != op_id or int(done[1]) != applied \
                or (done[2] or "") != (reason or ""):
            raise ReceiptConflict(
                "receipt %d of target %r already holds a different result "
                "(operation %r)" % (rno, target, done[0]),
                file=file, line=line)
        return                                  # identical repeat

    if op[1] != _OPEN:
        raise ReceiptConflict(
            "operation %r already finalised with receipt %s; receipt %d "
            "would give it a second result" % (op_id, op[2], rno),
            file=file, line=line)

    pend = conn.execute(
        "SELECT op_id, applied, reason FROM pending WHERE target = ? AND "
        "receipt = ?", (target, rno)).fetchone()
    if pend is not None:
        if pend[0] != op_id or int(pend[1]) != applied \
                or (pend[2] or "") != (reason or ""):
            raise ReceiptConflict(
                "receipt %d of target %r is already pending with a "
                "different result (operation %r)"
                % (rno, target, pend[0]), file=file, line=line)
        return                                  # identical cached repeat

    prefix = int(_target_meta(conn, target, "confirmed_prefix") or 0)
    slot = _summary_slot(summary, conn, target)
    if rno != prefix + 1:
        if rno <= prefix:
            raise LedgerStateError(
                "receipt %d of target %r is below the confirmed prefix %d "
                "but has no matching record" % (rno, target, prefix))
        prev = conn.execute(
            "SELECT receipt FROM pending WHERE target = ? AND op_id = ?",
            (target, op_id)).fetchone()
        if prev is not None:
            raise ReceiptConflict(
                "operation %r already has pending receipt %d; receipt %d "
                "would give it a second number"
                % (op_id, prev[0], rno), file=file, line=line)
        conn.execute(
            "INSERT INTO pending(target, receipt, op_id, applied, reason) "
            "VALUES(?,?,?,?,?)", (target, rno, op_id, applied, reason))
        slot["pending"] += 1
        return

    _materialise(conn, target, rno, op_id, applied, reason)
    slot["confirmed"] += 1
    cur = rno
    while True:
        nxt = conn.execute(
            "SELECT op_id, applied, reason FROM pending WHERE target = ? "
            "AND receipt = ?", (target, cur + 1)).fetchone()
        if nxt is None:
            break
        _materialise(conn, target, cur + 1, nxt[0], nxt[1], nxt[2])
        conn.execute("DELETE FROM pending WHERE target = ? AND receipt = ?",
                     (target, cur + 1))
        cur += 1
        slot["drained"] += 1
    _set_target_meta(conn, target, "confirmed_prefix", cur)
    _set_target_meta(conn, target, "receipts_applied",
                     int(_target_meta(conn, target, "receipts_applied")
                         or 0) + slot["confirmed"] + slot["drained"])


def _materialise(conn, target, rno, op_id, applied, reason):
    row = conn.execute(
        "SELECT lang, shard, action, seq, content_hash FROM ops WHERE "
        "target = ? AND op_id = ?", (target, op_id)).fetchone()
    lang, shard, action, seq, content_hash = row
    conn.execute(
        "UPDATE ops SET status = ?, receipt = ?, applied = ?, reason = ?, "
        "closed_utc = ? WHERE target = ? AND op_id = ?",
        (_APPLIED if applied else _REJECTED, rno, applied, reason,
         _utcnow(), target, op_id))
    if applied:
        state = "none" if action == "clear" else "subtitles"
        conn.execute(
            "UPDATE confirmed SET state = ?, action = ?, receipt = ?, "
            "seq = ?, op_id = ?, content_hash = ? WHERE target = ? AND "
            "lang = ? AND shard = ?",
            (state, action, rno, seq, op_id, content_hash, target, lang,
             shard))


def _progress_snapshot(conn, target):
    if conn.execute("SELECT 1 FROM targets WHERE target = ?",
                    (target,)).fetchone() is None:
        raise LedgerError("target %r is not registered" % target)
    prefix = int(_target_meta(conn, target, "confirmed_prefix") or 0)
    pending = [r[0] for r in conn.execute(
        "SELECT receipt FROM pending WHERE target = ? ORDER BY receipt",
        (target,))]
    open_ops = conn.execute(
        "SELECT COUNT(*) FROM ops WHERE target = ? AND status = 'open'",
        (target,)).fetchone()[0]
    return {"target": target, "confirmed_prefix": prefix,
            "first_missing": prefix + 1, "pending_receipts": pending,
            "open_operations": open_ops,
            "highest_release": _current_release(conn, target)}


def progress(conn, target):
    """Confirmed prefix and first missing receipt for one target."""
    conn.execute("BEGIN")
    try:
        if conn.execute("SELECT 1 FROM targets WHERE target = ?",
                        (target,)).fetchone() is None:
            raise LedgerError("target %r is not registered" % target)
        return _progress_snapshot(conn, target)
    finally:
        _commit(conn)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _shard_info(conn, target, lang, shard):
    row = conn.execute(
        "SELECT target_start, target_end FROM shards WHERE target = ? AND "
        "lang = ? AND name = ?", (target, lang, shard)).fetchone()
    if row is None:
        raise LedgerError("target %r has no shard %r for language %r"
                          % (target, shard, lang))
    return row


def _op_items(conn, op_id):
    return tuple(conn.execute(
        "SELECT rel_start, rel_end, text FROM op_items WHERE op_id = ? "
        "ORDER BY rel_start", (op_id,)))


def _content_at(conn, target, lang, shard, upto):
    """Platform content for a unit at a confirmed receipt position.

    Looks only at applied operations with receipt <= upto (indexed lookup):
    rejected attempts never count, and later receipts are invisible.
    """
    row = conn.execute(
        "SELECT op_id, action, seq, receipt FROM ops WHERE target = ? AND "
        "lang = ? AND shard = ? AND applied = 1 AND receipt IS NOT NULL "
        "AND receipt <= ? ORDER BY receipt DESC LIMIT 1",
        (target, lang, shard, upto)).fetchone()
    if row is None:
        return {"state": "initial_empty", "content": [],
                "last_applied": None, "evidence": False}
    op_id, action, seq, receipt = row
    if action == "clear":
        content, state = [], "cleared"
    else:
        content = [{"start": it[0], "end": it[1], "text": it[2]}
                   for it in _op_items(conn, op_id)]
        state = "subtitles"
    return {"state": state, "content": content,
            "last_applied": {"receipt": receipt, "op_id": op_id,
                             "action": action, "release": seq},
            "evidence": True}


def verify(conn, target, lang=None, shard=None, release=None, upto=None):
    """Read-only audit of a target or one unit.

    ``upto`` is a receipt position inside the confirmed prefix (0 allowed:
    the initial state).  Facts outside the confirmed prefix are never used
    as evidence.  The release defaults to the target's highest; the
    expected content shown is that release's wanted content.
    """
    conn.execute("BEGIN")
    try:
        trow = conn.execute("SELECT frames, root FROM targets WHERE "
                            "target = ?", (target,)).fetchone()
        if trow is None:
            raise LedgerError("target %r is not registered" % target)
        prefix = int(_target_meta(conn, target, "confirmed_prefix") or 0)
        highest = _current_release(conn, target)
        if release is None:
            release = highest
        elif conn.execute("SELECT 1 FROM releases WHERE target = ? AND "
                          "seq = ?", (target, release)).fetchone() is None:
            raise LedgerError("target %r has no release %d"
                              % (target, release))
        if upto is None:
            upto = prefix
        if not is_int(upto) or upto < 0:
            raise LedgerError("upto must be a non-negative receipt number")
        if upto > prefix:
            raise LedgerError(
                "receipt %d is outside the confirmed prefix %d of target "
                "%r; only confirmed receipts are evidence"
                % (upto, prefix, target))
        base = {"target": target, "frames": trow[0], "root": trow[1],
                "release": release, "highest_release": highest,
                "confirmed_prefix": prefix, "first_missing": prefix + 1,
                "position": upto}
        if lang is None and shard is None:
            base["units"] = conn.execute(
                "SELECT COUNT(DISTINCT lang || char(31) || shard) FROM "
                "shards WHERE target = ?", (target,)).fetchone()[0]
            base["open_operations"] = conn.execute(
                "SELECT COUNT(*) FROM ops WHERE target = ? AND status = "
                "'open'", (target,)).fetchone()[0]
            return base
        if lang is None or shard is None:
            raise LedgerError("verification of a unit needs both --lang "
                              "and --shard")
        s0, s1 = _shard_info(conn, target, lang, shard)
        platform = _content_at(conn, target, lang, shard, upto)
        at = conn.execute(
            "SELECT op_id, action, applied, reason, seq, closed_utc FROM "
            "ops WHERE target = ? AND receipt = ?",
            (target, upto)).fetchone() if upto >= 1 else None
        if at is not None:
            inside = at[4] is not None and upto <= prefix
            at = {"receipt": upto, "op_id": at[0], "action": at[1],
                  "status": "applied" if at[2] else "rejected",
                  "reason": at[3], "release": at[4],
                  "closed_utc": at[5], "evidence": bool(at[2]),
                  "inside_confirmed_prefix": inside}
        want = [{"start": r[0], "end": r[1], "text": r[2]}
                for r in conn.execute(
                    "SELECT rel_start, rel_end, text FROM cuts WHERE "
                    "target = ? AND seq = ? AND lang = ? AND shard = ? "
                    "ORDER BY rel_start",
                    (target, release, lang, shard))]
        gaps = [{"start": r[0], "end": r[1]}
                for r in conn.execute(
                    "SELECT start, \"end\" FROM gaps WHERE target = ? AND "
                    "seq = ? AND lang = ? AND shard = ? ORDER BY start",
                    (target, release, lang, shard))]
        expected_state = ("incomplete" if gaps else
                          ("subtitles" if want else "empty"))
        platform_copy = dict(platform)
        platform_copy["matches_expected"] = (
            None if expected_state == "incomplete"
            else platform["content"] == want)
        initial = conn.execute(
            "SELECT COUNT(*) FROM releases WHERE target = ? AND seq < ?",
            (target, release)).fetchone()[0] == 0
        if platform["state"] == "cleared" and initial:
            platform_copy["introspection"] = (
                "no issuance before the registered release; any content was "
                "cleared by an applied instruction")
        rejected = [
            {"receipt": r[0], "op_id": r[1], "seq": r[2], "reason": r[3]}
            for r in conn.execute(
                "SELECT receipt, op_id, seq, reason FROM ops WHERE target = "
                "? AND lang = ? AND shard = ? AND applied = 0 AND receipt "
                "IS NOT NULL AND receipt <= ? ORDER BY receipt", 
                (target, lang, shard, upto))]
        base.update({
            "lang": lang, "shard": shard, "shard_start": s0,
            "shard_end": s1, "platform": platform_copy,
            "rejected_attempts": rejected,
            "receipt_at_position": at,
            "expected": {"release": release, "state": expected_state,
                         "content": want, "gaps": gaps},
            "open_operations": [
                {"op_id": r[0], "action": r[1], "seq": r[2], "receipt": r[3]}
                for r in conn.execute(
                    "SELECT op_id, action, seq, receipt FROM ops WHERE "
                    "target = ? AND lang = ? AND shard = ? AND status = "
                    "'open' ORDER BY created_utc",
                    (target, lang, shard))],
        })
        return base
    finally:
        _commit(conn)


def batch_status(conn, request_id):
    """Re-read a dispatched batch: rows, occupancy and confirmation facts."""
    conn.execute("BEGIN")
    try:
        req = conn.execute(
            "SELECT targets, limit_n, body, created_utc FROM requests "
            "WHERE request = ?", (request_id,)).fetchone()
        if req is None:
            raise LedgerError("no dispatch request %r on file" % request_id)
        body = json.loads(req[2])
        ops = []
        for r in conn.execute(
                "SELECT op_id, target, lang, shard, seq, action, status, "
                "receipt, applied, reason, payload_hash, items FROM ops "
                "WHERE request = ? ORDER BY target, lang, "
                "(SELECT target_start FROM shards s WHERE s.target = "
                "ops.target AND s.lang = ops.lang AND s.name = ops.shard)",
                (request_id,)):
            ops.append({
                "op_id": r[0], "target": r[1], "lang": r[2], "shard": r[3],
                "seq": r[4], "action": r[5], "status": r[6],
                "receipt": r[7],
                "applied": None if r[8] is None else bool(r[8]),
                "reason": r[9], "payload_hash": r[10], "items": r[11],
            })
        out = {"request": request_id, "batch": body.get("batch"),
               "targets": json.loads(req[0]), "limit": body.get("limit"),
               "stored_count": body.get("count"),
               "more": body.get("more"), "created_utc": req[3],
               "operations": ops, "progress": []}
        for target in json.loads(req[0]):
            out["progress"].append(_progress_snapshot(conn, target))
        return out
    finally:
        _commit(conn)
