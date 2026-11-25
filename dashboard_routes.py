"""Organizer dashboard and volunteer review routes."""

from functools import wraps

from flask import Blueprint, abort, flash, g, make_response, redirect, render_template, request, send_file, url_for

from auth_routes import _login_required, _require_csrf
from analytics_service import (
    get_campus_impact,
    get_event_contributions,
    render_attendance_chart,
    render_campus_department_chart,
    render_campus_skill_chart,
    render_contribution_chart,
)
from dashboard_service import (
    dashboard_cards,
    get_event_applicants,
    bulk_accept_applicants as bulk_accept_applicants_service,
    create_organizer_event,
    get_organizer_event,
    hide_organizer_event,
    mark_attendance as mark_attendance_service,
    organizer_event_form_values,
    organizer_event_picker_options,
    preview_bulk_acceptance,
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


def _applicants_modal(
    organizer_id: str, event_id: str, error=None, notice=None, department=None
):
    if department is None:
        department = request.values.get("department", "")
    try:
        context = get_event_applicants(organizer_id, event_id, department)
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


@dashboard_bp.get("/impact")
@_login_required
@_organizer_required
def campus_impact():
    """Show privacy-safe campus-wide totals to signed-in organizers."""
    range_key = request.args.get("range", "365d")
    try:
        summary = get_campus_impact(range_key)
    except ValueError as error:
        abort(400, description=str(error))
    return render_template("campus_impact.html", summary=summary)


def _campus_impact_png(renderer, filename):
    range_key = request.args.get("range", "365d")
    try:
        summary = get_campus_impact(range_key)
    except ValueError as error:
        abort(400, description=str(error))
    png = renderer(summary)
    if png is None:
        abort(404)
    return send_file(BytesIO(png), mimetype="image/png", download_name=filename)


@dashboard_bp.get("/impact/departments.png")
@_login_required
@_organizer_required
def campus_impact_departments_chart():
    """Serve an aggregate department chart when every group meets privacy rules."""
    return _campus_impact_png(render_campus_department_chart, "campus-departments.png")


@dashboard_bp.get("/impact/skills.png")
@_login_required
@_organizer_required
def campus_impact_skills_chart():
    """Serve an aggregate skills chart when every group meets privacy rules."""
    return _campus_impact_png(render_campus_skill_chart, "campus-skills.png")


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


def _submitted_values(values, field):
    """Read a multi-value field from either Flask's form or a plain test mapping."""
    getlist = getattr(values, "getlist", None)
    raw = getlist(field) if callable(getlist) else values.get(field, [])
    if isinstance(raw, str):
        return [raw] if raw else []
    return [value for value in raw if isinstance(value, str)] if isinstance(raw, (list, tuple)) else []


def _raw_submitted_values(values, field):
    """Keep blank numeric inputs so validation or rerendering can handle them."""
    getlist = getattr(values, "getlist", None)
    raw = getlist(field) if callable(getlist) else values.get(field, [])
    if isinstance(raw, str):
        return [raw]
    return [value if isinstance(value, str) else "" for value in raw] if isinstance(raw, (list, tuple)) else []


def _skill_rating_rows(selected_skills, values, existing_skills=None):
    """Preserve current ratings or default new selections to minimum 1."""
    existing_skills = existing_skills if isinstance(existing_skills, dict) else {}
    submitted_names = _raw_submitted_values(values, "required_skill_name")
    submitted_ratings = _raw_submitted_values(values, "required_skill_rating")
    submitted = {
        name.casefold(): rating
        for name, rating in zip(submitted_names, submitted_ratings)
    }
    existing = {
        name.casefold(): rating
        for name, rating in existing_skills.items()
        if isinstance(name, str)
    }
    form_rows = values.get("skill_rating_rows", []) if hasattr(values, "get") else []
    saved_rows = {
        row["name"].casefold(): row.get("rating", 1)
        for row in form_rows
        if isinstance(form_rows, list)
        and isinstance(row, dict)
        and isinstance(row.get("name"), str)
    }
    return [
        {
            "name": skill,
            "rating": submitted.get(
                skill.casefold(),
                saved_rows.get(skill.casefold(), existing.get(skill.casefold(), 1)),
            ),
        }
        for skill in selected_skills
    ]


def _render_event_form(mode, values, event_id=None, error=None, status=200, event=None):
    selected_departments = _submitted_values(values, "departments")
    selected_skills = _submitted_values(values, "required_skill_names")
    existing_skills = (
        list(event.get("required_skills", {}))
        if isinstance(event, dict) and isinstance(event.get("required_skills"), dict)
        else []
    )
    existing_skill_ratings = (
        event.get("required_skills", {})
        if isinstance(event, dict) and isinstance(event.get("required_skills"), dict)
        else {}
    )
    skill_rating_rows = _skill_rating_rows(
        selected_skills, values, existing_skill_ratings
    )
    return render_template(
        "organizer_event_form.html",
        partial=False,
        mode=mode,
        event_id=event_id,
        form_values=values,
        error=error,
        selected_departments=selected_departments,
        selected_skills=selected_skills,
        skill_rating_rows=skill_rating_rows,
        department_option_groups=organizer_event_picker_options(
            "departments", selected=selected_departments
        ),
        skill_option_groups=organizer_event_picker_options(
            "skills", selected=selected_skills, existing_skills=existing_skills
        ),
    ), status


@dashboard_bp.get("/events/new")
@_login_required
@_organizer_required
def new_event():
    """Show the event creation form to the signed-in organizer."""
    return _render_event_form("create", organizer_event_form_values())


@dashboard_bp.get("/events/options")
@_login_required
@_organizer_required
def event_options():
    """Return searchable department or skill options for the event form."""
    option_group = request.args.get("option_group", "")
    if option_group not in {"departments", "skills"}:
        abort(400)
    search_field = "department_search" if option_group == "departments" else "skill_search"
    selection_field = "departments" if option_group == "departments" else "required_skill_names"
    event_id = request.args.get("event_id", "").strip()
    existing_skills = []
    if event_id and option_group == "skills":
        try:
            existing_event = get_organizer_event(g.current_user["username"], event_id)
        except KeyError:
            abort(404)
        required_skills = existing_event.get("required_skills", {})
        if isinstance(required_skills, dict):
            existing_skills = list(required_skills)
    option_groups = organizer_event_picker_options(
        option_group,
        request.args.get(search_field, ""),
        selected=_submitted_values(request.args, selection_field),
        existing_skills=existing_skills,
    )
    template = (
        "organizer_event_department_options.html"
        if option_group == "departments"
        else "organizer_event_skill_options.html"
    )
    selected = _submitted_values(request.args, selection_field)
    if option_group == "departments":
        return render_template(
            template,
            department_option_groups=option_groups,
            selected_departments=selected,
        )
    return render_template(
        template,
        skill_option_groups=option_groups,
        selected_skills=selected,
    )


@dashboard_bp.post("/events/skill-ratings")
@_login_required
@_organizer_required
@_require_csrf
def event_skill_ratings():
    """Render one minimum-rating control for each currently selected skill."""
    selected_skills = _submitted_values(request.form, "required_skill_names")
    event_id = request.form.get("event_id", "").strip()
    existing_skills = {}
    if event_id:
        try:
            event = get_organizer_event(g.current_user["username"], event_id)
        except KeyError:
            abort(404)
        existing_skills = event.get("required_skills", {})
        if not isinstance(existing_skills, dict):
            existing_skills = {}
    return render_template(
        "organizer_event_skill_ratings.html",
        skill_rating_rows=_skill_rating_rows(
            selected_skills, request.form, existing_skills
        ),
    )


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
    return _render_event_form(
        "edit", organizer_event_form_values(event), event_id, event=event
    )


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
        event = get_organizer_event(organizer_id, event_id)
        return _render_event_form(
            "edit", values, event_id, error=str(error), status=400, event=event
        )
    flash("Event changes saved.", "success")
    return redirect(url_for("dashboard.home"), code=303)


@dashboard_bp.get("/events/<event_id>/delete")
@_login_required
@_organizer_required
def confirm_delete_event(event_id):
    """Show an explicit confirmation before hiding an organizer-owned event."""
    try:
        event = get_organizer_event(g.current_user["username"], event_id)
    except KeyError:
        abort(404)
    if event.get("source") == "prismlabs":
        abort(404)
    return render_template("delete_event_confirm.html", event_id=event_id, event=event)


@dashboard_bp.post("/events/<event_id>/delete")
@_login_required
@_organizer_required
@_require_csrf
def delete_event(event_id):
    """Hide an owned event after the organizer confirms the action."""
    try:
        hide_organizer_event(g.current_user["username"], event_id)
    except KeyError:
        abort(404)
    except ValueError as error:
        abort(400, description=str(error))
    flash("Event hidden. Applications and certificates were kept.", "success")
    return redirect(url_for("dashboard.home"), code=303)


@dashboard_bp.get("/events/<event_id>/applicants")
@_login_required
@_organizer_required
def applicants(event_id):
    """Load one organizer-owned event's applicants into the shared modal."""
    try:
        event = get_organizer_event(g.current_user["username"], event_id)
    except KeyError:
        abort(404)
    if event.get("source") == "prismlabs":
        roster_url = url_for("prismlabs.admin_volunteers", event_id=event_id)
        if _is_htmx_request():
            response = make_response("", 200)
            response.headers["HX-Redirect"] = roster_url
            return response
        return redirect(roster_url)
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


@dashboard_bp.post("/events/<event_id>/applicants/bulk-accept")
@_login_required
@_organizer_required
@_require_csrf
def bulk_accept(event_id):
    """Preview or confirm acceptance for a selected group of pending applicants."""
    organizer_id = g.current_user["username"]
    selected_ids = request.form.getlist("volunteer_id")
    department = request.form.get("department", "")
    try:
        if request.form.get("select_all_volunteers") == "1":
            applicants = get_event_applicants(
                organizer_id, event_id, department
            )["applicants"]
            selected_ids = [
                applicant["username"]
                for applicant in applicants
                if applicant["status"] == "pending"
            ]
        roles = {
            username: request.form.get(f"role_{username}", "")
            for username in selected_ids
        }
        if request.form.get("confirm") == "yes":
            accepted = bulk_accept_applicants_service(
                organizer_id, event_id, selected_ids, roles
            )
            return _applicants_modal(
                organizer_id,
                event_id,
                notice=(
                    f"{len(accepted)} applicant(s) accepted. "
                    "Each volunteer was notified."
                ),
            )
        preview = preview_bulk_acceptance(
            organizer_id, event_id, selected_ids, roles
        )
    except KeyError:
        abort(404)
    except ValueError as error:
        return _applicants_modal(organizer_id, event_id, error=str(error))

    return render_template(
        "dashboard.html",
        partial="bulk-confirm",
        department=department,
        **preview,
    )


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
