"""Atomic publication of completed Stage 9 fixture checkpoints only."""

from __future__ import annotations

import os
import time
from pathlib import Path


def publish_checkpoint(temporary: Path, target: Path) -> None:
    """Retry a transient publish lock without discarding a failed candidate."""
    for attempt in range(20):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)
