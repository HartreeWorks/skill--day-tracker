"""The web UI refuses cross-site requests, foreign hosts and path traversal."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # TestClient needs httpx, which the tool itself does not.
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):
    TestClient = None

import server

BASE = "http://127.0.0.1:8765"


@unittest.skipIf(TestClient is None, "httpx is not installed")
class ServerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.captures = root / "captures"
        self.daily = root / "daily"
        self.reports = root / "reports"
        for d in (self.captures, self.daily, self.reports):
            d.mkdir()
        capture = self.captures / "2026-11-03" / "10-00-00"
        capture.mkdir(parents=True)
        (capture / "screen-1.webp").write_bytes(b"RIFF....WEBP")
        (root / "secret.txt").write_text("secret")
        self.patches = [
            patch.object(server, "CAPTURES_DIR", self.captures),
            patch.object(server, "DAILY_DIR", self.daily),
            patch.object(server, "REPORTS_DIR", self.reports),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(server.app, base_url=BASE)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_own_page_is_served_with_csp(self):
        response = self.client.get("/api/dates", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("connect-src 'self'", response.headers["content-security-policy"])

    def test_foreign_host_is_refused(self):
        client = TestClient(server.app, base_url="http://evil.example:8765")
        self.assertEqual(client.get("/api/dates").status_code, 403)

    def test_cross_site_and_same_site_requests_are_refused(self):
        for site in ("cross-site", "same-site"):
            response = self.client.get("/api/dates", headers={"Sec-Fetch-Site": site})
            self.assertEqual(response.status_code, 403, site)

    def test_cross_site_top_level_navigation_is_allowed(self):
        headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                   "Sec-Fetch-Dest": "document"}
        self.assertEqual(self.client.get("/api/dates", headers=headers).status_code, 200)
        # but not a cross-site form POST, nor a frame
        self.assertEqual(self.client.post("/api/story?date=2026-11-03", headers=headers).status_code, 403)
        frame = dict(headers, **{"Sec-Fetch-Dest": "iframe"})
        self.assertEqual(self.client.get("/api/dates", headers=frame).status_code, 403)

    def test_foreign_origin_is_refused(self):
        response = self.client.post(
            "/api/tag", json={"captures": [], "project": "x"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_story_generation_is_post_only(self):
        self.assertEqual(self.client.get("/api/story?date=2026-11-03").status_code, 405)

    def test_traversal_in_dates_and_paths_is_rejected(self):
        self.assertEqual(self.client.get("/api/captures?date=/etc").status_code, 400)
        self.assertEqual(self.client.get("/api/captures?date=../..").status_code, 400)
        self.assertEqual(self.client.post("/api/story?date=/tmp/x").status_code, 400)
        response = self.client.post(
            "/api/tag", json={"captures": ["/tmp/x"], "project": "x"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.get("/screenshot/2026-11-03/10-00-00/secret.txt").status_code, 400
        )

    def test_tag_requires_json_content_type(self):
        response = self.client.post(
            "/api/tag", content='{"captures": [], "project": "x"}',
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(response.status_code, 415)

    def test_day_page_renders_and_escapes_titles(self):
        capture = self.captures / "2026-11-03" / "10-00-00"
        (capture / "metadata.json").write_text(
            '{"timestamp": "2026-11-03T10:00:00", "screens": ["screen-1.webp"],'
            ' "active_window": {"app": "Chrome", "title": "<img src=x onerror=alert(1)>"},'
            ' "analysis": {"description": "d", "category": "coding",'
            ' "oneline": "<b>Editing</b> the timeline"}}'
        )
        response = self.client.get("/day/2026-11-03")
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;b&gt;Editing&lt;/b&gt; the timeline", response.text)
        self.assertNotIn("<img src=x", response.text)
        self.assertNotIn("<b>Editing</b>", response.text)

    def test_legacy_cached_digest_html_is_never_served(self):
        (self.reports / "2026-11-03-digest.html").write_text("<img src=x onerror=alert(1)>")
        (self.reports / "2026-11-03-digest.md").write_text("Worked on <script>alert(1)</script> things")
        data = self.client.post("/api/story?date=2026-11-03").json()
        self.assertTrue(data["cached"])
        self.assertNotIn("onerror", data["html"])
        self.assertNotIn("<script>", data["html"])
        self.assertIn("&lt;script&gt;", data["html"])

    def test_generated_digest_escapes_raw_html(self):
        (self.daily / "2026-11-03.json").write_text(
            '{"date": "2026-11-03", "entries": [{"timestamp": "2026-11-03T10:00:00",'
            ' "capture_dir": "2026-11-03/10-00-00", "oneline": "x", "category": "coding"}]}'
        )
        hostile = "## Day\n\nEdited <img src=x onerror=alert(1)> all morning"
        with patch.object(server, "get_gemini_api_key", return_value="test-key"), \
             patch("summarize.generate_digest", return_value=hostile):
            data = self.client.post("/api/story?date=2026-11-03&force=true").json()
        self.assertFalse(data["cached"])
        self.assertIn("<h2>Day</h2>", data["html"])
        self.assertNotIn("<img", data["html"])
        self.assertIn("&lt;img", data["html"])
        # a later read serves the cached Markdown, escaped the same way
        again = self.client.post("/api/story?date=2026-11-03").json()
        self.assertTrue(again["cached"])
        self.assertEqual(again["html"], data["html"])

    def test_valid_screenshot_is_served(self):
        response = self.client.get("/screenshot/2026-11-03/10-00-00/screen-1.webp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/webp")


if __name__ == "__main__":
    unittest.main()
