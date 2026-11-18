"""Unit tests for precedence-aware intent classification."""

import pytest

from app.voice.intent import Intent, classify_caller_intent


def test_empty_or_whitespace():
    assert classify_caller_intent("") == Intent.UNCERTAIN
    assert classify_caller_intent("   ") == Intent.UNCERTAIN


@pytest.mark.parametrize(
    "utterance",
    [
        "Is that tomorrow at 2 PM?",
        "Can you check if doctor is available?",
        "Could you tell me the address?",
        "What if I need to cancel later?",
        "Will it be Dr. Smith?",
        "How about Monday instead?",
        "Yes, but what time?",  # Question mark triggers inquiry first
    ],
)
def test_inquiry_precedence(utterance):
    assert classify_caller_intent(utterance) == Intent.INQUIRY


@pytest.mark.parametrize(
    "utterance",
    [
        "No, don't do that",
        "Stop",
        "Cancel",
        "Wait, that is wrong",
        "Not yet",
        "No please",
        "Incorrect date",
        "Don't confirm it",
    ],
)
def test_negation_precedence(utterance):
    assert classify_caller_intent(utterance) == Intent.NEGATIVE


@pytest.mark.parametrize(
    "utterance",
    [
        "Maybe that works",
        "If possible, please",
        "Assuming my car works",
        "Um, I think so",
        "Probably yes",
        "Perhaps tomorrow",
    ],
)
def test_hesitation_precedence(utterance):
    assert classify_caller_intent(utterance) == Intent.UNCERTAIN


@pytest.mark.parametrize(
    "utterance",
    [
        "Yes",
        "YES",
        "yes please",
        "Yes, please!",
        "Yes, I confirm.",
        "Please confirm",
        "That is correct",
        "Sounds good",
        "I approve",
        "Go ahead",
        "Confirm booking",
        "Confirm reschedule",
        "Confirm cancellation",
    ],
)
def test_affirmative_allowlist(utterance):
    assert classify_caller_intent(utterance) == Intent.AFFIRMATIVE


def test_unrecognized_phrasing():
    assert classify_caller_intent("I am a banana") == Intent.UNCERTAIN
    assert classify_caller_intent("The weather is nice today") == Intent.UNCERTAIN
