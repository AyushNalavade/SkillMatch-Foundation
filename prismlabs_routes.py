"""PrismLabs event tools inside SkillMatch's existing sign-in and dashboards."""

import csv
from functools import wraps
from io import BytesIO, StringIO

from flask import (
    Blueprint, abort, flash, g, make_response, redirect, render_template, request,
    send_file, url_for,
)

from auth_routes import _login_required, _require_csrf
from data_store import load
from prismlabs_service import (
    CERTIFICATES_FILE, EVENT_ID, EVENTS_FILE, certificate_for_id, certificate_zip,
    create_volunteer_account, ensure_seeded, event_stats,
    format_registration_period, list_volunteers, own_profile,
    render_prism_certificate, set_hours, update_event,
)

prism_bp = Blueprint("prismlabs", __name__)


@prism_bp.before_request
def _ensure_prism_event():
    ensure_seeded()


def _organizer_event_required(view):
    @wraps(view)
    def checked(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not isinstance(user, dict) or user.get("role") != "Organizer":
            abort(403)
        event_id = kwargs.get("event_id")
        event = load(EVENTS_FILE).get(event_id)
        if (
            not isinstance(event, dict)
            or event.get("source") != "prismlabs"
            or event.get("organizer") != user.get("username")
        ):
            abort(404)
        return view(*args, **kwargs)
    return checked


def _student_required(view):
    @wraps(view)
    def checked(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not isinstance(user, dict) or user.get("role") != "Student Volunteer":
            abort(403)
        return view(*args, **kwargs)
    return checked


@prism_bp.get("/prismlabs")
def public_event_redirect():
    """Keep old bookmarks working while using the normal SkillMatch event page."""
    return redirect(url_for("events.event_detail", event_id=EVENT_ID), code=301)


@prism_bp.get("/prismlabs/login")
def shared_login_redirect():
    """Send legacy links to SkillMatch's one shared sign-in page."""
    return redirect(url_for("auth.login"), code=302)


@prism_bp.get("/prismlabs/profile")
@_login_required
def shared_profile_redirect():
    return redirect(url_for("auth.profile"), code=302)


@prism_bp.get("/prismlabs/admin")
def shared_dashboard_redirect():
    return redirect(url_for("dashboard.home"), code=302)


@prism_bp.get("/dashboard/events/<event_id>/prismlabs")
@_login_required
@_organizer_event_required
def admin_dashboard(event_id):
    event = dict(load(EVENTS_FILE).get(event_id, {}))
    stats = event_stats()
    certificates = load(CERTIFICATES_FILE)
    return render_template(
        "prismlabs/admin.html", event=event, event_id=event_id, stats=stats,
        certificate_count=len(certificates),
        registration_label=format_registration_period(event.get("registration_period")),
    )


@prism_bp.post("/dashboard/events/<event_id>/prismlabs/event")
@_login_required
@_organizer_event_required
@_require_csrf
def edit_event(event_id):
    success, message = update_event(request.form)
    if not success:
        flash(message, "error")
        return redirect(url_for("prismlabs.admin_dashboard", event_id=event_id), code=303)
    flash(message, "success")
    return redirect(url_for("prismlabs.admin_dashboard", event_id=event_id), code=303)


@prism_bp.get("/dashboard/events/<event_id>/prismlabs/volunteers")
@_login_required
@_organizer_event_required
def admin_volunteers(event_id):
    search = request.args.get("q", "")
    year = request.args.get("year", "")
    department = request.args.get("department", "")
    area = request.args.get("area", "")
    all_volunteers = list_volunteers()
    volunteers = list_volunteers(search, year, department, area)
    years = sorted({item["academic_year"] for item in all_volunteers if item["academic_year"]})
    departments = sorted({item["department"] for item in all_volunteers if item["department"]})
    certificate_by_prn = {
        record.get("prn"): record.get("certificate_id")
        for record in load(CERTIFICATES_FILE).values()
        if isinstance(record, dict)
    }
    return render_template(
        "prismlabs/volunteers.html", event_id=event_id,
        volunteers=volunteers, all_volunteers=all_volunteers, search=search,
        selected_year=year, selected_department=department, selected_area=area,
        years=years, departments=departments,
        total_participants=len(all_volunteers),
        linked_accounts=sum(bool(row.get("skillmatch_username")) for row in all_volunteers),
        areas=("Technical & Innovation", "Events & Operations", "Media & Marketing & Design",
               "Industry Outreach & PR", "Corporate & External Relations"),
        certificate_by_prn=certificate_by_prn,
    )


@prism_bp.get("/dashboard/events/<event_id>/prismlabs/volunteers.csv")
@_login_required
@_organizer_event_required
def export_volunteers(event_id):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "full_name", "prn", "email", "academic_year", "department",
        "interested_areas", "registered_at", "hours", "needs_admin_review",
    ])
    for volunteer in list_volunteers(
        request.args.get("q", ""), request.args.get("year", ""),
        request.args.get("department", ""), request.args.get("area", ""),
    ):
        values = [
            volunteer["full_name"], volunteer["prn"], volunteer["email"],
            volunteer["academic_year"], volunteer["department"],
            ", ".join(volunteer["interested_areas"]),
            volunteer.get("registered_at") or "",
            volunteer["hours"] if volunteer["hours"] is not None else "N/A",
            "yes" if volunteer.get("needs_admin_review") else "no",
        ]
        writer.writerow([
            "'" + str(value) if str(value).startswith(("=", "+", "-", "@", "\t", "\r"))
            else value
            for value in values
        ])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="prismlabs-volunteers.csv"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


