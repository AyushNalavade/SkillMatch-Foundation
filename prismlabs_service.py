"""Private PrismLabs event, roster, account, and certificate services."""

import csv
from datetime import datetime, timezone
from io import BytesIO
import math
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from werkzeug.security import generate_password_hash

from auth_service import PASSWORD_PATTERN
from certificate_service import _render_certificate_pdf
from data_store import edit_documents, load, save
from account_service import email_available
from event_presentation import validate_image_url


EVENT_ID = "prismlabs-recruitment"
ORGANIZER_USERNAME = "prismlabs.organizer"
TEST_VOLUNTEER_USERNAME = "test.volunteer@example.com"
CSV_PATH = (
    Path(__file__).resolve().parent
    / "attached_assets"
    / "prismlabs_volunteers_1794562100357.csv"
)
VOLUNTEERS_FILE = "prismlabs_volunteers.json"
PARTICIPANTS_FILE = "prismlabs_participants.json"
LEGACY_ACCOUNTS_FILE = "prismlabs_accounts.json"
EVENTS_FILE = "events.json"
APPLICATIONS_FILE = "applications.json"
USERS_FILE = "users.json"
CERTIFICATES_FILE = "prismlabs_certificates.json"
SEED_FILE = "prismlabs_seed.json"
AREAS = (
    "Technical & Innovation",
    "Events & Operations",
    "Media & Marketing & Design",
    "Industry Outreach & PR",
    "Corporate & External Relations",
)
ADMIN_PASSWORD_HASH = (
    "scrypt:32768:8:1$FzK5kPtb0V5nR6tN$"
    "b22b282b00333ae02352d1bc0885de034f830599228e34f266a59836263e1a633"
    "1c7bf87e4af7fff9b1e1c1db319ad082ad3e5c87abe59bdcfd246aa35e59867"
)
VOLUNTEER_PASSWORD_HASH = (
    "scrypt:32768:8:1$kafgEofYt7kgPskB$"
    "96b1dfa7e105200f016d105d85ddae7a778e91d4506a68a3c9193ef30aa6b8e9"
    "299eddccb47c8b7c3786ea77872d3a3fcd23456c2336b5f28e5ec36439dc442f"
)
_seed_lock = threading.RLock()


def _doc(filename: str) -> dict:
    value = load(filename)
    if not isinstance(value, dict):
        raise ValueError(f"{filename} must contain an object.")
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), pattern)
        except ValueError:
            continue
    return None


def _csv_volunteers(path: Path = CSV_PATH) -> tuple[dict, list[str], tuple[str, str] | None]:
    if not path.is_file():
        raise RuntimeError(
            "The PrismLabs seed CSV is missing. Keep the supplied CSV in attached_assets."
        )
    headers = {
        "time": "Timestamp",
        "name": "Full Name",
        "prn": "PRN (Permanent Registration Number)",
        "email": "Official MIT WPU Email",
        "year": "Academic Year",
        "department": "Department",
        "areas": "Department interested in Club",
    }
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not set(headers.values()).issubset(set(reader.fieldnames or [])):
            raise ValueError("The PrismLabs CSV is missing required columns.")
        for index, row in enumerate(reader):
            clean = {key: str(row.get(column) or "").strip() for key, column in headers.items()}
            clean["index"] = index
            clean["parsed_time"] = _parse_timestamp(clean["time"])
            rows.append(clean)
    if not rows:
        raise ValueError("The PrismLabs CSV contains no volunteer rows.")

    duplicates: dict[str, int] = {}
    latest: dict[str, dict] = {}
    for row in rows:
        key = row["prn"] or f"review-row-{row['index'] + 1}"
        duplicates[key] = duplicates.get(key, 0) + 1
        previous = latest.get(key)
        current_sort = (row["parsed_time"] or datetime.min, row["index"])
        previous_sort = (
            previous["parsed_time"] or datetime.min, previous["index"]
        ) if previous else None
        if previous is None or current_sort >= previous_sort:
            latest[key] = row

    volunteers = {}
    for key, row in latest.items():
        email = row["email"]
        email_valid = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email))
        email_suspicious = not email_valid or email.casefold().rsplit("@", 1)[-1] != "mitwpu.edu.in"
        prn_suspicious = not bool(re.fullmatch(r"\d{10}", row["prn"])) or duplicates[key] > 1
        areas = [part.strip() for part in row["areas"].split(",") if part.strip()]
        review_flags = []
        if not email_valid:
            review_flags.append("Invalid email format")
        elif email.casefold().rsplit("@", 1)[-1] != "mitwpu.edu.in":
            review_flags.append("Email is outside the MIT-WPU domain")
        if not re.fullmatch(r"\d{10}", row["prn"]):
            review_flags.append("PRN is not 10 digits")
        elif duplicates[key] > 1:
            review_flags.append("Duplicate PRN was deduplicated; latest row kept")
        if not row["parsed_time"]:
            review_flags.append("Registration timestamp needs review")
        volunteers[key] = {
            "full_name": row["name"],
            "prn": row["prn"],
            "email": email,
            "academic_year": row["year"],
            "department": row["department"],
            "interested_areas": areas,
            "registered_at": row["parsed_time"].isoformat() if row["parsed_time"] else None,
            "hours": None,
            "email_suspicious": email_suspicious,
            "prn_suspicious": prn_suspicious,
            "review_flags": review_flags,
            "needs_admin_review": email_suspicious or prn_suspicious or not row["parsed_time"],
        }

    timestamps = [row["parsed_time"] for row in rows if row["parsed_time"]]
    period = (
        (min(timestamps).date().isoformat(), max(timestamps).date().isoformat())
        if timestamps else None
    )
    first_key = next(
        row["prn"] or f"review-row-{row['index'] + 1}" for row in rows
    )
    return volunteers, [first_key], period


