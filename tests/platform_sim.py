"""Minimal platform simulator for the delivery ledger.

This is deliberately a *separate* implementation of the receiving side:
it knows nothing about the ledger's schema, only about the instruction and
receipt JSON formats.  It keeps, per target and delivery unit, the content
the platform actually holds and a ``done`` table keyed by operation id so a
re-sent instruction is applied at most once.

Usage as a test helper::

    pf = Platform()
    for row in batch["instructions"]:
        r = pf.deliver(row, applied=...)
    pf.receipt("A", 3)                 # take receipt number 3

The simulator is used by the tests to prove the difference between
"instruction sent" and "platform confirmed", and to check that the ledger's
confirmed content tracks what the platform really holds.
"""

import json


class Platform(object):
    def __init__(self):
        self.content = {}      # (target, lang, shard) -> list of items
        self.done = {}         # (target, op_id) -> receipt number
        self.seq = {}          # target -> last receipt number
        self.receipts = {}     # target -> {number: receipt dict}

    # -- receiver -------------------------------------------------------

    def deliver(self, row, applied=True, reason=None):
        """Apply (or reject) one instruction; returns its receipt dict.

        A repeated operation id is *not* applied again; its original
        receipt is returned unchanged (receipts are per operation).
        """
        target = row["target"]
        op_id = row["op_id"]
        if (target, op_id) in self.done:
            n = self.done[(target, op_id)]
            return dict(self.receipts[target][n])
        n = self.seq.get(target, 0) + 1
        self.seq[target] = n
        unit = (target, row["lang"], row["shard"])
        if applied:
            self.content[unit] = [dict(it) for it in row.get("items", [])] \
                if row["action"] == "replace" else []
        rec = {"target": target, "receipt": n, "op_id": op_id,
               "status": "applied" if applied else "rejected"}
        if not applied:
            rec["reason"] = reason or "simulated refusal"
        self.done[(target, op_id)] = n
        self.receipts.setdefault(target, {})[n] = rec
        return dict(rec)

    # -- receipt transport ---------------------------------------------

    def receipt(self, target, number):
        return dict(self.receipts[target][number])

    def receipt_numbers(self, target):
        return sorted(self.receipts.get(target, {}))

    def content_of(self, target, lang, shard):
        return [dict(it) for it in self.content.get((target, lang, shard),
                                                    [])]

    def dump_receipts(self, target, numbers=None):
        nums = self.receipt_numbers(target) if numbers is None else numbers
        return [self.receipt(target, n) for n in nums]


def write_receipts(path, receipts):
    with open(path, "w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path
