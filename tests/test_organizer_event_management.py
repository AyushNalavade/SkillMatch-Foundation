"""Organizer event form and ownership checks using disposable storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
import notification_service
from app import app
from event_service import find_eligible_events, get_event_eligibility_reason
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

    def test_event_form_uses_searchable_checkboxes_and_explicit_skill_modes(self):
        response = self.client.get("/dashboard/events/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'type="checkbox" name="departments"', b" ".join(response.data.split()))
        self.assertIn(b'type="checkbox" name="required_skill_names"', b" ".join(response.data.split()))
        self.assertNotIn(b"Ctrl/Command", response.data)
        self.assertIn(b"Update selected skills", response.data)
        self.assertIn(b'value="none"', response.data)
        self.assertIn(b'value="selected"', response.data)
        self.assertIn(b"Leave the selection empty to invite all departments", response.data)

        departments = self.client.get(
            "/dashboard/events/options",
            query_string={
                "option_group": "departments",
                "department_search": "Computer",
                "departments": ["Civil Engineering"],
            },
        )
        self.assertEqual(departments.status_code, 200)
        self.assertIn(b"Computer Engineering and Technology", departments.data)
        self.assertIn(b'value="Civil Engineering" checked', b" ".join(departments.data.split()))

        skills = self.client.get(
            "/dashboard/events/options",
            query_string={
                "option_group": "skills",
                "skill_search": "Python",
                "required_skill_names": ["Python", "Event planning"],
            },
        )
        self.assertEqual(skills.status_code, 200)
        compact = b" ".join(skills.data.split())
        self.assertIn(b'value="Python" checked', compact)
        self.assertIn(b'value="Event planning" checked', compact)

        ratings = self.client.post(
            "/dashboard/events/skill-ratings",
            data={
                "csrf_token": "test-csrf",
                "required_skill_names": ["Python", "Event planning"],
                "required_skill_name": ["Python"],
                "required_skill_rating": ["4"],
            },
        )
        self.assertEqual(ratings.status_code, 200)
        compact_ratings = b" ".join(ratings.data.split())
        self.assertIn(b'name="required_skill_rating" min="0" step="1"', compact_ratings)
        self.assertIn(b'value="4"', compact_ratings)
        self.assertIn(b'value="1"', compact_ratings)

    def test_skill_rating_controls_are_organizer_only_and_check_event_ownership(self):
        payload = {
            "csrf_token": "test-csrf",
            "required_skill_names": ["Python"],
        }
        self.sign_in("volunteer")
        response = self.client.post("/dashboard/events/skill-ratings", data=payload)
        self.assertEqual(response.status_code, 403)

        self.sign_in("organizer")
        self.client.post("/dashboard/events/new", data=self.form())
        self.sign_in("other_organizer")
        response = self.client.post(
            "/dashboard/events/skill-ratings",
            data={**payload, "event_id": "e1"},
        )
        self.assertEqual(response.status_code, 404)

    def test_create_event_saves_selected_skills_and_departments(self):
        response = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names=["Python", "Event planning"],
                required_skill_name=["Python", "Event planning"],
                required_skill_rating=["3", "1"],
                required_skill_names_present="1",
                skill_requirement_mode="selected",
                departments=["Computer Science and Applications", "Civil Engineering"],
                departments_present="1",
            ),
        )

        self.assertEqual(response.status_code, 303)
        event = data_store.load("events.json")["e1"]
        self.assertEqual(event["departments"], [
            "Computer Science and Applications",
            "Civil Engineering",
        ])
        self.assertEqual(event["department"], "Multiple departments")
        self.assertEqual(event["required_skills"], {
            "Python": 3,
            "Event planning": 1,
        })
        detail = self.client.get("/events/e1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Departments invited", detail.data)
        self.assertIn(b"Civil Engineering", detail.data)
        self.assertIn(b"Computer Science and Applications", detail.data)

    def test_no_required_skills_and_no_department_selection_are_valid(self):
        response = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names_present="1",
                skill_requirement_mode="none",
                departments_present="1",
                department="",
            ),
        )

        self.assertEqual(response.status_code, 303)
        event = data_store.load("events.json")["e1"]
        self.assertEqual(event["required_skills"], {})
        self.assertEqual(event["departments"], [])
        self.assertNotIn("department", event)
        detail = self.client.get("/events/e1")
        self.assertIn(b"All departments", detail.data)
        self.assertIn(b"No specific skills required", detail.data)

        volunteer = {
            "role": "Student Volunteer",
            "dept": "Arts",
            "skills": {},
        }
        self.assertIsNone(get_event_eligibility_reason(volunteer, event))
        self.assertEqual(
            [event_id for event_id, _ in find_eligible_events(volunteer, department="Arts")],
            ["e1"],
        )

    def test_new_picker_rejects_unlisted_department_and_skill_values(self):
        invalid_department = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names_present="1",
                skill_requirement_mode="none",
                departments=["Unlisted Department"],
                departments_present="1",
            ),
        )
        self.assertEqual(invalid_department.status_code, 400)
        self.assertIn(b"Choose a listed department", invalid_department.data)

        invalid_skill = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names=["Unlisted Skill"],
                required_skill_names_present="1",
                skill_requirement_mode="selected",
                departments_present="1",
            ),
        )
        self.assertEqual(invalid_skill.status_code, 400)
        self.assertIn(b"Choose a listed skill", invalid_skill.data)
        self.assertEqual(data_store.load("events.json"), {})

    def test_departments_limit_eligibility_and_department_filter_matches_multiple(self):
        response = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names_present="1",
                skill_requirement_mode="none",
                departments=["Computer Science and Applications", "Civil Engineering"],
                departments_present="1",
            ),
        )
        self.assertEqual(response.status_code, 303)
        event = data_store.load("events.json")["e1"]

        allowed = {
            "role": "Student Volunteer",
            "dept": "Civil Engineering",
            "skills": {},
        }
        blocked = {
            "role": "Student Volunteer",
            "dept": "Arts",
            "skills": {},
        }
        self.assertIsNone(get_event_eligibility_reason(allowed, event))
        self.assertIn("Civil Engineering", get_event_eligibility_reason(blocked, event))
        self.assertEqual(
            [event_id for event_id, _ in find_eligible_events(allowed, department="Civil Engineering")],
            ["e1"],
        )
        self.assertEqual(
            find_eligible_events(allowed, department="Arts"),
            [],
        )

    def test_edit_form_prefills_selected_options_and_keeps_unlisted_saved_skill(self):
        self.client.post(
            "/dashboard/events/new",
            data=self.form(required_skills="Unique legacy skill: 2"),
        )
        events = data_store.load("events.json")
        events["e1"]["departments"] = ["Civil Engineering"]
        data_store.save("events.json", events)
        page = self.client.get("/dashboard/events/e1/edit")

        self.assertEqual(page.status_code, 200)
        compact = b" ".join(page.data.split())
        self.assertIn(b'value="Civil Engineering" checked', compact)
        self.assertIn(b'value="Unique legacy skill" checked', compact)
        self.assertIn(b'Minimum rating', compact)
        self.assertIn(b'value="2"', compact)

        response = self.client.post(
            "/dashboard/events/e1/edit",
            data=self.form(
                title="Updated Workshop",
                required_skills="",
                required_skill_names=["Unique legacy skill"],
                required_skill_name=["Unique legacy skill"],
                required_skill_rating=["4"],
                required_skill_names_present="1",
                skill_requirement_mode="selected",
                departments=["Civil Engineering"],
                departments_present="1",
            ),
        )
        self.assertEqual(response.status_code, 303)
        updated = data_store.load("events.json")["e1"]
        self.assertEqual(updated["required_skills"], {"Unique legacy skill": 4})
        self.assertEqual(updated["departments"], ["Civil Engineering"])

    def test_invalid_per_skill_ratings_are_rejected_on_create_and_edit(self):
        invalid_create = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names=["Python"],
                required_skill_name=["Python"],
                required_skill_rating=["-1"],
                required_skill_names_present="1",
                skill_requirement_mode="selected",
                departments_present="1",
            ),
        )
        self.assertEqual(invalid_create.status_code, 400)
        self.assertIn(b"non-negative whole numbers", invalid_create.data)
        self.assertEqual(data_store.load("events.json"), {})

        self.client.post("/dashboard/events/new", data=self.form())
        invalid_edit = self.client.post(
            "/dashboard/events/e1/edit",
            data=self.form(
                required_skills="",
                required_skill_names=["Python"],
                required_skill_name=["Python"],
                required_skill_rating=["not-a-number"],
                required_skill_names_present="1",
                skill_requirement_mode="selected",
                departments_present="1",
            ),
        )
        self.assertEqual(invalid_edit.status_code, 400)
        self.assertIn(b"non-negative whole numbers", invalid_edit.data)
        self.assertEqual(
            data_store.load("events.json")["e1"]["required_skills"],
            {"Python": 2, "Event planning": 1},
        )

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

    def test_organizer_can_hide_owned_event_without_deleting_history(self):
        self.client.post("/dashboard/events/new", data=self.form())
        data_store.save("applications.json", {
            "e1": {"volunteer": {"status": "accepted", "role": "Stage crew"}}
        })
        data_store.save("certificates.json", {
            "certificate-code": {"event_id": "e1", "student_id": "volunteer"}
        })

        confirmation = self.client.get("/dashboard/events/e1/delete")
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"Applications, certificates, and the event record will be kept", confirmation.data)

        response = self.client.post(
            "/dashboard/events/e1/delete", data={"csrf_token": "test-csrf"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(b"Event hidden.", self.client.get("/dashboard").data)
        self.assertNotIn(b"Campus Workshop", self.client.get("/dashboard").data)
        self.assertEqual(self.client.get("/events/e1").status_code, 404)
        self.assertTrue(data_store.load("events.json")["e1"]["deleted_at"])
        self.assertIn("volunteer", data_store.load("applications.json")["e1"])
        self.assertIn("certificate-code", data_store.load("certificates.json"))

    def test_only_owner_organizer_can_hide_events_and_csrf_is_required(self):
        self.client.post("/dashboard/events/new", data=self.form())

        self.sign_in("volunteer")
        self.assertEqual(self.client.get("/dashboard/events/e1/delete").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/dashboard/events/e1/delete", data={"csrf_token": "test-csrf"}
            ).status_code,
            403,
        )

        self.sign_in("other_organizer")
        self.assertEqual(self.client.get("/dashboard/events/e1/delete").status_code, 404)
        self.assertEqual(
            self.client.post(
                "/dashboard/events/e1/delete", data={"csrf_token": "test-csrf"}
            ).status_code,
            404,
        )

        self.sign_in("organizer")
        self.assertEqual(
            self.client.post("/dashboard/events/e1/delete", data={"csrf_token": ""}).status_code,
            400,
        )
        self.assertNotIn("deleted_at", data_store.load("events.json")["e1"])

    def test_invalid_form_shows_error_without_saving(self):
        response = self.client.post(
            "/dashboard/events/new",
            data=self.form(required_skills="Python without a rating"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"must use the format", response.data)
        self.assertEqual(data_store.load("events.json"), {})

    def test_non_organizers_and_other_organizers_cannot_manage_event(self):
        created = self.client.post("/dashboard/events/new", data=self.form())
        self.assertEqual(created.status_code, 303)

        self.sign_in("volunteer")
        self.assertEqual(self.client.get("/dashboard/events/new").status_code, 403)
        self.assertEqual(
            self.client.get(
                "/dashboard/events/options?option_group=skills"
            ).status_code,
            403,
        )
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

    def test_applicant_filter_form_uses_htmx_for_changes_and_submit(self):
        self.client.post("/dashboard/events/new", data=self.form())
        users = data_store.load("users.json")
        users["volunteer"]["dept"] = "Computer Science"
        data_store.save("users.json", users)
        data_store.save("applications.json", {
            "e1": {"volunteer": {"status": "pending"}}
        })

        response = self.client.get("/dashboard/events/e1/applicants")

        self.assertEqual(response.status_code, 200)
        compact = b" ".join(response.data.split())
        self.assertIn(b'hx-trigger="change, submit"', compact)
        self.assertIn(b'hx-target="#modal-root"', compact)

    def test_select_all_pending_volunteers_previews_every_pending_applicant(self):
        self.client.post("/dashboard/events/new", data=self.form())
        users = data_store.load("users.json")
        users.update({
            "volunteer_two": {
                "name": "Second Volunteer",
                "role": "Student Volunteer",
                "dept": "Computer Science",
                "skills": {},
            },
            "already_accepted": {
                "name": "Already Accepted",
                "role": "Student Volunteer",
                "dept": "Computer Science",
                "skills": {},
            },
        })
        data_store.save("users.json", users)
        data_store.save("applications.json", {
            "e1": {
                "volunteer": {"status": "pending"},
                "volunteer_two": {"status": "pending"},
                "already_accepted": {"status": "accepted"},
            }
        })

        response = self.client.post(
            "/dashboard/events/e1/applicants/bulk-accept",
            data={
                "csrf_token": "test-csrf",
                "department": "",
                "select_all_volunteers": "1",
            },
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Confirm batch acceptance", response.data)
        self.assertIn(b"Test Volunteer", response.data)
        self.assertIn(b"Second Volunteer", response.data)
        self.assertNotIn(b"Already Accepted", response.data)
        self.assertIn(b"Assigned role: Not assigned", response.data)
        self.assertIn(b'value="volunteer"', response.data)
        self.assertIn(b'value="volunteer_two"', response.data)

        confirmed = self.client.post(
            "/dashboard/events/e1/applicants/bulk-accept",
            data={
                "csrf_token": "test-csrf",
                "department": "",
                "confirm": "yes",
                "volunteer_id": ["volunteer", "volunteer_two"],
            },
            headers={"HX-Request": "true"},
        )

        self.assertEqual(confirmed.status_code, 200)
        applications = data_store.load("applications.json")["e1"]
        self.assertEqual(applications["volunteer"]["status"], "accepted")
        self.assertIsNone(applications["volunteer"]["role"])
        self.assertEqual(applications["volunteer_two"]["status"], "accepted")
        self.assertIsNone(applications["volunteer_two"]["role"])
        self.assertIn(
            "was accepted",
            notification_service.get_user_notifications("volunteer")[0]["message"],
        )
        self.assertNotIn(
            "assigned role",
            notification_service.get_user_notifications("volunteer")[0]["message"],
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

    def test_applicant_department_filter_limits_list_and_keeps_review_context(self):
        created = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names_present="1",
                skill_requirement_mode="none",
                departments=[
                    "Computer Science and Applications",
                    "Civil Engineering",
                ],
                departments_present="1",
            ),
        )
        self.assertEqual(created.status_code, 303)
        users = data_store.load("users.json")
        users.update({
            "civil_applicant": {
                "name": "Civil Applicant",
                "role": "Student Volunteer",
                "dept": "Civil Engineering",
                "skills": {},
            },
            "cs_applicant": {
                "name": "CS Applicant",
                "role": "Student Volunteer",
                "dept": "Computer Science and Applications",
                "skills": {},
            },
        })
        data_store.save("users.json", users)
        data_store.save("applications.json", {
            "e1": {
                "civil_applicant": {"status": "pending"},
                "cs_applicant": {"status": "pending"},
            }
        })

        filtered = self.client.get(
            "/dashboard/events/e1/applicants",
            query_string={"department": "Civil Engineering"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Civil Applicant", filtered.data)
        self.assertNotIn(b"CS Applicant", filtered.data)
        self.assertIn(b'value="Civil Engineering" selected', b" ".join(filtered.data.split()))
        self.assertIn(b'name="department" value="Civil Engineering"', filtered.data)

        reviewed = self.client.post(
            "/dashboard/events/e1/applicants/civil_applicant/status",
            data={
                "csrf_token": "test-csrf",
                "department": "Civil Engineering",
                "status": "rejected",
            },
            headers={"HX-Request": "true"},
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertIn(b"Civil Applicant", reviewed.data)
        self.assertNotIn(b"CS Applicant", reviewed.data)
        self.assertIn(b'value="Civil Engineering" selected', b" ".join(reviewed.data.split()))
        self.assertEqual(
            data_store.load("applications.json")["e1"]["civil_applicant"]["status"],
            "rejected",
        )

        unfiltered = self.client.get("/dashboard/events/e1/applicants")
        self.assertEqual(unfiltered.status_code, 200)
        self.assertIn(b"Civil Applicant", unfiltered.data)
        self.assertIn(b"CS Applicant", unfiltered.data)
        self.assertIn(b'value="" selected', b" ".join(unfiltered.data.split()))

    def test_applicant_filter_for_all_department_event_uses_applicant_departments(self):
        created = self.client.post(
            "/dashboard/events/new",
            data=self.form(
                required_skills="",
                required_skill_names_present="1",
                skill_requirement_mode="none",
                departments_present="1",
                department="",
            ),
        )
        self.assertEqual(created.status_code, 303)
        users = data_store.load("users.json")
        users.update({
            "arts_applicant": {
                "name": "Arts Applicant",
                "role": "Student Volunteer",
                "dept": "Arts",
                "skills": {},
            },
            "civil_applicant": {
                "name": "Civil Applicant",
                "role": "Student Volunteer",
                "dept": "Civil Engineering",
                "skills": {},
            },
        })
        data_store.save("users.json", users)
        data_store.save("applications.json", {
            "e1": {
                "arts_applicant": {"status": "pending"},
                "civil_applicant": {"status": "pending"},
            }
        })

        filtered = self.client.get(
            "/dashboard/events/e1/applicants",
            query_string={"department": "Arts"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Arts Applicant", filtered.data)
        self.assertNotIn(b"Civil Applicant", filtered.data)
        compact = b" ".join(filtered.data.split())
        self.assertIn(b'value="Arts" selected', compact)
        self.assertIn(b'value="Civil Engineering"', compact)

        all_applicants = self.client.get("/dashboard/events/e1/applicants")
        self.assertEqual(all_applicants.status_code, 200)
        self.assertIn(b"Arts Applicant", all_applicants.data)
        self.assertIn(b"Civil Applicant", all_applicants.data)

    def test_applicant_list_and_filter_are_not_available_to_non_owners(self):
        self.client.post("/dashboard/events/new", data=self.form())

        self.sign_in("volunteer")
        self.assertEqual(
            self.client.get(
                "/dashboard/events/e1/applicants?department=Computer%20Science"
            ).status_code,
            403,
        )

        self.sign_in("other_organizer")
        self.assertEqual(
            self.client.get(
                "/dashboard/events/e1/applicants?department=Computer%20Science"
            ).status_code,
            404,
        )

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
