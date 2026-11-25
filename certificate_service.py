"""Event completion and volunteer certificate operations."""

from collections.abc import Mapping
from datetime import date, datetime, timezone
import math
import re
import secrets
import string
from typing import Any
from xml.etree import ElementTree

from data_store import load, save
from formatting import format_decimal
from notification_service import add_notifications
from certificate_themes import CERTIFICATE_THEMES, get_certificate_theme
from event_presentation import event_category
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.svgpath import SvgPath


EVENTS_FILE = "events.json"
APPLICATIONS_FILE = "applications.json"
USERS_FILE = "users.json"
CERTIFICATES_FILE = "certificates.json"


def _load_object(filename: str, description: str) -> dict[str, Any]:
    data = load(filename)
    if not isinstance(data, dict):
        raise ValueError(f"{filename} must contain an object {description}.")
    return data


def _safe_pdf_text(value: Any) -> str:
    """Make user-provided text safe for a basic Helvetica PDF content stream."""
    text = " ".join(str(value).split())
    text = text.encode("cp1252", errors="replace").decode("cp1252")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _parse_certificate_date(value: Any) -> date | None:
    """Parse supported ISO date/timestamp values without inventing a fallback date."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str) and value.strip():
        raw_value = value.strip()
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


def _format_certificate_date(value: Any) -> str | None:
    parsed = _parse_certificate_date(value)
    if parsed is None:
        return None
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def _event_date_value(event: Mapping[str, Any]) -> str | None:
    """Use the canonical event date first, then supported legacy scheduling fields."""
    for field in ("date", "event_date", "starts_at", "start_at", "start_date"):
        parsed = _parse_certificate_date(event.get(field))
        if parsed is not None:
            return parsed.isoformat()
    return None


def _normalize_verification_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if re.fullmatch(r"[A-Z0-9]{10}", normalized) else None


def _estimated_text_width(text: str, font_size: int, bold: bool = False) -> float:
    """Conservatively estimate Helvetica text width for line wrapping and centering."""
    width = 0.0
    for character in text:
        if character in " ilI.,:;!|'`":
            factor = 0.29
        elif character in "MW@%&":
            factor = 0.86
        elif character in "frt()[]":
            factor = 0.36
        elif character.isupper():
            factor = 0.66
        else:
            factor = 0.54
        width += factor * font_size * (1.05 if bold else 1)
    return width


def _wrap_pdf_text(text: Any, max_width: float, font_size: int, bold: bool) -> list[str]:
    normalized = " ".join(str(text).split())
    if not normalized:
        return [""]

    tokens: list[str] = []
    for word in normalized.split(" "):
        if _estimated_text_width(word, font_size, bold) <= max_width:
            tokens.append(word)
            continue
        chunk = ""
        for character in word:
            if chunk and _estimated_text_width(chunk + character, font_size, bold) > max_width:
                tokens.append(chunk)
                chunk = character
            else:
                chunk += character
        if chunk:
            tokens.append(chunk)

    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}".strip()
        if current and _estimated_text_width(candidate, font_size, bold) > max_width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_pdf_text(
    text: Any,
    max_width: float,
    preferred_size: int,
    minimum_size: int,
    max_lines: int,
    bold: bool = False,
) -> tuple[list[str], int]:
    for font_size in range(preferred_size, minimum_size - 1, -1):
        lines = _wrap_pdf_text(text, max_width, font_size, bold)
        if len(lines) <= max_lines:
            return lines, font_size

    font_size = minimum_size
    lines = _wrap_pdf_text(text, max_width, font_size, bold)[:max_lines]
    if lines:
        suffix = "..."
        last_line = lines[-1]
        while last_line and _estimated_text_width(last_line + suffix, font_size, bold) > max_width:
            last_line = last_line[:-1].rstrip()
        lines[-1] = last_line + suffix
    return lines, font_size


def _pdf_text_command(
    text: Any,
    x: float,
    y: float,
    font_size: int,
    font: str,
    color: tuple[float, float, float],
    centered: bool = False,
) -> str:
    normalized = " ".join(str(text).split())
    if centered:
        x -= _estimated_text_width(normalized, font_size, font == "F2") / 2
    red, green, blue = color
    return (
        f"{red:.3f} {green:.3f} {blue:.3f} rg "
        f"BT /{font} {font_size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm "
        f"({_safe_pdf_text(normalized)}) Tj ET"
    )


def _draw_theme_emblem(pdf: Canvas, theme: Mapping[str, Any]) -> None:
    """Render our trusted inline SVG paths as vectors, with no files or extra dependencies."""
    root = ElementTree.fromstring(theme["emblem_svg"])
    drawing = Drawing(24, 24)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue
        drawing.add(SvgPath(
            element.attrib["d"],
            fillColor=None,
            strokeColor=HexColor(theme["primary"]),
            strokeWidth=1.4,
        ))
    renderPDF.draw(drawing, pdf, 418, 544)


def _render_certificate_pdf(
    volunteer_name: str,
    event_name: str,
    hours: int | float | None,
    event_date: str | None = None,
    issue_date: str | None = None,
    volunteer_role: str | None = None,
    organizer_name: str | None = None,
    theme_category: str | None = None,
    omit_missing_event_date: bool = False,
    issue_date_label: str = "Issue date",
) -> bytes:
    """Render a single-page landscape certificate with a split photo-and-type layout."""
    _register_certificate_fonts()
    theme = get_certificate_theme(theme_category)
    navy = HexColor(theme["primary"])
    accent = HexColor(theme["accent"])
    paper = Color(0.976, 0.965, 0.929)
    muted_ink = HexColor("#48566b")
    dome_jpeg, dome_width, dome_height = _read_dome_image()
    image_scale = 558 / dome_height
    image_width = dome_width * image_scale
    image_height = dome_height * image_scale
    image_x = 27 - 315 * image_scale
    image_y = 27
    buffer = BytesIO()
    pdf = Canvas(
        buffer,
        pagesize=(792, 612),
        pageCompression=0,
        pdfVersion=(1, 4),
        invariant=1,
    )
    pdf.setTitle("SkillMatch Certificate of Participation")
    pdf.setFillColor(paper)
    pdf.rect(0, 0, 792, 612, stroke=0, fill=1)

    # Keep the existing double border, campus-photo crop, and split-page layout.
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(0.9)
    pdf.rect(18, 18, 756, 576, stroke=1, fill=0)
    if theme["border_pattern"] == "dashed":
        pdf.setDash(4, 3)
    elif theme["border_pattern"] == "dotted":
        pdf.setDash(1, 3)
    pdf.rect(25, 25, 742, 562, stroke=1, fill=0)
    pdf.setDash()
    photo_clip = pdf.beginPath()
    photo_clip.moveTo(27, 27)
    photo_clip.lineTo(27, 585)
    photo_clip.lineTo(350, 585)
    photo_clip.curveTo(369, 548, 380, 495, 381, 430)
    photo_clip.curveTo(383, 352, 380, 280, 371, 210)
    photo_clip.curveTo(361, 140, 347, 75, 337, 27)
    photo_clip.lineTo(27, 27)
    photo_clip.close()
    pdf.saveState()
    pdf.clipPath(photo_clip, stroke=0, fill=0)
    pdf.drawImage(
        ImageReader(BytesIO(dome_jpeg)),
        image_x,
        image_y,
        width=image_width,
        height=image_height,
        preserveAspectRatio=False,
    )
    pdf.restoreState()

    photo_edge = pdf.beginPath()
    photo_edge.moveTo(27, 585)
    photo_edge.lineTo(350, 585)
    photo_edge.curveTo(369, 548, 380, 495, 381, 430)
    photo_edge.curveTo(383, 352, 380, 280, 371, 210)
    photo_edge.curveTo(361, 140, 347, 75, 337, 27)
    photo_edge.lineTo(27, 27)
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(1.5)
    pdf.drawPath(photo_edge, stroke=1, fill=0)

    photo_detail = pdf.beginPath()
    photo_detail.moveTo(346, 582)
    photo_detail.curveTo(365, 545, 375, 490, 377, 427)
    photo_detail.curveTo(379, 352, 376, 280, 367, 210)
    photo_detail.curveTo(358, 140, 344, 75, 333, 30)
    pdf.setLineWidth(0.55)
    pdf.drawPath(photo_detail, stroke=1, fill=0)

    diamond = pdf.beginPath()
    diamond.moveTo(576, 532)
    diamond.lineTo(581, 537)
    diamond.lineTo(586, 532)
    diamond.lineTo(581, 527)
    diamond.close()
    pdf.setFillColor(accent)
    pdf.drawPath(diamond, stroke=0, fill=1)
    pdf.setLineWidth(0.9)
    pdf.line(448, 534, 510, 534)
    pdf.line(652, 534, 714, 534)

    # Preserve the certificate's existing gold botanical ornament.
    branch = pdf.beginPath()
    branch.moveTo(366, 29)
    branch.curveTo(380, 55, 390, 100, 414, 151)
    pdf.setLineWidth(0.75)
    pdf.drawPath(branch, stroke=1, fill=0)
    leaf_curves = (
        ((371, 47), (362, 45), (357, 49), (354, 55), (362, 59), (369, 56), (371, 47)),
        ((378, 64), (388, 57), (396, 58), (400, 63), (394, 70), (386, 70), (378, 64)),
        ((382, 81), (372, 77), (365, 81), (363, 87), (371, 92), (378, 89), (382, 81)),
        ((388, 98), (398, 90), (407, 91), (411, 96), (404, 103), (396, 104), (388, 98)),
        ((394, 116), (384, 112), (377, 116), (375, 122), (383, 127), (390, 124), (394, 116)),
        ((401, 134), (411, 126), (419, 128), (422, 133), (415, 140), (407, 140), (401, 134)),
    )
    for start, c1, c2, end, c3, c4, finish in leaf_curves:
        leaf = pdf.beginPath()
        leaf.moveTo(*start)
        leaf.curveTo(*c1, *c2, *end)
        leaf.curveTo(*c3, *c4, *finish)
        pdf.drawPath(leaf, stroke=1, fill=0)

    _draw_theme_emblem(pdf, theme)
    _draw_certificate_text(
        pdf, "MIT WORLD PEACE UNIVERSITY", 581, 566, 10, "DM-Sans-Bold", navy,
    )
    _draw_certificate_text(pdf, "PUNE", 581, 551, 7, "DM-Sans-Bold", navy)
    _draw_certificate_text(
        pdf, "CERTIFICATE OF", 581, 501, 20, "Fraunces-SemiBold", navy,
    )
    heading_lines, heading_size = _fit_pdf_text(
        theme["heading"], 346, 29, 13, 1, bold=True, font_name="Fraunces-Bold",
    )
    _draw_certificate_text(
        pdf, heading_lines[0], 581, 472, heading_size, "Fraunces-Bold", navy,
    )
    _draw_certificate_text(
        pdf, "PRESENTED TO", 581, 438, 8, "DM-Sans-Bold", navy,
    )

    name_lines, name_size = _fit_pdf_text(
        volunteer_name, 670, preferred_size=34, minimum_size=22, max_lines=3, bold=True,
    )
    name_y = 462
    for line in name_lines:
        commands.append(_pdf_text_command(line, 396, name_y, name_size, "F2", ink, centered=True))
        name_y -= name_size + 3

    event_label_y = name_y - 10
    commands.append(_pdf_text_command(
        "FOR PARTICIPATION IN", 396, event_label_y, 10, "F2", muted_green, centered=True,
    ))
    event_lines, event_size = _fit_pdf_text(
        event_name, 670, preferred_size=20, minimum_size=13, max_lines=3, bold=True,
    )
    event_y = event_label_y - 28
    for line in event_lines:
        commands.append(_pdf_text_command(line, 396, event_y, event_size, "F2", ink, centered=True))
        event_y -= event_size + 2

    event_date_text = _format_certificate_date(event_date)
    issue_date_text = _format_certificate_date(issue_date) or "Not recorded"
    if event_date_text or not omit_missing_event_date:
        _draw_certificate_text(
            pdf, f"Event date: {event_date_text or 'Not recorded'}",
            text_center, details_y, 9, "DM-Sans", muted_ink,
        )
    else:
        details_y += 16
    _draw_certificate_text(
        pdf,
        f"{issue_date_label}: {issue_date_text}",
        text_center,
        details_y - 16,
        9,
        "DM-Sans",
        muted_ink,
    )
    _draw_certificate_text(
        pdf,
        "Hours: N/A" if hours is None else f"{format_decimal(hours)} volunteer hours",
        text_center,
        details_y - 45,
        14,
        "DM-Sans-Bold",
        navy,
    )
    citation_lines, citation_size = _fit_pdf_text(
        theme["citation"], 346, 8, 7, 1, font_name="DM-Sans",
    )
    _draw_certificate_text(
        pdf, citation_lines[0], text_center, details_y - 62,
        citation_size, "DM-Sans", navy,
    )
    _draw_certificate_text(
        pdf,
        "Verification code available in your SkillMatch profile",
        text_center,
        details_y - 76,
        8,
        "DM-Sans-Bold",
        navy,
    )
    _draw_certificate_text(
        pdf, "Verify at /verify", text_center, details_y - 94, 8, "DM-Sans", muted_ink,
    )

    pdf.setStrokeColor(accent)
    pdf.setLineWidth(0.9)
    pdf.line(507, 82, 655, 82)
    signature_name = (
        " ".join(organizer_name.split())
        if isinstance(organizer_name, str) and organizer_name.strip()
        else "Event Organizer"
    )
    organizer_lines, organizer_size = _fit_pdf_text(
        signature_name,
        140,
        preferred_size=9,
        minimum_size=6,
        max_lines=1,
        bold=True,
        font_name="DM-Sans-Bold",
    )
    _draw_certificate_text(
        pdf,
        organizer_lines[0],
        text_center,
        65,
        organizer_size,
        "DM-Sans-Bold",
        navy,
    )
    _draw_certificate_text(
        pdf,
        "Event Organizer Signature",
        text_center,
        51,
        7,
        "DM-Sans",
        muted_ink,
    )
    return bytes(pdf)


def render_certificate_pdf(record: Mapping[str, Any]) -> bytes | None:
    """Render a persisted certificate record as PDF bytes for an on-demand download."""
    if not isinstance(record, Mapping):
        return None
    code = _normalize_verification_code(record.get("verification_code"))
    if code is None or record.get("verification_code") != code:
        return None

    hours = record.get("hours", 0)
    if (
        not isinstance(hours, (int, float))
        or isinstance(hours, bool)
        or not math.isfinite(hours)
        or hours < 0
    ):
        hours = 0
    volunteer_name = record.get("volunteer_name")
    if not isinstance(volunteer_name, str) or not volunteer_name.strip():
        volunteer_name = record.get("username", "Student Volunteer")
    event_name = record.get("event_name")
    if not isinstance(event_name, str) or not event_name.strip():
        event_name = record.get("event_id", "Campus event")

    return _render_certificate_pdf(
        str(volunteer_name),
        str(event_name),
        hours,
        code,
        event_date=record.get("event_date"),
        issue_date=record.get("generated_at"),
        volunteer_role=volunteer_role,
        organizer_name=organizer_name,
        theme_category=record.get("theme_category"),
    )


def _certificate_for(event_id: str, username: str) -> dict[str, Any] | None:
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    return next(
        (
            record
            for record in certificates.values()
            if isinstance(record, dict)
            and record.get("event_id") == event_id
            and record.get("username") == username
        ),
        None,
    )


def generate_certificate(
    event_id: str, username: str
) -> tuple[bool, str]:
    """Generate or refresh a PDF only for an accepted applicant of a completed event."""
    try:
        if not isinstance(event_id, str) or not event_id.strip():
            return False, "Enter a valid event ID."
        if not isinstance(username, str) or not username.strip():
            return False, "Enter a valid volunteer username."
        event_id, username = event_id.strip(), username.strip()

        events = _load_object(EVENTS_FILE, "keyed by event ID")
        event = events.get(event_id)
        if not isinstance(event, dict):
            return False, "The event was not found."
        applications = _load_object(APPLICATIONS_FILE, "keyed by event ID")
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, Mapping):
            return False, "The event's application records are invalid."
        application = event_applications.get(username)
        if not isinstance(application, Mapping):
            return False, "This volunteer did not apply to the event."
        if not isinstance(application.get("status"), str) or (
            application["status"].casefold() != "accepted"
        ):
            return False, "Only accepted applicants can receive a certificate."
        if event.get("status") != "completed":
            return False, "The event must be marked complete before issuing certificates."

        users = _load_object(USERS_FILE, "keyed by username")
        user = users.get(username)
        if not isinstance(user, Mapping):
            return False, "The volunteer's account could not be found."
        if user.get("role") != "Student Volunteer":
            return False, "Certificates are issued only to student volunteers."

        event_date = _event_date_value(event)
        certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
        record = _certificate_for(event_id, username)
        if record is None:
            alphabet = string.ascii_uppercase + string.digits
            code = ""
            while not code or code in certificates:
                code = "".join(secrets.choice(alphabet) for _ in range(10))
            record = {
                "verification_code": code,
                "event_id": event_id,
                "username": username,
                "event_name": event.get("name", event_id),
                "event_date": event_date,
                "volunteer_name": user.get("name", username),
                "hours": application.get("hours", 0),
                "notes": application.get("notes", ""),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "category": event_category(event),
                "theme_category": event_category(event),
            }

        code = record.get("verification_code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{10}", code):
            return False, "The stored certificate has an invalid verification code."
        record.update({
            "event_name": event.get("name", event_id),
            "volunteer_name": user.get("name", username),
            "hours": application.get("hours", 0),
            "notes": application.get("notes", ""),
        })
        if event_date is not None:
            record["event_date"] = event_date
        certificates[code] = record
        save(CERTIFICATES_FILE, certificates)
        return True, code
    except Exception as error:
        return False, f"Could not generate the certificate: {error}"


def complete_event(
    event_id: str, contribution_updates: Mapping[str, Any]
) -> tuple[bool, str]:
    """Save final accepted-volunteer contributions, complete the event, and issue certificates."""
    try:
        if not isinstance(event_id, str) or not event_id.strip():
            return False, "Enter a valid event ID."
        event_id = event_id.strip()
        if not isinstance(contribution_updates, Mapping):
            return False, "Contribution updates must be provided for each accepted volunteer."

        events = _load_object(EVENTS_FILE, "keyed by event ID")
        event = events.get(event_id)
        if not isinstance(event, dict):
            return False, "The event was not found."
        applications = _load_object(APPLICATIONS_FILE, "keyed by event ID")
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, dict):
            return False, "The event's application records are invalid."
        accepted = {
            username: application
            for username, application in event_applications.items()
            if isinstance(application, Mapping)
            and isinstance(application.get("status"), str)
            and application["status"].casefold() == "accepted"
        }
        if event.get("status") == "completed":
            for username in accepted:
                success, result = generate_certificate(event_id, username)
                if not success:
                    return False, f"Event already complete, but certificate issuance failed: {result}"
            return True, "Event was already complete; certificates are available."
        if not accepted:
            if contribution_updates:
                return False, "There are no accepted volunteers with contribution details to save."
            event["status"] = "completed"
            event["completed_at"] = datetime.now(timezone.utc).isoformat()
            events[event_id] = event
            save(EVENTS_FILE, events)
            return True, "Event completed. There were no accepted volunteers to certify."
        if set(contribution_updates) != set(accepted):
            return False, "Enter final hours and notes for every accepted volunteer."

        cleaned_updates: dict[str, tuple[int | float, str]] = {}
        for username in accepted:
            update = contribution_updates.get(username)
            if not isinstance(update, Mapping):
                return False, f"Contribution details for '{username}' are invalid."
            hours = update.get("hours")
            if isinstance(hours, str):
                try:
                    hours = float(hours.strip())
                except ValueError:
                    hours = None
            if (
                not isinstance(hours, (int, float))
                or isinstance(hours, bool)
                or not math.isfinite(hours)
                or hours < 0
            ):
                return False, f"Hours for '{username}' must be a non-negative finite number."
            if float(hours).is_integer():
                hours = int(hours)
            notes = update.get("notes", "")
            if not isinstance(notes, str) or len(notes) > 1000:
                return False, f"Notes for '{username}' must be 1,000 characters or fewer."
            cleaned_updates[username] = (hours, notes.strip())

        for username, (hours, notes) in cleaned_updates.items():
            accepted[username]["hours"] = hours
            accepted[username]["notes"] = notes
        event["status"] = "completed"
        event["completed_at"] = datetime.now(timezone.utc).isoformat()
        applications[event_id] = event_applications
        events[event_id] = event
        save(APPLICATIONS_FILE, applications)
        save(EVENTS_FILE, events)

        for username in accepted:
            success, result = generate_certificate(event_id, username)
            if not success:
                return False, f"Event completed, but a certificate could not be issued: {result}"
        return True, "Event completed and certificates issued."
    except Exception as error:
        return False, f"Could not complete the event: {error}"


def get_organizer_events(organizer_id: str) -> list[dict[str, Any]]:
    """Return this organizer's events with accepted volunteers for completion forms."""
    events = _load_object(EVENTS_FILE, "keyed by event ID")
    applications = _load_object(APPLICATIONS_FILE, "keyed by event ID")
    users = _load_object(USERS_FILE, "keyed by username")
    result = []
    for event_id, event in events.items():
        if not isinstance(event, dict) or event.get("organizer") != organizer_id:
            continue
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, Mapping):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        accepted = []
        for username, application in event_applications.items():
            if not isinstance(application, Mapping) or not isinstance(
                application.get("status"), str
            ):
                continue
            if application["status"].casefold() != "accepted":
                continue
            user = users.get(username, {})
            accepted.append({
                "username": username,
                "name": user.get("name", username) if isinstance(user, Mapping) else username,
                "hours": application.get("hours", 0),
                "notes": application.get("notes", ""),
            })
        result.append({
            "event_id": event_id,
            "event": event,
            "volunteers": sorted(accepted, key=lambda item: str(item["name"]).casefold()),
        })
    return sorted(result, key=lambda item: str(item["event"].get("name", "")).casefold())


