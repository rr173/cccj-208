"""Package ingestion: JSONL tables -> validated staging tables (on disk).

Tables delivered by the handover package (UTF-8 JSONL, records in any order,
blank lines ignored; a missing file means an empty table):

clips.jsonl    {"id","seq","track","start","end","kind","ref","base","p","q"}
               kind is "asset" | "seq" | "blank".  Blank clips carry no
               ref/base/p/q.  A reference clip reads child frame
               base + floor(k*p/q) at local frame start+k (p != 0, q > 0,
               floor rounds toward -infinity).
coverage.jsonl {"asset","lang","start","end"}     (intervals may overlap)
cues.jsonl     {"id","asset","lang","start","end","text"}

Every record is validated; invalid records are rejected together with their
file and 1-based line number.  Global rules (duplicate ids, same-track
overlap, cue containment in checked coverage, reference cycles) are checked
afterwards with SQL over the staging tables, so input larger than memory is
fine: SQLite sorts and scans on disk.
"""

import json
import os

from .errors import PackageError
from .manifest import CLIPS_NAME, COVERAGE_NAME, CUES_NAME
from .model import check_id, check_lang, is_int, record_hash

SCHEMA = """
CREATE TABLE clips(
  id TEXT NOT NULL, seq TEXT NOT NULL, track INTEGER NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  kind TEXT NOT NULL, ref TEXT, base INTEGER, p INTEGER, q INTEGER,
  file TEXT NOT NULL, line INTEGER NOT NULL, rec_hash TEXT NOT NULL);
CREATE TABLE coverage(
  asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  file TEXT NOT NULL, line INTEGER NOT NULL, rec_hash TEXT NOT NULL);
CREATE TABLE cues(
  id TEXT NOT NULL, asset TEXT NOT NULL, lang TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL, text TEXT NOT NULL,
  file TEXT NOT NULL, line INTEGER NOT NULL, rec_hash TEXT NOT NULL);
"""

INDEXES = [
    "CREATE INDEX idx_clips_seq ON clips(seq, track, start)",
    "CREATE INDEX idx_coverage ON coverage(asset, lang, start)",
    "CREATE INDEX idx_cues ON cues(asset, lang, start)",
]

_CLIP_KEYS = {"id", "seq", "track", "start", "end", "kind",
              "ref", "base", "p", "q"}
_CLIP_KEYS_BLANK = {"id", "seq", "track", "start", "end", "kind"}
_COV_KEYS = {"asset", "lang", "start", "end"}
_CUE_KEYS = {"id", "asset", "lang", "start", "end", "text"}


