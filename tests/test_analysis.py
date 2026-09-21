import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

import analyze
from config import CaptureConfig
from models import CaptureMetadata


class AnalysisTests(unittest.TestCase):
    def analyse_response(self, response):
        client = Mock()
        client.models.generate_content.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            capture_dir = Path(directory)
            Image.new("RGB", (10, 10)).save(capture_dir / "screen.jpg")
            with patch.object(analyze, "get_gemini_client", return_value=client):
                result = analyze.analyze_capture(
                    capture_dir, ["screen.jpg"], None, [], CaptureConfig()
                )
        self.assertIsNone(
            client.models.generate_content.call_args.kwargs["config"].safety_settings
        )
        return result

    def test_successful_analysis_preserves_sensitivity(self):
        result = self.analyse_response(SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason="STOP")],
            text='{"description":"Account settings", "category":"admin", '
                 '"oneline":"Reviewing settings", "sensitive":true, '
                 '"sensitive_reason":"Credentials visible"}',
        ))
        self.assertTrue(result.sensitive)
        self.assertFalse(result.safety_blocked)

    def test_provider_block_does_not_read_response_text(self):
        class BlockedResponse:
            prompt_feedback = SimpleNamespace(block_reason="SAFETY")

            @property
            def text(self):
                raise AssertionError("Blocked response text must not be read")

        result = self.analyse_response(BlockedResponse())
        self.assertTrue(result.safety_blocked)
        self.assertTrue(result.sensitive)

    def test_old_metadata_ignores_retired_analysis_fields(self):
        result = CaptureMetadata.from_dict({
            "timestamp": "2026-01-01T12:00:00",
            "screens": ["screen.jpg"],
            "analysis": {
                "description": "Writing notes",
                "category": "writing",
                "oneline": "Writing notes",
                "retired_flag": False,
            },
        })
        self.assertEqual(result.analysis.description, "Writing notes")
        self.assertFalse(result.analysis.safety_blocked)


if __name__ == "__main__":
    unittest.main()
