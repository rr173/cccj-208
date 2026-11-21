"""Error types for subwarehouse."""


class PackageError(Exception):
    """Invalid input package or record.

    Carries the originating file and 1-based line number when known so the
    handover team can locate the rejected record in the original delivery.
    """

    def __init__(self, message, file=None, line=None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line

    def __str__(self):
        loc = ""
        if self.file is not None:
            loc = str(self.file)
            if self.line is not None:
                loc += ":" + str(self.line)
            loc += ": "
        return loc + self.message


class TargetExistsError(Exception):
    """The target library path already exists; builds never overwrite."""


class LibraryError(Exception):
    """The target file is not a readable subwarehouse library."""


class LedgerError(Exception):
    """A refused delivery-ledger operation (registration, conflict, ...)."""


class LedgerStateError(LedgerError):
    """The delivery ledger violates one of its documented invariants."""


class ReceiptConflict(LedgerError):
    """A receipt input contradicts receipts already on file.

    Raising this rejects the *whole input*; nothing from it is committed.
    """

    def __init__(self, message, file=None, line=None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line

    def __str__(self):
        loc = ""
        if self.file is not None:
            loc = str(self.file)
            if self.line is not None:
                loc += ":" + str(self.line)
            loc += ": "
        return loc + self.message


class ReviewError(Exception):
    """A refused correction-inventory operation.

    Raised for an unusable inventory directory and for any invalid
    correction order; an invalid order is refused as a whole and the
    `item` attribute identifies the offending order item (window index
    and, when known, the cue id) so the supplier can fix exactly it.
    """

    def __init__(self, message, item=None):
        super().__init__(message)
        self.message = message
        self.item = item


class CorpusError(Exception):
    """A refused corpus-export operation.

    Raised for any invalid batch input (sample sheet, library list,
    mapping table, grant table, previous registry) and for an unusable
    output location.  An invalid batch is refused as a whole: nothing is
    published.  `item` identifies the offending input record when known
    (for example {"sample": "s17"} or {"mapping": 3}).
    """

    def __init__(self, message, item=None):
        super().__init__(message)
        self.message = message
        self.item = item
