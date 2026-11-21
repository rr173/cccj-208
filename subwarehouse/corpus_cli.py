"""Machine-readable CLI for corpus export.

The export subcommand prints one JSON object on stdout and returns 0;
refusals print a JSON error object (``ok`` false, plus ``item`` when the
offending input record is known) on stderr and return 2.

Commands
--------
corpus-export --libraries LIBS.json --samples S.jsonl --mapping M.jsonl
              --grants G.jsonl --out DIR [--registry REG.sqlite]
              [--mem-budget-mb M] [--stats]
corpus-registry-info --registry REG.sqlite

See the README corpus section for the exact input and output formats.
"""

import json
import sys

from . import corpus
from .errors import CorpusError


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
    p = sub.add_parser(
        "corpus-export",
        help="export one batch of training/validation subtitle samples "
             "from finished-film windows")
    p.add_argument("--libraries", required=True,
                   help="JSON object listing alias, path and expected "
                        "logical_version of each library")
    p.add_argument("--samples", required=True,
                   help="sample sheet (JSONL): id, library, window, "
                        "languages")
    p.add_argument("--mapping", required=True,
                   help="mapping table (JSONL): (library, asset) -> "
                        "source-material key")
    p.add_argument("--grants", required=True,
                   help="grant table (JSONL): allowed source-frame "
                        "intervals per (source key, language)")
    p.add_argument("--registry",
                   help="previous batch's registry file (optional)")
    p.add_argument("--out", required=True,
                   help="output directory (must not exist)")
    p.add_argument("--mem-budget-mb", type=int, default=256,
                   help="soft working-memory budget in MiB (default 256)")
    p.add_argument("--stats", action="store_true",
                   help="include workload counters in the JSON summary")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("corpus-registry-info",
                       help="summarise a corpus registry file")
    p.add_argument("--registry", required=True, help="registry path")
    p.set_defaults(func=cmd_registry_info)
    return sub


def cmd_export(args):
    summary, stats = corpus.export_corpus(
        args.libraries, args.samples, args.mapping, args.grants,
        args.registry, args.out, mem_budget_mb=args.mem_budget_mb)
    if args.stats:
        summary["stats"] = stats
    return _emit(summary)


def cmd_registry_info(args):
    return _emit(corpus.registry_info(args.registry))


def run(args):
    """Entry point used by the main CLI."""
    try:
        return args.func(args)
    except CorpusError as e:
        return _fail(str(e), getattr(e, "item", None))
    except OSError as e:
        return _fail(str(e))
