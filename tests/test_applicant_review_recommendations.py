"""Explainable applicant fit ordering and organizer-controlled batch acceptance."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
import notification_service
from app import app
from dashboard_service import get_event_applicants, update_application_status


class ApplicantReviewRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-applicant-review-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save("users.json", {
            "organizer": {
                "name": "Event Organizer",
                "role": "Organizer",
                "skills": {},
            },
            "other_organizer": {
                "name": "Other Organizer",
                "role": "Organizer",
                "skills": {},
            },
            "volunteer_high": {
                "name": "Zoe Strong",
                "role": "Student Volunteer",
                "dept": "Design",
                "email": "not-used-for-ranking@example.com",
                "skills": {"Python": 4, "Design": 3},
            },
            "tie_z": {
                "name": "Zulu First",
                "role": "Student Volunteer",
                "dept": "Design",
                "skills": {"Python": 2},
            },
            "volunteer_low": {
                "name": "Alpha Last",
                "role": "Student Volunteer",
                "dept": "Computer Science",
                "skills": {"Python": 1},
            },
            "tie_a": {
                "name": "Alpha Second",
                "role": "Student Volunteer",
                "dept": "Arts",
                "skills": {"Python": 2},
            },
        })
        data_store.save("events.json", {
            "e1": {
                "name": "Campus Festival",
                "description": "Support the annual festival.",
                "organizer": "organizer",
                "status": "open",
                "required_skills": {"Python": 2, "Design": 2},
                "slots_needed": 3,
            }
        })
        data_store.save("applications.json", {
            "e1": {
                "tie_z": {"status": "pending", "role": None},
                "volunteer_low": {"status": "pending", "role": None},
                "tie_a": {"status": "pending", "role": None},
                "volunteer_high": {"status": "pending", "role": None},
            }
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

    def post_batch(self, selected, roles, confirm=False):
        data = {
            "csrf_token": "test-csrf",
            "volunteer_id": selected,
        }
        data.update({f"role_{username}": role for username, role in roles.items()})
        if confirm:
            data["confirm"] = "yes"
        return self.client.post(
            "/dashboard/events/e1/applicants/bulk-accept",
            data=data,
            headers={"HX-Request": "true"},
        )

    def test_pending_applicants_are_ranked_by_skill_fit_with_stable_ties(self):
        applicants = get_event_applicants("organizer", "e1")["applicants"]

        self.assertEqual(
            [applicant["username"] for applicant in applicants],
            ["volunteer_high", "tie_z", "tie_a", "volunteer_low"],
        )
        self.assertEqual(
            [(applicant["fit_met_count"], applicant["fit_total_count"],
              applicant["fit_percent"]) for applicant in applicants],
            [(2, 2, 100), (1, 2, 50), (1, 2, 50), (0, 2, 0)],
        )

    def test_large_applicant_list_is_ranked_without_using_personal_fields(self):
        users = data_store.load("users.json")
        applications = {}
        for index in range(120):
            username = f"student_{index:03d}"
            users[username] = {
                "name": f"Student {119 - index:03d}",
                "role": "Student Volunteer",
                "dept": "Department that must not affect ranking",
                "email": f"person{index}@example.com",
                "skills": {"Python": 1 if index % 2 == 0 else 0},
            }
            applications[username] = {"status": "pending", "role": None}
        data_store.save("users.json", users)
        data_store.save("events.json", {
            "e1": {
                **data_store.load("events.json")["e1"],
                "required_skills": {"Python": 1},
            }
        })
        data_store.save("applications.json", {"e1": applications})

        ranked = get_event_applicants("organizer", "e1")["applicants"]

        self.assertEqual(len(ranked), 120)
        self.assertTrue(all(item["fit_percent"] == 100 for item in ranked[:60]))
        self.assertTrue(all(item["fit_percent"] == 0 for item in ranked[60:]))
        self.assertEqual(ranked[0]["username"], "student_000")

    def test_batch_preview_then_confirmation_accepts_only_selected_and_notifies(self):
        preview = self.post_batch(
            ["volunteer_high", "tie_z"],
            {
                "volunteer_high": "Festival lead",
                "tie_z": "Stage assistant",
            },
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Confirm batch acceptance", preview.data)
        self.assertIn(b"Skill fit: 2 of", preview.data)
        self.assertIn(b"Confirm and accept 2", preview.data)
        self.assertTrue(all(
            application["status"] == "pending"
            for application in data_store.load("applications.json")["e1"].values()
        ))
        self.assertEqual(notification_service.get_user_notifications("volunteer_high"), [])

        confirmed = self.post_batch(
            ["volunteer_high", "tie_z"],
            {
                "volunteer_high": "Festival lead",
                "tie_z": "Stage assistant",
            },
            confirm=True,
        )

        self.assertEqual(confirmed.status_code, 200)
        self.assertIn(b"2 applicant(s) accepted", confirmed.data)
        applications = data_store.load("applications.json")["e1"]
        self.assertEqual(applications["volunteer_high"]["status"], "accepted")
        self.assertEqual(applications["volunteer_high"]["role"], "Festival lead")
        self.assertEqual(applications["tie_z"]["status"], "accepted")
        self.assertEqual(applications["tie_z"]["role"], "Stage assistant")
        self.assertEqual(applications["volunteer_low"]["status"], "pending")
        self.assertEqual(
            notification_service.get_user_notifications("volunteer_high")[0]["kind"],
            "application_accepted",
        )
        self.assertEqual(
            notification_service.get_user_notifications("tie_z")[0]["kind"],
            "application_accepted",
        )
        self.assertEqual(notification_service.get_user_notifications("volunteer_low"), [])

    def test_batch_capacity_is_checked_at_preview_and_again_at_confirmation(self):
        events = data_store.load("events.json")
        events["e1"]["slots_needed"] = 1
        data_store.save("events.json", events)

        too_many = self.post_batch(
            ["volunteer_high", "tie_z"],
            {"volunteer_high": "Lead", "tie_z": "Assistant"},
        )
        self.assertIn(b"Only 1 volunteer slot(s) remain", too_many.data)
        self.assertTrue(all(
            application["status"] == "pending"
            for application in data_store.load("applications.json")["e1"].values()
        ))

        preview = self.post_batch(
            ["tie_z"], {"tie_z": "Stage assistant"}
        )
        self.assertIn(b"Confirm batch acceptance", preview.data)
        update_application_status(
            "organizer", "e1", "volunteer_high", "accepted", "Festival lead"
        )
        confirmation = self.post_batch(
            ["tie_z"], {"tie_z": "Stage assistant"}, confirm=True
        )

        self.assertIn(b"Only 0 volunteer slot(s) remain", confirmation.data)
        applications = data_store.load("applications.json")["e1"]
        self.assertEqual(applications["volunteer_high"]["status"], "accepted")
        self.assertEqual(applications["tie_z"]["status"], "pending")

    def test_batch_requires_selection_and_role_and_does_not_accept_rejected_people(self):
        no_selection = self.post_batch([], {})
        self.assertIn(b"Select at least one pending applicant", no_selection.data)

        missing_role = self.post_batch(["volunteer_high"], {})
        self.assertIn(b"role", missing_role.data.lower())
        self.assertEqual(
            data_store.load("applications.json")["e1"]["volunteer_high"]["status"],
            "pending",
        )

        applications = data_store.load("applications.json")
        applications["e1"]["volunteer_high"]["status"] = "rejected"
        data_store.save("applications.json", applications)
        rejected = self.post_batch(
            ["volunteer_high"], {"volunteer_high": "Festival lead"}
        )
        self.assertIn(b"Only pending applicants can be batch accepted", rejected.data)
        self.assertEqual(
            data_store.load("applications.json")["e1"]["volunteer_high"]["status"],
            "rejected",
        )

    def test_only_event_owner_organizer_can_open_batch_review(self):
        self.sign_in("volunteer_high")
        self.assertEqual(
            self.client.get("/dashboard/events/e1/applicants").status_code, 403
        )
        self.assertEqual(
            self.post_batch(
                ["tie_z"], {"tie_z": "Assistant"}
            ).status_code,
            403,
        )

        self.sign_in("other_organizer")
        self.assertEqual(
            self.client.get("/dashboard/events/e1/applicants").status_code, 404
        )
        self.assertEqual(
            self.post_batch(
                ["tie_z"], {"tie_z": "Assistant"}
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
