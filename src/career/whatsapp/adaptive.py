"""Adaptive delivery planner (whatsapp §08) — the binding rule, pure.

Open window  → send cards/documents directly (free).
Closed window → send ONE morning template, then descend (send the held bundle)
                when the customer taps/replies and the window opens.
Opted out     → send nothing.

Why not push files proactively when closed: higher template cost, higher block
risk, unopened files, and a lost engagement signal. The CVs are ready in advance
regardless — readiness is not proactive sending.
"""

from __future__ import annotations

from enum import StrEnum

from career.whatsapp.window import WindowState


class DeliveryAction(StrEnum):
    SEND_DIRECT = "send_direct"
    SEND_TEMPLATE_THEN_WAIT = "send_template_then_wait"
    SKIP_OPTED_OUT = "skip_opted_out"


def plan_delivery(window: WindowState) -> DeliveryAction:
    if window is WindowState.OPTED_OUT:
        return DeliveryAction.SKIP_OPTED_OUT
    if window is WindowState.OPEN:
        return DeliveryAction.SEND_DIRECT
    return DeliveryAction.SEND_TEMPLATE_THEN_WAIT
