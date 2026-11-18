"""Mock the clinic repository for quota-free LiveKit dispatch evaluations."""

from unittest.mock import AsyncMock

from app.services import clinic_lookup


def install_fake_clinic(monkeypatch, profile, hours, faqs):
    profile.timezone = "Asia/Kolkata"
    profile.email = None
    profile.website = None
    monkeypatch.setattr(clinic_lookup, "get_profile_by_id", AsyncMock(return_value=profile))
    monkeypatch.setattr(clinic_lookup, "get_hours_by_clinic_id", AsyncMock(return_value=hours))
    monkeypatch.setattr(
        clinic_lookup, "get_upcoming_closures_by_clinic_id", AsyncMock(return_value=())
    )
    monkeypatch.setattr(clinic_lookup, "get_policies_by_clinic_id", AsyncMock(return_value=()))
    monkeypatch.setattr(clinic_lookup, "get_active_faqs_by_clinic_id", AsyncMock(return_value=faqs))
