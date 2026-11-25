"""Organizer contribution summaries and in-memory chart rendering."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from io import BytesIO
import math
import textwrap
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib import pyplot as plt

from data_store import load


EVENTS_FILE = "events.json"
APPLICATIONS_FILE = "applications.json"
USERS_FILE = "users.json"
CAMPUS_PRIVACY_MIN_VOLUNTEERS = 5
CAMPUS_IMPACT_RANGES = {
    "30d": {"days": 30, "label": "Last 30 days"},
    "90d": {"days": 90, "label": "Last 90 days"},
    "365d": {"days": 365, "label": "Last 12 months"},
    "all": {"days": None, "label": "All time"},
}


def _event_completion_date(event: Mapping[str, Any]) -> tuple[date | None, bool]:
    """Use completion time first, then the scheduled date as a marked fallback."""
    completed_at = event.get("completed_at")
    if isinstance(completed_at, str) and completed_at.strip():
        try:
            return (
                datetime.fromisoformat(
                    completed_at.strip().replace("Z", "+00:00")
                ).date(),
                False,
            )
        except ValueError:
            pass

    for field in ("ends_at", "date", "event_date", "starts_at", "start_at"):
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            if len(value.strip()) == 10:
                return date.fromisoformat(value.strip()), True
            return (
                datetime.fromisoformat(
                    value.strip().replace("Z", "+00:00")
                ).date(),
                True,
            )
        except ValueError:
            continue
    return None, True


def _normalized_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _privacy_safe_breakdown(
    buckets: Mapping[str, set[str]], data_complete: bool
) -> tuple[bool, list[dict[str, Any]]]:
    if not data_complete or not buckets:
        return False, []
    if any(
        len(volunteers) < CAMPUS_PRIVACY_MIN_VOLUNTEERS
        for volunteers in buckets.values()
    ):
        return False, []
    return True, [
        {"label": label, "volunteers": len(volunteers)}
        for label, volunteers in sorted(
            buckets.items(),
            key=lambda item: (-len(item[1]), item[0].casefold()),
        )
    ]


def get_campus_impact(
    range_key: str = "365d",
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Aggregate completed, organizer-recorded contributions without personal data."""
    if not isinstance(range_key, str) or range_key not in CAMPUS_IMPACT_RANGES:
        raise ValueError("Choose a supported campus impact time range.")
    current_day = today or date.today()
    if not isinstance(current_day, date) or isinstance(current_day, datetime):
        raise ValueError("'today' must be a calendar date.")

    events = load(EVENTS_FILE)
    applications = load(APPLICATIONS_FILE)
    users = load(USERS_FILE)
    if not isinstance(events, dict):
        raise ValueError("events.json must contain an object keyed by event ID.")
    if not isinstance(applications, dict):
        raise ValueError("applications.json must contain an object keyed by event ID.")
    if not isinstance(users, dict):
        raise ValueError("users.json must contain an object keyed by username.")

    selected_range = CAMPUS_IMPACT_RANGES[range_key]
    days = selected_range["days"]
    period_start = current_day - timedelta(days=days - 1) if days else None
    completed_event_count = 0
    excluded_undated_events = False
    used_schedule_date = False
    total_hours = 0.0
    hours_complete = True
    attendance_complete = True
    departments_complete = True
    skills_complete = True
    volunteer_ids: set[str] = set()
    department_volunteers: dict[str, set[str]] = {}
    skill_volunteers: dict[str, set[str]] = {}
    department_names: dict[str, str] = {}
    skill_names: dict[str, str] = {}
    event_participation_count = 0

    for event_id, event in events.items():
        if not isinstance(event_id, str) or not isinstance(event, Mapping):
            raise ValueError("Each event must be an object keyed by a string ID.")
        if event.get("status") != "completed":
            continue

        completion_date, used_fallback = _event_completion_date(event)
        if completion_date is None:
            if period_start is not None:
                excluded_undated_events = True
                continue
            excluded_undated_events = True
        elif period_start is not None and not (
            period_start <= completion_date <= current_day
        ):
            continue
        used_schedule_date = used_schedule_date or used_fallback
        completed_event_count += 1

        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, Mapping):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        required_skills = event.get("required_skills")
        event_skill_names: list[str] = []
        if isinstance(required_skills, Mapping):
            seen_event_skills: set[str] = set()
            for raw_skill in required_skills:
                skill = _normalized_category(raw_skill)
                if skill is None:
                    skills_complete = False
                    continue
                normalized_skill = skill.casefold()
                if normalized_skill not in seen_event_skills:
                    seen_event_skills.add(normalized_skill)
                    event_skill_names.append(skill)
        else:
            skills_complete = False
        if not event_skill_names:
            skills_complete = False

        for username, application in event_applications.items():
            if not isinstance(username, str) or not isinstance(application, Mapping):
                raise ValueError("Each application must be an object keyed by username.")
            status = application.get("status")
            if not isinstance(status, str) or status.casefold() not in {
                "pending", "accepted", "rejected",
            }:
                raise ValueError(f"Application for '{username}' has an invalid status.")
            if status.casefold() != "accepted":
                continue

            attended = application.get("attended")
            if not isinstance(attended, bool):
                attendance_complete = False
                attended = False
            elif "attended" not in application:
                attendance_complete = False

            raw_hours = application.get("hours")
            hours_recorded = (
                isinstance(raw_hours, (int, float))
                and not isinstance(raw_hours, bool)
                and math.isfinite(raw_hours)
                and raw_hours >= 0
            )
            hours = float(raw_hours) if hours_recorded else 0.0
            if attended is not True and hours <= 0:
                continue

            volunteer_ids.add(username)
            event_participation_count += 1
            if hours_recorded:
                total_hours += hours
            else:
                hours_complete = False

            user = users.get(username)
            department = None
            if isinstance(user, Mapping):
                department = _normalized_category(
                    user.get("dept", user.get("department"))
                )
            if department is None:
                departments_complete = False
            else:
                normalized_department = department.casefold()
                department_names.setdefault(normalized_department, department)
                department_volunteers.setdefault(normalized_department, set()).add(username)

            for skill in event_skill_names:
                normalized_skill = skill.casefold()
                skill_names.setdefault(normalized_skill, skill)
                skill_volunteers.setdefault(normalized_skill, set()).add(username)

    participant_count = len(volunteer_ids)
    has_completed_events = completed_event_count > 0
    has_participation_data = participant_count > 0
    privacy_suppressed = (
        has_participation_data
        and participant_count < CAMPUS_PRIVACY_MIN_VOLUNTEERS
    )
    date_notes = []
    if used_schedule_date:
        date_notes.append(
            "Some events use their scheduled date because a completion timestamp was not recorded."
        )
    if excluded_undated_events:
        date_notes.append(
            (
                "Some completed events have no usable date and are excluded from this time range."
                if period_start is not None
                else "Some completed events have no usable date; they are included in all-time totals but cannot be assigned a date."
            )
        )
    result: dict[str, Any] = {
        "range_key": range_key,
        "range_label": selected_range["label"],
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": current_day.isoformat() if period_start else None,
        "has_completed_events": has_completed_events,
        "has_participation_data": has_participation_data,
        "privacy_suppressed": privacy_suppressed,
        "privacy_min_volunteers": CAMPUS_PRIVACY_MIN_VOLUNTEERS,
        "totals": None,
        "department_breakdown_available": False,
        "department_breakdown": [],
        "skill_breakdown_available": False,
        "skill_breakdown": [],
        "data_notes": date_notes,
    }
    if not has_participation_data:
        return result
    if privacy_suppressed:
        return result

    department_display_buckets = {
        department_names[normalized]: volunteers
        for normalized, volunteers in department_volunteers.items()
    }
    skill_display_buckets = {
        skill_names[normalized]: volunteers
        for normalized, volunteers in skill_volunteers.items()
    }
    department_breakdown_available, department_breakdown = _privacy_safe_breakdown(
        department_display_buckets, departments_complete
    )
    skill_breakdown_available, skill_breakdown = _privacy_safe_breakdown(
        skill_display_buckets, skills_complete
    )
    known_department_count = len(department_names)
    known_skill_count = len(skill_names)
    result.update({
        "totals": {
            "recorded_hours": round(total_hours, 2),
            "event_participations": event_participation_count,
            "distinct_volunteers": participant_count,
            "completed_events": completed_event_count,
            "departments_represented": (
                known_department_count if departments_complete else None
            ),
            "skills_practiced": known_skill_count if skills_complete else None,
        },
        "department_breakdown_available": department_breakdown_available,
        "department_breakdown": department_breakdown,
        "skill_breakdown_available": skill_breakdown_available,
        "skill_breakdown": skill_breakdown,
        "data_notes": date_notes + [
            note
            for condition, note in (
                (
                    not hours_complete,
                    "Some hours are missing or invalid; the total includes recorded values only.",
                ),
                (
                    not attendance_complete,
                    "Some attendance records are missing; positive recorded hours also count as participation.",
                ),
                (
                    not departments_complete,
                    "Some volunteer department details are missing, so the department breakdown is hidden.",
                ),
                (
                    not skills_complete,
                    "Some completed events have no recorded required skills, so the skills breakdown is hidden.",
                ),
                (
                    not department_breakdown_available and bool(department_volunteers)
                    and departments_complete,
                    "The department breakdown is hidden because at least one group has fewer than five volunteers.",
                ),
                (
                    not skill_breakdown_available and bool(skill_volunteers)
                    and skills_complete,
                    "The skills breakdown is hidden because at least one group has fewer than five volunteers.",
                ),
            )
            if condition
        ],
    })
    return result


