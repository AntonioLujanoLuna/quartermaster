"""Startup recovery and lightweight maintenance."""

from __future__ import annotations

from .db import SQLiteStore
from .handles import HandleRepository
from .projections import release_projection_claims
from .receipts import ReceiptRepository


def recover_startup(
    store: SQLiteStore,
    receipts: ReceiptRepository,
    handles: HandleRepository,
    *,
    receipt_retention_seconds: int = 86_400,
    handle_retention_seconds: int = 600,
) -> dict[str, int]:
    if receipt_retention_seconds <= 0 or handle_retention_seconds <= 0:
        raise ValueError("retention periods must be positive")
    failed_receipts = receipts.recover_processing()
    released_claims = release_projection_claims(store)
    removed_handles = handles.cleanup(replay_retention_seconds=handle_retention_seconds)
    removed_receipts = receipts.cleanup_terminal(retention_seconds=receipt_retention_seconds)
    return {
        "failed_deferred_receipts": failed_receipts,
        "released_projection_claims": released_claims,
        "removed_handles": removed_handles,
        "removed_receipts": removed_receipts,
    }
