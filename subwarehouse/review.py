"""Cross-program material-correction impact inventory.

A *review directory* is built once from an ordinary handover package plus
the list of sequences that are registered as finished films (programs).
It is self-contained: the ingested tables, the per-(asset, language) and
per-(sequence, language) opaque segmentations reachable from the
programs, and every program's final segments are materialised into one
SQLite file, so trials keep working after the handover package is moved
away.  The directory carries a *baseline* identifier binding the
manifest, the registered programs and the logical content of every
record.

Editors then submit small *correction orders* instead of a full subtitle
package.  An order names the baseline and replaces, for one
``(asset, lang)`` at a time, the checked coverage and the cues inside a
half-open window ``[start, end)``:

* the window's new coverage is a full declaration -- frames of the window
  it does not declare become unconfirmed (``unknown``);
* baseline cues intersecting the window must lie wholly inside it (no
  implicit truncation); they are replaced by the window's new cues;
* several windows may ride on one order, but windows of one
  ``(asset, lang)`` must not intersect;
* clips and sequence lengths never change;
* the patched cue set must still satisfy the usual constraints (cues
  inside checked coverage, no overlap, package-wide unique cue ids);
  any violation -- a baseline mismatch, an out-of-bounds window, one
  invalid item -- refuses the whole order and names the offending item.

Every trial compares the order against the *same* baseline; trials never
stack and never mutate the directory.  The trial walks only the
references, time ranges and languages the order touches: from each
touched ``(asset, lang)`` window it climbs the reverse reference graph
towards the registered programs, computing per-sequence *candidate
windows* (closed-form preimages, never per-frame iteration).  Only
sequences with a non-empty candidate window are re-resolved, bottom-up,
over exactly those windows; inside them the full track stack is
re-evaluated, so lower tracks the order never touched are still found
when a correction uncovers them.  Everything else is read back from the
materialised baseline.

Changed frames are reported per program and language in two classes:

* ``playback`` -- the playback judgement changed: different verbatim
  text, text appeared/disappeared among confirmed states, or a confirmed
  state turned into ``unknown`` (or back).  Reviewers must re-watch.
* ``evidence`` -- playback is unchanged but the state or the source
  changed (same text under another cue id, same pending frame now
  missing a different asset, same text through another clip path).
  Only the evidence needs a re-check.

Rows are emitted in stable ``(program, lang, start)`` order; adjacent
frames merge only when class and both sides' full evidence (state, text,
winning clip chain, endpoint) are identical.  Reordering the records of
an accepted order cannot change the resulting list.
"""

import hashlib
import json
import os
import resource
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from . import engine as eng
from .builder import _budget, _connect
from .errors import ReviewError
from .ingest import ingest_package
from .manifest import load_manifest
from .model import canonical_json, check_id, check_lang, is_int

FORMAT = "subwarehouse-review/1"
GENERATOR = "subwarehouse review 1.0"
DB_NAME = "inventory.sqlite"

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE programs(seq TEXT PRIMARY KEY, frames INTEGER NOT NULL);
CREATE TABLE clips(
  id TEXT NOT NULL, seq TEXT NOT NULL, track INTEGER NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  kind TEXT NOT NULL, ref TEXT, base INTEGER, p INTEGER, q INTEGER,
  rec_hash TEXT NOT NULL);
CREATE TABLE coverage(
  asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL, rec_hash TEXT NOT NULL);
CREATE TABLE cues(
  id TEXT NOT NULL, asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL, text TEXT NOT NULL,
  rec_hash TEXT NOT NULL);
CREATE TABLE refs(child TEXT NOT NULL, parent TEXT NOT NULL,
                  clip TEXT NOT NULL);
CREATE TABLE aseg(
  asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, cue_id TEXT, text TEXT);
CREATE TABLE sseg(
  seq TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, text TEXT, cue_id TEXT, asset_id TEXT,
  path TEXT NOT NULL);
CREATE TABLE segments(
  program TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, text TEXT, cue_id TEXT, asset_id TEXT,
  path TEXT NOT NULL);
"""

INDEXES = [
    "CREATE INDEX idx_clips_seq ON clips(seq, track, start)",
    "CREATE INDEX idx_coverage ON coverage(asset, lang, start)",
    "CREATE INDEX idx_cues_pair ON cues(asset, lang, start)",
    "CREATE INDEX idx_cues_id ON cues(id)",
    "CREATE INDEX idx_refs ON refs(child)",
    "CREATE INDEX idx_aseg ON aseg(asset, lang, start)",
    "CREATE INDEX idx_sseg ON sseg(seq, lang, start)",
    "CREATE INDEX idx_segments ON segments(program, lang, start)",
]

STAGING_SCHEMA = """
CREATE TABLE paseg(
  asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, cue_id TEXT, text TEXT);
CREATE TABLE psseg(
  seq TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  state TEXT NOT NULL, text TEXT, cue_id TEXT, asset_id TEXT,
  path TEXT NOT NULL);
CREATE TABLE rows(
  program TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  class TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL);
