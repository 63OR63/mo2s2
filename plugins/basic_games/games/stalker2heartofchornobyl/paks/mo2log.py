"""Qt/MO2 error reporter for host-independent installation helpers.

No Qt import at module load: the container/parser tests also run headlessly.
All errors bypass Verbosity filtering by design.
"""
from __future__ import annotations
import sys


def error(message: str) -> None:
    """Log an error through Qt when available, or stderr otherwise."""
    message = '[mo2s2] [ERROR] ' + message
    try:
        from PyQt6.QtCore import qCritical
        qCritical(message)  # MO2 captures Qt messages in its application log.
    except (ImportError, AttributeError, RuntimeError, TypeError):
        print(message, file=sys.stderr)