def _new_certificate(prn: str, issued_on: str | None = None) -> dict:
    return {
        "certificate_id": secrets.token_urlsafe(18),
        "prn": prn,
        "event_id": EVENT_ID,
        "issued_on": issued_on or datetime.now(timezone.utc).date().isoformat(),
    }


def ensure_seeded() -> None:
    """Import the roster into SkillMatch's regular event and account records."""
    with _seed_lock:
        meta = _doc(SEED_FILE)
        if meta.get("schema_version") == 2:
            return
        volunteers = _doc(VOLUNTEERS_FILE)
        first_prns: list[str] = []
        period = meta.get("registration_period")
        if not volunteers:
            imported, first_prns, period = _csv_volunteers()
            volunteers = imported
            save(VOLUNTEERS_FILE, volunteers)
        else:
            first_prns = [next(iter(volunteers))]
        if not first_prns:
            first_prns = [next(iter(volunteers))]

        participants = _doc(PARTICIPANTS_FILE)
        for prn, volunteer in volunteers.items():
            participants.setdefault(prn, {
                "event_id": EVENT_ID,
                "hours": volunteer.get("hours"),
            })
        participants.setdefault(first_prns[0], {})["skillmatch_username"] = (
            TEST_VOLUNTEER_USERNAME
        )

        users = _doc(USERS_FILE)
        legacy_accounts = _doc(LEGACY_ACCOUNTS_FILE)
        organizer_legacy = legacy_accounts.get("admin@prismlabs.test", {})
        volunteer_legacy = legacy_accounts.get(TEST_VOLUNTEER_USERNAME, {})
        users.setdefault(ORGANIZER_USERNAME, {
            "password_hash": organizer_legacy.get("password_hash", ADMIN_PASSWORD_HASH),
            "role": "Organizer",
            "name": "PrismLabs Event Coordinator",
            "dept": None,
            "skills": {},
            "must_change_password": True,
        })
        first_volunteer = volunteers[first_prns[0]]
        users.setdefault(TEST_VOLUNTEER_USERNAME, {
            "password_hash": volunteer_legacy.get("password_hash", VOLUNTEER_PASSWORD_HASH),
            "role": "Student Volunteer",
            "name": first_volunteer.get("full_name") or "PrismLabs Volunteer",
            "dept": first_volunteer.get("department") or "Not recorded",
            "skills": {},
            "must_change_password": True,
        })
        if users[ORGANIZER_USERNAME].get("role") != "Organizer":
            raise ValueError("The PrismLabs coordinator username is already used by a non-organizer.")
        if users[TEST_VOLUNTEER_USERNAME].get("role") != "Student Volunteer":
            raise ValueError("The PrismLabs test volunteer username is already used by a non-volunteer.")
        with edit_documents(USERS_FILE) as docs:
            for username in (ORGANIZER_USERNAME, TEST_VOLUNTEER_USERNAME):
                docs[USERS_FILE].setdefault(username, users[username])
        save(PARTICIPANTS_FILE, participants)

        events = _doc(EVENTS_FILE)
        legacy_event = _doc("prismlabs_events.json").get(EVENT_ID, {})
        event_data = {
            "event_id": EVENT_ID,
            "name": "Team Prismlabs Recruitment",
            "organizer": ORGANIZER_USERNAME,
            "organizer_name": "PrismLabs Event Coordinator",
            "required_skills": {},
            "slots_needed": max(1, len(volunteers)),
            "date": None,
            "venue": "Not recorded",
            "location": "Not recorded",
            "description": (
                "Participation records for volunteers who took part in Team Prismlabs recruitment. "
                "Interest areas are informational tags; event tasks and assignments were not recorded."
            ),
            "status": "completed",
            "category": "Community Service",
            "source": "prismlabs",
            "show_names_publicly": False,
            "registration_period": period or meta.get("registration_period"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(legacy_event, Mapping):
            for field in ("name", "date", "venue", "description", "show_names_publicly", "registration_period"):
                if field in legacy_event:
                    event_data[field] = legacy_event[field]
            event_data["location"] = event_data.get("venue") or "Not recorded"
        events.setdefault(EVENT_ID, event_data)
        save(EVENTS_FILE, events)
        applications = _doc(APPLICATIONS_FILE)
        event_applications = applications.setdefault(EVENT_ID, {})
        event_applications.setdefault(TEST_VOLUNTEER_USERNAME, {
            "status": "accepted",
            "role": None,
            "attended": True,
            "hours": participants[first_prns[0]].get("hours"),
        })
        save(APPLICATIONS_FILE, applications)

        certificates = _doc(CERTIFICATES_FILE)
        for prn in volunteers:
            if not any(
                isinstance(record, Mapping) and record.get("prn") == prn
                for record in certificates.values()
            ):
                record = _new_certificate(prn)
                certificates[record["certificate_id"]] = record
        save(CERTIFICATES_FILE, certificates)
        meta.update({
            "seeded": True,
            "schema_version": 2,
            "volunteer_count": len(volunteers),
            "first_volunteer_prn": first_prns[0],
            "registration_period": period,
        })
        save(SEED_FILE, meta)
        if legacy_accounts:
            save(LEGACY_ACCOUNTS_FILE, {})
        if legacy_event:
            save("prismlabs_events.json", {})


def list_volunteers(search="", year="", department="", area="") -> list[dict]:
    volunteers = _doc(VOLUNTEERS_FILE)
    participants = _doc(PARTICIPANTS_FILE)
    result = []
    needle = search.strip().casefold()
    for prn, record in volunteers.items():
        if not isinstance(record, Mapping):
            continue
        if needle and needle not in " ".join(
            (str(prn), str(record.get("full_name") or ""), str(record.get("email") or ""))
        ).casefold():
            continue
        if year and record.get("academic_year") != year:
            continue
        if department and record.get("department") != department:
            continue
        if area and area not in record.get("interested_areas", []):
            continue
        participant = participants.get(prn, {})
        result.append({
            "prn": prn, **record,
            "skillmatch_username": participant.get("skillmatch_username")
            if isinstance(participant, Mapping) else None,
        })
    return sorted(result, key=lambda item: (item["full_name"].casefold(), item["prn"]))


def event_stats() -> dict:
    volunteers = list_volunteers()
    def tally(values):
        result = {}
        for value in values:
            key = value or "Not recorded"
            result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items(), key=lambda item: (-item[1], item[0].casefold())))
    years = tally([item.get("academic_year") for item in volunteers])
    departments = tally([item.get("department") for item in volunteers])
    areas = tally([area for item in volunteers for area in item.get("interested_areas", [])])
    return {
        "total": len(volunteers),
        "linked_accounts": sum(bool(item.get("skillmatch_username")) for item in volunteers),
        "years": years,
        "departments": departments,
        "areas": areas,
        "review_count": sum(item.get("needs_admin_review", False) for item in volunteers),
    }


