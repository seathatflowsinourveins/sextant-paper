"""Durability primitives carried from northstar-trading@8256c16c31dc:persist.py."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY})


def full_fsync(fd: int) -> None:
    """Flush to the drive: F_FULLFSYNC where the platform has it (macOS), plain fsync otherwise."""
    flag = getattr(fcntl, "F_FULLFSYNC", None)
    if flag is not None:
        try:
            fcntl.fcntl(fd, flag)
            return
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED:
                raise  # a real flush failure (EIO, ENOSPC...) is a failed write, never a fallback
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """Sync the containing directory so a rename or a new file survives a power loss."""
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        full_fsync(fd)
    finally:
        os.close(fd)
