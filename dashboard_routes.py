"""Organizer dashboard and volunteer review routes."""

from functools import wraps

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

from auth_routes import _login_required, _require_csrf
from dashboard_service import (
    get_event_applicants,
    get_organizer_events,
    mark_attendance as mark_attendance_service,
    update_application_status,
)


dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _organizer_required(view):
    @wraps(view)
    def checked_view(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if user is None or user.get("role") != "Organizer":
            abort(403)
        return view(*args, **kwargs)

    return checked_view


def _is_htmx_request() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _applicants_modal(organizer_id: str, event_id: str, error=None, notice=None):
    try:
        context = get_event_applicants(organizer_id, event_id)
    except KeyError:
        abort(404)
    return render_template(
        "dashboard.html",
        partial="applicants",
        error=error,
        notice=notice,
        **context,
    )


@dashboard_bp.get("")
@_login_required
@_organizer_required
def home():
    """List the signed-in organizer's events and application counts."""
    try:
        events = get_organizer_events(g.current_user["username"])
    except ValueError as error:
        return render_template("dashboard.html", partial=False, events=[], error=str(error))
    return render_template("dashboard.html", partial=False, events=events, error=None)


@dashboard_bp.get("/events/<event_id>/applicants")
@_login_required
@_organizer_required
def applicants(event_id):
    """Load one organizer-owned event's applicants into the shared modal."""
    return _applicants_modal(g.current_user["username"], event_id)


@dashboard_bp.post("/events/<event_id>/applicants/<volunteer_id>/status")
@_login_required
@_organizer_required
@_require_csrf
def review_application(event_id, volunteer_id):
    """Update an applicant's decision and assigned volunteer role."""
    organizer_id = g.current_user["username"]
    try:
        update_application_status(
            organizer_id,
            event_id,
            volunteer_id,
            request.form.get("status", ""),
            request.form.get("role"),
        )
    except KeyError:
        abort(404)
    except ValueError as error:
        return _applicants_modal(organizer_id, event_id, error=str(error))

    if not _is_htmx_request():
        return redirect(url_for("dashboard.home"), code=303)
    return _applicants_modal(organizer_id, event_id, notice="Application decision saved.")


@dashboard_bp.post("/events/<event_id>/applicants/<volunteer_id>/attendance")
@_login_required
@_organizer_required
@_require_csrf
def mark_volunteer_attendance(event_id, volunteer_id):
    """Record attendance only when the event is taking place."""
    organizer_id = g.current_user["username"]
    try:
        marked = mark_attendance_service(organizer_id, event_id, volunteer_id)
    except KeyError:
        abort(404)
    except ValueError as error:
        return _applicants_modal(organizer_id, event_id, error=str(error))

    if not _is_htmx_request():
        return redirect(url_for("dashboard.home"), code=303)
    notice = "Attendance marked." if marked else "Attendance was already recorded."
    return _applicants_modal(organizer_id, event_id, notice=notice)