CREATE INDEX idx_paseg ON paseg(asset, lang, start);
CREATE INDEX idx_psseg ON psseg(seq, lang, start);
CREATE INDEX idx_rows ON rows(program, lang, start);
"""


def new_stats():
    return {
        "ingested": {"clips": 0, "coverage": 0, "cues": 0},
        "read": {"clips": 0, "coverage": 0, "cues": 0, "aseg": 0,
                 "sseg": 0},
        "written": {"aseg": 0, "sseg": 0, "segments": 0},
        "objects": {"assets": 0, "sequences": 0},
    }


def new_trial_stats():
    return {
        "windows": 0,
        "languages": [],
        "refs_walked": 0,
        "sequences_touched": 0,
        "sequences_rejudged": 0,
        "programs_rejudged": 0,
        "candidate_windows": 0,
        "candidate_frames": 0,
        "read": {"refs": 0, "clips": 0, "coverage": 0, "cues": 0,
                 "aseg": 0, "sseg": 0, "paseg": 0, "psseg": 0,
                 "segments": 0},
        "written": {"paseg": 0, "psseg": 0},
        "review_rows": 0,
    }


# ---------------------------------------------------------------------------
# Inventory directory: build once, reopen many times
# ---------------------------------------------------------------------------

def compute_baseline(stg, man, programs, batch):
    """Order-independent baseline id: manifest + registered programs +
    the sorted multiset of per-record hashes (SQLite sorts on disk)."""
    h = hashlib.sha256()
    h.update(b"subwarehouse-review/1\n")
    h.update(man.canonical())
    for prog in sorted(programs):
        h.update(b"\nprogram:")
        h.update(prog.encode("utf-8"))
    for tbl in ("clips", "coverage", "cues"):
        h.update(b"\ntable:")
        h.update(tbl.encode("ascii"))
        cur = stg.execute("SELECT rec_hash FROM %s ORDER BY rec_hash" % tbl)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for (rh,) in rows:
                h.update(bytes.fromhex(rh))
    return h.hexdigest()


def _fetch(cur, batch):
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield r


def _reachable(stg, programs, batch):
    """Sequences reachable from the registered programs, and the assets
    they reference (one-time init cost)."""
    reach = set(programs)
    assets = set()
    queue = list(programs)
    while queue:
        seq = queue.pop()
        cur = stg.execute("SELECT kind, ref FROM clips WHERE seq = ? "
                          "AND kind <> 'blank'", (seq,))
        for kind, ref in _fetch(cur, batch):
            if kind == "asset":
                assets.add(ref)
            elif ref not in reach:
                reach.add(ref)
                queue.append(ref)
    return reach, assets


def init_inventory(package_dir, programs, dir_path, mem_budget_mb=256):
    """Build the review directory.  Returns (stats, meta).  Never
    overwrites an existing path; failures remove every temporary file."""
    if os.path.lexists(dir_path):
        raise ReviewError(
            "inventory directory %s already exists; init never overwrites"
            % dir_path)
    if not os.path.isdir(package_dir):
        raise ReviewError("package directory %s not found" % package_dir)
    man = load_manifest(package_dir)
    programs = list(programs)
    if not programs:
        raise ReviewError("register at least one program sequence")
    seen = set()
    for prog in programs:
        try:
            check_id(prog)
        except ValueError as e:
            raise ReviewError("program: %s" % e)
        if prog in seen:
            raise ReviewError("duplicate program %r" % prog)
        seen.add(prog)
        if prog not in man.sequences:
            raise ReviewError(
                "program %r is not declared in the manifest (sequences: "
                "%s)" % (prog, ", ".join(sorted(man.sequences)) or "none"))

    token = "%d-%s" % (os.getpid(), secrets.token_hex(4))
    os.makedirs(dir_path)
    tmp_inv = os.path.join(dir_path, ".inv-%s.tmp" % token)
    tmp_stg = os.path.join(dir_path, ".stg-%s.tmp" % token)
    final = os.path.join(dir_path, DB_NAME)
    cache_kb, batch = _budget(mem_budget_mb)
    stats = new_stats()
    started = time.monotonic()
    stg = inv = None
    try:
        stg = _connect(tmp_stg, cache_kb)
        ingest_package(stg, package_dir, man, stats, batch)
        baseline = compute_baseline(stg, man, programs, batch)
        reach, assets = _reachable(stg, programs, batch)

        inv = _connect(tmp_inv, cache_kb)
        inv.executescript(SCHEMA)
        meta = {
            "format": FORMAT,
            "package": man.package,
            "baseline": baseline,
            "languages": json.dumps(sorted(man.languages)),
            "programs": json.dumps(sorted(programs)),
            "manifest": man.canonical().decode("utf-8"),
            "generator": GENERATOR,
            "created_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
        inv.execute("BEGIN")
        inv.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
        inv.executemany("INSERT INTO programs VALUES(?,?)",
                        [(p, man.sequences[p]) for p in sorted(programs)])
        inv.execute("COMMIT")

        inv.execute("ATTACH DATABASE ? AS stg", (tmp_stg,))
        inv.execute("INSERT INTO clips SELECT id, seq, track, start, "
                    "\"end\", kind, ref, base, p, q, rec_hash "
                    "FROM stg.clips")
        inv.execute("INSERT INTO coverage SELECT asset, lang, start, "
                    "\"end\", rec_hash FROM stg.coverage")
        inv.execute("INSERT INTO cues SELECT id, asset, lang, start, "
                    "\"end\", text, rec_hash FROM stg.cues")
        inv.execute("DETACH DATABASE stg")

        # Reverse reference edges, parents restricted to the reachable
        # set, so a trial's climb never leaves the registered universe.
        rows = []
        cur = stg.execute("SELECT ref, seq, id FROM clips "
                          "WHERE kind <> 'blank'")
        for ref, seq, cid in _fetch(cur, batch):
            if seq in reach:
                rows.append((ref, seq, cid))
                if len(rows) >= batch:
                    inv.executemany("INSERT INTO refs VALUES(?,?,?)", rows)
                    rows = []
        if rows:
            inv.executemany("INSERT INTO refs VALUES(?,?,?)", rows)

        engine = eng.Engine(stg, man, stats, batch)
        seg_writer = eng.BatchWriter(
            inv, "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)",
            batch, stats, "segments")
        for lang in sorted(man.languages):
            for asset in sorted(assets):
                engine.ensure_aseg(asset, lang)
            for seq in sorted(reach):
                engine.ensure_sseg(seq, lang)
            for prog in sorted(programs):
                def emit(seg, prog=prog, lang=lang):
                    s, e, state, text, cue_id, asset_id, path = seg
                    if state == eng.TRANSPARENT:
                        seg = (s, e, "silence", None, None, None, ())
                    seg_writer.write(
                        (prog, lang, seg[0], seg[1], seg[2], seg[3],
                         seg[4], seg[5],
                         json.dumps(eng.path_to_json(seg[6]),
                                    ensure_ascii=False)))
                engine.resolve_root(prog, lang, emit)
            seg_writer.flush()
            # Copy this language's materialised tables, then drop them.
            inv.execute("ATTACH DATABASE ? AS stg", (tmp_stg,))
            for asset in sorted(assets):
                tbl = engine.aseg_tbl[(asset, lang)]
                inv.execute(
                    "INSERT INTO aseg SELECT ?, ?, start, \"end\", state, "
                    "cue_id, text FROM stg.%s" % tbl, (asset, lang))
            for seq in sorted(reach):
                tbl = engine.sseg_tbl[(seq, lang)]
                inv.execute(
                    "INSERT INTO sseg SELECT ?, ?, start, \"end\", state, "
                    "text, cue_id, asset_id, path FROM stg.%s" % tbl,
                    (seq, lang))
            inv.execute("DETACH DATABASE stg")
            engine.drop_language_tables()
        for stmt in INDEXES:
            inv.execute(stmt)
        inv.execute("INSERT INTO meta VALUES('segments', ?)",
                    (str(stats["written"]["segments"]),))
        inv.close()
        inv = None
        with open(tmp_inv, "rb") as fh:
            os.fsync(fh.fileno())
        stg.close()
        stg = None
        os.link(tmp_inv, final)
        os.unlink(tmp_inv)
    finally:
        if inv is not None:
            inv.close()
        if stg is not None:
            stg.close()
        for tmp in (tmp_inv, tmp_stg):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if not os.path.lexists(final):
            # Nothing was published: remove the directory we created.
            try:
                os.rmdir(dir_path)
            except OSError:
                pass

    stats["elapsed_sec"] = round(time.monotonic() - started, 6)
    stats["peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    stats["mem_budget_mb"] = mem_budget_mb
    meta_out = dict(meta)
    meta_out["languages"] = json.loads(meta["languages"])
    meta_out["programs"] = json.loads(meta["programs"])
    return stats, meta_out


def open_inventory(dir_path):
    """Open a review directory read-only; raise ReviewError if invalid."""
    db = os.path.join(dir_path, DB_NAME)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    except sqlite3.Error as e:
        raise ReviewError("cannot open inventory %s: %s" % (db, e))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    except sqlite3.Error as e:
        conn.close()
        raise ReviewError("%s is not a review inventory: %s" % (db, e))
    if rows.get("format") != FORMAT:
        conn.close()
        raise ReviewError("%s: unsupported inventory format %r"
                          % (db, rows.get("format")))
    return conn, rows


# ---------------------------------------------------------------------------
# Correction orders: parsing and validation
# ---------------------------------------------------------------------------

_ORDER_KEYS = {"baseline", "order", "windows"}
_WIN_KEYS = {"asset", "lang", "start", "end", "coverage", "cues"}
_WIN_REQ = {"asset", "lang", "start", "end"}


def _load_order(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except OSError as e:
        raise ReviewError("cannot read order %s: %s" % (path, e))
    except ValueError as e:
        raise ReviewError("order %s is not valid JSON: %s" % (path, e))
    if not isinstance(obj, dict):
        raise ReviewError("order must be a JSON object")
    extra = set(obj) - _ORDER_KEYS
    if extra:
        raise ReviewError("order: unexpected keys: %s"
                          % ", ".join(sorted(extra)))
    missing = _ORDER_KEYS - set(obj)
    if missing:
        raise ReviewError("order: missing keys: %s"
                          % ", ".join(sorted(missing)))
    if not isinstance(obj["baseline"], str) or not obj["baseline"]:
        raise ReviewError("order: 'baseline' must be a non-empty string")
    try:
        order_id = check_id(obj["order"])
    except ValueError as e:
        raise ReviewError("order: 'order': %s" % e)
    if not isinstance(obj["windows"], list):
        raise ReviewError("order: 'windows' must be an array")
    return obj["baseline"], order_id, obj["windows"]


def _norm_window(w, i, man):
    """Validate one window's shape; return its normalised form with the
    original 0-based order index kept for diagnostics."""
    item = {"window": i}
    if not isinstance(w, dict):
        raise ReviewError("window %d: must be an object" % i, item)
    extra = set(w) - _WIN_KEYS
    if extra:
        raise ReviewError("window %d: unexpected fields: %s"
                          % (i, ", ".join(sorted(extra))), item)
    missing = _WIN_REQ - set(w)
    if missing:
        raise ReviewError("window %d: missing fields: %s"
                          % (i, ", ".join(sorted(missing))), item)
    asset, lang = w["asset"], w["lang"]
    try:
        check_id(asset)
    except ValueError as e:
        raise ReviewError("window %d: asset: %s" % (i, e), item)
    if asset not in man["assets"]:
        raise ReviewError(
            "window %d: asset %r is not declared in the manifest"
            % (i, asset), item)
    try:
        check_lang(lang)
    except ValueError as e:
        raise ReviewError("window %d: lang: %s" % (i, e), item)
    if lang not in man["languages"]:
        raise ReviewError(
            "window %d: language %r is not declared in the manifest"
            % (i, lang), item)
    s, e = w["start"], w["end"]
    if not is_int(s) or not is_int(e):
        raise ReviewError("window %d: start/end must be integers" % i,
                          item)
    frames = man["assets"][asset]
    if not (0 <= s < e):
        raise ReviewError(
            "window %d: empty or negative interval [%s,%s) "
            "(windows are [start,end), start < end)" % (i, s, e), item)
    if e > frames:
        raise ReviewError(
            "window %d: interval [%d,%d) exceeds asset %r length %d"
            % (i, s, e, asset, frames), item)
    cov = w.get("coverage", [])
    cues = w.get("cues", [])
    if not isinstance(cov, list) or not isinstance(cues, list):
        raise ReviewError(
            "window %d: 'coverage' and 'cues' must be arrays" % i, item)
    covn = []
    for j, c in enumerate(cov):
        it = {"window": i, "coverage": j}
        if not isinstance(c, dict) or set(c) != {"start", "end"}:
            raise ReviewError(
                "window %d coverage %d: must be an object with exactly "
                "'start' and 'end'" % (i, j), it)
        cs, ce = c["start"], c["end"]
        if not is_int(cs) or not is_int(ce):
            raise ReviewError(
                "window %d coverage %d: start/end must be integers"
                % (i, j), it)
        if not (s <= cs < ce <= e):
            raise ReviewError(
                "window %d coverage %d: [%s,%s) must be a non-empty "
                "interval inside the window [%d,%d)"
                % (i, j, cs, ce, s, e), it)
        covn.append((cs, ce))
    cuen = []
    for j, c in enumerate(cues):
        it = {"window": i, "cue": j}
        if not isinstance(c, dict) \
                or set(c) != {"id", "start", "end", "text"}:
            raise ReviewError(
                "window %d cue %d: must be an object with exactly "
                "'id', 'start', 'end', 'text'" % (i, j), it)
        try:
            cid = check_id(c["id"])
        except ValueError as ex:
            raise ReviewError("window %d cue %d: %s" % (i, j, ex), it)
        cs, ce = c["start"], c["end"]
        if not is_int(cs) or not is_int(ce):
            raise ReviewError(
                "window %d cue %r: start/end must be integers"
                % (i, cid), {"window": i, "cue": cid})
        if not (s <= cs < ce <= e):
            raise ReviewError(
                "window %d cue %r: [%s,%s) must be a non-empty interval "
                "inside the window [%d,%d)"
                % (i, cid, cs, ce, s, e), {"window": i, "cue": cid})
        if not isinstance(c["text"], str):
            raise ReviewError(
                "window %d cue %r: text must be a string" % (i, cid),
                {"window": i, "cue": cid})
        cuen.append((cs, ce, cid, c["text"]))
    return {"index": i, "asset": asset, "lang": lang, "start": s,
            "end": e, "coverage": sorted(covn),
            "cues": sorted(cuen, key=lambda c: (c[0], c[2]))}


def _validate_windows(trial, windows):
    """Cross-checks of the whole order against the baseline.  Any
    violation refuses the whole order and names the offending item."""
    by_pair = {}
    for w in windows:
        by_pair.setdefault((w["asset"], w["lang"]), []).append(w)
    new_ids = {}
    for (asset, lang), lst in sorted(by_pair.items()):
        lst.sort(key=lambda w: (w["start"], w["index"]))
        prev = None
        for w in lst:
            if prev is not None and w["start"] < prev["end"]:
                raise ReviewError(
                    "windows %d and %d overlap on asset %r lang %r "
                    "([%d,%d) vs [%d,%d))"
                    % (prev["index"], w["index"], asset, lang,
                       prev["start"], prev["end"],
                       w["start"], w["end"]), {"window": w["index"]})
            prev = w
        for w in lst:
            cues = w["cues"]
            for a, b in zip(cues, cues[1:]):
                if b[0] < a[1]:
                    raise ReviewError(
                        "window %d: cues %r and %r overlap on asset %r "
                        "lang %r" % (w["index"], a[2], b[2], asset, lang),
                        {"window": w["index"], "cue": b[2]})
            for c in cues:
                if c[2] in new_ids:
                    raise ReviewError(
                        "cue id %r is declared by both window %d and "
                        "window %d" % (c[2], new_ids[c[2]], w["index"]),
                        {"window": w["index"], "cue": c[2]})
                new_ids[c[2]] = w["index"]
            # New cues must lie inside the window's declared coverage.
            union = list(eng._union(iter(w["coverage"])))
            ui = 0
            for cs, ce, cid, _ in cues:
                while ui < len(union) and union[ui][1] <= cs:
                    ui += 1
                if ui == len(union) or union[ui][0] > cs \
                        or union[ui][1] < ce:
                    raise ReviewError(
                        "window %d cue %r [%d,%d) is not inside the "
                        "window's declared coverage of asset %r lang %r"
                        % (w["index"], cid, cs, ce, asset, lang),
                        {"window": w["index"], "cue": cid})
        # Baseline cues intersecting a window must lie wholly inside it.
        wins = [(w["start"], w["end"], w["index"]) for w in lst]
        cur = trial.inv.execute(
            "SELECT id, start, \"end\" FROM cues "
            "WHERE asset = ? AND lang = ? ORDER BY start", (asset, lang))
        wi = 0
        for cid, cs, ce in trial._batched(cur, "cues"):
            while wi < len(wins) and wins[wi][1] <= cs:
                wi += 1
            if wi == len(wins) or wins[wi][0] >= ce:
                continue
            ws, we, widx = wins[wi]
            if not (ws <= cs and ce <= we):
                raise ReviewError(
                    "baseline cue %r [%d,%d) intersects window %d "
                    "[%d,%d) but is not wholly inside it; intersecting "
                    "cues must be wholly contained in the same window "
                    "(no implicit truncation)"
                    % (cid, cs, ce, widx, ws, we),
                    {"window": widx, "cue": cid})
    # New cue ids must not collide with baseline cue ids that survive.
    for cid, widx in sorted(new_ids.items()):
        row = trial.inv.execute(
            "SELECT asset, lang, start, \"end\" FROM cues WHERE id = ?",
            (cid,)).fetchone()
        if row is None:
            continue
        trial.stats["read"]["cues"] += 1
        asset, lang, cs, ce = row
        removed = False
        for w in by_pair.get((asset, lang), ()):
            if w["start"] < ce and cs < w["end"]:
                removed = True      # intersecting == contained (checked)
                break
        if not removed:
            raise ReviewError(
                "cue id %r already exists in the baseline on asset %r "
                "lang %r and is not replaced by this order"
                % (cid, asset, lang), {"window": widx, "cue": cid})
    return by_pair


# ---------------------------------------------------------------------------
# The trial engine
# ---------------------------------------------------------------------------

def _playback_key(state, text):
    """What a viewer would judge: the verbatim text, a confirmed blank,
    or a pending frame."""
    if state == "cue":
        return ("cue", text)
    if state == "unknown":
        return ("unknown",)
    return ("none",)                      # blank and silence alike


def _evidence_json(state, text, cue_id, asset_id, path_json):
    obj = {"state": state}
    if state == "cue":
        obj["text"] = text
        obj["cue"] = cue_id
        obj["asset"] = asset_id
    elif state == "unknown":
        obj["asset"] = asset_id
    obj["path"] = json.loads(path_json)
    return canonical_json(obj).decode("utf-8")


def _merge2(a, b):
    """Merge two streams of intervals sorted by start."""
    a, b = iter(a), iter(b)
    x, y = next(a, None), next(b, None)
    while x is not None or y is not None:
        if y is None or (x is not None and x[0] <= y[0]):
            yield x
            x = next(a, None)
        else:
            yield y
            y = next(b, None)


class Trial(object):
    """One correction order measured against the directory's baseline."""

    def __init__(self, inv, man, stg, programs, stats, batch):
        self.inv = inv
        self.man = man
        self.stg = stg
        self.programs = programs
        self.stats = stats
        self.batch = batch
        self.cand = {}               # (seq, lang) -> merged windows
        self.patched_assets = set()  # (asset, lang) with paseg rows
        self._clips_cache = {}       # seq -> clip tuples
        self.psseg_writer = eng.BatchWriter(
            stg, "INSERT INTO psseg VALUES(?,?,?,?,?,?,?,?,?)",
            batch, stats, "psseg")
        self.rows_writer = eng.BatchWriter(
            stg, "INSERT INTO rows VALUES(?,?,?,?,?,?,?)",
            batch, {"written": {"rows": 0}}, "rows")

    # -- batched reads ----------------------------------------------------

    def _batched(self, cur, key):
        while True:
            rows = cur.fetchmany(self.batch)
            if not rows:
                return
            self.stats["read"][key] += len(rows)
            for r in rows:
                yield r

    def clips_of(self, seq):
        clips = self._clips_cache.get(seq)
        if clips is None:
            cur = self.inv.execute(
                "SELECT id, seq, track, start, \"end\", kind, ref, base, "
                "p, q FROM clips WHERE seq = ? ORDER BY track DESC, start",
                (seq,))
            clips = list(self._batched(cur, "clips"))
            self._clips_cache[seq] = clips
        return clips

    # -- patched asset views ------------------------------------------------

    def materialize_paseg(self, asset, lang, wins, new_cov, new_cues):
        """Materialise the patched opaque segments of one (asset, lang):
        baseline coverage minus the windows plus the declared coverage,
        baseline cues outside the windows plus the declared cues."""
        frames = self.man["assets"][asset]

        def base_cov():
            cur = self.inv.execute(
                "SELECT start, \"end\" FROM coverage "
                "WHERE asset = ? AND lang = ? ORDER BY start",
                (asset, lang))
            # Baseline intervals may overlap; union first so the pieces
            # left after subtracting the windows stay sorted.
            merged = eng._union(self._batched(cur, "coverage"))
            for s, e in merged:
                x = s
                for ws, we in wins:
                    if we <= x:
                        continue
                    if ws >= e:
                        break
                    if ws > x:
                        yield (x, ws)
                    x = we
                    if x >= e:
                        break
                if x < e:
                    yield (x, e)

        def base_cues():
            cur = self.inv.execute(
                "SELECT start, \"end\", id, text FROM cues "
                "WHERE asset = ? AND lang = ? ORDER BY start",
                (asset, lang))
            for s, e, cid, text in self._batched(cur, "cues"):
                for ws, we in wins:
                    if ws < e and s < we:
                        break           # intersecting == removed
                else:
                    yield (s, e, cid, text)

        cov = eng._union(_merge2(base_cov(), iter(new_cov)))
        cues = _merge2(base_cues(), iter(new_cues))
        out = eng.BatchWriter(
            self.stg, "INSERT INTO paseg VALUES(?,?,?,?,?,?,?)",
            self.batch, self.stats, "paseg")
        for s, e, state, cid, text in eng.asset_segments(frames, cues,
                                                         cov):
            out.write((asset, lang, s, e, state, cid, text))
        out.flush()
        self.patched_assets.add((asset, lang))

    # -- candidate windows ----------------------------------------------------

    def propagate(self, lang, touched_assets, pair_windows):
        """Climb the reverse reference graph from the touched assets and
        compute candidate windows for every touched sequence."""
        seen_seqs = set()
        queue = list(touched_assets)
        seen = set(queue)
        while queue:
            child = queue.pop()
            cur = self.inv.execute(
                "SELECT parent, clip FROM refs WHERE child = ?", (child,))
            for parent, _clip in self._batched(cur, "refs"):
                self.stats["refs_walked"] += 1
                if parent not in seen:
                    seen.add(parent)
                    seen_seqs.add(parent)
                    queue.append(parent)
        self.stats["sequences_touched"] += len(seen_seqs)
        if not seen_seqs:
            return []
        for seq in seen_seqs:
            self.clips_of(seq)

        height = {}
        stack = [(s, False) for s in seen_seqs]
        while stack:
            node, done = stack.pop()
            if done:
                if node in height:
                    continue
                h = 0
                for c in self.clips_of(node):
                    if c[5] == "blank":
                        continue
                    child = c[6]
                    if child in touched_assets:
                        ch = 0
                    elif child in seen_seqs:
                        ch = height[child]
                    else:
                        continue
                    if ch + 1 > h:
                        h = ch + 1
                height[node] = h
                continue
            if node in height:
                continue
            stack.append((node, True))
            for c in self.clips_of(node):
                if c[5] != "blank" and c[6] in seen_seqs \
                        and c[6] not in height:
                    stack.append((c[6], False))

        for seq in sorted(seen_seqs, key=lambda s: (height[s], s)):
            pieces = []
            for c in self.clips_of(seq):
                if c[5] == "blank":
                    continue
                child = c[6]
                if child in touched_assets:
                    cw = pair_windows[child]
                elif child in seen_seqs:
                    cw = self.cand.get((child, lang))
                    if not cw:
                        continue
                else:
                    continue
                length = c[4] - c[3]
                for ws, we in cw:
                    iv = eng.lift_interval(ws, we, c[7], c[8], c[9],
                                           length)
                    if iv is not None:
                        pieces.append((c[3] + iv[0], c[3] + iv[1]))
            if pieces:
                merged = list(eng._union(sorted(pieces)))
                self.cand[(seq, lang)] = merged
                self.stats["candidate_windows"] += len(merged)
                self.stats["candidate_frames"] += \
                    sum(e - s for s, e in merged)
        order = [s for s in seen_seqs if (s, lang) in self.cand]
        order.sort(key=lambda s: (height[s], s))
        return order

    # -- windowed re-resolution ------------------------------------------------

    def compute_sequence_window(self, seq, lang, lo, hi, emit):
        """Re-resolve one sequence over [lo, hi) only, reading untouched
        children from the baseline and touched ones from their patched
        windows; the full track stack is overlaid inside the window."""
        clips = self.clips_of(seq)
        tracks = sorted({c[2] for c in clips}, reverse=True)
        if not tracks:
            emit((lo, hi, eng.TRANSPARENT, None, None, None, ()))
            return
        streams = [self._window_track(seq, t, lang, lo, hi)
                   for t in tracks]
        eng.merge_streams(streams, lo, hi, emit, context=repr(seq))

    def _window_track(self, seq, track, lang, lo, hi):
        pos = lo
        for clip in self.clips_of(seq):
            if clip[2] != track or clip[3] >= hi or clip[4] <= lo:
                continue
            cstart = max(clip[3], lo)
            cend = min(clip[4], hi)
            if cstart > pos:
                yield (pos, cstart, eng.TRANSPARENT, None, None, None, ())
                pos = cstart
            for seg in self._clip_window(clip, lang, cstart, cend):
                if seg[0] > pos:
                    yield (pos, seg[0], eng.TRANSPARENT,
                           None, None, None, ())
                yield seg
                pos = seg[1]
            if pos < cend:
                yield (pos, cend, eng.TRANSPARENT, None, None, None, ())
            pos = cend
        if pos < hi:
            yield (pos, hi, eng.TRANSPARENT, None, None, None, ())

    def _clip_window(self, clip, lang, lo, hi):
        cid, seq, track, start, end, kind = clip[:6]
        if kind == "blank":
            yield (lo, hi, "blank", None, None, None,
                   (eng.blank_level(clip),))
            return
        ref, base, p, q = clip[6], clip[7], clip[8], clip[9]
        length = end - start
        k0 = lo - start
        k1 = hi - start
        f0 = base + (k0 * p) // q
        f1 = base + ((k1 - 1) * p) // q
        clo, chi = (f0, f1 + 1) if f0 <= f1 else (f1, f0 + 1)
        level = eng.ref_level(clip)
        for cs, ce, state, text, cue_id, asset_id, cpath in \
                self._child_stream(ref, kind, lang, clo, chi, p < 0):
            iv = eng.lift_interval(cs, ce, base, p, q, length)
            if iv is None:
                continue
            s = start + iv[0]
            e = start + iv[1]
            if e <= lo or s >= hi:
                continue
            if s < lo:
                s = lo
            if e > hi:
                e = hi
            yield (s, e, state, text, cue_id, asset_id, (level,) + cpath)

    def _child_stream(self, ref, kind, lang, clo, chi, desc):
        if kind == "asset":
            if (ref, lang) in self.patched_assets:
                yield from self._query_aseg(
                    self.stg, "paseg", "paseg", ref, lang, clo, chi, desc)
            else:
                yield from self._query_aseg(
                    self.inv, "aseg", "aseg", ref, lang, clo, chi, desc)
            return
        cw = self.cand.get((ref, lang))
        if not cw:
            yield from self._query_sseg(self.inv, "sseg", "sseg",
                                        ref, lang, clo, chi, desc)
            return
        for s, e, patched in _spans(cw, clo, chi, desc):
            if patched:
                yield from self._query_sseg(self.stg, "psseg", "psseg",
                                            ref, lang, s, e, desc)
            else:
                yield from self._query_sseg(self.inv, "sseg", "sseg",
                                            ref, lang, s, e, desc)

    def _query_aseg(self, conn, table, key, asset, lang, lo, hi, desc):
        cur = conn.execute(
            "SELECT start, \"end\", state, cue_id, text FROM %s "
            "WHERE asset = ? AND lang = ? AND start < ? AND \"end\" > ? "
            "ORDER BY start %s"
            % (table, "DESC" if desc else "ASC"), (asset, lang, hi, lo))
        for s, e, state, cid, text in self._batched(cur, key):
            # Clip to the queried window: a row read for one span must
            # never leak baseline content into a neighbouring patched
            # span (or vice versa).
            yield (max(s, lo), min(e, hi), state, text, cid, asset, ())

    def _query_sseg(self, conn, table, key, seq, lang, lo, hi, desc):
        cur = conn.execute(
            "SELECT start, \"end\", state, text, cue_id, asset_id, path "
            "FROM %s WHERE seq = ? AND lang = ? AND start < ? "
            "AND \"end\" > ? ORDER BY start %s"
            % (table, "DESC" if desc else "ASC"), (seq, lang, hi, lo))
        for row in self._batched(cur, key):
            s, e, state, text, cue_id, asset_id, path_json = row
            yield (max(s, lo), min(e, hi), state, text, cue_id, asset_id,
                   eng.path_from_json(json.loads(path_json)))

    # -- recompute + diff ----------------------------------------------------

    def recompute(self, seq, lang, lo, hi):
        diff = _WindowDiff(self, seq, lang, lo, hi) \
            if seq in self.programs else None
        writer = self.psseg_writer

        def emit(seg):
            s, e, state, text, cue_id, asset_id, path = seg
            if state != eng.TRANSPARENT:
                writer.write((seq, lang, s, e, state, text, cue_id,
                              asset_id,
                              json.dumps(eng.path_to_json(path),
                                         ensure_ascii=False)))
            if diff is not None:
                if state == eng.TRANSPARENT:
                    mapped = (s, e, "silence", None, None, None, "[]")
                else:
                    mapped = (s, e, state, text, cue_id, asset_id,
                              json.dumps(eng.path_to_json(path),
                                         ensure_ascii=False))
                diff.push(mapped)

        self.compute_sequence_window(seq, lang, lo, hi, emit)
        if diff is not None:
            diff.finish()

    def _baseline_segments(self, prog, lang, lo, hi):
        cur = self.inv.execute(
            "SELECT start, \"end\", state, text, cue_id, asset_id, path "
            "FROM segments WHERE program = ? AND lang = ? AND start < ? "
            "AND \"end\" > ? ORDER BY start", (prog, lang, hi, lo))
        for s, e, state, text, cue_id, asset_id, path in \
                self._batched(cur, "segments"):
            yield (max(s, lo), min(e, hi), state, text, cue_id,
                   asset_id, path)

    def emit_row(self, prog, lang, s, e, before, after):
        cls = "playback" \
            if _playback_key(before[2], before[3]) \
                != _playback_key(after[2], after[3]) \
            else "evidence"
        self.rows_writer.write(
            (prog, lang, s, e, cls,
             _evidence_json(*before[2:]), _evidence_json(*after[2:])))
        self.stats["review_rows"] += 1


