"""Friendly error, empty dashboard, and HTMX loading state checks."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from app import app, internal_server_error


class ErrorAndLoadingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-ui-states-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key]
            for key in ("TESTING", "SESSION_COOKIE_SECURE", "PROPAGATE_EXCEPTIONS")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save("users.json", {
            "organizer": {"name": "Test Organizer", "role": "Organizer", "skills": {}},
        })
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "organizer"
            session["csrf_token"] = "test-csrf"

    def tearDown(self):
        app.config.update(self.old_config)
        self.storage.stop()
        self.temp.cleanup()

    def test_custom_500_handler_renders_friendly_page_without_exception_details(self):
        with app.test_request_context("/"):
            response_body, status = internal_server_error(None)
        self.assertEqual(status, 500)
        self.assertIn("We hit a snag", response_body)
        self.assertIn("Back to SkillMatch", response_body)
        self.assertNotIn("Traceback", response_body)

    def test_organizer_dashboard_empty_state_links_to_create_event(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You haven\xe2\x80\x99t posted any events yet", response.data)
        self.assertIn(b'href="/dashboard/events/new"', response.data)
        self.assertIn(b"Create your first event", response.data)

    def test_event_search_has_skeleton_cards_during_htmx_requests(self):
        response = self.client.get("/events/discover")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="event-loading"', response.data)
        self.assertIn(b'class="team-card skeleton-card"', response.data)
        self.assertIn(b"Loading matching events", response.data)

        with self.client.get("/static/style.css") as css:
            self.assertEqual(css.status_code, 200)
            self.assertIn(b"#discover-filters.htmx-request + .event-loading", css.data)
            self.assertIn(b"@keyframes skeleton-shimmer", css.data)


if __name__ == "__main__":
    unittest.main()
