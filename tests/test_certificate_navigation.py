"""Certificate navigation and access checks using disposable storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from app import app


class CertificateNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-cert-nav-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save(
            "users.json",
            {
                "organizer": {
                    "name": "Test Organizer",
                    "role": "Organizer",
                    "skills": {},
                },
                "volunteer": {
                    "name": "Test Volunteer",
                    "role": "Student Volunteer",
                    "skills": {},
                },
                "other": {
                    "name": "Other User",
                    "role": "Other",
                    "skills": {},
                },
            },
        )
        self.client = app.test_client()

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def sign_in_as(self, username):
        with self.client.session_transaction() as session:
            session["username"] = username

    def test_public_verification_is_linked_and_available_without_sign_in(self):
        home = self.client.get("/")
        self.assertIn(b'href="/verify">Verify certificate</a>', home.data)
        self.assertNotIn(b'href="/certificates">Certificates</a>', home.data)

        verify = self.client.get("/verify")
        self.assertEqual(verify.status_code, 200)
        self.assertIn(b"Verify certificate", verify.data)

    def test_organizers_and_volunteers_can_find_their_certificate_workspace(self):
        for username in ("organizer", "volunteer"):
            with self.subTest(username=username):
                self.sign_in_as(username)
                home = self.client.get("/")
                self.assertIn(b'href="/certificates">Certificates</a>', home.data)
                self.assertEqual(self.client.get("/certificates").status_code, 200)

    def test_certificate_workspace_remains_restricted(self):
        self.assertEqual(self.client.get("/certificates").status_code, 302)
        self.sign_in_as("other")
        home = self.client.get("/")
        self.assertNotIn(b'href="/certificates">Certificates</a>', home.data)
        self.assertEqual(self.client.get("/certificates").status_code, 403)


if __name__ == "__main__":
    unittest.main()
