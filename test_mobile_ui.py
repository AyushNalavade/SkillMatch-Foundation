"""Responsive navigation, touch target, and event-template checks."""

import re
import unittest
from pathlib import Path

from app import app


ROOT = Path(__file__).resolve().parents[1]


class MobileUiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_primary_navigation_uses_a_native_collapsible_menu(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<details class="nav-menu">', response.data)
        self.assertIn(b'<summary class="nav-menu-toggle">', response.data)
        self.assertIn(b'aria-label="Primary navigation"', response.data)
        self.assertIn(b"Discover events", response.data)

    def test_event_templates_do_not_use_inline_styles(self):
        for template in ("discover.html", "event_detail.html"):
            source = (ROOT / "templates" / template).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\sstyle\s*=", source), template)

    def test_desktop_navigation_is_outside_the_closed_mobile_menu(self):
        html = self.client.get("/").get_data(as_text=True)
        navigation = html.split('<nav class="primary-nav"', 1)[1].split("</nav>", 1)[0]
        desktop, mobile = navigation.split('<details class="nav-menu">', 1)
        self.assertIn('class="top-nav top-nav--desktop"', desktop)
        for label in ("Home", "Discover events", "Verify certificate", "Sign in"):
            self.assertIn(f">{label}</a>", desktop)
            self.assertIn(f">{label}</a>", mobile)
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        desktop_css, mobile_css = css.split("@media (max-width: 760px)", 1)
        self.assertRegex(desktop_css, r"\.nav-menu\s*\{\s*display:\s*none;")
        self.assertRegex(mobile_css, r"\.nav-menu\s*\{\s*display:\s*block;")

    def test_mobile_layout_and_modal_keyboard_support_are_present(self):
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".nav-menu[open] > .top-nav", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn('event.key !== "Tab"', script)
        self.assertIn("modalOpener.focus()", script)

    def test_homepage_hero_image_fills_its_cropped_background_frame(self):
        html = self.client.get("/").get_data(as_text=True)
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('width="775"', html)
        self.assertIn('height="517"', html)
        self.assertIn(".home-hero-image", css)
        self.assertIn("width: 100%;\n  height: 100%;", css)
        self.assertIn("object-fit: cover", css)


if __name__ == "__main__":
    unittest.main()
