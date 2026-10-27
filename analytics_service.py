"""Organizer contribution summaries and in-memory chart rendering."""

from collections.abc import Mapping
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
