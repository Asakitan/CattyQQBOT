"""Per-turn delivery receipts; only acknowledged messages enter conversation history."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ReplyDeliveryReceipt:
    text: str
    status: str = "pending"
    error_type: str = ""


@dataclass
class ReplyDeliveryState:
    receipts: list[ReplyDeliveryReceipt] = field(default_factory=list)

    async def send(
        self,
        sender: Callable[[Any], Awaitable[Any]],
        message: Any,
        *,
        history_text: str,
    ) -> Any:
        receipt = ReplyDeliveryReceipt(text=history_text)
        self.receipts.append(receipt)
        try:
            result = await sender(message)
        except asyncio.CancelledError:
            # Cancellation or a missing ACK does not prove the platform did not send.
            receipt.status = "unconfirmed"
            receipt.error_type = "CancelledError"
            raise
        except Exception as exc:
            receipt.status = "unconfirmed"
            receipt.error_type = type(exc).__name__
            raise
        receipt.status = "delivered"
        return result

    def delivered_texts(self) -> list[str]:
        return [r.text for r in self.receipts if r.status == "delivered" and r.text]

    def status_counts(self) -> dict[str, int]:
        result = {"pending": 0, "delivered": 0, "unconfirmed": 0}
        for receipt in self.receipts:
            result[receipt.status] += 1
        return result