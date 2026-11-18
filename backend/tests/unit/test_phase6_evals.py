"""Offline scripted scenarios exercise the production tool boundary, not model judgment."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from livekit.agents import llm

from app.services.voice_booking_service import SlotUnavailableError, UnverifiedCallerSessionError
from app.voice.clinic_tools import ClinicVoiceTools
from app.voice.text_loop import ScriptedPlanner, TextToolLoop, ToolStep
from tests.fake_clinic_lookup import install_fake_clinic

SCENARIOS = [
    ("faq address", "get_clinic_info", {"topic": "address"}, "located"),
    ("faq hours", "get_clinic_info", {"topic": "hours"}, "open"),
    ("faq contact", "get_clinic_info", {"topic": "phone"}, "phone"),
    ("faq unknown", "get_clinic_info", {"topic": "clinic"}, "do not have"),
    ("availability bad date", "check_availability", {"service_id": 1, "start_date": "bad"}, "valid start date"),
    ("booking readback", "create_booking_proposal", {"service_id": 1, "start_at": "2027-01-01T10:00:00+05:30", "caller_name": "Test", "caller_phone": "5555555555"}, "[readback]"),
    ("booking conflict", "create_booking_proposal", {"service_id": 2, "start_at": "2027-01-01T10:00:00+05:30", "caller_name": "Test", "caller_phone": "5555555555"}, "no longer available"),
    ("verify caller", "lookup_appointment", {"phone_last4": "5555", "caller_name": "Test"}, "verification"),
    ("step up", "verify_appointment_step_up", {"full_phone": "5555555555", "caller_name": "Test"}, "verified"),
    ("unverified cancellation", "create_cancellation_proposal", {}, "verify"),
    ("unverified reschedule", "create_reschedule_proposal", {"new_start_at": "2027-01-01T10:00:00+05:30"}, "verify"),
    ("human handoff", "request_human_handoff", {}, "transfer"),
    ("urgent symptoms", "escalate_medical_emergency", {}, "emergency"),
    ("injection attempts confirmation", "confirm_booking", {"proposal_id": "attacker"}, "no booking awaiting"),
    ("injection attempts cancellation", "confirm_cancellation", {"proposal_id": "attacker"}, "no cancellation awaiting"),
    ("provider failure handoff", "request_human_handoff", {"category": "provider_failure"}, "failure"),
    ("unknown handoff", "request_human_handoff", {"category": "not-real"}, "transfer"),
]


@pytest.mark.parametrize("utterance,tool,args,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_scripted_scenarios(utterance, tool, args, expected, monkeypatch):
    service = MagicMock()
    async def proposal(**kwargs):
        if kwargs["service_id"] == 2:
            raise SlotUnavailableError("conflict")
        return {"readback_text": "Confirm?", "challenge_id": str(uuid4()), "proposal_id": str(uuid4())}
    service.create_booking_proposal = AsyncMock(side_effect=proposal)
    service.lookup_appointment = AsyncMock(return_value={"message": "verification required"})
    service.verify_appointment_step_up = AsyncMock(return_value={"message": "verified"})
    service.create_cancellation_proposal = AsyncMock(side_effect=UnverifiedCallerSessionError("unverified"))
    service.create_reschedule_proposal = AsyncMock(side_effect=UnverifiedCallerSessionError("unverified"))
    service.request_human_handoff = AsyncMock()
    profile = SimpleNamespace(address_line1="Clinic address", address_line2=None, city="City", state="State", postal_code="12345", country="India", phone="5555555555", name="Clinic")
    trace = MagicMock()
    from datetime import time
    hours = [SimpleNamespace(day_of_week=0, is_closed=False, open_time=time(9), close_time=time(18), break_start=None, break_end=None)]
    install_fake_clinic(monkeypatch, profile, hours, [])
    tools = ClinicVoiceTools(service, MagicMock(), SimpleNamespace(profile=profile, hours=hours, faqs=[]), "sess-abcdef123456", "client", trace=trace)
    ctx = SimpleNamespace(session=SimpleNamespace(say=MagicMock(return_value=MagicMock()), history=SimpleNamespace(items=[])), function_call=SimpleNamespace(call_id="missing"))
    loop = TextToolLoop(tools, ScriptedPlanner({utterance: [ToolStep(tool, args)]}), ctx)
    responses = await loop.turn(utterance)
    assert expected in responses[0].lower()
    trace.record_tool.assert_called_once()
    if utterance == "booking conflict":
        assert trace.record_tool.call_args.args[3] == "slot_conflict"
    if utterance.startswith("injection"):
        service.confirm_proposal.assert_not_called()


async def test_silence_has_no_tool_call():
    tools = MagicMock()
    tools.get_tools.return_value = []
    assert await TextToolLoop(tools, ScriptedPlanner({}), None).turn("") == []


@pytest.mark.parametrize("tool,expected,cancellation", [("confirm_booking", "confirmed", False), ("confirm_cancellation", "cancelled", True)])
async def test_confirmation_reaches_service_with_correlated_user_turn(tool, expected, cancellation):
    service = MagicMock()
    service.confirm_proposal = AsyncMock(return_value={"appointment_id": 123})
    voice_tools = ClinicVoiceTools(service, MagicMock(), MagicMock(), "sess-abcdef123456", "client", trace=MagicMock())
    voice_tools._pending_proposal = ("cancellation" if cancellation else "booking", str(uuid4()))
    user = llm.ChatMessage(role="user", content=["Yes"])
    user.metrics = {"started_speaking_at": 1700000000.0}
    voice_tools.record_turn(user.id)
    call = llm.FunctionCall(call_id="call-1", name=tool, arguments="{}")
    ctx = SimpleNamespace(session=SimpleNamespace(history=SimpleNamespace(items=[user, call])), function_call=SimpleNamespace(call_id="call-1"))
    loop = TextToolLoop(voice_tools, ScriptedPlanner({"Yes": [ToolStep(tool, {"proposal_id": "proposal"})]}), ctx)
    assert expected in (await loop.turn("Yes"))[0].lower()
    assert service.confirm_proposal.await_args.kwargs["is_cancellation"] is cancellation
    assert all(llm.is_function_tool(t) for t in voice_tools.get_tools())


async def test_conflict_at_confirmation_records_typed_failure():
    service = MagicMock()
    service.confirm_proposal = AsyncMock(side_effect=SlotUnavailableError("internal detail"))
    trace = MagicMock()
    voice_tools = ClinicVoiceTools(service, MagicMock(), MagicMock(), "sess-abcdef123456", "client", trace=trace)
    voice_tools._pending_proposal = ("booking", str(uuid4()))
    user = llm.ChatMessage(role="user", content=["Yes"])
    user.metrics = {"started_speaking_at": 1700000000.0}
    voice_tools.record_turn(user.id)
    call = llm.FunctionCall(call_id="call-1", name="confirm_booking", arguments="{}")
    ctx = SimpleNamespace(session=SimpleNamespace(history=SimpleNamespace(items=[user, call])), function_call=SimpleNamespace(call_id="call-1"))
    loop = TextToolLoop(voice_tools, ScriptedPlanner({"Yes": [ToolStep("confirm_booking", {"proposal_id": "proposal"})]}), ctx)
    await loop.turn("Yes")
    assert trace.record_tool.call_args.args[1] == "failed"
    assert trace.record_tool.call_args.args[3] == "slot_conflict"
