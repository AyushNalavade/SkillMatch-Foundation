"""PrismLabs is an ordinary SkillMatch event with shared authentication."""

import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from app import app
from dashboard_service import dashboard_cards, get_organizer_events
from prismlabs_service import (
    EVENT_ID, ORGANIZER_USERNAME, TEST_VOLUNTEER_USERNAME, _csv_volunteers,
    _doc, certificate_for_id, ensure_seeded, own_profile, render_prism_certificate,
)


class PrismLabsPortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prismlabs-test-")
        self.data_patch = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.data_patch.start()
        self.previous = (app.config["TESTING"], app.config["SESSION_COOKIE_SECURE"])
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        ensure_seeded()
        self.client = app.test_client()
        self.manage = f"/dashboard/events/{EVENT_ID}/prismlabs"

    def tearDown(self):
        app.config.update(TESTING=self.previous[0], SESSION_COOKIE_SECURE=self.previous[1])
        self.data_patch.stop()
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session.setdefault("csrf_token", secrets.token_urlsafe(24))
            return session["csrf_token"]

    @staticmethod
    def readme_password(username):
        for line in Path("README.md").read_text(encoding="utf-8").splitlines():
            if f"`{username}`" in line:
                return line.split("|")[3].strip().strip("`")
        raise AssertionError("The temporary SkillMatch login is missing from README.md.")

    def sign_in(self, username):
        password = self.readme_password(username)
        response = self.client.post("/login", data={
            "username": username, "password": password, "csrf_token": self.csrf(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/account/password"))
        self.assertEqual(self.client.get("/account/password").status_code, 200)
        response = self.client.post("/account/password", data={
            "current_password": password,
            "new_password": f"Changed{secrets.token_hex(6)}9!",
            "confirm_password": "",
            "csrf_token": self.csrf(),
        })
        self.assertEqual(response.status_code, 400)
        new_password = f"Changed{secrets.token_hex(6)}9!"
        response = self.client.post("/account/password", data={
            "current_password": password,
            "new_password": new_password,
            "confirm_password": new_password,
            "csrf_token": self.csrf(),
        })
        self.assertEqual(response.status_code, 302)

    def test_seed_uses_normal_accounts_and_event_without_separate_auth(self):
        users = _doc("users.json")
        self.assertEqual(users[ORGANIZER_USERNAME]["role"], "Organizer")
        self.assertEqual(users[ORGANIZER_USERNAME]["name"], "PrismLabs Event Coordinator")
        self.assertEqual(users[TEST_VOLUNTEER_USERNAME]["role"], "Student Volunteer")
        self.assertEqual(_doc("prismlabs_accounts.json"), {})
        event = _doc("events.json")[EVENT_ID]
        self.assertEqual(event["organizer"], ORGANIZER_USERNAME)
        self.assertEqual(event["status"], "completed")
        self.assertIsNone(event["date"])
        self.assertEqual(len(_doc("prismlabs_volunteers.json")), 98)
        self.assertEqual(len(_doc("prismlabs_certificates.json")), 98)
        self.assertEqual(own_profile(TEST_VOLUNTEER_USERNAME)["volunteer"]["prn"], "1262251979")
        redirect = self.client.get("/prismlabs/login")
        self.assertEqual(redirect.status_code, 302)
        self.assertTrue(redirect.headers["Location"].endswith("/login"))
        self.assertNotIn("PrismLabs sign in", self.client.get("/login").get_data(as_text=True))

    def test_organizer_uses_existing_dashboard_and_event_owned_roster(self):
        self.assertEqual(self.client.get(self.manage).status_code, 302)
        self.sign_in(ORGANIZER_USERNAME)
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Review participants", dashboard.get_data(as_text=True))
        self.assertNotIn("Review applicants", dashboard.get_data(as_text=True))
        card = dashboard_cards(ORGANIZER_USERNAME)[0]
        self.assertEqual(card["participant_count"], 98)
        self.assertEqual(card["linked_account_count"], 1)
        self.assertEqual(get_organizer_events(ORGANIZER_USERNAME)[0]["accepted_count"], 1)
        old_review = f"/dashboard/events/{EVENT_ID}/applicants"
        self.assertEqual(self.client.get(old_review).headers["Location"], self.manage + "/volunteers")
        htmx_review = self.client.get(old_review, headers={"HX-Request": "true"})
        self.assertEqual(htmx_review.headers["HX-Redirect"], self.manage + "/volunteers")
        self.assertEqual(self.client.get(self.manage).status_code, 200)
        roster = self.client.get(self.manage + "/volunteers")
        self.assertEqual(roster.status_code, 200)
        self.assertEqual(roster.get_data(as_text=True).count("<tr>"), 99)
        export = self.client.get(self.manage + "/volunteers.csv")
        self.assertEqual(export.mimetype, "text/csv")
        self.assertEqual(self.client.post(self.manage + "/event", data={
            "csrf_token": self.csrf(), "name": "Team Prismlabs Recruitment",
            "date": "2026-08-04", "venue": "MIT-WPU",
            "description": "A completed PrismLabs volunteering event.",
        }).status_code, 303)
        profile = own_profile(TEST_VOLUNTEER_USERNAME)
        prn = profile["volunteer"]["prn"]
        self.client.post(self.manage + f"/volunteers/{prn}/hours", data={
            "csrf_token": self.csrf(), "hours": "12",
        })
        payload = certificate_for_id(profile["certificate"]["certificate_id"])
        pdf = render_prism_certificate(payload, "https://example.com/verify/test")
        text = subprocess.run(
            ["pdftotext", "-", "-"], input=pdf, capture_output=True, check=True,
        ).stdout.decode()
        self.assertTrue("Event date: August 4, 2026" in text)
        self.assertTrue("12 volunteer hours" in text)
        self.assertIn(b"/Subtype /Image", pdf)
        self.assertNotIn(payload["record"]["certificate_id"], text)
        users = _doc("users.json")
        users["another-organizer"] = {"role": "Organizer", "name": "Another Organizer", "skills": {}}
        data_store.save("users.json", users)
        with self.client.session_transaction() as session:
            session.clear()
            session["username"] = "another-organizer"
        self.assertEqual(self.client.get(self.manage).status_code, 404)

    def test_volunteer_uses_existing_profile_and_only_own_certificate(self):
        self.sign_in(TEST_VOLUNTEER_USERNAME)
        profile = self.client.get("/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertIn("PrismLabs event participation", profile.get_data(as_text=True))
        self.assertEqual(self.client.get(self.manage).status_code, 403)
        event = self.client.get(f"/events/{EVENT_ID}")
        self.assertEqual(event.status_code, 200)
        text = event.get_data(as_text=True)
        self.assertIn("Date: Not recorded", text)
        self.assertIn("1 Aug 2026 – 5 Aug 2026", text)
        self.assertNotIn("1262251979", text)
        self.assertIn("Team Prismlabs Recruitment", self.client.get("/events/discover").get_data(as_text=True))
        self.assertNotIn(
            "Team Prismlabs Recruitment",
            self.client.get("/events/discover?search=unrelated-test-search").get_data(as_text=True),
        )
        pdf = self.client.get("/profile/prismlabs/certificate.pdf")
        self.assertEqual(pdf.mimetype, "application/pdf")
        extracted = subprocess.run(
            ["pdftotext", "-", "-"], input=pdf.data, capture_output=True, check=True,
        ).stdout.decode()
        self.assertTrue("Hours: N/A" in extracted)
        self.assertTrue("Event date:" not in extracted)
        self.assertTrue("Issued on:" in extracted)
        certificate = own_profile(TEST_VOLUNTEER_USERNAME)["certificate"]
        certificate_id = certificate["certificate_id"]
        self.assertIn(b"/Subtype /Image", pdf.data)
        self.assertNotIn(certificate_id, extracted)
        self.assertIn(certificate_id, profile.get_data(as_text=True))
        listing = self.client.get("/certificates")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(certificate_id, listing.get_data(as_text=True))
        self.assertIn("Hours: N/A", listing.get_data(as_text=True))
        generic_download = self.client.get(f"/certificates/{certificate_id}/download")
        self.assertEqual(generic_download.mimetype, "application/pdf")
        self.assertEqual(generic_download.data, pdf.data)
        form_verify = self.client.post("/verify", data={"verification_code": certificate_id})
        self.assertEqual(form_verify.status_code, 303)
        self.assertTrue(form_verify.headers["Location"].endswith(f"/verify/{certificate_id}"))
        verify = self.client.get(f"/verify/{certificate['certificate_id']}")
        self.assertEqual(verify.status_code, 200)
        self.assertNotIn("1262251979", verify.get_data(as_text=True))
        self.assertEqual(self.client.get("/verify/not-a-certificate").status_code, 404)

    def test_other_students_cannot_list_or_download_a_roster_certificate(self):
        certificate_id = own_profile(TEST_VOLUNTEER_USERNAME)["certificate"]["certificate_id"]
        users = _doc("users.json")
        users["unlinked-student"] = {
            "name": "Unlinked Student", "role": "Student Volunteer", "dept": "CSE", "skills": {},
        }
        data_store.save("users.json", users)
        with self.client.session_transaction() as session:
            session["username"] = "unlinked-student"
        self.assertNotIn(certificate_id, self.client.get("/certificates").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/certificates/{certificate_id}/download").status_code, 404)
        self.assertEqual(self.client.get("/profile/prismlabs/certificate.pdf").status_code, 404)
        self.assertEqual(self.client.get(self.manage + "/volunteers").status_code, 403)

    def test_optional_event_image_validation_and_removal(self):
        self.sign_in(ORGANIZER_USERNAME)
        self.assertIn(b'name="image_url"', self.client.get(self.manage).data)
        form = {
            "csrf_token": self.csrf(), "name": "Team Prismlabs Recruitment",
            "date": "", "venue": "MIT-WPU", "description": "Past volunteering event.",
            "image_url": "https://images.example.org/prismlabs.jpg",
        }
        self.assertEqual(self.client.post(self.manage + "/event", data=form).status_code, 303)
        self.assertIn(b"prismlabs.jpg", self.client.get(f"/events/{EVENT_ID}").data)
        before = _doc("events.json")[EVENT_ID]
        self.client.post(self.manage + "/event", data={**form, "image_url": "javascript:bad"})
        self.assertEqual(_doc("events.json")[EVENT_ID], before)
        self.client.post(self.manage + "/event", data={**form, "image_url": ""})
        self.assertNotIn(b'<figure class="event-visual', self.client.get(f"/events/{EVENT_ID}").data)

    def test_csv_period_deduplication_and_review_flags_are_preserved(self):
        volunteers, first, period = _csv_volunteers()
        self.assertEqual(len(volunteers), 98)
        self.assertEqual(first[0], "1262251979")
        self.assertEqual(period, ("2026-08-01", "2026-08-05"))
        self.assertTrue(all(row["hours"] is None for row in volunteers.values()))
        self.assertTrue(any(row["needs_admin_review"] for row in volunteers.values()))

    def test_legacy_portal_migrates_without_losing_event_edits_or_certificate_ids(self):
        certificates_before = set(_doc("prismlabs_certificates.json"))
        event = dict(_doc("events.json")[EVENT_ID])
        event["date"] = "2026-08-04"
        event["venue"] = "Recorded venue"
        users = _doc("users.json")
        data_store.save("prismlabs_events.json", {EVENT_ID: event})
        data_store.save("prismlabs_accounts.json", {
            "admin@prismlabs.test": {"password_hash": users[ORGANIZER_USERNAME]["password_hash"]},
            TEST_VOLUNTEER_USERNAME: {"password_hash": users[TEST_VOLUNTEER_USERNAME]["password_hash"]},
        })
        data_store.save("events.json", {})
        data_store.save("users.json", {"existing-user": {"role": "Organizer", "name": "Existing Organizer"}})
        data_store.save("prismlabs_seed.json", {
            "seeded": True, "registration_period": ["2026-08-01", "2026-08-05"],
        })
        ensure_seeded()
        self.assertIn("existing-user", _doc("users.json"))
        self.assertEqual(_doc("events.json")[EVENT_ID]["date"], "2026-08-04")
        self.assertEqual(_doc("events.json")[EVENT_ID]["location"], "Recorded venue")
        self.assertEqual(set(_doc("prismlabs_certificates.json")), certificates_before)
        self.assertEqual(_doc("prismlabs_accounts.json"), {})
        self.assertEqual(_doc("prismlabs_events.json"), {})


if __name__ == "__main__":
    unittest.main()
