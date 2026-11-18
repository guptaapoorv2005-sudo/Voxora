"""Precedence-aware intent classification for affirmative consent verification.

Implements strict 4-step precedence classification:
1. Question / Inquiry check -> Intent.INQUIRY
2. Negation / Rejection check -> Intent.NEGATIVE
3. Hesitation / Conditional check -> Intent.UNCERTAIN
4. Normalized Allowlist check -> Intent.AFFIRMATIVE
Otherwise -> Intent.UNCERTAIN
"""

from __future__ import annotations

import re
from enum import StrEnum


class Intent(StrEnum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    INQUIRY = "inquiry"
    UNCERTAIN = "uncertain"


INQUIRY_TRIGGERS = (
    "can you",
    "could you",
    "what if",
    "is that",
    "will it",
    "would you",
    "how about",
)

NEGATION_PHRASES = (
    "don't",
    "dont",
    "do not",
    "not yet",
)

NEGATION_WORDS = {
    "no",
    "cancel",
    "stop",
    "wait",
    "wrong",
    "incorrect",
    "not",
}

HESITATION_TRIGGERS = (
    "maybe",
    "if possible",
    "assuming",
    "um",
    "uh",
    "i think",
    "probably",
    "perhaps",
)

ALLOWLIST = {
    "yes",
    "yes please",
    "yes i confirm",
    "please confirm",
    "that is correct",
    "sounds good",
    "i approve",
    "go ahead",
    "confirm booking",
    "confirm reschedule",
    "confirm cancellation",
}


def classify_caller_intent(raw_utterance: str) -> Intent:
    """Classify a caller utterance into an Intent using strict precedence rules."""
    if not raw_utterance or not raw_utterance.strip():
        return Intent.UNCERTAIN

    lower_raw = raw_utterance.lower().strip()

    # Step 1: Raw Inquiry & Question Mark Check
    if "?" in lower_raw:
        return Intent.INQUIRY
    if any(
        lower_raw.startswith(trigger) or f" {trigger} " in lower_raw for trigger in INQUIRY_TRIGGERS
    ):
        return Intent.INQUIRY

    # Step 2: Negation & Rejection Check (phrases or whole words)
    if any(phrase in lower_raw for phrase in NEGATION_PHRASES):
        return Intent.NEGATIVE
    words = set(re.findall(r"\b[a-z0-9]+\b", lower_raw))
    if words.intersection(NEGATION_WORDS):
        return Intent.NEGATIVE

    # Step 3: Hesitation & Conditional Check
    if any(trigger in lower_raw for trigger in HESITATION_TRIGGERS):
        return Intent.UNCERTAIN

    # Step 4: Strict Normalized Allowlist Check
    normalized = re.sub(r"[^\w\s]", "", lower_raw).strip()
    if normalized in ALLOWLIST:
        return Intent.AFFIRMATIVE

    return Intent.UNCERTAIN