def _spans(cw, lo, hi, desc):
    """Split [lo, hi) into parts inside the candidate windows (patched)
    and outside them (baseline)."""
    spans = []
    x = lo
    for ws, we in cw:
        if we <= lo or ws >= hi:
            continue
        if ws > x:
            spans.append((x, min(ws, hi), False))
        spans.append((max(ws, lo), min(we, hi), True))
        x = min(we, hi)
    if x < hi:
        spans.append((x, hi, False))
    if desc:
        spans.reverse()
    return spans


class _WindowDiff(object):
    """Streaming two-pointer diff of one candidate window: the patched
    segmentation is pushed segment by segment while the baseline
    segmentation is pulled lazily, so neither side is materialised."""

    def __init__(self, trial, prog, lang, lo, hi):
        self.trial = trial
        self.prog = prog
        self.lang = lang
        self.base = trial._baseline_segments(prog, lang, lo, hi)
        self.b = next(self.base, None)

    def push(self, n):
        ns, ne = n[0], n[1]
        pos = ns
        while pos < ne:
            b = self.b
            if b is None or not (b[0] <= pos < b[1]):
                raise AssertionError(
                    "baseline stream desynchronized in %r" % self.prog)
            e = min(b[1], ne)
            if b[2:] != n[2:]:
                self.trial.emit_row(self.prog, self.lang, pos, e, b, n)
            if b[1] == e:
                self.b = next(self.base, None)
            pos = e

    def finish(self):
        if self.b is not None:
            raise AssertionError(
                "baseline stream left unconsumed in %r" % self.prog)


