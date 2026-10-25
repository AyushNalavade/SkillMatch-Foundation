"""Role-aware registration and profile checks using disposable storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth_service
import data_store
from app import app


class OrganizerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-profile-tests-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()
        self.client.get("/register")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def test_organizer_signup_ignores_department_and_skills_on_server(self):
        response = self.client.post("/register", data={
            "username": "organizer",
            "name": "Organizer One",
            "password": "safe-password",
            "role": "Organizer",
            "dept": "Ignore this",
            "skills": "not a valid skill list",
            "csrf_token": self.csrf,
        })
        self.assertEqual(response.status_code, 302)
        account = data_store.load("users.json")["organizer"]
        self.assertIsNone(account["dept"])
        self.assertEqual(account["skills"], {})
        self.assertIn("password_hash", account)

        profile = self.client.get("/profile")
        self.assertIn(b"Profile 100% complete", profile.data)
        editing = self.client.get("/profile/edit")
        self.assertNotIn(b'name="dept"', editing.data)
        self.assertNotIn(b'name="skills"', editing.data)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/events/discover").status_code, 200)

    def test_role_change_returns_only_applicable_fields_without_javascript(self):
        organizer = self.client.get("/register/profile-fields?role=Organizer")
        self.assertEqual(organizer.status_code, 200)
        self.assertNotIn(b'name="dept"', organizer.data)
        self.assertNotIn(b'name="skills"', organizer.data)
        student = self.client.get("/register/profile-fields?role=Student+Volunteer")
        self.assertIn(b'name="dept"', student.data)
        self.assertIn(b'name="skills"', student.data)
        self.assertIn(b"required", student.data)
        self.assertIn(
            b'hx-get="/register/profile-fields"', self.client.get("/register").data
        )
        organizer_page = self.client.get("/register?role=Organizer")
        self.assertNotIn(b'name="dept"', organizer_page.data)
        self.assertNotIn(b'name="skills"', organizer_page.data)

    def test_organizer_service_normalizes_unused_profile_fields(self):
        success, message = auth_service.register_user(
            "organizer", "safe-password", "Organizer", "Organizer One",
            dept="Anything", skills={"Invalid": True},
        )
        self.assertTrue(success, message)
        account = data_store.load("users.json")["organizer"]
        self.assertIsNone(account["dept"])
        self.assertEqual(account["skills"], {})

    def test_student_signup_requires_department_and_skills(self):
        for dept, skills, expected in (
            (None, {"Python": 2}, "department"),
            ("CSE", {}, "skill"),
            ("CSE", None, "skill"),
        ):
            success, message = auth_service.register_user(
                "student", "safe-password", "Student Volunteer", "Student One",
                dept=dept, skills=skills,
            )
            self.assertFalse(success)
            self.assertIn(expected, message)
        self.assertEqual(data_store.load("users.json"), {})
        success, message = auth_service.register_user(
            "student", "safe-password", "Student Volunteer", "Student One",
            dept="CSE", skills={"Python": 2},
        )
        self.assertTrue(success, message)

    def test_organizer_profile_save_keeps_other_record_fields(self):
        data_store.save("users.json", {"organizer": {
            "name": "Organizer", "role": "Organizer", "dept": "Old",
            "skills": {"Old": 2}, "saved_events": ["e1"], "custom": "keep",
        }})
        with self.client.session_transaction() as session:
            session["username"] = "organizer"
        response = self.client.post("/profile/edit", data={
            "name": "Renamed Organizer", "dept": "Ignore",
            "skills": "not a skill", "csrf_token": self.csrf,
        })
        self.assertEqual(response.status_code, 302)
        account = data_store.load("users.json")["organizer"]
        self.assertIsNone(account["dept"])
        self.assertEqual(account["skills"], {})
        self.assertEqual(account["saved_events"], ["e1"])
        self.assertEqual(account["custom"], "keep")

    def test_student_profile_cannot_remove_required_fields(self):
        account = {
            "name": "Student", "role": "Student Volunteer",
            "dept": "CSE", "skills": {"Python": 2},
        }
        data_store.save("users.json", {"student": account})
        with self.client.session_transaction() as session:
            session["username"] = "student"
        for dept, skills, message in (
            ("", "Python:2", b"Enter your department"),
            ("CSE", "", b"Add at least one skill"),
        ):
            response = self.client.post("/profile/edit", data={
                "name": "Student", "dept": dept, "skills": skills,
                "csrf_token": self.csrf,
            })
            self.assertIn(message, response.data)
            self.assertEqual(data_store.load("users.json")["student"], account)


if __name__ == "__main__":
    unittest.main()
