"""Startup recovery and lightweight maintenance."""

from __future__ import annotations

from .handles import HandleRepository
from .receipts import ReceiptRepository


def recover_startup(receipts: ReceiptRepository, handles: HandleRepository) -> dict[str, int]:
    failed_receipts = receipts.recover_processing()
    removed_handles = handles.cleanup()
    return {"failed_deferred_receipts": failed_receipts, "removed_handles": removed_handles}