def get_user_certificates(username: str) -> list[dict[str, Any]]:
    """Return only the certificate records belonging to the specified user."""
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    return sorted(
        (
            dict(record)
            for record in certificates.values()
            if isinstance(record, Mapping) and record.get("username") == username
        ),
        key=lambda record: str(record.get("generated_at", "")),
        reverse=True,
    )


def get_skill_passport(username: str) -> dict[str, Any]:
    """Build a private skill portfolio from completed, organizer-confirmed work.

    A verified skill must have been required by a completed event whose accepted
    application has an organizer-assigned role and confirmed attendance or
    positive recorded hours. Self-reported skills alone never create a claim.
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Enter a valid volunteer username.")
    username = username.strip()

    users = _load_object(USERS_FILE, "keyed by username")
    user = users.get(username)
    if not isinstance(user, Mapping) or user.get("role") != "Student Volunteer":
        return {
            "completed_event_count": 0,
            "total_hours": 0.0,
            "contributions": [],
            "verified_skills": [],
        }

    events = _load_object(EVENTS_FILE, "keyed by event ID")
    applications = _load_object(APPLICATIONS_FILE, "keyed by event ID")
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    certificates_by_event: dict[str, str] = {}
    for record in certificates.values():
        if (
            isinstance(record, Mapping)
            and record.get("username") == username
            and isinstance(record.get("event_id"), str)
        ):
            code = _normalize_verification_code(record.get("verification_code"))
            if code:
                certificates_by_event.setdefault(record["event_id"], code)

    contributions: list[dict[str, Any]] = []
    skills_by_name: dict[str, dict[str, Any]] = {}
    total_hours = 0.0

    for event_id, event in events.items():
        if (
            not isinstance(event_id, str)
            or not isinstance(event, Mapping)
            or event.get("status") != "completed"
        ):
            continue

        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, Mapping):
            continue
        application = event_applications.get(username)
        if (
            not isinstance(application, Mapping)
            or not isinstance(application.get("status"), str)
            or application["status"].casefold() != "accepted"
        ):
            continue

        role = application.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        hours = application.get("hours", 0)
        if (
            not isinstance(hours, (int, float))
            or isinstance(hours, bool)
            or not math.isfinite(hours)
            or hours < 0
        ):
            hours = 0
        if application.get("attended") is not True and hours <= 0:
            continue

        required_skills = event.get("required_skills", {})
        event_skills = []
        if isinstance(required_skills, Mapping):
            seen_skills: set[str] = set()
            for skill in required_skills:
                if not isinstance(skill, str) or not skill.strip():
                    continue
                normalized_skill = " ".join(skill.split()).casefold()
                if normalized_skill in seen_skills:
                    continue
                seen_skills.add(normalized_skill)
                event_skills.append(" ".join(skill.split()))

        if float(hours).is_integer():
            hours = int(hours)
        certificate_code = certificates_by_event.get(event_id)
        contribution = {
            "event_id": event_id,
            "event_name": (
                event.get("name")
                if isinstance(event.get("name"), str) and event.get("name").strip()
                else event_id
            ),
            "event_date": _event_date_value(event),
            "role": role.strip(),
            "hours": hours,
            "skills": event_skills,
            "certificate_code": certificate_code,
            "event_linkable": not bool(event.get("deleted_at")),
            "sort_date": str(
                event.get("completed_at") or event.get("date") or ""
            ),
        }
        contributions.append(contribution)
        total_hours += float(hours)

        for skill in event_skills:
            normalized_skill = " ".join(skill.split()).casefold()
            skill_entry = skills_by_name.setdefault(
                normalized_skill, {"name": skill, "evidence": []}
            )
            skill_entry["evidence"].append(contribution)

    contributions.sort(
        key=lambda item: (item["sort_date"], item["event_name"].casefold()),
        reverse=True,
    )
    verified_skills = sorted(
        skills_by_name.values(), key=lambda item: item["name"].casefold()
    )
    for skill in verified_skills:
        skill["evidence"].sort(
            key=lambda item: (item["sort_date"], item["event_name"].casefold()),
            reverse=True,
        )

    if total_hours.is_integer():
        total_hours = int(total_hours)
    return {
        "completed_event_count": len(contributions),
        "total_hours": total_hours,
        "contributions": contributions,
        "verified_skills": verified_skills,
    }


def get_volunteer_badge(username: str) -> dict[str, Any] | None:
    """Return the volunteer's current hours tier, based on completed-event records."""
    if not isinstance(username, str) or not username.strip():
        return None

    events = _load_object(EVENTS_FILE, "keyed by event ID")
    applications = _load_object(APPLICATIONS_FILE, "keyed by event ID")
    total_hours = 0.0
    for event_id, event in events.items():
        if not isinstance(event, Mapping) or event.get("status") != "completed":
            continue
        event_applications = applications.get(event_id, {})
        if not isinstance(event_applications, Mapping):
            raise ValueError(f"Applications for event '{event_id}' must be an object.")
        application = event_applications.get(username)
        if (
            not isinstance(application, Mapping)
            or not isinstance(application.get("status"), str)
            or application["status"].casefold() != "accepted"
        ):
            continue
        hours = application.get("hours")
        if (
            isinstance(hours, (int, float))
            and not isinstance(hours, bool)
            and math.isfinite(hours)
            and hours > 0
        ):
            total_hours += float(hours)

    if total_hours <= 0:
        return None
    if total_hours >= 100:
        badge, next_badge, next_threshold = "Diamond", None, None
    elif total_hours >= 25:
        badge, next_badge, next_threshold = "Gold", "Diamond", 100
    elif total_hours >= 10:
        badge, next_badge, next_threshold = "Silver", "Gold", 25
    else:
        badge, next_badge, next_threshold = "Copper", "Silver", 10
    return {
        "name": badge,
        "slug": badge.lower(),
        "total_hours": total_hours,
        "next_badge": next_badge,
        "hours_until_next": (
            next_threshold - total_hours if next_threshold is not None else None
        ),
    }


