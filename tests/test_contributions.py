"""Organizer-only contribution charts and access-control tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from app import app


class ContributionAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-contributions-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save("users.json", {
            "organizer": {"name": "Test Organizer", "role": "Organizer"},
            "other_organizer": {"name": "Other Organizer", "role": "Organizer"},
            "volunteer": {"name": "A very long volunteer name that should wrap cleanly", "role": "Student Volunteer"},
            "pending": {"name": "Pending Applicant", "role": "Student Volunteer"},
        })
        data_store.save("events.json", {
            "e1": {
                "name": "Campus Cleanup",
                "organizer": "organizer",
                "status": "completed",
                "slots_needed": 4,
            },
            "e2": {
                "name": "Private Event",
                "organizer": "other_organizer",
                "status": "open",
                "slots_needed": 2,
            },
        })
        data_store.save("applications.json", {
            "e1": {
                "volunteer": {
                    "status": "accepted",
                    "attended": True,
                    "hours": 3.5,
                },
                "pending": {
                    "status": "pending",
                    "attended": False,
                    "hours": 0,
                },
            }
        })
        self.client = app.test_client()

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def sign_in(self, username):
        with self.client.session_transaction() as session:
            session["username"] = username
            session["csrf_token"] = "test-csrf"

    def test_logged_out_and_student_accounts_are_blocked(self):
        self.assertEqual(
            self.client.get("/dashboard/events/e1/contributions").status_code,
            302,
        )
        self.sign_in("volunteer")
        self.assertEqual(
            self.client.get("/dashboard/events/e1/contributions").status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/dashboard/events/e1/contributions/volunteers.png"
            ).status_code,
            403,
        )

    def test_organizer_gets_contribution_page_and_png_charts(self):
        self.sign_in("organizer")
        page = self.client.get("/dashboard/events/e1/contributions")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Campus Cleanup contributions", page.data)
        self.assertIn(b"Horizontal bar chart", page.data)

        for path in (
            "/dashboard/events/e1/contributions/volunteers.png",
            "/dashboard/events/e1/contributions/attendance.png",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/png")
                self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_organizer_cannot_view_another_organizers_event_or_chart(self):
        self.sign_in("organizer")
        self.assertEqual(
            self.client.get("/dashboard/events/e2/contributions").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/dashboard/events/e2/contributions/volunteers.png"
            ).status_code,
            404,
        )

    def test_no_applicants_uses_empty_state(self):
        data_store.save("applications.json", {})
        self.sign_in("organizer")
        response = self.client.get("/dashboard/events/e1/contributions")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"There are no applicants to show", response.data)

    def test_pending_only_event_shows_no_attendance_empty_state(self):
        data_store.save("applications.json", {
            "e1": {
                "pending": {
                    "status": "pending",
                    "attended": False,
                    "hours": 0,
                },
            },
        })
        self.sign_in("organizer")
        response = self.client.get("/dashboard/events/e1/contributions")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No attendance has been recorded yet", response.data)
        self.assertIn(b"1 pending applicants", response.data)
        chart = self.client.get("/dashboard/events/e1/contributions/attendance.png")
        self.assertEqual(chart.status_code, 200)

    def test_dashboard_links_each_event_to_contributions(self):
        self.sign_in("organizer")
        response = self.client.get("/dashboard")
        self.assertIn(b"View Contributions", response.data)
        self.assertIn(b"/dashboard/events/e1/contributions", response.data)


if __name__ == "__main__":
    unittest.main()
