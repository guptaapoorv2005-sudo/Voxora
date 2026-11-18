"""Unit tests verifying phone number masking, structured event redaction, and PII protection."""

from app.voice.events import (
    mask_phone_number,
    redact_event_metadata,
    sanitize_opaque_identifier,
)


def test_mask_phone_number_standard() -> None:
    """Phone numbers are masked to only reveal the trailing 4 digits."""
    assert mask_phone_number("+919876543210") == "***-***-3210"
    assert mask_phone_number("9876543210") == "***-***-3210"
    assert mask_phone_number("+1-555-123-4567") == "***-***-4567"
    assert mask_phone_number("1234") == "***-***-1234"


def test_mask_phone_number_short_or_empty() -> None:
    """Short digit strings or empty inputs return safe masking."""
    assert mask_phone_number("12") == "****"
    assert mask_phone_number("") == "****"
    assert mask_phone_number(None) == "****"  # type: ignore[arg-type]


def test_redact_event_metadata_strips_disallowed_keys() -> None:
    """Disallowed keys such as caller_phone, caller_name, email are excluded from metadata."""
    raw = {
        "stage": "stt",
        "caller_phone": "+919876543210",
        "caller_name": "Priya Sharma",
        "caller_email": "priya@example.com",
        "arbitrary_secret": "my-secret-key",
        "is_final": True,
    }
    redacted = redact_event_metadata(raw)
    assert redacted == {
        "stage": "stt",
        "is_final": True,
    }
    assert "caller_phone" not in redacted
    assert "caller_name" not in redacted
    assert "caller_email" not in redacted
    assert "arbitrary_secret" not in redacted


def test_redact_event_metadata_scrubs_pii_in_allowed_keys() -> None:
    """Phone numbers or emails embedded inside allowlisted keys are scrubbed to [REDACTED]."""
    raw = {
        "stage": "stt",
        "reason": "contact customer at priya@example.com or +919876543210",
    }
    redacted = redact_event_metadata(raw)
    assert redacted["stage"] == "stt"
    assert redacted["reason"] == "[REDACTED]"


def test_sanitize_opaque_identifier_preserves_opaque_ids() -> None:
    """Opaque voxora-* and sess-* room and session IDs are preserved as-is."""
    assert sanitize_opaque_identifier("voxora-123456789abc", prefix="room") == "voxora-123456789abc"
    assert sanitize_opaque_identifier("sess-abcdef123456", prefix="session") == "sess-abcdef123456"


def test_sanitize_opaque_identifier_hashes_pii_names() -> None:
    """Non-opaque room names containing PII are replaced with one-way SHA-256 digests."""
    sanitized = sanitize_opaque_identifier("room-priya-sharma-9876543210", prefix="room")
    assert sanitized.startswith("room-hash-")
    assert "priya" not in sanitized
    assert "9876543210" not in sanitized
