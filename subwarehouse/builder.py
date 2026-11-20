"""Build orchestration: package -> library, atomically published.

Failure safety: the library is built at a temporary path next to the
target and published with os.link(2), which never replaces an existing
file.  Any failure (invalid input, existing target, write error) removes
the temporary files, so an interrupted build can neither overwrite an
existing library nor leave a new openable half-product behind.
"""

import errno
import hashlib
import json
import os
import resource
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from . import engine as eng
from . import library as lib
from .errors import PackageError, TargetExistsError
from .ingest import ingest_package
from .manifest import load_manifest
from .model import canonical_json

GENERATOR = "subwarehouse 1.0"


def new_stats():
    return {
        "ingested": {"clips": 0, "coverage": 0, "cues": 0},
        "read": {"clips": 0, "coverage": 0, "cues": 0, "aseg": 0,
                 "sseg": 0},
        "written": {"aseg": 0, "sseg": 0, "segments": 0},
        "objects": {"assets": 0, "sequences": 0},
    }


def _budget(mem_budget_mb):
    """Split the working-memory budget into explainable parts.

    The budget is a soft target covering SQLite page caches and the
    Python-side batch buffers; interpreter baseline and the per-track
    overlay state (one segment per track) are outside it but bounded.
    """
    mb = max(16, int(mem_budget_mb))
    total = mb * 1024 * 1024
    cache_kb = max(1024, (total * 6 // 10) // 1024)   # 60%: SQLite caches
    batch = max(500, min(500000, total // 4096))      # rest: row buffers
    return cache_kb, batch


def _connect(path, cache_kb):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA cache_size = %d" % -cache_kb)
    conn.execute("PRAGMA temp_store = 1")   # sort spill goes to temp files
    return conn


def compute_version(stg, man, root, batch):
    """Order-independent logical version: manifest + root + the sorted
    multiset of per-record hashes (SQLite sorts on disk)."""
    h = hashlib.sha256()
    h.update(b"subwarehouse/1\n")
    h.update(man.canonical())
    h.update(b"\nroot:")
    h.update(root.encode("utf-8"))
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


def build(package_dir, root, target, mem_budget_mb=256):
    """Build a library.  Returns (stats, meta).  Raises PackageError,
    TargetExistsError, LibraryError or OSError on failure."""
    target = os.path.abspath(target)
    if os.path.lexists(target):
        raise TargetExistsError(
            "target %s already exists; builds never overwrite" % target)
    if not os.path.isdir(package_dir):
        raise PackageError("package directory %s not found" % package_dir)
    man = load_manifest(package_dir)
    if root not in man.sequences:
        raise PackageError(
            "root sequence %r is not declared in the manifest (sequences: "
            "%s)" % (root, ", ".join(sorted(man.sequences)) or "none"))

    token = "%d-%s" % (os.getpid(), secrets.token_hex(4))
    tmp_lib = "%s.lib-%s.tmp" % (target, token)
    tmp_stg = "%s.stg-%s.tmp" % (target, token)
    cache_kb, batch = _budget(mem_budget_mb)
    stats = new_stats()
    started = time.monotonic()
    stg = libdb = None
    try:
        stg = _connect(tmp_stg, cache_kb)
        ingest_package(stg, package_dir, man, stats, batch)
        version = compute_version(stg, man, root, batch)

        libdb = _connect(tmp_lib, cache_kb)
        libdb.executescript(lib.SCHEMA)
        meta = {
            "format": lib.FORMAT,
            "package": man.package,
            "root": root,
            "root_frames": str(man.sequences[root]),
            "languages": json.dumps(sorted(man.languages)),
            "logical_version": version,
            "generator": GENERATOR,
            "created_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
        libdb.execute("BEGIN")
        libdb.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
        libdb.execute("COMMIT")

        writer = eng.BatchWriter(
            libdb, "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)",
            batch, stats, "segments")
        engine = eng.Engine(stg, man, stats, batch)
        for lang in sorted(man.languages):
            def emit(seg, lang=lang):
                s, e, state, text, cue_id, asset_id, path = seg
                writer.write((lang, s, e, state, text, cue_id, asset_id,
                              json.dumps(eng.path_to_json(path),
                                         ensure_ascii=False)))
            engine.resolve_root(root, lang, emit)
            engine.drop_language_tables()
        writer.flush()

        # Provenance: stable id -> original record location.
        libdb.execute("ATTACH DATABASE ? AS stg", (tmp_stg,))
        libdb.execute("BEGIN")
        libdb.execute("INSERT INTO sources SELECT 'clip', id, file, line "
                      "FROM stg.clips")
        libdb.execute("INSERT INTO sources SELECT 'cue', id, file, line "
                      "FROM stg.cues")
        libdb.execute("COMMIT")
        libdb.execute("DETACH DATABASE stg")
        libdb.close()
        libdb = None
        with open(tmp_lib, "rb") as fh:
            os.fsync(fh.fileno())
        stg.close()
        stg = None

        # Atomic no-overwrite publish: hard-link then drop the temp name.
        # Filesystems without hard links fall back to rename after a
        # final existence check; genuine write errors propagate.
        try:
            os.link(tmp_lib, target)
        except FileExistsError:
            raise TargetExistsError(
                "target %s already exists; builds never overwrite" % target)
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.ENOSYS, errno.EXDEV,
                               errno.ENOTSUP):
                raise
            if os.path.lexists(target):
                raise TargetExistsError(
                    "target %s already exists; builds never overwrite"
                    % target)
            os.rename(tmp_lib, target)
        if os.path.lexists(tmp_lib):
            os.unlink(tmp_lib)
    finally:
        if libdb is not None:
            libdb.close()
        if stg is not None:
            stg.close()
        for tmp in (tmp_lib, tmp_stg):
            try:
                os.unlink(tmp)
            except OSError:
                pass

    stats["elapsed_sec"] = round(time.monotonic() - started, 6)
    stats["peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    stats["mem_budget_mb"] = mem_budget_mb
    meta_out = dict(meta)
    meta_out["root_frames"] = int(meta["root_frames"])
    return stats, meta_out