def get_certificate_for_user(
    verification_code: str, username: str
) -> dict[str, Any] | None:
    """Find a certificate only when its verification code belongs to the user."""
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    record = certificates.get(verification_code)
    if not isinstance(record, Mapping) or record.get("username") != username:
        return None
    if (
        not isinstance(record.get("verification_code"), str)
        or not re.fullmatch(r"[A-Z0-9]{10}", record["verification_code"])
        or record["verification_code"] != verification_code
    ):
        return None
    return dict(record)


def render_verified_certificate_pdf(verification_code: str) -> bytes | None:
    """Render public certificate content only for an existing, exact verification code."""
    code = _normalize_verification_code(verification_code)
    if code is None:
        return None
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    record = certificates.get(code)
    if not isinstance(record, Mapping) or record.get("verification_code") != code:
        return None
    return render_certificate_pdf(record)


def verify_certificate(verification_code: str) -> dict[str, Any] | None:
    """Return safe public certificate details, or None for an invalid or unknown code."""
    code = _normalize_verification_code(verification_code)
    if code is None:
        return None
    certificates = _load_object(CERTIFICATES_FILE, "keyed by verification code")
    record = certificates.get(code)
    if (
        not isinstance(record, Mapping)
        or record.get("verification_code") != code
    ):
        return None

    events = _load_object(EVENTS_FILE, "keyed by event ID")
    event_id = record.get("event_id")
    event = events.get(event_id) if isinstance(event_id, str) else None
    event_date = _format_certificate_date(record.get("event_date"))
    if event_date is None and isinstance(event, Mapping):
        event_date = _format_certificate_date(_event_date_value(event))

    hours = record.get("hours")
    if (
        not isinstance(hours, (int, float))
        or isinstance(hours, bool)
        or not math.isfinite(hours)
        or hours < 0
    ):
        hours = None
    event_name = record.get("event_name")
    if not isinstance(event_name, str) and isinstance(event, Mapping):
        event_name = event.get("name")

    return {
        "verification_code": code,
        "volunteer_name": (
            record.get("volunteer_name")
            if isinstance(record.get("volunteer_name"), str)
            else ""
        ),
        "event_name": event_name if isinstance(event_name, str) else "",
        "event_date": event_date,
        "issue_date": _format_certificate_date(record.get("generated_at")),
        "hours": hours,
        "pdf_available": True,
        "theme": get_certificate_theme(record.get("theme_category")),
    }