@prism_bp.post("/dashboard/events/<event_id>/prismlabs/volunteers/<prn>/hours")
@_login_required
@_organizer_event_required
@_require_csrf
def update_volunteer_hours(event_id, prn):
    success, message = set_hours(prn, request.form.get("hours", ""))
    flash(message, "success" if success else "error")
    return redirect(url_for("prismlabs.admin_volunteers", event_id=event_id), code=303)


@prism_bp.post("/dashboard/events/<event_id>/prismlabs/accounts")
@_login_required
@_organizer_event_required
@_require_csrf
def add_volunteer_login(event_id):
    success, message = create_volunteer_account(
        request.form.get("prn", ""), request.form.get("username", ""),
        request.form.get("password", ""),
    )
    flash(message, "success" if success else "error")
    return redirect(url_for("prismlabs.admin_volunteers", event_id=event_id), code=303)


@prism_bp.get("/dashboard/events/<event_id>/prismlabs/certificates.zip")
@_login_required
@_organizer_event_required
def download_all_certificates(event_id):
    certificates = load(CERTIFICATES_FILE)
    records = [
        payload for certificate_id in certificates
        if (payload := certificate_for_id(certificate_id)) is not None
        and payload["record"].get("event_id") == event_id
    ]
    body = certificate_zip(
        records,
        lambda cert_id: url_for(
            "certificates.verify_prismlabs_certificate",
            certificate_id=cert_id, _external=True,
        ),
    )
    response = send_file(
        BytesIO(body), mimetype="application/zip", as_attachment=True,
        download_name="prismlabs-certificates.zip",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _pdf_response(certificate_id, attachment):
    payload = certificate_for_id(certificate_id)
    if payload is None:
        abort(404)
    verify_url = url_for(
        "certificates.verify_prismlabs_certificate",
        certificate_id=certificate_id, _external=True,
    )
    return send_file(
        BytesIO(render_prism_certificate(payload, verify_url)),
        mimetype="application/pdf", as_attachment=attachment,
        download_name=f"certificate-{payload['record']['prn']}.pdf",
    )


@prism_bp.get("/dashboard/events/<event_id>/prismlabs/certificates/<certificate_id>.pdf")
@_login_required
@_organizer_event_required
def admin_certificate_pdf(event_id, certificate_id):
    payload = certificate_for_id(certificate_id)
    if payload is None or payload["record"].get("event_id") != event_id:
        abort(404)
    response = _pdf_response(certificate_id, request.args.get("download") == "1")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@prism_bp.get("/profile/prismlabs/certificate.pdf")
@_login_required
@_student_required
def volunteer_certificate_pdf():
    profile = own_profile(g.current_user["username"])
    if not profile or not profile.get("certificate"):
        abort(404)
    certificate = profile["certificate"]
    if certificate.get("prn") != profile["volunteer"].get("prn"):
        abort(404)
    response = _pdf_response(certificate["certificate_id"], True)
    response.headers["Cache-Control"] = "private, no-store"
    return response