def _render_aggregate_chart(
    items: list[Mapping[str, Any]],
    *,
    title: str,
    x_label: str,
) -> bytes | None:
    if not items:
        return None
    labels = [
        textwrap.fill(str(item["label"]), width=24, break_long_words=True)
        for item in items
    ]
    values = [int(item["volunteers"]) for item in items]
    row_heights = [0.48 * max(1, len(label.splitlines())) for label in labels]
    positions = []
    position = 0.0
    for row_height in row_heights:
        positions.append(position)
        position += row_height

    fig, axis = plt.subplots(
        figsize=(9, max(3.4, 1.8 + sum(row_heights))),
        constrained_layout=True,
    )
    fig.patch.set_facecolor("#fffefa")
    axis.set_facecolor("#fffefa")
    axis.barh(
        positions,
        values,
        height=[min(0.34, row_height * 0.7) for row_height in row_heights],
        color="#D4B483",
        edgecolor="#17243b",
        linewidth=0.5,
    )
    axis.set_yticks(positions, labels=labels, fontsize=8)
    axis.invert_yaxis()
    axis.set_xlabel(x_label)
    axis.set_title(title, loc="left", fontweight="bold", pad=14)
    axis.grid(axis="x", color="#d9ded7", linewidth=0.7, alpha=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.tick_params(axis="y", length=0, pad=8)
    return _png_bytes(fig)


def render_campus_department_chart(summary: Mapping[str, Any]) -> bytes | None:
    """Render a department breakdown only when its groups meet the privacy floor."""
    if summary.get("department_breakdown_available") is not True:
        return None
    return _render_aggregate_chart(
        summary.get("department_breakdown", []),
        title="Volunteers represented by department",
        x_label="Distinct volunteers",
    )


def render_campus_skill_chart(summary: Mapping[str, Any]) -> bytes | None:
    """Render practiced event skills only when every group meets the privacy floor."""
    if summary.get("skill_breakdown_available") is not True:
        return None
    return _render_aggregate_chart(
        summary.get("skill_breakdown", []),
        title="Skills practiced through completed events",
        x_label="Distinct volunteers",
    )


def get_event_contributions(event_id: str) -> dict[str, Any]:
    """Build contribution and attendance chart data from existing records."""
    events = load(EVENTS_FILE)
    applications = load(APPLICATIONS_FILE)
    users = load(USERS_FILE)
    if not isinstance(events, dict):
        raise ValueError("events.json must contain an object keyed by event ID.")
    if not isinstance(applications, dict):
        raise ValueError("applications.json must contain an object keyed by event ID.")
    if not isinstance(users, dict):
        raise ValueError("users.json must contain an object keyed by username.")

    event = events.get(event_id)
    if not isinstance(event, Mapping):
        raise KeyError(event_id)
    event_applications = applications.get(event_id, {})
    if not isinstance(event_applications, Mapping):
        raise ValueError(f"Applications for event '{event_id}' must be an object.")

    accepted: list[dict[str, Any]] = []
    counts = {"attended": 0, "not_attended": 0, "pending": 0}
    accepted_have_hours = False
    for username, application in event_applications.items():
        if not isinstance(username, str) or not isinstance(application, Mapping):
            raise ValueError("Each application must be an object keyed by username.")
        status = application.get("status")
        if not isinstance(status, str) or status.casefold() not in {
            "pending", "accepted", "rejected",
        }:
            raise ValueError(f"Application for '{username}' has an invalid status.")

        status = status.casefold()
        attended = application.get("attended", False)
        if not isinstance(attended, bool):
            raise ValueError(f"Application for '{username}' has invalid attendance data.")
        user = users.get(username, {})
        if not isinstance(user, Mapping):
            user = {}
        name = user.get("name")
        if not isinstance(name, str) or not name.strip():
            name = username

        if status == "pending":
            counts["pending"] += 1
        elif status == "accepted":
            accepted_have_hours = accepted_have_hours or "hours" in application
            hours = application.get("hours")
            if (
                not isinstance(hours, (int, float))
                or isinstance(hours, bool)
                or not math.isfinite(hours)
                or hours < 0
            ):
                hours = 0
            accepted.append({
                "username": username,
                "name": name.strip(),
                "hours": float(hours),
                "attended": attended,
            })
            if attended:
                counts["attended"] += 1
            elif event.get("status") == "completed":
                counts["not_attended"] += 1

    contribution_metric = "Volunteer hours"
    for volunteer in accepted:
        if not accepted_have_hours:
            volunteer["contribution"] = (
                1 if event.get("status") == "completed" and volunteer["attended"] else 0
            )
            contribution_metric = "Completed attendance (volunteers)"
        else:
            volunteer["contribution"] = volunteer["hours"]

    accepted.sort(
        key=lambda volunteer: (
            -volunteer["contribution"],
            volunteer["name"].casefold(),
            volunteer["username"].casefold(),
        )
    )
    has_attendance_records = (
        counts["attended"] > 0 or counts["not_attended"] > 0
    )
    return {
        "event": dict(event),
        "applicants_count": len(event_applications),
        "accepted_count": len(accepted),
        "volunteers": accepted,
        "contribution_metric": contribution_metric,
        "attendance_counts": counts,
        "has_attendance_records": has_attendance_records,
        "has_attendance_data": any(counts.values()),
    }


def _png_bytes(fig) -> bytes:
    """Serialize a Matplotlib figure to PNG bytes and always close it."""
    output = BytesIO()
    try:
        fig.savefig(output, format="png", dpi=110, bbox_inches="tight")
        return output.getvalue()
    finally:
        plt.close(fig)


def render_contribution_chart(summary: Mapping[str, Any]) -> bytes | None:
    """Render a descending horizontal bar chart, or None when there are no accepted applicants."""
    volunteers = summary.get("volunteers", [])
    if not isinstance(volunteers, list) or not volunteers:
        return None

    name_counts: dict[str, int] = {}
    for volunteer in volunteers:
        name_counts[volunteer["name"].casefold()] = (
            name_counts.get(volunteer["name"].casefold(), 0) + 1
        )
    labels = []
    for volunteer in volunteers:
        name = volunteer["name"]
        if name_counts[name.casefold()] > 1:
            name = f"{name} ({volunteer['username']})"
        labels.append(textwrap.fill(
            name,
            width=25,
            break_long_words=True,
            break_on_hyphens=True,
        ))

    values = [volunteer["contribution"] for volunteer in volunteers]
    row_heights = [
        0.42 * max(1, len(label.splitlines()))
        for label in labels
    ]
    positions = []
    position = 0.0
    for row_height in row_heights:
        positions.append(position)
        position += row_height
    figure_height = max(3.4, 1.8 + sum(row_heights))
    fig, axis = plt.subplots(figsize=(10, figure_height), constrained_layout=True)
    fig.patch.set_facecolor("#fffefa")
    axis.set_facecolor("#fffefa")
    axis.barh(
        positions,
        values,
        height=[min(0.32, row_height * 0.7) for row_height in row_heights],
        color="#18715d",
        edgecolor="#10513f",
        linewidth=0.5,
    )
    axis.set_yticks(positions, labels=labels, fontsize=8)
    axis.invert_yaxis()
    axis.set_xlabel(str(summary.get("contribution_metric", "Contribution")))
    axis.set_title("Volunteer contributions", loc="left", fontweight="bold", pad=14)
    axis.grid(axis="x", color="#d9ded7", linewidth=0.7, alpha=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.tick_params(axis="y", length=0, pad=8)
    return _png_bytes(fig)


def render_attendance_chart(summary: Mapping[str, Any]) -> bytes | None:
    """Render attendance and pending-application counts, or None when none are known."""
    counts = summary.get("attendance_counts", {})
    if not isinstance(counts, Mapping):
        return None
    values = [
        counts.get("attended", 0),
        counts.get("not_attended", 0),
        counts.get("pending", 0),
    ]
    if not any(values):
        return None

    labels = ["Attended", "Not attended", "Pending applicants"]
    colors = ["#18715d", "#d85d47", "#f0c96d"]
    fig, axis = plt.subplots(figsize=(6.5, 5), constrained_layout=True)
    fig.patch.set_facecolor("#fffefa")
    axis.set_facecolor("#fffefa")
    axis.pie(
        values,
        labels=labels,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "#fffefa", "linewidth": 2},
        autopct=lambda percent: f"{percent:.0f}%" if percent > 0 else "",
        pctdistance=0.78,
        textprops={"fontsize": 9},
    )
    axis.set_title("Attendance and application status", loc="left", fontweight="bold", pad=14)
    axis.axis("equal")
    return _png_bytes(fig)
