"""Command line interface: build / export / inspect."""

import argparse
import json
import sys

from . import __version__
from .builder import build
from .errors import LibraryError, PackageError, TargetExistsError
from .library import FORMAT, inspect_frame, iter_segments, open_library


def _cmd_build(args):
    stats, meta = build(args.package, args.root, args.out,
                        mem_budget_mb=args.mem_budget_mb)
    summary = {
        "library": args.out,
        "package": meta["package"],
        "root": meta["root"],
        "root_frames": meta["root_frames"],
        "languages": json.loads(meta["languages"]),
        "logical_version": meta["logical_version"],
    }
    if args.stats:
        summary["stats"] = stats
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _open_checked(args):
    conn, meta = open_library(args.lib)
    languages = json.loads(meta["languages"])
    root_frames = int(meta["root_frames"])
    return conn, meta, languages, root_frames


def _seg_to_obj(row):
    lang, start, end, state, text, cue_id, asset_id, path_json = row
    obj = {"lang": lang, "start": start, "end": end, "state": state}
    if state == "cue":
        obj["text"] = text
        obj["cue"] = cue_id
        obj["asset"] = asset_id
    elif state == "unknown":
        obj["asset"] = asset_id
    obj["path"] = json.loads(path_json)
    return obj


def _fmt_path(path):
    hops = []
    for lv in path:
        if lv["kind"] == "blank":
            hops.append("%s(%s@%d,blank)" % (lv["clip"], lv["seq"],
                                             lv["track"]))
        else:
            hops.append("%s(%s@%d,%s:%s,%d/%d)" % (
                lv["clip"], lv["seq"], lv["track"], lv["kind"], lv["ref"],
                lv["p"], lv["q"]))
    return " -> ".join(hops)


def _cmd_export(args):
    conn, meta, languages, root_frames = _open_checked(args)
    try:
        lo = 0 if args.start is None else args.start
        hi = root_frames if args.end is None else args.end
        if not (0 <= lo < hi <= root_frames):
            raise LibraryError(
                "export range [%s,%s) outside [0,%d)" % (lo, hi, root_frames))
        langs = languages if args.lang is None else [args.lang]
        for lg in langs:
            if lg not in languages:
                raise LibraryError(
                    "language %r is not declared in this library "
                    "(declared: %s)" % (lg, ", ".join(languages)))
        out = open(args.out, "w", encoding="utf-8") if args.out \
            else sys.stdout
        try:
            for lg in langs:
                for row in iter_segments(conn, lg, lo, hi):
                    if args.format == "jsonl":
                        out.write(json.dumps(_seg_to_obj(row),
                                             ensure_ascii=False) + "\n")
                    else:
                        lang, start, end, state, text, cue_id, asset_id, \
                            path_json = row
                        path = json.loads(path_json)
                        line = "%s [%d,%d) %s" % (lang, start, end, state)
                        if state == "cue":
                            line += " %s cue=%s asset=%s" % (
                                json.dumps(text, ensure_ascii=False),
                                cue_id, asset_id)
                        elif state == "unknown":
                            line += " asset=%s" % asset_id
                        if path:
                            line += " via " + _fmt_path(path)
                        out.write(line + "\n")
        finally:
            if args.out:
                out.close()
    finally:
        conn.close()
    return 0


def _cmd_inspect(args):
    conn, meta, languages, root_frames = _open_checked(args)
    try:
        info = inspect_frame(conn, meta, args.lang, args.frame)
    finally:
        conn.close()
    if args.json:
        json.dump(info, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    print("library package=%s root=%s version=%s"
          % (meta["package"], meta["root"], meta["logical_version"][:16]))
    print("frame %d lang %s -> %s  (segment [%d,%d))"
          % (info["frame"], info["lang"], info["state"],
             info["segment"][0], info["segment"][1]))
    for i, lv in enumerate(info["levels"]):
        if lv["kind"] == "blank":
            print("  level %d: clip %s  seq %s track %d  local frame %d"
                  "  -- explicit blank (all languages)"
                  % (i, lv["clip"], lv["seq"], lv["track"],
                     lv["local_frame"]))
        else:
            print("  level %d: clip %s  seq %s track %d  local frame %d"
                  "  base=%d p=%d q=%d  -> %s %s frame %d"
                  % (i, lv["clip"], lv["seq"], lv["track"],
                     lv["local_frame"], lv["base"], lv["p"], lv["q"],
                     lv["kind"], lv["ref"], lv["child_frame"]))
    state = info["state"]
    if state == "cue":
        print("endpoint: asset %s cue %s  text=%s"
              % (info["asset"], info["cue"],
                 json.dumps(info["text"], ensure_ascii=False)))
    elif state == "unknown":
        print("missing: asset %s lang %s frame %d -- no checked coverage "
              "for this frame" % (info["asset"], info["lang"],
                                  info["missing_frame"]))
    elif state == "blank":
        print("endpoint: explicit blank clip %s" % info["blank_clip"])
    else:
        print("silence: transparent at root; no source")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="subwarehouse",
        description="Offline subtitle warehousing: align handed-over "
                    "subtitles with edit decisions into a queryable "
                    "SQLite library.")
    ap.add_argument("--version", action="version",
                    version="%(prog)s " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build a library from a handover "
                                     "package")
    b.add_argument("--package", required=True, help="package directory")
    b.add_argument("--root", required=True, help="root sequence id "
                                                 "(the finished film)")
    b.add_argument("--out", required=True, help="target library path "
                                                "(must not exist)")
    b.add_argument("--mem-budget-mb", type=int, default=256,
                   help="soft working-memory budget in MiB (default 256)")
    b.add_argument("--stats", action="store_true",
                   help="include interval/read/write counters, peak memory "
                        "and elapsed time in the JSON summary")
    b.set_defaults(func=_cmd_build)

    e = sub.add_parser("export", help="export final segments by language "
                                      "and frame range")
    e.add_argument("--lib", required=True, help="library path")
    e.add_argument("--lang", help="language code (default: all declared)")
    e.add_argument("--start", type=int, help="first frame (default 0)")
    e.add_argument("--end", type=int, help="end frame, exclusive "
                                           "(default: root length)")
    e.add_argument("--format", choices=["jsonl", "text"], default="jsonl")
    e.add_argument("--out", help="output file (default: stdout)")
    e.set_defaults(func=_cmd_export)

    i = sub.add_parser("inspect", help="explain a single frame")
    i.add_argument("--lib", required=True, help="library path")
    i.add_argument("--lang", required=True, help="language code")
    i.add_argument("--frame", required=True, type=int, help="frame number")
    i.add_argument("--json", action="store_true", help="JSON output")
    i.set_defaults(func=_cmd_inspect)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (PackageError, TargetExistsError, LibraryError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
