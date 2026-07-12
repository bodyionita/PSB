"""Vault backup seam (ADR-014).

The capture pipeline must not know *how* the vault is committed and pushed — only that after a
write batch it should request a backup. This module defines that seam. The full
``VaultBackupService`` (debounced commit + ff-only push + heal-on-reject + the R2/integrity
jobs behind one git lock) lands in the M1 durability task; until then the pipeline runs against
:class:`LoggingVaultBackup`, which records the request without performing git operations.

Keeping this a narrow ``Protocol`` also keeps the pipeline unit-testable with a fake that just
counts commit requests — no git, no network (08 testing policy).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class VaultBackup(Protocol):
    """What the capture pipeline needs from the backup layer."""

    async def request_commit(self, reason: str) -> None:
        """Ask the backup layer to (eventually) commit + push the current vault state.

        Implementations debounce and serialise; callers treat this as fire-and-forget and must
        not depend on completion for the capture's success (notes are already on disk)."""
        ...


class LoggingVaultBackup:
    """Placeholder until the real git-backed service exists. Records intent only."""

    async def request_commit(self, reason: str) -> None:
        logger.info("vault backup requested (no-op until durability task): %s", reason)
