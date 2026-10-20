"""Organizer dashboard and volunteer-review operations backed by JSON storage."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from data_store import load, save


EVENTS_FILE = "events.json"
APPLICATIONS_FILE = "applications.json"
USERS_FILE = "users.json"
APPLICATION_STATUSES = {"pending", "accepted", "rejected"}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string.")
    return value.strip()


def _load_owned_event(organizer_id: str, event_id: str) -> tuple[dict, dict, dict]:
    organizer_id = _required_text(organizer_id, "organizer_id")
    event_id = _required_text(event_id, "event_id")

    events = load(EVENTS_FILE)
    if not isinstance(events, dict):
        raise ValueError("events.json must contain an object keyed by event ID.")
    event = events.get(event_id)
    if not isinstance(event, dict) or event.get("organizer") != organizer_id:
        raise KeyError(event_id)

    applications = load(APPLICATIONS_FILE)
    if not isinstance(applications, dict):
        raise ValueError("applications.json must contain an object keyed by event ID.")
    event_applications = applications.get(event_id, {})
    if not isinstance(event_applications, dict):
        raise ValueError(f"Applications for event '{event_id}' must be an object.")
    return event, applications, event_applications


def _validated_status(application: Mapping[str, Any], volunteer_id: str) -> str:
    status = application.get("status")
    if not isinstance(status, str) or status.casefold() not in APPLICATION_STATUSES:
        raise ValueError(f"Application for '{volunteer_id}' has an invalid status.")
    return status.casefold()


def _application_counts(event_applications: Mapping[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in APPLICATION_STATUSES}
    for volunteer_id, application in event_applications.items():
        if not isinstance(application, Mapping):
            raise ValueError(f"Application for '{volunteer_id}' must be an object.")
        counts[_validated_status(application, volunteer_id)] += 1
    return counts


def event_calendar_date(event: Mapping[str, Any]) -> date | None:
    """Return the event's calendar date, or None when no date was recorded."""
    for field in ("date", "event_date", "starts_at", "start_at", "start_date"):
        value = event.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"Event '{field}' must be an ISO date or timestamp.")
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError as error:
            raise ValueError(f"Event '{field}' must be a valid ISO date or timestamp.") from error
    return None


def is_event_day(event: Mapping[str, Any], today: date | None = None) -> bool:
    """Whether the event's recorded calendar date is today."""
    event_date = event_calendar_date(event)
    return event_date is not None and event_date == (today or date.today())


def get_organizer_events(organizer_id: str) -> list[dict[str, Any]]:
    """List an organizer's posted events with application counts."""
    organizer_id = _required_text(organizer_id, "organizer_id")
    events = load(EVENTS_FILE)
    applications = load(APPLICATIONS_FILE)
    if not isinstance(events, dict):
        raise ValueError("events.json must contain an object keyed by event ID.")
    if not isinstance(applications, dict):
        raise ValueError("applications.json must contain an object keyed by event ID.")

    results = []
    for event_id, event in events.items():
        if not isinstance(event_id, str) or not isinstance(event, dict):
            raise ValueError("Each event must be an object keyed by a string ID.")
        if event.get("organizer") != organizer_id:
            continue

        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, dict):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        counts = _application_counts(event_applications)
        slots_needed = event.get("slots_needed")
        if (
            not isinstance(slots_needed, int)
            or isinstance(slots_needed, bool)
            or slots_needed <= 0
        ):
            raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")

        event_date = event_calendar_date(event)
        results.append({
            "event_id": event_id,
            "event": event,
            "event_date": event_date.isoformat() if event_date else None,
            "pending_count": counts["pending"],
            "accepted_count": counts["accepted"],
            "rejected_count": counts["rejected"],
            "slots_remaining": max(0, slots_needed - counts["accepted"]),
        })

    return sorted(
        results,
        key=lambda item: (
            item["event_date"] or "9999-12-31",
            item["event"].get("name", "").casefold()
            if isinstance(item["event"].get("name"), str)
            else "",
        ),
    )


