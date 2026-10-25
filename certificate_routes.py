"""Certificate pages, organizer event completion, and public verification."""

from functools import wraps

from flask import Blueprint, abort, g, render_template, request, send_file, url_for

from auth_routes import _login_required, _require_csrf
from certificate_service import (
    complete_event as complete_event_service,
    get_certificate_for_user,
    get_certificate_pdf_path,
    get_organizer_events,
    get_user_certificates,
    verify_certificate,
)


certificates_bp = Blueprint("certificates", __name__)


def _role_required(role: str):
    def decorate(view):
        @wraps(view)
        def checked_view(*args, **kwargs):
            user = getattr(g, "current_user", None)
            if user is None or user.get("role") != role:
                abort(403)
            return view(*args, **kwargs)

        return checked_view

    return decorate


@certificates_bp.get("/certificates")
@_login_required
def home():
    """Show event completion controls to organizers and personal certificates to volunteers."""
    current_user = g.current_user
    if current_user.get("role") == "Organizer":
        try:
            events = get_organizer_events(current_user["username"])
        except ValueError as error:
            return render_template(
                "certificates.html", role="Organizer", events=[], certificates=[],
                error=str(error), notice=None,
            )
        return render_template(
            "certificates.html", role="Organizer", events=events, certificates=[],
            error=None, notice=None,
        )
    if current_user.get("role") == "Student Volunteer":
        try:
            certificates = get_user_certificates(current_user["username"])
        except ValueError as error:
            return render_template(
                "certificates.html", role="Student Volunteer", events=[], certificates=[],
                error=str(error), notice=None,
            )
        return render_template(
            "certificates.html", role="Student Volunteer", events=[],
            certificates=certificates, error=None, notice=None,
        )
    abort(403)


@certificates_bp.post("/certificates/events/<event_id>/complete")
@_login_required
@_role_required("Organizer")
@_require_csrf
def complete_event(event_id):
    """Accept final contribution details only for an event owned by this organizer."""
    try:
        events = get_organizer_events(g.current_user["username"])
    except ValueError as error:
        abort(400, description=str(error))
    if not any(item["event_id"] == event_id for item in events):
        abort(404)

    contribution_updates = {}
    for key, hours_value in request.form.items():
        if key.startswith("hours_"):
            username = key.removeprefix("hours_")
            contribution_updates[username] = {
                "hours": hours_value,
                "notes": request.form.get(f"notes_{username}", ""),
            }
    success, message = complete_event_service(event_id, contribution_updates)
    try:
        events = get_organizer_events(g.current_user["username"])
    except ValueError:
        events = []
    return render_template(
        "certificates.html",
        role="Organizer",
        events=events,
        certificates=[],
        error=None if success else message,
        notice=message if success else None,
    )


@certificates_bp.get("/certificates/<verification_code>/download")
@_login_required
@_role_required("Student Volunteer")
def download(verification_code):
    """Download only a certificate owned by the signed-in volunteer."""
    record = get_certificate_for_user(verification_code, g.current_user["username"])
    if record is None:
        abort(404)
    file_path = get_certificate_pdf_path(record["verification_code"])
    if file_path is None or not file_path.is_file():
        abort(404)
    return send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"SkillMatch-{record['verification_code']}.pdf",
    )


@certificates_bp.route("/verify", methods=["GET", "POST"])
def verify():
    """Public certificate verification lookup; reveals details only for a valid code."""
    code = (
        request.form.get("verification_code", "").strip().upper()
        if request.method == "POST"
        else ""
    )
    result = verify_certificate(code) if code else None
    searched = request.method == "POST"
    return render_template(
        "verify.html",
        verification_code=code,
        certificate=result,
        searched=searched,
    )
