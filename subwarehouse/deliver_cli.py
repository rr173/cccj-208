"""Machine-readable CLI for the delivery ledger.

Every subcommand prints one JSON object on stdout (``ok`` plus a payload)
and returns 0; refusals print a JSON error object on stderr and return 2,
so a hand-over script never has to parse prose.

Commands
--------
ledger-init     --ledger PATH
ledger-register --ledger PATH --target T --release N --lib L
                [--languages FILE]        (required on first registration)
ledger-dispatch --ledger PATH --request ID --target T [--target ...]
                [--limit N]
ledger-batch    --ledger PATH --request ID
ledger-receipts --ledger PATH FILE [FILE ...] [--target T]
ledger-progress --ledger PATH --target T
ledger-verify   --ledger PATH --target T [--lang L --shard S]
                [--release N] [--upto K]

``--languages FILE`` is a JSON object mapping each language to its shard
table::

    {"zho": [{"name": "head",  "start": 0, "end": 4},
             {"name": "body",  "start": 4, "end": 8},
             {"name": "tail",  "start": 8, "end": 12}]}

A shard table must tile [0, film frames) without gaps or overlaps.  All
outputs use relative shard frames ``[start,end)``; the absolute shard
origin is included as ``shard_start``.
"""

import json
import sys

from . import deliver
from .errors import LedgerError


class _Fail(Exception):
    pass


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise _Fail("cannot read %s %s: %s" % (what, path, e))
    except ValueError as e:
        raise _Fail("%s %s is not valid JSON: %s" % (what, path, e))


def add_parsers(sub):
    p = sub.add_parser("ledger-init",
                       help="create a new delivery ledger file")
    p.add_argument("--ledger", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ledger-register",
                       help="register a subtitle release for a target")
    p.add_argument("--ledger", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--release", required=True, type=int)
    p.add_argument("--lib", required=True)
    p.add_argument("--languages",
                   help="JSON file mapping language -> shard table "
                        "(required on the first registration)")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("ledger-dispatch",
                       help="take a bounded batch of instructions for the "
                            "targets' current releases")
    p.add_argument("--ledger", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--target", action="append", required=True)
    p.add_argument("--limit", type=int,
                   help="maximum instruction rows (default: no cap)")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("ledger-batch",
                       help="re-read a batch taken earlier")
    p.add_argument("--ledger", required=True)
    p.add_argument("--request", required=True)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("ledger-receipts",
                       help="import platform receipt files")
    p.add_argument("--ledger", required=True)
    p.add_argument("files", nargs="+")
    p.add_argument("--target", help="reject receipts for other targets")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("ledger-progress",
                       help="confirmed receipt prefix and first missing "
                            "number")
    p.add_argument("--ledger", required=True)
    p.add_argument("--target", required=True)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("ledger-verify",
                       help="audit the platform content at a confirmed "
                            "position against a registered release")
    p.add_argument("--ledger", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--lang")
    p.add_argument("--shard")
    p.add_argument("--release", type=int)
    p.add_argument("--upto", type=int,
                   help="receipt position inside the confirmed prefix "
                        "(0 = initial state)")
    p.set_defaults(func=cmd_verify)
    return sub


def _emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False,
              indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_init(args):
    conn = deliver.init_ledger(args.ledger)
    conn.close()
    return _emit({"ok": True, "ledger": args.ledger, "format": deliver.FORMAT})


def cmd_register(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        langs = None
        if args.languages:
            langs = _load_json(args.languages, "language file")
        res = deliver.register(conn, args.target, args.release, args.lib,
                               langs)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def cmd_dispatch(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        res = deliver.dispatch(conn, args.request, args.target, args.limit)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def cmd_batch(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        res = deliver.batch_status(conn, args.request)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def cmd_receipts(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        res = deliver.import_receipts(conn, args.files, args.target)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def cmd_progress(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        res = deliver.progress(conn, args.target)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def cmd_verify(args):
    conn = deliver.open_ledger(args.ledger)
    try:
        res = deliver.verify(conn, args.target, args.lang, args.shard,
                             args.release, args.upto)
    finally:
        conn.close()
    return _emit(dict(res, ok=True))


def run(args):
    """Entry point used by the main CLI."""
    try:
        return args.func(args)
    except _Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except LedgerError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
