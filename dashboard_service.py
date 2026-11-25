"""Organizer dashboard and volunteer-review operations backed by JSON storage."""

from collections.abc import Mapping
import calendar
import json
import math
from datetime import date, datetime, time as time_value, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from data_store import edit_documents, load, save
from event_service import (
    EVENTS_FILE as EVENT_RECORDS_FILE,
    create_event,
    get_event_eligibility_reason,
    validate_event_data,
)
from formatting import format_decimal
from notification_service import add_individual_notifications, add_notifications
from event_presentation import EVENT_CATEGORIES, event_category
from prismlabs_service import EVENT_ID as PRISMLABS_EVENT_ID, event_stats as prismlabs_stats


EVENTS_FILE = "events.json"
APPLICATIONS_FILE = "applications.json"
USERS_FILE = "users.json"
APPLICATION_STATUSES = {"pending", "accepted", "rejected"}
PROFILE_SKILLS_FILE = Path(__file__).resolve().parent / "static" / "data" / "profile-skills.json"


def _event_profile_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the same department and skill catalog used by profile selectors."""
    with PROFILE_SKILLS_FILE.open("r", encoding="utf-8") as source:
        data = json.load(source)
    schools = data.get("schools") if isinstance(data, dict) else None
    if not isinstance(schools, list):
        raise ValueError("Profile skills data must contain a schools list.")

    department_groups = []
    skill_groups: list[dict[str, Any]] = []
    seen_departments: set[str] = set()
    seen_skills: set[str] = set()

    def add_skills(group_name: str, values: Any) -> None:
        if not isinstance(values, list):
            return
        group_skills = []
        for skill in values:
            if not isinstance(skill, str) or not skill.strip():
                continue
            clean = skill.strip()
            normalized = clean.casefold()
            if normalized not in seen_skills:
                seen_skills.add(normalized)
                group_skills.append(clean)
        if group_skills:
            skill_groups.append({"label": group_name, "options": group_skills})

    for school in schools:
        if not isinstance(school, Mapping):
            continue
        school_name = str(school.get("name", "Other departments"))
        departments = school.get("departments", [])
        department_names = []
        school_skills = []
        if isinstance(departments, list) and departments:
            for department in departments:
                if not isinstance(department, Mapping):
                    continue
                name = department.get("name")
                if isinstance(name, str) and name.strip():
                    clean = name.strip()
                    if clean.casefold() not in seen_departments:
                        seen_departments.add(clean.casefold())
                        department_names.append(clean)
                school_skills.extend(
                    department.get("skills", [])
                    if isinstance(department.get("skills"), list)
                    else []
                )
        else:
            name = school.get("name")
            if isinstance(name, str) and name.strip():
                clean = name.strip()
                if clean.casefold() not in seen_departments:
                    seen_departments.add(clean.casefold())
                    department_names.append(clean)
            school_skills = school.get("skills", [])

        if department_names:
            department_groups.append({
                "label": school_name,
                "options": department_names,
            })
        add_skills(f"{school_name} skills", school_skills)

    add_skills("General skills", data.get("general_skills", []))
    add_skills("Spoken languages", data.get("spoken_languages", []))
    return department_groups, skill_groups


def organizer_event_department_names() -> list[str]:
    """Return department choices in the same order as the profile form."""
    groups, _skills = _event_profile_catalog()
    return [
        name for group in groups for name in group["options"]
    ]


def organizer_event_skill_names() -> list[str]:
    """Return skill choices in the same order as the profile form."""
    _departments, groups = _event_profile_catalog()
    return [name for group in groups for name in group["options"]]


def organizer_event_picker_options(
    option_group: str,
    search: str = "",
    selected: list[str] | tuple[str, ...] = (),
    existing_skills: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Build searchable native-select option groups while retaining selections."""
    departments, skills = _event_profile_catalog()
    groups = departments if option_group == "departments" else skills
    selected_names = [value.strip() for value in selected if isinstance(value, str) and value.strip()]
    selected_keys = {value.casefold() for value in selected_names}
    normalized_search = search.strip().casefold()
    result = []
    displayed = set()

    # Keep selected values visible even when they do not match the active search.
    selected_not_in_results = [
        value for value in selected_names
        if normalized_search and normalized_search not in value.casefold()
    ]
    if option_group == "skills":
        known = {name.casefold() for group in groups for name in group["options"]}
        for value in existing_skills:
            if (
                isinstance(value, str)
                and value.strip()
                and value.casefold() not in known
                and value.casefold() in selected_keys
                and value not in selected_not_in_results
            ):
                selected_not_in_results.append(value.strip())
    if selected_not_in_results:
        result.append({
            "label": "Selected",
            "options": [
                {"name": value, "selected": True}
                for value in selected_not_in_results
            ],
        })
        displayed.update(value.casefold() for value in selected_not_in_results)

    for group in groups:
        matching = []
        for name in group["options"]:
            normalized = name.casefold()
            if normalized in displayed:
                continue
            if normalized_search and normalized_search not in normalized:
                continue
            matching.append({"name": name, "selected": normalized in selected_keys})
        if matching:
            result.append({"label": group["label"], "options": matching})
            displayed.update(option["name"].casefold() for option in matching)

    if not result and selected_names:
        result.append({
            "label": "Selected",
            "options": [
                {"name": value, "selected": True}
                for value in selected_names
            ],
        })
    return result


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
    if (
        not isinstance(event, dict)
        or event.get("organizer") != organizer_id
        or event.get("deleted_at")
    ):
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
        if event.get("deleted_at"):
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
        is_imported_roster = event_id == PRISMLABS_EVENT_ID and event.get("source") == "prismlabs"
        roster_stats = prismlabs_stats() if is_imported_roster else {}
        results.append({
            "event_id": event_id,
            "event": event,
            "event_date": event_date.isoformat() if event_date else None,
            "pending_count": counts["pending"],
            "accepted_count": counts["accepted"],
            "rejected_count": counts["rejected"],
            "is_imported_roster": is_imported_roster,
            "participant_count": roster_stats.get("total", 0),
            "linked_account_count": roster_stats.get("linked_accounts", 0),
            "slots_remaining": (
                0 if event.get("source") == "prismlabs"
                else max(0, slots_needed - counts["accepted"])
            ),
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


def dashboard_cards(username: str) -> list[dict[str, Any]]:
    """Build display-ready event summaries for one organizer."""
    cards = []
    for item in get_organizer_events(username):
        event = item["event"]
        event_date = event_calendar_date(event)
        status = event.get("status")
        if not isinstance(status, str) or not status.strip():
            raise ValueError(f"Event '{item['event_id']}' must have a status.")

        counts = {
            "pending": item["pending_count"],
            "accepted": item["accepted_count"],
            "rejected": item["rejected_count"],
        }
        cards.append({
            "event_id": item["event_id"],
            "event": event,
            "day": event_date.strftime("%d") if event_date else "",
            "month": calendar.month_abbr[event_date.month] if event_date else "",
            "status": status,
            "seats_filled": item["participant_count"] if item["is_imported_roster"] else counts["accepted"],
            "is_imported_roster": item["is_imported_roster"],
            "participant_count": item["participant_count"],
            "linked_account_count": item["linked_account_count"],
            "total_seats": event["slots_needed"],
            "applicant_counts": [
                {"status": key, "label": key.title(), "count": counts[key]}
                for key in ("pending", "accepted", "rejected")
            ],
        })
    return cards


def _event_form_payload(organizer_id: str, form: Mapping[str, Any]) -> dict[str, Any]:
    """Parse and validate the organizer form into the shared event data shape."""
    organizer_id = _required_text(organizer_id, "organizer_id")

    def text_field(name: str, label: str, limit: int) -> str:
        value = form.get(name)
        value = _required_text(value, label)
        if len(value) > limit:
            raise ValueError(f"'{label}' must be {limit} characters or fewer.")
        return value

    def list_field(name: str) -> list[str]:
        getlist = getattr(form, "getlist", None)
        raw_values = getlist(name) if callable(getlist) else form.get(name, [])
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if not isinstance(raw_values, (list, tuple)):
            return []
        return [value.strip() for value in raw_values if isinstance(value, str) and value.strip()]

    def raw_list_field(name: str) -> list[str]:
        getlist = getattr(form, "getlist", None)
        raw_values = getlist(name) if callable(getlist) else form.get(name, [])
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if not isinstance(raw_values, (list, tuple)):
            return []
        return [value.strip() if isinstance(value, str) else "" for value in raw_values]

    required_skills: dict[str, int] = {}
    if form.get("required_skill_names_present") == "1":
        selected_skills = list_field("required_skill_names")
        skill_requirement_mode = form.get("skill_requirement_mode", "none")
        if skill_requirement_mode not in {"none", "selected"}:
            raise ValueError("Choose whether this event has required skills.")
        if skill_requirement_mode == "none":
            selected_skills = []
        elif not selected_skills:
            raise ValueError("Select at least one required skill.")

        normalized_selected: dict[str, str] = {}
        if skill_requirement_mode == "selected":
            catalog_skills = {
                skill.casefold() for skill in organizer_event_skill_names()
            }
            existing_skill_values = form.get("_allowed_existing_skills", [])
            if isinstance(existing_skill_values, str):
                existing_skill_values = [existing_skill_values]
            allowed_skills = catalog_skills | {
                skill.casefold() for skill in existing_skill_values
                if isinstance(skill, str)
            }
            for skill in selected_skills:
                normalized = skill.casefold()
                if normalized not in allowed_skills:
                    raise ValueError(f"Choose a listed skill: {skill}.")
                if normalized in normalized_selected:
                    raise ValueError(f"Choose each skill only once: {skill}.")
                normalized_selected[normalized] = skill
            required_skills = {
                skill: 1 for skill in normalized_selected.values()
            }

        rating_names = raw_list_field("required_skill_name")
        rating_values = raw_list_field("required_skill_rating")
        if skill_requirement_mode == "selected" and (rating_names or rating_values):
            if len(rating_names) != len(rating_values):
                raise ValueError("Set a minimum rating for every selected skill.")
            rated_skills: set[str] = set()
            for skill, rating_text in zip(rating_names, rating_values):
                normalized_name = skill.casefold()
                selected_name = normalized_selected.get(normalized_name)
                if selected_name is None:
                    # Stale rating rows can remain when a user changes selections
                    # without refreshing the controls; they are not saved.
                    continue
                if normalized_name in rated_skills:
                    raise ValueError(f"Set only one minimum rating for {selected_name}.")
                if not rating_text.isascii() or not rating_text.isdigit():
                    raise ValueError("Minimum ratings must be non-negative whole numbers.")
                try:
                    rating = int(rating_text)
                except ValueError as error:
                    raise ValueError("Minimum ratings must be valid whole numbers.") from error
                rated_skills.add(normalized_name)
                required_skills[selected_name] = rating
        elif skill_requirement_mode == "selected":
            # Accept old forms that were open in another tab while preserving new
            # form submissions as per-skill controls.
            overrides_text = form.get("required_skills", "")
            if isinstance(overrides_text, str) and overrides_text:
                overridden_skills: set[str] = set()
                for line_number, line in enumerate(overrides_text.splitlines(), start=1):
                    line = line.strip()
                    if not line:
                        continue
                    skill, separator, rating_text = line.rpartition(":")
                    skill = skill.strip()
                    rating_text = rating_text.strip()
                    if (
                        not separator
                        or not skill
                        or not rating_text.isascii()
                        or not rating_text.isdigit()
                    ):
                        raise ValueError(
                            f"Required skill line {line_number} must use the format 'Skill: rating'."
                        )
                    selected_name = normalized_selected.get(skill.casefold())
                    if selected_name is None:
                        raise ValueError(f"Add {skill} to the selected skills before setting its rating.")
                    normalized_name = selected_name.casefold()
                    if normalized_name in overridden_skills:
                        raise ValueError(f"Set only one minimum rating for {selected_name}.")
                    overridden_skills.add(normalized_name)
                    required_skills[selected_name] = int(rating_text)
    else:
        # Keep accepting the earlier form format while older clients/bookmarks are in use.
        skills_text = form.get("required_skills")
        if not isinstance(skills_text, str):
            skills_text = ""
        for line_number, line in enumerate(skills_text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            skill, separator, rating_text = line.rpartition(":")
            skill = skill.strip()
            rating_text = rating_text.strip()
            if not separator or not skill or not rating_text.isdigit():
                raise ValueError(
                    f"Required skill line {line_number} must use the format 'Skill: rating'."
                )
            required_skills[skill] = int(rating_text)

    departments: list[str] = []
    if form.get("departments_present") == "1":
        selected_departments = list_field("departments")
        catalog_departments = {
            name.casefold(): name for name in organizer_event_department_names()
        }
        for department in selected_departments:
            canonical = catalog_departments.get(department.casefold())
            if canonical is None:
                raise ValueError(f"Choose a listed department: {department}.")
            if canonical not in departments:
                departments.append(canonical)
        department_value = (
            departments[0] if len(departments) == 1
            else "Multiple departments" if departments else ""
        )
    else:
        # Legacy forms used one free-text event department, not participation limits.
        department_value = form.get("department", "")

    date_text = form.get("date")
    if not isinstance(date_text, str):
        raise ValueError("'date' must be a valid date.")
    try:
        event_date = date.fromisoformat(date_text.strip())
    except ValueError as error:
        raise ValueError("'date' must be a valid date.") from error

    start_time_text = form.get("start_time", "09:00")
    if not isinstance(start_time_text, str):
        raise ValueError("'start_time' must be a valid time.")
    try:
        start_time = time_value.fromisoformat(start_time_text.strip())
    except ValueError as error:
        raise ValueError("'start_time' must be a valid time.") from error
    if start_time.tzinfo is not None:
        raise ValueError("'start_time' must not include a timezone.")

    duration_text = form.get("duration")
    try:
        duration_hours = float(duration_text)
    except (TypeError, ValueError) as error:
        raise ValueError("'duration' must be a number of hours.") from error
    if not math.isfinite(duration_hours) or not 0 < duration_hours <= 72:
        raise ValueError("'duration' must be more than 0 and no more than 72 hours.")

    seats_text = form.get("seats")
    if not isinstance(seats_text, str) or not seats_text.strip().isdigit():
        raise ValueError("'seats' must be a positive whole number.")
    seats = int(seats_text.strip())
    if not 0 < seats <= 10000:
        raise ValueError("'seats' must be between 1 and 10000.")

    starts_at = datetime.combine(
        event_date, start_time, tzinfo=ZoneInfo("Asia/Kolkata")
    )
    ends_at = starts_at + timedelta(hours=duration_hours)
    category = form.get("category")
    if category and category not in EVENT_CATEGORIES:
        raise ValueError("Choose a category from the available event categories.")
    return {
        "name": text_field("title", "title", 120),
        "description": text_field("description", "description", 3000),
        "organizer": organizer_id,
        "required_skills": required_skills,
        "slots_needed": seats,
        **(
            {"departments": departments}
            if form.get("departments_present") == "1"
            else {}
        ),
        **(
            {"department": department_value.strip()}
            if isinstance(department_value, str) and department_value.strip()
            else {}
        ),
        "type": text_field("event_type", "event type", 100),
        "location": text_field("venue", "venue", 160),
        "date": event_date.isoformat(),
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "duration_hours": duration_hours,
        "category": category,
        "image_url": form.get("image_url", ""),
    }


def organizer_event_form_values(event: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build form values from an existing event, or return create-form defaults."""
    if not isinstance(event, Mapping):
        return {
            "start_time": "09:00",
            "duration": "1",
            "category": "Community Service",
            "departments": [],
            "required_skill_names": [],
            "required_skills": "",
            "skill_requirement_mode": "none",
        }
    start_value = event.get("starts_at")
    starts_at = None
    if isinstance(start_value, str):
        try:
            starts_at = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=timezone.utc)
            starts_at = starts_at.astimezone(ZoneInfo("Asia/Kolkata"))
        except ValueError:
            starts_at = None
    event_date = event.get("date")
    if not isinstance(event_date, str) and starts_at is not None:
        event_date = starts_at.date().isoformat()
    duration = event.get("duration_hours")
    if not isinstance(duration, (int, float)) and starts_at is not None:
        ends_value = event.get("ends_at")
        if isinstance(ends_value, str):
            try:
                ends_at = datetime.fromisoformat(ends_value.replace("Z", "+00:00"))
                duration = (ends_at - starts_at).total_seconds() / 3600
            except ValueError:
                duration = 1
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        duration = 1
    skills = event.get("required_skills", {})
    skill_lines = (
        "\n".join(f"{skill}: {rating}" for skill, rating in skills.items())
        if isinstance(skills, Mapping)
        else ""
    )
    return {
        "title": str(event.get("name", "")),
        "description": str(event.get("description", "")),
        "required_skills": skill_lines,
        "required_skill_names": list(skills) if isinstance(skills, Mapping) else [],
        "skill_rating_rows": (
            [
                {"name": name, "rating": rating}
                for name, rating in skills.items()
            ]
            if isinstance(skills, Mapping)
            else []
        ),
        "skill_requirement_mode": "selected" if isinstance(skills, Mapping) and skills else "none",
        "departments": (
            [value for value in event.get("departments", []) if isinstance(value, str)]
            if isinstance(event.get("departments"), list)
            else []
        ),
        "department": str(event.get("department", event.get("dept", ""))),
        "event_type": str(event.get("type", event.get("event_type", ""))),
        "date": event_date if isinstance(event_date, str) else "",
        "start_time": starts_at.strftime("%H:%M") if starts_at else "09:00",
        "venue": str(event.get("location", event.get("venue", ""))),
        "duration": format_decimal(duration),
        "seats": str(event.get("slots_needed", "")),
        "category": event_category(event),
        "image_url": str(event.get("image_url") or ""),
    }


def create_organizer_event(organizer_id: str, form: Mapping[str, Any]) -> str:
    """Validate and create an event owned by the signed-in organizer."""
    payload = _event_form_payload(organizer_id, form)
    users = load(USERS_FILE)
    if not isinstance(users, dict):
        raise ValueError("users.json must contain an object keyed by username.")
    event_id = create_event(payload)
    event_name = payload["name"]
    add_notifications(
        [organizer_id],
        kind="event_posted",
        title="Event posted",
        message=f"Your event, {event_name}, is now open for applications.",
        event_id=event_id,
    )
    volunteer_ids = [
        username
        for username, user in users.items()
        if isinstance(username, str)
        and isinstance(user, Mapping)
        and user.get("role") == "Student Volunteer"
    ]
    if volunteer_ids:
        add_notifications(
            volunteer_ids,
            kind="event_posted",
            title="New event posted",
            message=f"{event_name} is open. Review the event details and apply if interested.",
            event_id=event_id,
        )
    return event_id


def get_organizer_event(organizer_id: str, event_id: str) -> dict[str, Any]:
    """Return an event only when it belongs to the requested organizer."""
    event, _events, _applications = _load_owned_event(organizer_id, event_id)
    return event


def hide_organizer_event(organizer_id: str, event_id: str) -> None:
    """Hide an owned event while preserving its applications and certificates."""
    organizer_id = _required_text(organizer_id, "organizer_id")
    event_id = _required_text(event_id, "event_id")
    with edit_documents(EVENTS_FILE) as docs:
        events = docs[EVENTS_FILE]
        if not isinstance(events, dict):
            raise ValueError("events.json must contain an object keyed by event ID.")
        event = events.get(event_id)
        if (
            not isinstance(event, dict)
            or event.get("organizer") != organizer_id
            or event.get("deleted_at")
        ):
            raise KeyError(event_id)
        if event.get("source") == "prismlabs":
            raise ValueError("Manage this imported event in the PrismLabs section.")
        event["deleted_at"] = datetime.now(timezone.utc).isoformat()


def compare_applicant_skills(
    applicant_skills: Mapping[str, Any],
    required_skills: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare required event skills to applicant ratings without case sensitivity."""
    if not isinstance(applicant_skills, Mapping):
        raise ValueError("Applicant skills must be an object.")
    if not isinstance(required_skills, Mapping):
        raise ValueError("Required skills must be an object.")

    applicant_ratings: dict[str, int] = {}
    for name, rating in applicant_skills.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(rating, int)
            or isinstance(rating, bool)
            or rating < 0
        ):
            raise ValueError("Applicant skill names and ratings are invalid.")
        applicant_ratings[name.strip().casefold()] = rating

    comparisons = []
    for name, required_rating in required_skills.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Required skill names must be non-empty strings.")
        if (
            not isinstance(required_rating, int)
            or isinstance(required_rating, bool)
            or required_rating < 0
        ):
            raise ValueError("Required skill ratings must be non-negative integers.")
        applicant_rating = applicant_ratings.get(name.strip().casefold())
        comparisons.append({
            "name": name.strip(),
            "applicant_rating": applicant_rating,
            "required_rating": required_rating,
            "met": (
                applicant_rating is not None
                and applicant_rating >= required_rating
            ),
        })
    return comparisons


def _skill_fit_summary(skill_matches: list[dict[str, Any]]) -> dict[str, int | None]:
    """Summarize fit using only published requirements and volunteer skill ratings."""
    required_count = len(skill_matches)
    met_count = sum(match["met"] for match in skill_matches)
    return {
        "fit_met_count": met_count,
        "fit_total_count": required_count,
        "fit_percent": round(met_count * 100 / required_count) if required_count else None,
    }


def update_organizer_event(
    organizer_id: str, event_id: str, form: Mapping[str, Any]
) -> None:
    """Update an owned event while preserving its ID, status, and creation time."""
    event, events, event_applications = _load_owned_event(organizer_id, event_id)
    form = form.copy() if callable(getattr(form, "copy", None)) else dict(form)
    existing_skills = event.get("required_skills", {})
    form["_allowed_existing_skills"] = (
        list(existing_skills) if isinstance(existing_skills, Mapping) else []
    )
    form = {
        "category": event_category(event),
        "image_url": event.get("image_url", ""),
        **form,
    }
    payload = validate_event_data(_event_form_payload(organizer_id, form))
    payload["status"] = event.get("status", "open")
    if payload["status"] not in ("open", "completed"):
        raise ValueError(f"Event '{event_id}' has an invalid status.")
    payload["created_at"] = event.get("created_at")
    updated_event = dict(event)
    updated_event.update(payload)
    details_fields = (
        "name", "description", "required_skills", "departments", "slots_needed", "department",
        "type", "location", "date", "starts_at", "ends_at", "duration_hours",
        "category", "image_url",
    )
    details_changed = any(
        event.get(field) != updated_event.get(field) for field in details_fields
    )
    events[event_id] = updated_event
    save(EVENT_RECORDS_FILE, events)
    if details_changed:
        recipients = [organizer_id]
        recipients.extend(
            applicant_id
            for applicant_id in event_applications
            if isinstance(applicant_id, str)
        )
        add_notifications(
            recipients,
            kind="event_details_updated",
            title="Event details updated",
            message=(
                f"The details for {updated_event['name']} changed. "
                "Open the event to see the latest schedule, venue, and requirements."
            ),
            event_id=event_id,
        )


def get_event_applicants(
    organizer_id: str, event_id: str, department: str | None = None
) -> dict[str, Any]:
    """Return an owned event and its applicants, optionally filtered by department."""
    event, _applications, event_applications = _load_owned_event(organizer_id, event_id)
    users = load(USERS_FILE)
    if not isinstance(users, dict):
        raise ValueError("users.json must contain an object keyed by username.")
    required_skills = event.get("required_skills", {})
    if not isinstance(required_skills, Mapping):
        raise ValueError(f"Event '{event_id}' must have required_skills as an object.")

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
        skill_matches = compare_applicant_skills(skills, required_skills)
        applicants.append({
            "username": volunteer_id,
            "name": user.get("name") if isinstance(user.get("name"), str) else volunteer_id,
            "department": user.get("dept") if isinstance(user.get("dept"), str) else None,
            "skill_items": sorted(skill_items, key=lambda item: item[0].casefold()),
            "skill_matches": skill_matches,
            **_skill_fit_summary(skill_matches),
            "status": status,
            "role": role,
            "attended": attended,
            "attended_on": application.get("attended_on"),
        })

    status_order = {"pending": 0, "accepted": 1, "rejected": 2}
    applicants.sort(key=lambda applicant: (
        status_order[applicant["status"]],
        -(
            applicant["fit_percent"]
            if applicant["fit_percent"] is not None
            else -1
        ),
    ))

    invited_departments = event.get("departments", [])
    if not isinstance(invited_departments, list):
        invited_departments = []
    department_options = []
    department_keys = set()
    for value in invited_departments:
        if (
            isinstance(value, str)
            and value.strip()
            and value.strip().casefold() not in department_keys
        ):
            clean = value.strip()
            department_options.append(clean)
            department_keys.add(clean.casefold())
    # An empty event department list means the event is open to all departments.
    # In that case, offer the departments represented by its applicants.
    if not department_options:
        for applicant in applicants:
            value = applicant["department"]
            if (
                isinstance(value, str)
                and value.strip()
                and value.strip().casefold() not in department_keys
            ):
                clean = value.strip()
                department_options.append(clean)
                department_keys.add(clean.casefold())
        department_options.sort(key=str.casefold)

    selected_department = None
    if isinstance(department, str) and department.strip():
        selected_department = next(
            (
                option for option in department_options
                if option.casefold() == department.strip().casefold()
            ),
            None,
        )
    if selected_department is not None:
        selected_key = selected_department.casefold()
        applicants = [
            applicant for applicant in applicants
            if isinstance(applicant["department"], str)
            and applicant["department"].strip().casefold() == selected_key
        ]

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
        "department_options": department_options,
        "selected_department": selected_department or "",
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

    event, _applications, _event_applications = _load_owned_event(organizer_id, event_id)
    slots_needed = event.get("slots_needed")
    if (
        not isinstance(slots_needed, int)
        or isinstance(slots_needed, bool)
        or slots_needed <= 0
    ):
        raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")

    with edit_documents(APPLICATIONS_FILE) as documents:
        applications = documents[APPLICATIONS_FILE]
        if not isinstance(applications, dict):
            raise ValueError("applications.json must contain an object keyed by event ID.")
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, dict):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        application = event_applications.get(volunteer_id)
        if not isinstance(application, dict):
            raise ValueError(f"Application for '{volunteer_id}' was not found.")
        current_status = _validated_status(application, volunteer_id)
        if application.get("attended") is True and new_status != "accepted":
            raise ValueError(
                "Attendance has been recorded; this application cannot be rejected."
            )

        counts = _application_counts(event_applications)
        if new_status == "accepted" and current_status != "accepted":
            if counts["accepted"] >= slots_needed:
                raise ValueError(f"Event '{event_id}' has no volunteer slots remaining.")

        application["status"] = new_status
        application["role"] = role
        event_applications[volunteer_id] = application
        applications[event_id] = event_applications

    if current_status != new_status:
        event_name = event.get("name", "the event")
        if new_status == "accepted":
            title = "Application accepted"
            message = f"Your application for {event_name} was accepted. Your assigned role is {role}."
            kind = "application_accepted"
        else:
            title = "Application rejected"
            message = f"Your application for {event_name} was not accepted."
            kind = "application_rejected"
        add_notifications(
            [volunteer_id],
            kind=kind,
            title=title,
            message=message,
            event_id=event_id,
        )


def _normalize_bulk_acceptance(
    selected_volunteer_ids: list[str] | tuple[str, ...],
    role_assignments: Mapping[str, Any] | None,
) -> list[tuple[str, str | None]]:
    if not isinstance(selected_volunteer_ids, (list, tuple)):
        raise ValueError("Select at least one pending applicant.")
    if role_assignments is None:
        role_assignments = {}
    if not isinstance(role_assignments, Mapping):
        raise ValueError("Assigned roles must be text.")
    if not selected_volunteer_ids:
        raise ValueError("Select at least one pending applicant.")

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_username in selected_volunteer_ids:
        username = _required_text(raw_username, "volunteer_id")
        if username in seen:
            raise ValueError("An applicant can only be selected once.")
        seen.add(username)
        raw_role = role_assignments.get(username, "")
        if raw_role is None:
            role = None
        elif not isinstance(raw_role, str):
            raise ValueError("Each assigned role must be text.")
        else:
            role = raw_role.strip() or None
        if role is not None and len(role) > 80:
            raise ValueError("Each assigned role must be 80 characters or fewer.")
        normalized.append((username, role))
    return normalized


def preview_bulk_acceptance(
    organizer_id: str,
    event_id: str,
    selected_volunteer_ids: list[str] | tuple[str, ...],
    role_assignments: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a batch and return its preview without changing any applications."""
    event, _applications, event_applications = _load_owned_event(organizer_id, event_id)
    selected = _normalize_bulk_acceptance(selected_volunteer_ids, role_assignments)
    counts = _application_counts(event_applications)
    slots_needed = event.get("slots_needed")
    if (
        not isinstance(slots_needed, int)
        or isinstance(slots_needed, bool)
        or slots_needed <= 0
    ):
        raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")
    slots_remaining = max(0, slots_needed - counts["accepted"])
    if len(selected) > slots_remaining:
        raise ValueError(
            f"Only {slots_remaining} volunteer slot(s) remain; reduce your selection."
        )

    applicants_by_username = {
        applicant["username"]: applicant
        for applicant in get_event_applicants(organizer_id, event_id)["applicants"]
    }
    preview = []
    for username, role in selected:
        application = event_applications.get(username)
        if not isinstance(application, Mapping):
            raise ValueError(f"Application for '{username}' was not found.")
        if _validated_status(application, username) != "pending":
            raise ValueError("Only pending applicants can be batch accepted.")
        applicant = applicants_by_username.get(username)
        if applicant is None:
            raise ValueError(f"Application for '{username}' was not found.")
        preview.append({**applicant, "assigned_role": role})

    return {
        "event": event,
        "event_id": event_id,
        "selected_applicants": preview,
        "slots_remaining": slots_remaining,
    }


def bulk_accept_applicants(
    organizer_id: str,
    event_id: str,
    selected_volunteer_ids: list[str] | tuple[str, ...],
    role_assignments: Mapping[str, Any],
) -> list[str]:
    """Accept a selected pending group atomically, enforcing event capacity."""
    selected = _normalize_bulk_acceptance(selected_volunteer_ids, role_assignments)
    event, _applications, _event_applications = _load_owned_event(organizer_id, event_id)
    slots_needed = event.get("slots_needed")
    if (
        not isinstance(slots_needed, int)
        or isinstance(slots_needed, bool)
        or slots_needed <= 0
    ):
        raise ValueError(f"Event '{event_id}' must have a positive slots_needed value.")

    with edit_documents(APPLICATIONS_FILE) as documents:
        applications = documents[APPLICATIONS_FILE]
        if not isinstance(applications, dict):
            raise ValueError("applications.json must contain an object keyed by event ID.")
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, dict):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        counts = _application_counts(event_applications)
        slots_remaining = max(0, slots_needed - counts["accepted"])
        if len(selected) > slots_remaining:
            raise ValueError(
                f"Only {slots_remaining} volunteer slot(s) remain; reduce your selection."
            )

        selected_records = []
        for username, role in selected:
            application = event_applications.get(username)
            if not isinstance(application, dict):
                raise ValueError(f"Application for '{username}' was not found.")
            if _validated_status(application, username) != "pending":
                raise ValueError("Only pending applicants can be batch accepted.")
            if application.get("attended") is True:
                raise ValueError(
                    "An applicant with recorded attendance cannot be batch accepted."
                )
            selected_records.append((username, role, application))

        for username, role, application in selected_records:
            application["status"] = "accepted"
            application["role"] = role
            event_applications[username] = application
        applications[event_id] = event_applications

    event_name = event.get("name", "the event")
    add_individual_notifications(
        [
            (
                username,
                (
                    f"Your application for {event_name} was accepted. "
                    f"Your assigned role is {role}."
                    if role
                    else f"Your application for {event_name} was accepted."
                ),
            )
            for username, role in selected
        ],
        kind="application_accepted",
        title="Application accepted",
        event_id=event_id,
    )
    return [username for username, _role in selected]


def mark_attendance(
    organizer_id: str,
    event_id: str,
    volunteer_id: str,
    today: date | None = None,
) -> bool:
    """Mark an accepted volunteer present on the event's calendar day."""
    volunteer_id = _required_text(volunteer_id, "volunteer_id")
    event, _applications, _event_applications = _load_owned_event(organizer_id, event_id)
    event_date = event_calendar_date(event)
    if event_date is None:
        raise ValueError("Add an event date before recording attendance.")
    current_date = today or date.today()
    if not isinstance(current_date, date) or isinstance(current_date, datetime):
        raise ValueError("'today' must be a calendar date.")
    if event_date != current_date:
        raise ValueError(f"Attendance can only be recorded on {event_date.isoformat()}.")

    with edit_documents(APPLICATIONS_FILE) as documents:
        applications = documents[APPLICATIONS_FILE]
        if not isinstance(applications, dict):
            raise ValueError("applications.json must contain an object keyed by event ID.")
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, dict):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        application = event_applications.get(volunteer_id)
        if not isinstance(application, dict):
            raise ValueError(f"Application for '{volunteer_id}' was not found.")
        if _validated_status(application, volunteer_id) != "accepted":
            raise ValueError("Only accepted volunteers can have attendance recorded.")
        if application.get("attended") is True:
            return False
        application["attended"] = True
        application["attended_on"] = current_date.isoformat()
        event_applications[volunteer_id] = application
        applications[event_id] = event_applications
    return True
