"""Certificate pages, organizer event completion, and public verification."""

from functools import wraps
from io import BytesIO

from flask import (
    Blueprint, abort, g, make_response, redirect, render_template, request, send_file, url_for,
)

from auth_routes import _login_required, _require_csrf
from certificate_service import (
    complete_event as complete_event_service,
    get_certificate_for_user,
    get_organizer_events,
    get_user_certificates,
    render_certificate_pdf,
    render_verified_certificate_pdf,
    verify_certificate,
)
from data_store import load
from prismlabs_service import certificate_for_id, get_prismlabs_certificates, render_prism_certificate


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
            imported = get_prismlabs_certificates(current_user["username"])
            imported_event_ids = {record["event_id"] for record in imported}
            certificates = [
                record for record in certificates
                if record.get("event_id") not in imported_event_ids
            ] + imported
            certificates.sort(key=lambda record: str(record.get("generated_at", "")), reverse=True)
            event_records = load("events.json")
            if not isinstance(event_records, dict):
                raise ValueError("events.json must contain an object keyed by event ID.")
            certificates = [
                {
                    **record,
                    "event": event_records.get(record.get("event_id"), {
                        "name": record.get("event_name"),
                        "category": record.get("category", "Community Service"),
                    }),
                }
                for record in certificates
            ]
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
        owned = next((
            item for item in get_prismlabs_certificates(g.current_user["username"])
            if item["verification_code"] == verification_code
        ), None)
        if owned is None:
            abort(404)
        payload = certificate_for_id(verification_code)
        if payload is None:
            abort(404)
        record = owned
        pdf_bytes = render_prism_certificate(
            payload, url_for(
                "certificates.verify_prismlabs_certificate",
                certificate_id=verification_code, _external=True,
            ),
        )
    else:
        pdf_bytes = render_certificate_pdf(record)
    if pdf_bytes is None:
        abort(404)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"SkillMatch-{record['verification_code']}.pdf",
    )


@certificates_bp.route("/verify", methods=["GET", "POST"])
def verify():
    """Public certificate verification lookup; reveals details only for a valid code."""
    raw_code = (
        request.form.get("verification_code", "").strip()
        if request.method == "POST"
        else request.args.get("code", "").strip()
    )
    if raw_code and certificate_for_id(raw_code) is not None:
        return redirect(url_for(
            "certificates.verify_prismlabs_certificate", certificate_id=raw_code,
        ), code=303 if request.method == "POST" else 302)
    code = raw_code.upper()
    result = verify_certificate(code) if code else None
    searched = request.method == "POST" or bool(code)
    return render_template(
        "verify.html",
        verification_code=code,
        certificate=result,
        searched=searched,
    )


@certificates_bp.get("/verify/<certificate_id>")
def verify_prismlabs_certificate(certificate_id):
    """Public PrismLabs verification URL; never exposes roster-only fields."""
    payload = certificate_for_id(certificate_id)
    response = make_response(render_template(
        "prismlabs/verify.html",
        result=None if payload is None else {
            "name": payload["volunteer"]["full_name"],
            "event": payload["event"]["name"],
            "date": payload["event"].get("date"),
            "valid": True,
        },
    ), 200 if payload else 404)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@certificates_bp.get("/verify/<verification_code>/certificate.pdf")
def verified_certificate_preview(verification_code):
    """Public inline copy matches the owner's PDF; verification codes stay off the PDF."""
    pdf = render_verified_certificate_pdf(verification_code)
    if pdf is None:
        abort(404)
    response = send_file(
        BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
        download_name="SkillMatch-certificate.pdf",
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
