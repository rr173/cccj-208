"""Machine-readable CLI for the correction impact inventory.

Every subcommand prints one JSON object on stdout and returns 0;
refusals print a JSON error object (``ok`` false, plus ``item`` when the
order item is known) on stderr and return 2.

Commands
--------
review-init  --package PKG --dir DIR --program P [--program P ...]
             [--mem-budget-mb M] [--stats]
review-info  --dir DIR
review-trial --dir DIR --order ORDER.json --out LIST.jsonl
             [--mem-budget-mb M] [--stats]

A correction order is one JSON object::

    {"baseline": "<id from review-info>",
     "order":    "corr-2026-11-20-01",
     "windows":  [{"asset": "T", "lang": "zho", "start": 2, "end": 5,
                   "coverage": [{"start": 2, "end": 4}],
                   "cues":     [{"id": "A", "start": 2, "end": 4,
                                 "text": "..."}]},
                  ...]}

Each window fully replaces the checked coverage and the cues of one
(asset, language) inside [start, end); coverage and cues default to
empty.  See the README section on the review directory for the exact
semantics.
"""

import json
import sys

from . import review
from .errors import PackageError, ReviewError


def _emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2,
              sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _fail(message, item=None):
    obj = {"ok": False, "error": message}
    if item is not None:
        obj["item"] = item
    json.dump(obj, sys.stderr, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stderr.write("\n")
    return 2


def add_parsers(sub):
    p = sub.add_parser("review-init",
                       help="build a review directory from a handover "
                            "package and the registered programs")
    p.add_argument("--package", required=True, help="package directory")
    p.add_argument("--dir", required=True, help="review directory to "
                                                "create (must not exist)")
    p.add_argument("--program", action="append", required=True,
                   help="sequence id registered as a finished film "
                        "(repeatable)")
    p.add_argument("--mem-budget-mb", type=int, default=256,
                   help="soft working-memory budget in MiB (default 256)")
    p.add_argument("--stats", action="store_true",
                   help="include build counters in the JSON summary")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("review-info",
                       help="show the directory's baseline and registered "
                            "programs")
    p.add_argument("--dir", required=True, help="review directory")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("review-trial",
                       help="measure one correction order against the "
                            "baseline and write the review list")
    p.add_argument("--dir", required=True, help="review directory")
    p.add_argument("--order", required=True, help="correction order JSON")
    p.add_argument("--out", required=True, help="review list output "
                                                "(JSONL)")
    p.add_argument("--mem-budget-mb", type=int, default=256,
                   help="soft working-memory budget in MiB (default 256)")
    p.add_argument("--stats", action="store_true",
                   help="include workload counters in the JSON summary")
    p.set_defaults(func=cmd_trial)
    return sub


def cmd_init(args):
    stats, meta = review.init_inventory(args.package, args.program,
                                        args.dir,
                                        mem_budget_mb=args.mem_budget_mb)
    out = {
        "ok": True,
        "dir": args.dir,
        "package": meta["package"],
        "baseline": meta["baseline"],
        "programs": meta["programs"],
        "languages": meta["languages"],
    }
    if args.stats:
        out["stats"] = stats
    return _emit(out)


def cmd_info(args):
    conn, meta = review.open_inventory(args.dir)
    conn.close()
    return _emit({
        "ok": True,
        "dir": args.dir,
        "package": meta["package"],
        "baseline": meta["baseline"],
        "programs": json.loads(meta["programs"]),
        "languages": json.loads(meta["languages"]),
        "generator": meta["generator"],
        "created_utc": meta["created_utc"],
        "baseline_segments": int(meta["segments"]),
    })


def cmd_trial(args):
    summary, stats = review.run_trial(args.dir, args.order, args.out,
                                      mem_budget_mb=args.mem_budget_mb)
    if args.stats:
        summary["stats"] = stats
    return _emit(summary)


def run(args):
    """Entry point used by the main CLI."""
    try:
        return args.func(args)
    except ReviewError as e:
        return _fail(str(e), getattr(e, "item", None))
    except PackageError as e:
        return _fail(str(e))
