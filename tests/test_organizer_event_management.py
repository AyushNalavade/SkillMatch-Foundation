"""Organizer event form and ownership checks using disposable storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from app import app
from dashboard_service import (
    compare_applicant_skills,
    dashboard_cards,
    organizer_event_form_values,
)


class OrganizerEventManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-organizer-events-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save("users.json", {
            "organizer": {
                "name": "Test Organizer",
                "role": "Organizer",
                "skills": {},
            },
            "volunteer": {
                "name": "Test Volunteer",
                "role": "Student Volunteer",
                "skills": {"Python": 2},
            },
            "other_organizer": {
                "name": "Another Organizer",
                "role": "Organizer",
                "skills": {},
            },
        })
        self.client = app.test_client()
        self.sign_in("organizer")

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def sign_in(self, username):
        with self.client.session_transaction() as session:
            session["username"] = username
            session["csrf_token"] = "test-csrf"

    @staticmethod
    def form(**overrides):
        values = {
            "title": "Campus Workshop",
            "description": "A hands-on campus volunteering event.",
            "required_skills": "Python: 2\nEvent planning: 1",
            "department": "Computer Science",
            "event_type": "Workshop",
            "date": "2030-03-10",
            "start_time": "10:30",
            "venue": "The Dome",
            "duration": "2.5",
            "seats": "12",
            "csrf_token": "test-csrf",
        }
        values.update(overrides)
        return values

    def test_organizer_can_create_and_dashboard_shows_success_toast(self):
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Create event", dashboard.data)
        self.assertEqual(self.client.get("/dashboard/events/new").status_code, 200)

        response = self.client.post("/dashboard/events/new", data=self.form())
        self.assertEqual(response.status_code, 303)
        event = data_store.load("events.json")["e1"]
        self.assertEqual(event["organizer"], "organizer")
        self.assertEqual(event["name"], "Campus Workshop")
        self.assertEqual(event["required_skills"], {"Python": 2, "Event planning": 1})
        self.assertEqual(event["slots_needed"], 12)
        self.assertEqual(event["duration_hours"], 2.5)
        self.assertEqual(event["location"], "The Dome")
        self.assertIn("starts_at", event)
        self.assertIn("ends_at", event)

        dashboard = self.client.get("/dashboard")
        self.assertIn(b"Event created successfully.", dashboard.data)
        self.assertIn(b'href="/dashboard/events/e1/edit"', dashboard.data)

    def test_edit_preserves_owner_id_creation_time_and_applications(self):
        created = self.client.post("/dashboard/events/new", data=self.form())
        self.assertEqual(created.status_code, 303)
        events = data_store.load("events.json")
        original_created_at = events["e1"]["created_at"]
        events["e1"]["legacy_flag"] = "keep"
        data_store.save("events.json", events)
        data_store.save("applications.json", {
            "e1": {"volunteer": {"status": "pending", "role": None, "hours": 0, "notes": ""}}
        })

        page = self.client.get("/dashboard/events/e1/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Campus Workshop", page.data)
        response = self.client.post(
            "/dashboard/events/e1/edit",
            data=self.form(title="Updated Workshop", seats="8"),
        )
        self.assertEqual(response.status_code, 303)

        updated = data_store.load("events.json")["e1"]
        self.assertEqual(updated["name"], "Updated Workshop")
        self.assertEqual(updated["organizer"], "organizer")
        self.assertEqual(updated["slots_needed"], 8)
        self.assertEqual(updated["created_at"], original_created_at)
        self.assertEqual(updated["legacy_flag"], "keep")
        self.assertIn("volunteer", data_store.load("applications.json")["e1"])
        self.assertIn(b"Event changes saved.", self.client.get("/dashboard").data)

    def test_invalid_form_shows_error_without_saving(self):
        response = self.client.post(
            "/dashboard/events/new",
            data=self.form(required_skills="Python without a rating"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"must use the format", response.data)
        self.assertIn(b"Python without a rating", response.data)
        self.assertEqual(data_store.load("events.json"), {})

    def test_non_organizers_and_other_organizers_cannot_manage_event(self):
        created = self.client.post("/dashboard/events/new", data=self.form())
        self.assertEqual(created.status_code, 303)

        self.sign_in("volunteer")
        self.assertEqual(self.client.get("/dashboard/events/new").status_code, 403)
        self.assertEqual(
            self.client.post("/dashboard/events/new", data=self.form()).status_code,
            403,
        )

        self.sign_in("other_organizer")
        self.assertEqual(self.client.get("/dashboard/events/e1/edit").status_code, 404)
        self.assertEqual(
            self.client.post("/dashboard/events/e1/edit", data=self.form()).status_code,
            404,
        )

    def test_creation_requires_csrf(self):
        response = self.client.post("/dashboard/events/new", data=self.form(csrf_token=""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(data_store.load("events.json"), {})

    def test_decimal_durations_are_accepted_and_cleanly_refilled(self):
        for event_id, duration in zip(("e1", "e2", "e3"), ("1.5", "2.25", "0.5")):
            response = self.client.post(
                "/dashboard/events/new", data=self.form(duration=duration)
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                data_store.load("events.json")[event_id]["duration_hours"],
                float(duration),
            )
        values = organizer_event_form_values({"duration_hours": 2.5000001})
        self.assertEqual(values["duration"], "2.5")
        response = self.client.get("/dashboard/events/e1/edit")
        self.assertIn(b'step="any"', response.data)

    def test_dashboard_cards_format_event_summary_and_application_counts(self):
        data_store.save("events.json", {
            "e1": {
                "name": "Campus Workshop",
                "description": "A volunteering event.",
                "organizer": "organizer",
                "status": "open",
                "date": "2030-03-10",
                "slots_needed": 5,
                "required_skills": {"Python": 2},
            },
            "e2": {
                "name": "Undated Event",
                "description": "Date to be announced.",
                "organizer": "organizer",
                "status": "completed",
                "slots_needed": 3,
                "required_skills": {"Python": 1},
            },
            "e3": {
                "name": "Another Organizer's Event",
                "organizer": "other_organizer",
                "status": "open",
                "date": "2030-03-11",
                "slots_needed": 1,
                "required_skills": {},
            },
        })
        data_store.save("applications.json", {
            "e1": {
                "volunteer": {"status": "accepted"},
                "pending_one": {"status": "pending"},
                "pending_two": {"status": "pending"},
                "rejected": {"status": "rejected"},
            },
            "e2": {},
        })

        cards = dashboard_cards("organizer")

        self.assertEqual([card["event_id"] for card in cards], ["e1", "e2"])
        self.assertEqual(
            (cards[0]["day"], cards[0]["month"], cards[0]["status"]),
            ("10", "Mar", "open"),
        )
        self.assertEqual(cards[0]["seats_filled"], 1)
        self.assertEqual(
            [item["count"] for item in cards[0]["applicant_counts"]],
            [2, 1, 1],
        )
        self.assertEqual((cards[1]["day"], cards[1]["month"]), ("", ""))
        self.assertEqual(cards[1]["status"], "completed")
        self.assertEqual(cards[1]["seats_filled"], 0)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"event-cards.css", response.data)
        self.assertIn(b"dashboard-cards.css", response.data)
        self.assertIn(b"event-date-block__day", response.data)
        self.assertIn(b"status-pill dashboard-event-card__status", response.data)
        self.assertIn(b"Pending", response.data)
        self.assertIn(b"Mar", response.data)

    def test_applicant_skill_comparison_is_case_insensitive(self):
        matches = compare_applicant_skills(
            {"pYtHoN": 4, "Design": 1},
            {"Python": 3, "design": 2, "Planning": 0},
        )

        self.assertEqual(
            matches,
            [
                {
                    "name": "Python",
                    "applicant_rating": 4,
                    "required_rating": 3,
                    "met": True,
                },
                {
                    "name": "design",
                    "applicant_rating": 1,
                    "required_rating": 2,
                    "met": False,
                },
                {
                    "name": "Planning",
                    "applicant_rating": None,
                    "required_rating": 0,
                    "met": False,
                },
            ],
        )

    def test_applicant_modal_renders_skill_match_chips(self):
        self.client.post("/dashboard/events/new", data=self.form())
        data_store.save("applications.json", {
            "e1": {"volunteer": {"status": "pending"}}
        })
        users = data_store.load("users.json")
        users["volunteer"]["skills"] = {"pYtHoN": 2}
        data_store.save("users.json", users)
        events = data_store.load("events.json")
        events["e1"]["required_skills"] = {"Python": 2, "Design": 1}
        data_store.save("events.json", events)

        response = self.client.get("/dashboard/events/e1/applicants")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"dashboard-skill-chip--met", response.data)
        self.assertIn(b"dashboard-skill-chip--unmet", response.data)
        self.assertIn(b"Applicant: 2", response.data)
        self.assertIn(b"Not met", response.data)

    def test_invalid_durations_are_rejected_without_saving(self):
        for duration in ("0", "-0.5", "nan", "inf", "not-a-number"):
            with self.subTest(duration=duration):
                response = self.client.post(
                    "/dashboard/events/new", data=self.form(duration=duration)
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(b"duration", response.data.lower())
                self.assertEqual(data_store.load("events.json"), {})


if __name__ == "__main__":
    unittest.main()
