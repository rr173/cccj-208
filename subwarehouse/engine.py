"""Resolution engine: turns clips + coverage + cues into final segments.

Everything works on whole intervals, never per frame.  A reference clip
maps child segment [cs, ce) to one parent-local interval of frames k with
base + floor(k*p/q) inside [cs, ce); because the mapping is monotonic this
preimage is a single integer interval computed in closed form.  Nested
sequences are resolved bottom-up once per (sequence, language) and
materialized into on-disk staging tables, so peak memory stays bounded by
the number of tracks plus small read/write buffers, independent of the
frame span.

Segment tuple: (start, end, state, text, cue_id, asset_id, path)
  state    'transparent' | 'cue' | 'blank' | 'unknown'
  path     tuple of levels, root-ward first; each level is either
           ("ref", clip, seq, track, cstart, cend, kind, ref, base, p, q)
           ("blank", clip, seq, track, cstart, cend)
"""

import json

TRANSPARENT = "transparent"


def ceil_div(a, b):
    """ceil(a/b) for b > 0, any sign of a."""
    return -((-a) // b)


def lift_interval(cs, ce, base, p, q, length):
    """Parent-local k interval whose samples fall in child [cs, ce).

    Returns (k0, k1) intersected with [0, length), or None if empty.
    Derived from floor(k*p/q) >= m  <=>  k*p/q >= m  (m integer):
      p > 0: k >= ceil((cs-base)*q/p) and k <  ceil((ce-base)*q/p)
      p < 0: k >= floor((ce-base)*q/p)+1 and k <= floor((cs-base)*q/p)
    """
    if p > 0:
        k0 = ceil_div((cs - base) * q, p)
        k1 = ceil_div((ce - base) * q, p)
    else:
        k0 = ((ce - base) * q) // p + 1
        k1 = ((cs - base) * q) // p + 1
    if k0 < 0:
        k0 = 0
    if k1 > length:
        k1 = length
    if k0 >= k1:
        return None
    return (k0, k1)


def ref_level(clip):
    return ("ref", clip[0], clip[1], clip[2], clip[3], clip[4],
            clip[5], clip[6], clip[7], clip[8], clip[9])


def blank_level(clip):
    return ("blank", clip[0], clip[1], clip[2], clip[3], clip[4])


def path_to_json(path):
    out = []
    for lv in path:
        if lv[0] == "blank":
            out.append({"clip": lv[1], "seq": lv[2], "track": lv[3],
                        "start": lv[4], "end": lv[5], "kind": "blank"})
        else:
            out.append({"clip": lv[1], "seq": lv[2], "track": lv[3],
                        "start": lv[4], "end": lv[5], "kind": lv[6],
                        "ref": lv[7], "base": lv[8], "p": lv[9],
                        "q": lv[10]})
    return out


def path_from_json(obj):
    path = []
    for lv in obj:
        if lv["kind"] == "blank":
            path.append(("blank", lv["clip"], lv["seq"], lv["track"],
                         lv["start"], lv["end"]))
        else:
            path.append(("ref", lv["clip"], lv["seq"], lv["track"],
                         lv["start"], lv["end"], lv["kind"], lv["ref"],
                         lv["base"], lv["p"], lv["q"]))
    return tuple(path)


class BatchWriter(object):
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


def _batched(cur, batch, stats, key):
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        stats["read"][key] += len(rows)
        for r in rows:
            yield r


class Engine(object):
    def __init__(self, stg, man, stats, batch):
        self.stg = stg
        self.man = man
        self.stats = stats
        self.batch = batch
        self.aseg_tbl = {}   # (asset, lang) -> staging table
        self.sseg_tbl = {}   # (seq, lang)   -> staging table
        self._n = 0

    # ---- staging tables ------------------------------------------------

    def _new_table(self, prefix, ddl):
        self._n += 1
        name = "%s_%d" % (prefix, self._n)
        self.stg.execute("CREATE TABLE %s(%s)" % (name, ddl))
        return name

    def drop_language_tables(self):
        for tbl in list(self.aseg_tbl.values()) + list(self.sseg_tbl.values()):
            self.stg.execute("DROP TABLE IF EXISTS %s" % tbl)
        self.aseg_tbl.clear()
        self.sseg_tbl.clear()

    # ---- asset level ---------------------------------------------------

    def ensure_aseg(self, asset, lang):
        """Materialize opaque asset segments: cue and unknown intervals."""
        key = (asset, lang)
        tbl = self.aseg_tbl.get(key)
        if tbl:
            return tbl
        self.stats["objects"]["assets"] += 1
        tbl = self._new_table(
            "aseg", 'start INTEGER NOT NULL, "end" INTEGER NOT NULL, '
                    'state TEXT NOT NULL, cue_id TEXT, text TEXT')
        out = BatchWriter(
            self.stg, "INSERT INTO %s VALUES(?,?,?,?,?)" % tbl,
            self.batch, self.stats, "aseg")
        frames = self.man.assets[asset]
        cues = _batched(
            self.stg.execute(
                "SELECT start, \"end\", id, text FROM cues "
                "WHERE asset = ? AND lang = ? ORDER BY start",
                (asset, lang)), self.batch, self.stats, "cues")
        cov = _batched(
            self.stg.execute(
                "SELECT start, \"end\" FROM coverage "
                "WHERE asset = ? AND lang = ? ORDER BY start",
                (asset, lang)), self.batch, self.stats, "coverage")
        cov = _union(cov)
        cs, ce = next(cov, (None, None))
        pos = 0

        def classify(g0, g1):
            nonlocal cs, ce
            x = g0
            while x < g1:
                while cs is not None and ce <= x:
                    cs, ce = next(cov, (None, None))
                if cs is None or cs >= g1:
                    out.write((x, g1, "unknown", None, None))
                    return
                if cs > x:
                    out.write((x, min(cs, g1), "unknown", None, None))
                    x = min(cs, g1)
                else:
                    x = min(ce, g1)   # covered: transparent, not stored

        for s, e, cid, text in cues:
            classify(pos, s)
            out.write((s, e, "cue", cid, text))
            pos = e
        classify(pos, frames)
        out.flush()
        self.stg.execute("CREATE INDEX %s_i ON %s(start)" % (tbl, tbl))
        self.aseg_tbl[key] = tbl
        return tbl

    # ---- sequence level --------------------------------------------------

    def ensure_sseg(self, seq, lang):
        key = (seq, lang)
        tbl = self.sseg_tbl.get(key)
        if tbl:
            return tbl
        self.stats["objects"]["sequences"] += 1
        tbl = self._new_table(
            "sseg", 'start INTEGER NOT NULL, "end" INTEGER NOT NULL, '
                    'state TEXT NOT NULL, text TEXT, cue_id TEXT, '
                    'asset_id TEXT, path TEXT NOT NULL')
        out = BatchWriter(
            self.stg, "INSERT INTO %s VALUES(?,?,?,?,?,?,?)" % tbl,
            self.batch, self.stats, "sseg")

        def emit(seg):
            s, e, state, text, cue_id, asset_id, path = seg
            if state == TRANSPARENT:
                return
            out.write((s, e, state, text, cue_id, asset_id,
                       json.dumps(path_to_json(path), ensure_ascii=False)))

        self.compute_sequence(seq, lang, emit)
        out.flush()
        self.stg.execute("CREATE INDEX %s_i ON %s(start)" % (tbl, tbl))
        self.sseg_tbl[key] = tbl
        return tbl

    def compute_sequence(self, seq, lang, emit):
        """Overlay all tracks of `seq`, highest level first, and emit the
        merged segmentation of [0, frames) via `emit(seg)`."""
        frames = self.man.sequences[seq]
        tracks = [r[0] for r in self.stg.execute(
            "SELECT DISTINCT track FROM clips WHERE seq = ? "
            "ORDER BY track DESC", (seq,))]
        self.stats["read"]["clips"] += len(tracks)
        if not tracks:
            emit((0, frames, TRANSPARENT, None, None, None, ()))
            return
        streams = [self._track_segments(seq, t, lang, frames)
                   for t in tracks]
        currents = [next(s) for s in streams]
        pos = 0
        pending = None
        while pos < frames:
            for c in currents:            # invariant: each stream covers pos
                if not (c[0] <= pos < c[1]):
                    raise AssertionError(
                        "track stream desynchronized in %r: segment %s "
                        "does not cover %d" % (seq, c[:2], pos))
            nxt = min(c[1] for c in currents)
            win = None
            for c in currents:            # tracks already high -> low
                if c[2] != TRANSPARENT:
                    win = c
                    break
            if win is None:
                seg = (pos, nxt, TRANSPARENT, None, None, None, ())
            else:
                seg = (pos, nxt) + win[2:]
            if pending is not None and pending[2:] == seg[2:] \
                    and pending[1] == seg[0]:
                pending = (pending[0], seg[1]) + pending[2:]
            else:
                if pending is not None:
                    emit(pending)
                pending = seg
            pos = nxt
            if pos < frames:
                for i, c in enumerate(currents):
                    if c[1] == pos:
                        currents[i] = next(streams[i])
        if pending is not None:
            emit(pending)

    def _track_segments(self, seq, track, lang, frames):
        """Yield a full partition of [0, frames) for one track: opaque
        segments from its clips plus transparent fill for every gap."""
        pos = 0
        cur = self.stg.execute(
            "SELECT id, seq, track, start, \"end\", kind, ref, base, p, q "
            "FROM clips WHERE seq = ? AND track = ? ORDER BY start",
            (seq, track))
        for clip in _batched(cur, self.batch, self.stats, "clips"):
            start, end = clip[3], clip[4]
            if start > pos:
                yield (pos, start, TRANSPARENT, None, None, None, ())
                pos = start
            for seg in self._clip_segments(clip, lang):
                if seg[0] > pos:
                    yield (pos, seg[0], TRANSPARENT, None, None, None, ())
                yield seg
                pos = seg[1]
            if pos < end:
                yield (pos, end, TRANSPARENT, None, None, None, ())
                pos = end
        if pos < frames:
            yield (pos, frames, TRANSPARENT, None, None, None, ())

    def _clip_segments(self, clip, lang):
        """Yield opaque segments of one clip in ascending parent order."""
        cid, seq, track, start, end, kind = clip[:6]
        if kind == "blank":
            yield (start, end, "blank", None, None, None,
                   (blank_level(clip),))
            return
        ref, base, p, q = clip[6], clip[7], clip[8], clip[9]
        length = end - start
        if kind == "asset":
            tbl = self.ensure_aseg(ref, lang)
            cur = self.stg.execute(
                "SELECT start, \"end\", state, cue_id, text, NULL, NULL "
                "FROM %s ORDER BY start %s"
                % (tbl, "ASC" if p > 0 else "DESC"))
            src = "aseg"
        else:
            tbl = self.ensure_sseg(ref, lang)
            cur = self.stg.execute(
                "SELECT start, \"end\", state, text, cue_id, asset_id, "
                "path FROM %s ORDER BY start %s"
                % (tbl, "ASC" if p > 0 else "DESC"))
            src = "sseg"
        level = ref_level(clip)
        for row in _batched(cur, self.batch, self.stats, src):
            cs, ce = row[0], row[1]
            iv = lift_interval(cs, ce, base, p, q, length)
            if iv is None:
                continue
            if kind == "asset":
                state, cue_id, text = row[2], row[3], row[4]
                asset_id = ref
                child_path = ()
            else:
                state, text, cue_id, asset_id = row[2], row[3], row[4], row[5]
                child_path = path_from_json(json.loads(row[6]))
            yield (start + iv[0], start + iv[1], state, text, cue_id,
                   asset_id, (level,) + child_path)

    # ---- root ------------------------------------------------------------

    def resolve_root(self, root, lang, emit):
        """Resolve the root sequence; root-level transparent becomes
        'silence'.  `emit` receives (start, end, state, text, cue_id,
        asset_id, path) with state in cue/blank/unknown/silence."""
        def emit_mapped(seg):
            s, e, state, text, cue_id, asset_id, path = seg
            if state == TRANSPARENT:
                emit((s, e, "silence", None, None, None, ()))
            else:
                emit(seg)
        self.compute_sequence(root, lang, emit_mapped)


def _union(intervals):
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