def public_event() -> dict:
    event = dict(_doc(EVENTS_FILE).get(EVENT_ID, {}))
    volunteers = list_volunteers()
    event["stats"] = event_stats()
    event["names"] = (
        [item["full_name"] for item in volunteers]
        if event.get("show_names_publicly") else []
    )
    event["date_label"] = (
        f"Date: {_display_date(event.get('date'))}"
        if _display_date(event.get("date")) else "Date: Not recorded"
    )
    event["registration_label"] = format_registration_period(
        event.get("registration_period")
    )
    return event


def update_event(form: Mapping[str, Any]) -> tuple[bool, str]:
    name = str(form.get("name") or "").strip()
    description = str(form.get("description") or "").strip()
    venue = str(form.get("venue") or "").strip()
    raw_date = str(form.get("date") or "").strip()
    if not name or len(name) > 120:
        return False, "Enter an event name of 1 to 120 characters."
    if not description or len(description) > 500:
        return False, "Enter an event description of 1 to 500 characters."
    if not venue or len(venue) > 160:
        return False, "Enter a venue of 1 to 160 characters."
    if raw_date:
        try:
            raw_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return False, "Choose a valid event date or leave it blank."
    events = _doc(EVENTS_FILE)
    event = dict(events.get(EVENT_ID, {}))
    try:
        image_url = validate_image_url(form.get("image_url", event.get("image_url")))
    except ValueError as error:
        return False, str(error)
    event.update({
        "name": name,
        "description": description,
        "venue": venue,
        "location": venue,
        "image_url": image_url,
        "date": raw_date or None,
        "show_names_publicly": "show_names_publicly" in form,
        "status": "completed",
    })
    events[EVENT_ID] = event
    save(EVENTS_FILE, events)
    return True, "Event details saved."


