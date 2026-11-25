"""Privacy-safe campus-wide aggregate metrics and organizer-only dashboard."""

from datetime import date
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analytics_service
import data_store
from app import app


class CampusImpactTests(unittest.TestCase):
    TODAY = date(2026, 11, 19)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillmatch-campus-impact-")
        self.storage = patch.object(data_store, "DATA_DIR", Path(self.temp.name))
        self.storage.start()
        self.old_config = {
            key: app.config[key] for key in ("TESTING", "SESSION_COOKIE_SECURE")
        }
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        data_store.save("users.json", {
            "organizer": {"name": "Organizer Name", "role": "Organizer"},
            "other_organizer": {"name": "Other Organizer", "role": "Organizer"},
            **{
                f"volunteer_{index}": {
                    "name": f"Private Volunteer {index}",
                    "role": "Student Volunteer",
                    "dept": "Computer Science",
                    "email": f"private{index}@example.com",
                    "skills": {"Python": index},
                }
                for index in range(1, 6)
            },
        })
        data_store.save("events.json", {
            "e1": {
                "name": "Private Event One",
                "organizer": "organizer",
                "status": "completed",
                "completed_at": "2026-11-18T12:00:00+00:00",
                "required_skills": {"Python": 2},
            },
            "e2": {
                "name": "Private Event Two",
                "organizer": "other_organizer",
                "status": "completed",
                "completed_at": "2026-11-10T12:00:00+00:00",
                "required_skills": {"Python": 1},
            },
            "old": {
                "name": "Old Event",
                "organizer": "organizer",
                "status": "completed",
                "completed_at": "2024-01-01T12:00:00+00:00",
                "required_skills": {"Python": 1},
            },
            "upcoming": {
                "name": "Upcoming Event",
                "organizer": "organizer",
                "status": "open",
                "completed_at": "2026-11-18T12:00:00+00:00",
                "required_skills": {"Python": 1},
            },
        })
        data_store.save("applications.json", {
            "e1": {
                "volunteer_1": {"status": "accepted", "attended": True, "hours": 2},
                "volunteer_2": {"status": "accepted", "attended": True, "hours": 1.5},
                "volunteer_3": {"status": "accepted", "attended": True, "hours": 0},
                "pending_user": {"status": "pending", "attended": False, "hours": 0},
            },
            "e2": {
                "volunteer_4": {"status": "accepted", "attended": False, "hours": 1.25},
                "volunteer_5": {"status": "accepted", "attended": False, "hours": 2},
            },
            "upcoming": {
                "volunteer_1": {"status": "accepted", "attended": True, "hours": 50},
            },
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

    def impact(self, range_key="365d"):
        return analytics_service.get_campus_impact(
            range_key, today=self.TODAY
        )

    def test_aggregate_counts_only_completed_confirmed_contributions(self):
        summary = self.impact()

        self.assertEqual(summary["range_label"], "Last 12 months")
        self.assertEqual(summary["totals"], {
            "recorded_hours": 6.75,
            "event_participations": 5,
            "distinct_volunteers": 5,
            "completed_events": 2,
            "departments_represented": 1,
            "skills_practiced": 1,
        })
        self.assertTrue(summary["department_breakdown_available"])
        self.assertTrue(summary["skill_breakdown_available"])
        self.assertEqual(
            summary["department_breakdown"],
            [{"label": "Computer Science", "volunteers": 5}],
        )
        self.assertEqual(
            summary["skill_breakdown"],
            [{"label": "Python", "volunteers": 5}],
        )
        self.assertEqual(summary["data_notes"], [])

    def test_summary_and_charts_contain_no_personal_or_event_history(self):
        summary = self.impact("all")
        serialized = repr(summary)
        for private_value in (
            "volunteer_1",
            "Private Volunteer",
            "private1@example.com",
            "Private Event One",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertTrue(
            analytics_service.render_campus_department_chart(summary).startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        )
        self.assertTrue(
            analytics_service.render_campus_skill_chart(summary).startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        )

    def test_small_overall_groups_have_no_metric_values_or_breakdowns(self):
        applications = data_store.load("applications.json")
        applications["e2"].pop("volunteer_5")
        data_store.save("applications.json", applications)

        summary = self.impact("all")

        self.assertTrue(summary["privacy_suppressed"])
        self.assertIsNone(summary["totals"])
        self.assertEqual(summary["department_breakdown"], [])
        self.assertEqual(summary["skill_breakdown"], [])
        self.assertIsNone(analytics_service.render_campus_skill_chart(summary))

        self.sign_in("organizer")
        page = self.client.get("/dashboard/impact?range=all")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"hidden until at least five distinct volunteers", page.data)
        self.assertNotIn(b"Private Volunteer", page.data)
        self.assertNotIn(b"volunteer_1", page.data)

    def test_any_small_department_or_skill_group_suppresses_that_breakdown(self):
        users = data_store.load("users.json")
        users["volunteer_1"]["dept"] = "Rare Department"
        data_store.save("users.json", users)
        events = data_store.load("events.json")
        events["e2"]["required_skills"] = {"Rare Skill": 1}
        data_store.save("events.json", events)

        summary = self.impact()

        self.assertFalse(summary["department_breakdown_available"])
        self.assertFalse(summary["skill_breakdown_available"])
        self.assertEqual(summary["department_breakdown"], [])
        self.assertEqual(summary["skill_breakdown"], [])
        self.assertIsNone(analytics_service.render_campus_department_chart(summary))
        self.assertIsNone(analytics_service.render_campus_skill_chart(summary))
        self.assertNotIn("Rare Department", repr(summary))
        self.assertNotIn("Rare Skill", repr(summary))

    def test_ranges_and_incomplete_records_are_labeled_without_exposing_people(self):
        events = data_store.load("events.json")
        events["e1"]["date"] = "2026-11-18"
        events["e1"].pop("completed_at")
        events["undated"] = {
            "name": "Undated Event",
            "status": "completed",
            "required_skills": {},
        }
        data_store.save("events.json", events)
        applications = data_store.load("applications.json")
        applications["e1"]["volunteer_1"].pop("hours")
        applications["undated"] = {
            "volunteer_1": {"status": "accepted", "attended": True, "hours": 1},
            "volunteer_2": {"status": "accepted", "attended": True, "hours": 1},
        }
        data_store.save("applications.json", applications)
        users = data_store.load("users.json")
        users["volunteer_5"]["dept"] = None
        data_store.save("users.json", users)

        recent = self.impact("90d")
        all_time = self.impact("all")

        self.assertEqual(recent["totals"]["completed_events"], 2)
        self.assertEqual(all_time["totals"]["completed_events"], 4)
        self.assertIsNone(recent["totals"]["departments_represented"])
        self.assertEqual(recent["totals"]["skills_practiced"], 1)
        self.assertIsNone(all_time["totals"]["skills_practiced"])
        self.assertFalse(recent["department_breakdown_available"])
        self.assertTrue(recent["skill_breakdown_available"])
        self.assertFalse(all_time["skill_breakdown_available"])
        joined_notes = " ".join(recent["data_notes"])
        self.assertIn("scheduled date", joined_notes)
        self.assertIn("hours are missing", joined_notes)
        self.assertIn("department details are missing", joined_notes)
        self.assertIn("no usable date", joined_notes)
        all_notes = " ".join(all_time["data_notes"])
        self.assertIn("required skills", all_notes)
        self.assertIn("included in all-time totals", all_notes)
        self.assertNotIn("volunteer_1", repr(recent))
        self.assertNotIn("Private Volunteer", repr(all_time))

    def test_organizer_only_dashboard_and_png_routes(self):
        self.assertEqual(self.client.get("/dashboard/impact").status_code, 302)
        self.sign_in("volunteer_1")
        self.assertEqual(self.client.get("/dashboard/impact").status_code, 403)
        self.assertEqual(
            self.client.get("/dashboard/impact/skills.png").status_code, 403
        )

        self.sign_in("organizer")
        page = self.client.get("/dashboard/impact?range=90d")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Campus impact", page.data)
        self.assertIn(b"Departments represented", page.data)
        self.assertIn(b"Skills practiced", page.data)
        self.assertIn(b"Last 90 days", page.data)
        self.assertIn(b"Campus impact", self.client.get("/dashboard").data)
        for private_value in (
            b"volunteer_1",
            b"Private Volunteer",
            b"private1@example.com",
            b"Private Event One",
        ):
            self.assertNotIn(private_value, page.data)

        for route in (
            "/dashboard/impact/departments.png?range=90d",
            "/dashboard/impact/skills.png?range=90d",
        ):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))

        self.sign_in("other_organizer")
        self.assertEqual(self.client.get("/dashboard/impact").status_code, 200)

    def test_no_participation_uses_empty_state_and_unknown_range_is_rejected(self):
        data_store.save("applications.json", {})
        self.sign_in("organizer")
        page = self.client.get("/dashboard/impact?range=365d")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"No completed event contributions", page.data)
        self.assertEqual(
            self.client.get("/dashboard/impact?range=wrong").status_code,
            400,
        )

    def test_impact_charts_are_responsive_at_mobile_widths(self):
        stylesheet = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text()
        self.assertIn(".campus-impact-chart img", stylesheet)
        self.assertIn("width: 100%;", stylesheet[stylesheet.index(".campus-impact-chart img"):])
        mobile_rules = stylesheet[stylesheet.index("@media (max-width: 760px)"):]
        self.assertIn(".campus-impact-charts", mobile_rules)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", mobile_rules)


if __name__ == "__main__":
    unittest.main()