def get_event_applicants(organizer_id: str, event_id: str) -> dict[str, Any]:
    """Return an owned event and its applicants' safe profile details."""
    event, _applications, event_applications = _load_owned_event(organizer_id, event_id)
    users = load(USERS_FILE)
    if not isinstance(users, dict):
        raise ValueError("users.json must contain an object keyed by username.")

    applicants = []
    for volunteer_id, application in event_applications.items():
        if not isinstance(volunteer_id, str) or not isinstance(application, Mapping):
            raise ValueError("Each application must be an object keyed by username.")
        status = _validated_status(application, volunteer_id)
        user = users.get(volunteer_id, {})
        if not isinstance(user, Mapping):
            raise ValueError(f"User '{volunteer_id}' must be an object.")
        skills = user.get("skills", {})
        if not isinstance(skills, Mapping):
            raise ValueError(f"User '{volunteer_id}' must have a skills object.")
        skill_items = []
        for skill, rating in skills.items():
            if (
                not isinstance(skill, str)
                or not skill.strip()
                or not isinstance(rating, int)
                or isinstance(rating, bool)
                or rating < 0
            ):
                raise ValueError(f"User '{volunteer_id}' has invalid skill data.")
            skill_items.append((skill, rating))

        attended = application.get("attended", False)
        if not isinstance(attended, bool):
            raise ValueError(f"Application for '{volunteer_id}' has invalid attendance data.")
        role = application.get("role")
        if role is not None and not isinstance(role, str):
            raise ValueError(f"Application for '{volunteer_id}' has an invalid assigned role.")
        applicants.append({
            "username": volunteer_id,
            "name": user.get("name") if isinstance(user.get("name"), str) else volunteer_id,
            "department": user.get("dept") if isinstance(user.get("dept"), str) else None,
            "skill_items": sorted(skill_items, key=lambda item: item[0].casefold()),
            "status": status,
            "role": role,
            "attended": attended,
            "attended_on": application.get("attended_on"),
        })

    status_order = {"pending": 0, "accepted": 1, "rejected": 2}
    applicants.sort(key=lambda applicant: (
        status_order[applicant["status"]],
        applicant["name"].casefold(),
    ))
    event_date = event_calendar_date(event)
    counts = _application_counts(event_applications)
    slots_needed = event.get("slots_needed")
    if (
        not isinstance(slots_needed, int)
        or isinstance(slots_needed, bool)
        or slots_needed <= 0
    ):
        raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")

    return {
        "event_id": event_id,
        "event": event,
        "event_date": event_date.isoformat() if event_date else None,
        "is_event_day": is_event_day(event),
        "slots_remaining": max(0, slots_needed - counts["accepted"]),
        "applicants": applicants,
    }


def update_application_status(
    organizer_id: str,
    event_id: str,
    volunteer_id: str,
    status: str,
    role: str | None = None,
) -> None:
    """Accept or reject an applicant, assigning a role and enforcing capacity."""
    volunteer_id = _required_text(volunteer_id, "volunteer_id")
    if not isinstance(status, str) or status.strip().casefold() not in {"accepted", "rejected"}:
        raise ValueError("Choose whether to accept or reject the application.")
    new_status = status.strip().casefold()
    if new_status == "accepted":
        role = _required_text(role, "role")
        if len(role) > 80:
            raise ValueError("The assigned role must be 80 characters or fewer.")
    else:
        role = None

    event, applications, event_applications = _load_owned_event(organizer_id, event_id)
    application = event_applications.get(volunteer_id)
    if not isinstance(application, dict):
        raise ValueError(f"Application for '{volunteer_id}' was not found.")
    current_status = _validated_status(application, volunteer_id)
    if application.get("attended") is True and new_status != "accepted":
        raise ValueError("Attendance has been recorded; this application cannot be rejected.")

    slots_needed = event.get("slots_needed")
    if (
        not isinstance(slots_needed, int)
        or isinstance(slots_needed, bool)
        or slots_needed <= 0
    ):
        raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")
    counts = _application_counts(event_applications)
    if new_status == "accepted" and current_status != "accepted":
        if counts["accepted"] >= slots_needed:
            raise ValueError(f"Event '{event_id}' has no volunteer slots remaining.")

    application["status"] = new_status
    application["role"] = role
    event_applications[volunteer_id] = application
    applications[event_id] = event_applications
    save(APPLICATIONS_FILE, applications)


def mark_attendance(
    organizer_id: str,
    event_id: str,
    volunteer_id: str,
    today: date | None = None,
) -> bool:
    """Mark an accepted volunteer present on the event's calendar day."""
    volunteer_id = _required_text(volunteer_id, "volunteer_id")
    event, applications, event_applications = _load_owned_event(organizer_id, event_id)
    application = event_applications.get(volunteer_id)
    if not isinstance(application, dict):
        raise ValueError(f"Application for '{volunteer_id}' was not found.")
    if _validated_status(application, volunteer_id) != "accepted":
        raise ValueError("Only accepted volunteers can have attendance recorded.")

    event_date = event_calendar_date(event)
    if event_date is None:
        raise ValueError("Add an event date before recording attendance.")
    current_date = today or date.today()
    if not isinstance(current_date, date) or isinstance(current_date, datetime):
        raise ValueError("'today' must be a calendar date.")
    if event_date != current_date:
        raise ValueError(f"Attendance can only be recorded on {event_date.isoformat()}.")

    if application.get("attended") is True:
        return False
    application["attended"] = True
    application["attended_on"] = current_date.isoformat()
    event_applications[volunteer_id] = application
    applications[event_id] = event_applications
    save(APPLICATIONS_FILE, applications)
    return True
