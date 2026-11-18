"""Server-side caller identity normalization and matching service.

Provides strict, deterministic caller name normalization and token-set matching
to prevent enumeration leaks and enforce caller ownership across:
- lookup_appointment
- Date disambiguation
- verify_appointment_step_up
"""

from __future__ import annotations

import re
import unicodedata

HONORIFICS = {
    "dr",
    "doctor",
    "mr",
    "mrs",
    "ms",
    "miss",
    "prof",
    "professor",
}


def normalize_name(name: str) -> str:
    """Normalize caller/patient names: NFKD Unicode decomposition, strip combining marks, lowercase, alphanumeric extraction."""
    if not name or not name.strip():
        return ""

    # Decompose accented characters and strip combining diacritical marks
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))

    # Filter to alphanumeric ASCII and spaces, lowercase
    ascii_only = re.sub(r"[^a-zA-Z0-9\s]", " ", stripped).lower()
    # Collapse whitespace and strip honorifics
    tokens = [t for t in ascii_only.split() if t not in HONORIFICS]
    return " ".join(tokens)


def names_match(caller_name: str, patient_name: str) -> bool:
    """Match caller-provided name against stored patient name using strict token matching.

    Returns True if:
    1. Normalized strings match exactly.
    2. Given name and surname (first and last tokens) match exactly when >= 2 tokens present.
    """
    c_norm = normalize_name(caller_name)
    p_norm = normalize_name(patient_name)

    if not c_norm or not p_norm:
        return False

    if c_norm == p_norm:
        return True

    c_tokens = c_norm.split()
    p_tokens = p_norm.split()

    # If both have at least first and last names, check if first and last match
    if len(c_tokens) >= 2 and len(p_tokens) >= 2:
        return c_tokens[0] == p_tokens[0] and c_tokens[-1] == p_tokens[-1]

    # For single token names, exact match is required
    return False
