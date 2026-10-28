"""Organizer dashboard and volunteer review routes."""

from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from auth_routes import _login_required, _require_csrf
from analytics_service import (
    get_event_contributions,
    render_attendance_chart,
    render_contribution_chart,
)
from dashboard_service import (
    dashboard_cards,
    get_event_applicants,
    create_organizer_event,
    get_organizer_event,
    mark_attendance as mark_attendance_service,
    organizer_event_form_values,
    update_application_status,
    update_organizer_event,
)
from io import BytesIO


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


def _owned_contribution_summary(event_id: str):
    organizer_id = g.current_user["username"]
    try:
        event = get_organizer_event(organizer_id, event_id)
        summary = get_event_contributions(event_id)
    except KeyError:
        abort(404)
    if summary["event"].get("organizer") != organizer_id:
        abort(404)
    return event, summary


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
        cards = dashboard_cards(g.current_user["username"])
    except ValueError as error:
        return render_template(
            "dashboard.html", partial=False, dashboard_cards=[], error=str(error)
        )
    return render_template(
        "dashboard.html", partial=False, dashboard_cards=cards, error=None
    )


@dashboard_bp.get("/events/<event_id>/contributions")
@_login_required
@_organizer_required
def contributions(event_id):
    """Show contribution analytics only for an event owned by the organizer."""
    event, summary = _owned_contribution_summary(event_id)
    return render_template(
        "contributions.html",
        event_id=event_id,
        event=event,
        summary=summary,
    )


@dashboard_bp.get("/events/<event_id>/contributions/volunteers.png")
@_login_required
@_organizer_required
def contribution_chart(event_id):
    """Serve an owned event's contribution chart as an in-memory PNG."""
    _event, summary = _owned_contribution_summary(event_id)
    png = render_contribution_chart(summary)
    if png is None:
        abort(404)
    return send_file(BytesIO(png), mimetype="image/png", download_name="contributions.png")


@dashboard_bp.get("/events/<event_id>/contributions/attendance.png")
@_login_required
@_organizer_required
def attendance_chart(event_id):
    """Serve an owned event's attendance chart as an in-memory PNG."""
    _event, summary = _owned_contribution_summary(event_id)
    png = render_attendance_chart(summary)
    if png is None:
        abort(404)
    return send_file(BytesIO(png), mimetype="image/png", download_name="attendance.png")


def _render_event_form(mode, values, event_id=None, error=None, status=200):
    return render_template(
        "organizer_event_form.html",
        partial=False,
        mode=mode,
        event_id=event_id,
        form_values=values,
        error=error,
    ), status


@dashboard_bp.get("/events/new")
@_login_required
@_organizer_required
def new_event():
    """Show the event creation form to the signed-in organizer."""
    return _render_event_form("create", organizer_event_form_values())


@dashboard_bp.post("/events/new")
@_login_required
@_organizer_required
@_require_csrf
def create_event():
    """Validate and save an event owned by the signed-in organizer."""
    values = request.form
    try:
        create_organizer_event(g.current_user["username"], values)
    except ValueError as error:
        return _render_event_form("create", values, error=str(error), status=400)
    flash("Event created successfully.", "success")
    return redirect(url_for("dashboard.home"), code=303)


@dashboard_bp.get("/events/<event_id>/edit")
@_login_required
@_organizer_required
def edit_event(event_id):
    """Show the edit form only for an event owned by this organizer."""
    try:
        event = get_organizer_event(g.current_user["username"], event_id)
    except KeyError:
        abort(404)
    return _render_event_form("edit", organizer_event_form_values(event), event_id)


@dashboard_bp.post("/events/<event_id>/edit")
@_login_required
@_organizer_required
@_require_csrf
def save_event_edits(event_id):
    """Validate and save edits without changing the event's owner or ID."""
    values = request.form
    organizer_id = g.current_user["username"]
    try:
        update_organizer_event(organizer_id, event_id, values)
    except KeyError:
        abort(404)
    except ValueError as error:
        # Preserve submitted values so organizers can correct only the invalid field.
        return _render_event_form("edit", values, event_id, error=str(error), status=400)
    flash("Event changes saved.", "success")
    return redirect(url_for("dashboard.home"), code=303)


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