def set_hours(prn: str, raw_hours: str) -> tuple[bool, str]:
    participants = _doc(PARTICIPANTS_FILE)
    participant = participants.get(prn)
    if not isinstance(participant, dict):
        return False, "Volunteer participation was not found."
    value = raw_hours.strip()
    if not value:
        hours = None
    else:
        try:
            hours = float(value)
        except ValueError:
            return False, "Enter a positive number of hours, or leave blank for N/A."
        if not math.isfinite(hours) or hours <= 0 or hours > 10000:
            return False, "Hours must be greater than 0 and no more than 10,000."
        if hours.is_integer():
            hours = int(hours)
    participant["hours"] = hours
    participants[prn] = participant
    save(PARTICIPANTS_FILE, participants)
    username = participant.get("skillmatch_username")
    if username:
        applications = _doc(APPLICATIONS_FILE)
        application = applications.get(EVENT_ID, {}).get(username)
        if isinstance(application, dict):
            application["hours"] = hours
            save(APPLICATIONS_FILE, applications)
    volunteer = _doc(VOLUNTEERS_FILE).get(prn)
    if isinstance(volunteer, dict):
        volunteer["hours"] = hours
        volunteers = _doc(VOLUNTEERS_FILE)
        volunteers[prn] = volunteer
        save(VOLUNTEERS_FILE, volunteers)
    return True, "Hours saved."


def create_volunteer_account(prn: str, username: str, password: str) -> tuple[bool, str]:
    if prn not in _doc(VOLUNTEERS_FILE):
        return False, "Choose a volunteer record from the roster."
    username = username.strip() if isinstance(username, str) else ""
    if not username or len(username) > 64:
        return False, "Enter a SkillMatch username of 1 to 64 characters."
    if not re.fullmatch(PASSWORD_PATTERN, password or ""):
        return False, "Use at least 8 characters, including one digit and one special character."
    users = _doc(USERS_FILE)
    participants = _doc(PARTICIPANTS_FILE)
    if participants.get(prn, {}).get("skillmatch_username"):
        return False, "That participant already has a linked SkillMatch account."
    if username in users:
        return False, "That SkillMatch username is already in use."
    if any(
        participant.get("skillmatch_username") == username
        for participant in _doc(PARTICIPANTS_FILE).values()
        if isinstance(participant, Mapping)
    ):
        return False, "That SkillMatch username is already linked to a volunteer."
    volunteer = _doc(VOLUNTEERS_FILE)[prn]
    new_user = {
        "name": volunteer.get("full_name") or "PrismLabs Volunteer",
        "role": "Student Volunteer",
        "dept": volunteer.get("department") or "Not recorded",
        "skills": {},
        "password_hash": generate_password_hash(password),
        "must_change_password": True,
    }
    with edit_documents(USERS_FILE, PARTICIPANTS_FILE, APPLICATIONS_FILE) as docs:
        users = docs[USERS_FILE]
        participants = docs[PARTICIPANTS_FILE]
        if username in users or not email_available(users, username.casefold()):
            return False, "That SkillMatch username is already in use."
        if participants.get(prn, {}).get("skillmatch_username"):
            return False, "That participant already has a linked SkillMatch account."
        users[username] = new_user
        participants[prn]["skillmatch_username"] = username
        docs[APPLICATIONS_FILE].setdefault(EVENT_ID, {})[username] = {
            "status": "accepted", "role": None, "attended": True,
            "hours": participants[prn].get("hours"),
        }
    return True, "SkillMatch volunteer account created. Share the temporary sign-in details directly."