# ---------------------------------------------------------------------------
# Trial driver
# ---------------------------------------------------------------------------

def run_trial(dir_path, order_path, out_path, mem_budget_mb=256):
    """Measure one correction order against the directory's baseline and
    write the review list.  Returns (summary, stats).  The directory is
    only read; all scratch work lives in a temporary file that is always
    removed.  Any ReviewError refuses the whole order before the list is
    written."""
    inv, meta = open_inventory(dir_path)
    try:
        man = json.loads(meta["manifest"])
        programs = set(json.loads(meta["programs"]))
        baseline, order_id, raw = _load_order(order_path)
        if baseline != meta["baseline"]:
            raise ReviewError(
                "baseline mismatch: order targets %s but the inventory "
                "baseline is %s" % (baseline, meta["baseline"]))
        windows = [_norm_window(w, i, man) for i, w in enumerate(raw)]
        windows.sort(key=lambda w: (w["asset"], w["lang"], w["start"],
                                    w["index"]))

        cache_kb, batch = _budget(mem_budget_mb)
        stats = new_trial_stats()
        stats["windows"] = len(windows)
        started = time.monotonic()
        token = "%d-%s" % (os.getpid(), secrets.token_hex(4))
        tmp_stg = os.path.join(dir_path, ".trial-%s.tmp" % token)
        tmp_out = "%s.trial-%s.tmp" % (out_path, token)
        stg = None
        try:
            stg = _connect(tmp_stg, cache_kb)
            stg.executescript(STAGING_SCHEMA)
            trial = Trial(inv, man, stg, programs, stats, batch)
            by_pair = _validate_windows(trial, windows)
            pairs = {}
            for (asset, lang), lst in by_pair.items():
                wins = [(w["start"], w["end"]) for w in lst]
                cov = sorted(iv for w in lst for iv in w["coverage"])
                cues = sorted((c for w in lst for c in w["cues"]),
                              key=lambda c: (c[0], c[2]))
                pairs[(asset, lang)] = (wins, cov, cues)
            touched_langs = sorted({lang for _, lang in pairs})
            stats["languages"] = touched_langs
            for lang in touched_langs:
                touched_assets = {a for (a, l) in pairs if l == lang}
                pair_windows = {a: pairs[(a, lang)][0]
                                for a in touched_assets}
                order = trial.propagate(lang, touched_assets,
                                        pair_windows)
                # Patched asset views only where a rejudged sequence can
                # actually sample the asset.
                needed = set()
                for seq in order:
                    for c in trial.clips_of(seq):
                        if c[5] != "blank" and c[6] in touched_assets:
                            needed.add(c[6])
                for asset in sorted(needed):
                    wins, cov, cues = pairs[(asset, lang)]
                    trial.materialize_paseg(asset, lang, wins, cov, cues)
                for seq in order:
                    if seq in programs:
                        stats["programs_rejudged"] += 1
                    stats["sequences_rejudged"] += 1
                    for lo, hi in trial.cand[(seq, lang)]:
                        trial.recompute(seq, lang, lo, hi)
                    # Parents recomputed later must see this sequence's
                    # patched segments.
                    trial.psseg_writer.flush()
                trial.cand.clear()
                trial.patched_assets.clear()
                trial._clips_cache.clear()
            trial.psseg_writer.flush()
            trial.rows_writer.flush()

            counts = {}
            n_rows = 0
            with open(tmp_out, "w", encoding="utf-8") as out:
                cur = stg.execute(
                    "SELECT program, lang, start, \"end\", class, "
                    "before, after FROM rows "
                    "ORDER BY program, lang, start")
                prev = None
                for row in _fetch(cur, batch):
                    prog, lang, s, e, cls, bj, aj = row
                    if prev is not None and prev[0] == prog \
                            and prev[1] == lang and prev[3] == s \
                            and prev[4] == cls and prev[5] == bj \
                            and prev[6] == aj:
                        prev[3] = e
                        continue
                    if prev is not None:
                        n_rows += _write_row(out, counts, prev)
                    prev = [prog, lang, s, e, cls, bj, aj]
                if prev is not None:
                    n_rows += _write_row(out, counts, prev)
            os.replace(tmp_out, out_path)
        finally:
            if stg is not None:
                stg.close()
            for tmp in (tmp_stg, tmp_out):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    finally:
        inv.close()

    stats["elapsed_sec"] = round(time.monotonic() - started, 6)
    stats["peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    stats["mem_budget_mb"] = mem_budget_mb
    summary = {
        "ok": True,
        "dir": dir_path,
        "baseline": meta["baseline"],
        "order": order_id,
        "list": out_path,
        "programs_affected": len(counts),
        "segments": n_rows,
        "counts": counts,
    }
    return summary, stats


def _write_row(out, counts, prev):
    prog, lang, s, e, cls, bj, aj = prev
    line = {"program": prog, "lang": lang, "start": s, "end": e,
            "class": cls, "before": json.loads(bj),
            "after": json.loads(aj)}
    out.write(json.dumps(line, ensure_ascii=False) + "\n")
    counts.setdefault(prog, {}).setdefault(lang, {}).setdefault(cls, 0)
    counts[prog][lang][cls] += e - s
    return 1
