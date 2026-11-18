"""Unit tests for caller identity normalization and matching."""

from app.services.caller_identity import names_match, normalize_name


def test_normalize_name_empty():
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""


def test_normalize_name_accents_and_case():
    assert normalize_name("Renée O'Connor") == "renee o connor"
    assert normalize_name("José María García") == "jose maria garcia"


def test_normalize_name_strips_honorifics():
    assert normalize_name("Dr. Jane Doe") == "jane doe"
    assert normalize_name("Mr. John Smith Jr.") == "john smith jr"
    assert normalize_name("Professor Charles Xavier") == "charles xavier"


def test_names_match_exact():
    assert names_match("John Doe", "John Doe") is True
    assert names_match("john doe", "JOHN DOE") is True
    assert names_match("  Dr. John Doe  ", "John Doe") is True


def test_names_match_first_and_last_token():
    # Middle names or variations matching first and last
    assert names_match("John Robert Doe", "John Doe") is True
    assert names_match("John Doe", "John Michael Doe") is True
    assert names_match("Jane Mary Smith", "Jane Ann Smith") is True


def test_names_match_mismatch():
    assert names_match("John Doe", "Jane Doe") is False
    assert names_match("John Smith", "John Doe") is False
    assert names_match("Alice Cooper", "Bob Cooper") is False
    assert names_match("", "John Doe") is False