def own_profile(username: str) -> dict | None:
    user = _doc(USERS_FILE).get(username)
    if not isinstance(user, Mapping) or user.get("role") != "Student Volunteer":
        return None
    participants = _doc(PARTICIPANTS_FILE)
    prn = next((
        key for key, participant in participants.items()
        if isinstance(participant, Mapping)
        and participant.get("skillmatch_username") == username
    ), None)
    if prn is None:
        return None
    volunteer = _doc(VOLUNTEERS_FILE).get(prn)
    if not isinstance(volunteer, Mapping):
        return None
    event = dict(_doc(EVENTS_FILE).get(EVENT_ID, {}))
    certificates = _doc(CERTIFICATES_FILE)
    certificate = next(
        (
            record for record in certificates.values()
            if isinstance(record, Mapping) and record.get("prn") == prn
        ),
        None,
    )
    participant = _doc(PARTICIPANTS_FILE).get(prn, {})
    return {
        "volunteer": {"prn": prn, **volunteer},
        "event": event,
        "participant": dict(participant) if isinstance(participant, Mapping) else {},
        "certificate": dict(certificate) if isinstance(certificate, Mapping) else None,
    }


def certificate_for_id(certificate_id: str) -> dict | None:
    record = _doc(CERTIFICATES_FILE).get(certificate_id)
    if not isinstance(record, Mapping):
        return None
    volunteer = _doc(VOLUNTEERS_FILE).get(record.get("prn"))
    event = _doc(EVENTS_FILE).get(record.get("event_id"))
    participant = _doc(PARTICIPANTS_FILE).get(record.get("prn"))
    if not isinstance(volunteer, Mapping) or not isinstance(event, Mapping):
        return None
    return {
        "record": dict(record),
        "volunteer": dict(volunteer),
        "event": dict(event),
        "participant": dict(participant) if isinstance(participant, Mapping) else {},
    }


def get_prismlabs_certificates(username: str) -> list[dict]:
    """Adapt only the signed-in volunteer's linked certificate for the shared list."""
    profile = own_profile(username)
    if not profile or not profile.get("certificate"):
        return []
    record = profile["certificate"]
    event = profile["event"]
    return [{
        "source": "prismlabs",
        "verification_code": record["certificate_id"],
        "event_id": record["event_id"],
        "event_name": event["name"],
        "event": event,
        "hours": profile["participant"].get("hours"),
        "notes": "",
        "generated_at": record["issued_on"],
    }]


def _display_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return None


def format_registration_period(period: Any) -> str:
    if not isinstance(period, (tuple, list)) or len(period) != 2:
        return "Not recorded"
    start, end = _display_date(period[0]), _display_date(period[1])
    if not start or not end:
        return "Not recorded"
    return start if start == end else f"{start} – {end}"


def render_prism_certificate(payload: Mapping[str, Any], verify_url: str) -> bytes:
    """Reuse SkillMatch's dome layout; keep verification IDs/links off the PDF."""
    record, volunteer, event, participant = (
        payload["record"], payload["volunteer"], payload["event"], payload["participant"]
    )
    hours = participant.get("hours")
    if (
        not isinstance(hours, (int, float))
        or isinstance(hours, bool) or not math.isfinite(hours) or hours <= 0
    ):
        hours = None
    return _render_certificate_pdf(
        volunteer_name=volunteer["full_name"],
        event_name=event["name"],
        hours=hours,
        event_date=event.get("date"),
        issue_date=record.get("issued_on"),
        volunteer_role=participant.get("role"),
        organizer_name=event.get("organizer_name") or "PrismLabs Event Coordinator",
        omit_missing_event_date=True,
        issue_date_label="Issued on",
    )


def certificate_zip(records: list[dict], verify_url_for) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for payload in records:
            record = payload["record"]
            pdf = render_prism_certificate(
                payload, verify_url_for(record["certificate_id"])
            )
            safe_prn = re.sub(r"[^A-Za-z0-9_-]", "_", record["prn"])
            archive.writestr(f"certificate-{safe_prn}.pdf", pdf)
    return buffer.getvalue()
