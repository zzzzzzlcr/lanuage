"""Behavioral dropdown explorer — discovers controls by observing DOM changes."""

import time, json, logging
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SelectIntent:
    """What the operator wants to select."""
    label: str
    mode: str           # "exact" | "random"
    option: str | None  # "United States" (None when random)
    scope: dict | None = None


@dataclass
class CandidateRef:
    """A located DOM element that might be the dropdown trigger."""
    selector: str       # unique marker selector like '[data-probe="p1:c0"]'
    frame_id: str
    source: str         # "label_for" | "adjacent_text" | "aria" | ...
    confidence: float


@dataclass
class SelectOutcome:
    """Result of a select exploration."""
    status: str         # SELECTED | ALREADY_SELECTED | OPTION_NOT_FOUND
                        # | NOT_VERIFIED | AMBIGUOUS | NO_SAFE_TRIGGER
                        # | OPEN_FAILED | NO_CANDIDATE
    evidence: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    selected_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("SELECTED", "ALREADY_SELECTED")
