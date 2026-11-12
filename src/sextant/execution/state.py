"""Atomic state carry from northstar-trading@8256c16c31dc:persist.py.

Linux fsync + same-directory rename + directory fsync; no state repair or reset.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def touch_durable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def decimal(value: Any, name: str, *, zero: bool = True):
    from decimal import Decimal

    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value < 0
        or (not zero and value == 0)
    ):
        raise ValueError(
            f"{name} requires a finite {'nonnegative' if zero else 'positive'} Decimal"
        )
    return value