def _iter_jsonl(path):
    """Yield (line_no, object) for each record; skip blank lines."""
    if not os.path.exists(path):
        return
    try:
        fh = open(path, "r", encoding="utf-8", errors="strict")
    except OSError as e:
        raise PackageError("cannot read table: %s" % e, file=path)
    with fh:
        for n, raw in enumerate(fh, 1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise PackageError("invalid JSON: %s" % e.msg, file=path,
                                   line=n)
            if not isinstance(obj, dict):
                raise PackageError("record must be a JSON object",
                                   file=path, line=n)
            yield n, obj


def _check_keys(obj, allowed, required, path, line):
    extra = set(obj) - allowed
    if extra:
        raise PackageError("unexpected fields: %s" % ", ".join(sorted(extra)),
                           file=path, line=line)
    missing = required - set(obj)
    if missing:
        raise PackageError("missing fields: %s" % ", ".join(sorted(missing)),
                           file=path, line=line)


def _interval(obj, frames, what, path, line):
    s, e = obj["start"], obj["end"]
    if not is_int(s) or not is_int(e):
        raise PackageError("%s: start/end must be integers" % what,
                           file=path, line=line)
    if not (0 <= s < e):
        raise PackageError("%s: empty or negative interval [%s,%s) "
                           "(intervals are [start,end), start < end)"
                           % (what, s, e), file=path, line=line)
    if e > frames:
        raise PackageError("%s: interval [%s,%s) exceeds object length %d"
                           % (what, s, e, frames), file=path, line=line)
    return s, e


def _norm_clip(obj, man, path, line):
    kind = obj.get("kind")
    if kind == "blank":
        _check_keys(obj, _CLIP_KEYS_BLANK, _CLIP_KEYS_BLANK, path, line)
    else:
        _check_keys(obj, _CLIP_KEYS, _CLIP_KEYS, path, line)
    try:
        cid = check_id(obj["id"])
        seq = check_id(obj["seq"])
    except ValueError as e:
        raise PackageError("clip: %s" % e, file=path, line=line)
    if seq not in man.sequences:
        raise PackageError("clip %r: sequence %r is not declared in the "
                           "manifest" % (cid, seq), file=path, line=line)
    track = obj["track"]
    if not is_int(track):
        raise PackageError("clip %r: track must be an integer" % cid,
                           file=path, line=line)
    start, end = _interval(obj, man.sequences[seq], "clip %r" % cid,
                           path, line)
    if kind not in ("asset", "seq", "blank"):
        raise PackageError("clip %r: kind must be 'asset', 'seq' or 'blank'"
                           % cid, file=path, line=line)
    rec = {"id": cid, "seq": seq, "track": track, "start": start,
           "end": end, "kind": kind}
    if kind == "blank":
        return rec
    ref = obj["ref"]
    try:
        check_id(ref)
    except ValueError as e:
        raise PackageError("clip %r: ref: %s" % (cid, e), file=path,
                           line=line)
    pool = man.assets if kind == "asset" else man.sequences
    if ref not in pool:
        raise PackageError("clip %r: %s %r is not declared in the manifest"
                           % (cid, kind, ref), file=path, line=line)
    base, p, q = obj["base"], obj["p"], obj["q"]
    if not is_int(base):
        raise PackageError("clip %r: base must be an integer" % cid,
                           file=path, line=line)
    if not is_int(p) or p == 0:
        raise PackageError("clip %r: p must be a non-zero integer" % cid,
                           file=path, line=line)
    if not is_int(q) or q < 1:
        raise PackageError("clip %r: q must be a positive integer" % cid,
                           file=path, line=line)
    # Sampling bounds: child frame = base + floor(k*p/q) is monotonic in k,
    # so checking the two endpoints k=0 and k=length-1 covers every frame.
    length = end - start
    child_frames = pool[ref]
    f0 = base
    f1 = base + ((length - 1) * p) // q
    lo, hi = (f0, f1) if f0 <= f1 else (f1, f0)
    if lo < 0 or hi >= child_frames:
        raise PackageError(
            "clip %r: sampling leaves %s %r: child frames %d..%d sampled "
            "but %r has %d frames" % (cid, kind, ref, lo, hi, ref,
                                      child_frames),
            file=path, line=line)
    rec.update({"ref": ref, "base": base, "p": p, "q": q})
    return rec


def _norm_coverage(obj, man, path, line):
    _check_keys(obj, _COV_KEYS, _COV_KEYS, path, line)
    asset = obj["asset"]
    lang = obj["lang"]
    if asset not in man.assets:
        raise PackageError("coverage: asset %r is not declared in the "
                           "manifest" % asset, file=path, line=line)
    if lang not in man.languages:
        raise PackageError("coverage: language %r is not declared in the "
                           "manifest" % lang, file=path, line=line)
    s, e = _interval(obj, man.assets[asset], "coverage", path, line)
    return {"asset": asset, "lang": lang, "start": s, "end": e}


def _norm_cue(obj, man, path, line):
    _check_keys(obj, _CUE_KEYS, _CUE_KEYS, path, line)
    try:
        cid = check_id(obj["id"])
    except ValueError as e:
        raise PackageError("cue: %s" % e, file=path, line=line)
    asset, lang, text = obj["asset"], obj["lang"], obj["text"]
    if asset not in man.assets:
        raise PackageError("cue %r: asset %r is not declared in the "
                           "manifest" % (cid, asset), file=path, line=line)
    if lang not in man.languages:
        raise PackageError("cue %r: language %r is not declared in the "
                           "manifest" % (cid, lang), file=path, line=line)
    if not isinstance(text, str):
        raise PackageError("cue %r: text must be a string" % cid,
                           file=path, line=line)
    s, e = _interval(obj, man.assets[asset], "cue %r" % cid, path, line)
    return {"id": cid, "asset": asset, "lang": lang, "start": s, "end": e,
            "text": text}


def _batched(cur, sql, rows, batch):
    for i in range(0, len(rows), batch):
        cur.executemany(sql, rows[i:i + batch])


def _fetch_batched(cur, batch):
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield r


def ingest_package(stg, pkg_dir, man, stats, batch):
    """Read the three JSONL tables into the staging database (validated)."""
    stg.executescript(SCHEMA)
    cur = stg.cursor()

    clips_path = os.path.join(pkg_dir, CLIPS_NAME)
    rows = []
    for line, obj in _iter_jsonl(clips_path):
        rec = _norm_clip(obj, man, clips_path, line)
        rows.append((rec["id"], rec["seq"], rec["track"], rec["start"],
                     rec["end"], rec["kind"], rec.get("ref"),
                     rec.get("base"), rec.get("p"), rec.get("q"),
                     CLIPS_NAME, line, record_hash(rec)))
        if len(rows) >= batch:
            _batched(cur, "INSERT INTO clips VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     rows, batch)
            stats["ingested"]["clips"] += len(rows)
            rows = []
    if rows:
        _batched(cur, "INSERT INTO clips VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 rows, batch)
        stats["ingested"]["clips"] += len(rows)

    cov_path = os.path.join(pkg_dir, COVERAGE_NAME)
    rows = []
    for line, obj in _iter_jsonl(cov_path):
        rec = _norm_coverage(obj, man, cov_path, line)
        rows.append((rec["asset"], rec["lang"], rec["start"], rec["end"],
                     COVERAGE_NAME, line, record_hash(rec)))
        if len(rows) >= batch:
            _batched(cur, "INSERT INTO coverage VALUES(?,?,?,?,?,?,?)",
                     rows, batch)
            stats["ingested"]["coverage"] += len(rows)
            rows = []
    if rows:
        _batched(cur, "INSERT INTO coverage VALUES(?,?,?,?,?,?,?)",
                 rows, batch)
        stats["ingested"]["coverage"] += len(rows)

    cues_path = os.path.join(pkg_dir, CUES_NAME)
    rows = []
    for line, obj in _iter_jsonl(cues_path):
        rec = _norm_cue(obj, man, cues_path, line)
        rows.append((rec["id"], rec["asset"], rec["lang"], rec["start"],
                     rec["end"], rec["text"], CUES_NAME, line,
                     record_hash(rec)))
        if len(rows) >= batch:
            _batched(cur, "INSERT INTO cues VALUES(?,?,?,?,?,?,?,?,?)",
                     rows, batch)
            stats["ingested"]["cues"] += len(rows)
            rows = []
    if rows:
        _batched(cur, "INSERT INTO cues VALUES(?,?,?,?,?,?,?,?,?)",
                 rows, batch)
        stats["ingested"]["cues"] += len(rows)

    for stmt in INDEXES:
        stg.execute(stmt)
    _global_checks(stg, man, batch)


def _err_at(row, msg):
    # row tail is (..., file, line)
    raise PackageError(msg, file=row[-2], line=row[-1])


def _global_checks(stg, man, batch):
    # Duplicate cue ids / clip ids (ids are stable and package-wide unique).
    for table, what in (("cues", "cue"), ("clips", "clip")):
        row = stg.execute(
            "SELECT id FROM %s GROUP BY id HAVING COUNT(*) > 1 LIMIT 1"
            % table).fetchone()
        if row:
            dup = row[0]
            locs = stg.execute(
                "SELECT file, line FROM %s WHERE id = ? ORDER BY file, line"
                % table, (dup,)).fetchall()
            where = "; ".join("%s:%d" % (f, l) for f, l in locs)
            first = locs[0]
            raise PackageError(
                "duplicate %s id %r (defined at %s)" % (what, dup, where),
                file=first[0], line=first[1])

    # Same-track overlap: clips on one (seq, track) must not intersect.
    cur = stg.execute("SELECT seq, track, start, \"end\", id, file, line "
                      "FROM clips ORDER BY seq, track, start")
    prev = None
    for row in _fetch_batched(cur, batch):
        key = (row[0], row[1])
        if prev and prev[0] == key and row[2] < prev[1][1]:
            raise PackageError(
                "clips %r and %r overlap on track %d of sequence %r "
                "([%d,%d) vs [%d,%d))" % (prev[1][2], row[4], row[1],
                                          row[0], prev[1][0], prev[1][1],
                                          row[2], row[3]),
                file=row[5], line=row[6])
        if not prev or prev[0] != key or row[3] > prev[1][1]:
            prev = (key, (row[2], row[3], row[4]))

    # Per (asset, lang): cues must not overlap and every cue frame must lie
    # inside the union of checked coverage.
    pairs = stg.execute("SELECT DISTINCT asset, lang FROM cues")
    for asset, lang in pairs.fetchall():
        cov = _fetch_batched(
            stg.execute("SELECT start, \"end\" FROM coverage "
                        "WHERE asset = ? AND lang = ? ORDER BY start",
                        (asset, lang)), batch)
        cov = _union_iter(cov)
        cs, ce = next(cov, (None, None))
        prev_cue = None
        cues = _fetch_batched(
            stg.execute("SELECT start, \"end\", id, file, line FROM cues "
                        "WHERE asset = ? AND lang = ? ORDER BY start",
                        (asset, lang)), batch)
        for s, e, cid, file, line in cues:
            if prev_cue and s < prev_cue[0]:
                raise PackageError(
                    "cues %r and %r overlap on asset %r lang %r"
                    % (prev_cue[1], cid, asset, lang), file=file, line=line)
            prev_cue = (e, cid)
            while cs is not None and ce <= s:
                cs, ce = next(cov, (None, None))
            if cs is None or cs > s or ce < e:
                raise PackageError(
                    "cue %r [%d,%d) is not fully inside checked coverage of "
                    "asset %r lang %r" % (cid, s, e, asset, lang),
                    file=file, line=line)

    # Reference cycles among sequences (including self references).
    edges = {}
    cur = stg.execute("SELECT DISTINCT seq, ref FROM clips "
                      "WHERE kind = 'seq'")
    for seq, ref in _fetch_batched(cur, batch):
        edges.setdefault(seq, []).append(ref)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    for root in list(edges):
        if color.get(root, WHITE) != WHITE:
            continue
        stack = [(root, iter(edges.get(root, ())))]
        color[root] = GRAY
        path = [root]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    cycle = path[path.index(nxt):] + [nxt]
                    loc = stg.execute(
                        "SELECT file, line FROM clips WHERE kind='seq' "
                        "AND seq = ? AND ref = ? LIMIT 1",
                        (node, nxt)).fetchone()
                    raise PackageError(
                        "reference cycle among sequences: %s"
                        % " -> ".join(cycle),
                        file=loc[0] if loc else None,
                        line=loc[1] if loc else None)
                if c == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(edges.get(nxt, ()))))
                    path.append(nxt)
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
                path.pop()


def _union_iter(intervals):
    """Merge overlapping or touching [s, e) intervals into a union stream."""
    cs = ce = None
    for s, e in intervals:
        if cs is None:
            cs, ce = s, e
        elif s <= ce:
            if e > ce:
                ce = e
        else:
            yield cs, ce
            cs, ce = s, e
    if cs is not None:
        yield cs, ce
