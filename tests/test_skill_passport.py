"""Private verified skill passport tests using isolated document storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import certificate_service
import data_store
from app import app


class SkillPassportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-passport-tests-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key]
            for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()
        data_store.save("users.json", {
            "volunteer": {
                "name": "Amelia Johnson",
                "role": "Student Volunteer",
                "dept": "Media and Communication",
                "skills": {"Photography": 4, "Graphic Design": 5},
            },
            "other_volunteer": {
                "name": "Jordan Lee",
                "role": "Student Volunteer",
                "dept": "Computer Science",
                "skills": {"Python": 5},
            },
            "organizer": {
                "name": "Organizer One",
                "role": "Organizer",
                "dept": None,
                "skills": {},
            },
        })
        data_store.save("events.json", {
            "completed-event": {
                "name": "Campus Photo Walk",
                "status": "completed",
                "date": "2026-10-06",
                "completed_at": "2026-10-07T12:00:00+00:00",
                "required_skills": {"Photography": 3},
            },
            "completed-no-attendance": {
                "name": "No Show Event",
                "status": "completed",
                "required_skills": {"Graphic Design": 2},
            },
            "still-open": {
                "name": "Upcoming Event",
                "status": "open",
                "required_skills": {"Photography": 2},
            },
            "missing-role": {
                "name": "Role Not Assigned",
                "status": "completed",
                "required_skills": {"Photography": 2},
            },
            "other-volunteer-event": {
                "name": "Python Workshop",
                "status": "completed",
                "required_skills": {"Python": 2},
            },
            "hidden-history": {
                "name": "Archived Photography Event",
                "status": "completed",
                "deleted_at": "2026-10-08T12:00:00+00:00",
                "required_skills": {"Photography": 2},
            },
        })
        data_store.save("applications.json", {
            "completed-event": {
                "volunteer": {
                    "status": "accepted",
                    "role": "Photo Lead",
                    "attended": True,
                    "hours": 3.5,
                },
                "other_volunteer": {
                    "status": "rejected",
                    "role": None,
                    "hours": 0,
                },
            },
            "completed-no-attendance": {
                "volunteer": {
                    "status": "accepted",
                    "role": "Design Assistant",
                    "attended": False,
                    "hours": 0,
                },
            },
            "still-open": {
                "volunteer": {
                    "status": "accepted",
                    "role": "Photographer",
                    "attended": True,
                    "hours": 20,
                },
            },
            "missing-role": {
                "volunteer": {
                    "status": "accepted",
                    "role": None,
                    "attended": True,
                    "hours": 8,
                },
            },
            "other-volunteer-event": {
                "other_volunteer": {
                    "status": "accepted",
                    "role": "Workshop Assistant",
                    "attended": True,
                    "hours": 2,
                },
            },
            "hidden-history": {
                "volunteer": {
                    "status": "accepted",
                    "role": "Photo Editor",
                    "attended": True,
                    "hours": 1,
                },
            },
        })
        data_store.save("certificates.json", {
            "ABCDEF1234": {
                "verification_code": "ABCDEF1234",
                "event_id": "completed-event",
                "username": "volunteer",
            },
            "OTHER12345": {
                "verification_code": "OTHER12345",
                "event_id": "other-volunteer-event",
                "username": "other_volunteer",
            },
        })

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def sign_in(self, username):
        with self.client.session_transaction() as session:
            session["username"] = username

    def test_passport_requires_completed_accepted_role_and_contribution(self):
        passport = certificate_service.get_skill_passport("volunteer")

        self.assertEqual(passport["completed_event_count"], 2)
        self.assertEqual(passport["total_hours"], 4.5)
        self.assertEqual(
            [item["name"] for item in passport["verified_skills"]],
            ["Photography"],
        )
        evidence = passport["verified_skills"][0]["evidence"]
        self.assertEqual(
            {item["event_id"] for item in evidence},
            {"completed-event", "hidden-history"},
        )
        photo_walk = next(item for item in evidence if item["event_id"] == "completed-event")
        self.assertEqual(photo_walk["role"], "Photo Lead")
        self.assertEqual(photo_walk["hours"], 3.5)
        self.assertEqual(photo_walk["certificate_code"], "ABCDEF1234")
        archived = next(item for item in evidence if item["event_id"] == "hidden-history")
        self.assertFalse(archived["event_linkable"])
        self.assertIsNone(archived["certificate_code"])

    def test_self_reported_skill_without_completed_event_evidence_is_not_verified(self):
        passport = certificate_service.get_skill_passport("volunteer")
        verified_names = {item["name"] for item in passport["verified_skills"]}

        self.assertIn("Graphic Design", data_store.load("users.json")["volunteer"]["skills"])
        self.assertNotIn("Graphic Design", verified_names)
        self.assertNotIn("Python", verified_names)

    def test_profile_shows_only_signed_in_volunteers_own_passport(self):
        self.sign_in("volunteer")
        profile = self.client.get("/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertIn(b"Verified Skill Passport", profile.data)
        self.assertIn(b"Photography", profile.data)
        self.assertIn(b"Photo Lead", profile.data)
        self.assertIn(b"Download certificate", profile.data)
        self.assertNotIn(b"Workshop Assistant", profile.data)
        self.assertNotIn(b"Python Workshop", profile.data)

        self.sign_in("other_volunteer")
        other_profile = self.client.get("/profile")
        self.assertEqual(other_profile.status_code, 200)
        self.assertIn(b"Python", other_profile.data)
        self.assertNotIn(b"Campus Photo Walk", other_profile.data)
        self.assertNotIn(b"ABCDEF1234", other_profile.data)

    def test_organizers_and_logged_out_users_cannot_view_a_volunteer_passport(self):
        anonymous = app.test_client().get("/profile")
        self.assertEqual(anonymous.status_code, 302)

        self.sign_in("organizer")
        organizer_profile = self.client.get("/profile")
        self.assertEqual(organizer_profile.status_code, 200)
        self.assertNotIn(b"Verified Skill Passport", organizer_profile.data)
        self.assertNotIn(b"Campus Photo Walk", organizer_profile.data)

    def test_missing_documents_return_an_empty_passport(self):
        data_store.save("events.json", {})
        data_store.save("applications.json", {})
        passport = certificate_service.get_skill_passport("volunteer")

        self.assertEqual(passport["completed_event_count"], 0)
        self.assertEqual(passport["total_hours"], 0)
        self.assertEqual(passport["verified_skills"], [])
        self.assertEqual(passport["contributions"], [])

    def test_profile_shows_empty_state_until_verified_evidence_exists(self):
        data_store.save("events.json", {})
        data_store.save("applications.json", {})
        self.sign_in("volunteer")

        response = self.client.get("/profile")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Verified Skill Passport", response.data)
        self.assertIn(b"Verified skills will appear after a completed event", response.data)

    def test_edit_profile_does_not_show_the_passport(self):
        self.sign_in("volunteer")

        response = self.client.get("/profile/edit")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Verified Skill Passport", response.data)


if __name__ == "__main__":
    unittest.main()
