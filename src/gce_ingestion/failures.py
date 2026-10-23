"""Failure codes F1-F13 (Calibro design C.9) — contract, copied not invented."""

from __future__ import annotations

import enum


class FailureCode(str, enum.Enum):
    F1 = "F1"   # STOP run PARTIAL
    F2 = "F2"   # STOP adapter; escalate Nico
    F3 = "F3"   # quarantine PARTIAL resumable
    F4 = "F4"   # quarantine; no fabricate order
    F5 = "F5"   # keep split CONFLICT; escalate Luce
    F6 = "F6"   # quarantine observation
    F7 = "F7"   # CORRUPT; never parse
    F8 = "F8"   # ABSENT; continue
    F9 = "F9"   # refuse emit
    F10 = "F10" # CONFLICT review
    F11 = "F11" # quarantine; never trusted normalize
    F12 = "F12" # split + conflict
    F13 = "F13" # STOP stage methodology defect

    @property
    def meaning(self) -> str:
        return {
            "F1": "STOP run PARTIAL",
            "F2": "STOP adapter; escalate Nico",
            "F3": "quarantine PARTIAL resumable",
            "F4": "quarantine; no fabricate order",
            "F5": "keep split CONFLICT; escalate Luce",
            "F6": "quarantine observation",
            "F7": "CORRUPT; never parse",
            "F8": "ABSENT; continue",
            "F9": "refuse emit",
            "F10": "CONFLICT review",
            "F11": "quarantine; never trusted normalize",
            "F12": "split + conflict",
            "F13": "STOP stage methodology defect",
        }[self.value]

    @property
    def retryable(self) -> bool:
        """Retries apply ONLY to transport/network. Never retry F1/F2/F7/F9/F13."""
        return self not in (FailureCode.F1, FailureCode.F2,
                            FailureCode.F7, FailureCode.F9, FailureCode.F13)

    @property
    def stops_run(self) -> bool:
        return self in (FailureCode.F1, FailureCode.F2, FailureCode.F13)

    @property
    def stops_adapter(self) -> bool:
        return self in (FailureCode.F2,)
