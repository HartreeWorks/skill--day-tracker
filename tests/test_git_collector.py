import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collectors  # noqa: E402


class GitRepositoryDetectionTests(unittest.TestCase):
    def test_accepts_valid_git_repository(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repo)],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue(collectors._is_git_repository(repo))

    def test_rejects_incomplete_dot_git_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            (repo / ".git" / "objects").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

            self.assertFalse(collectors._is_git_repository(repo))


if __name__ == "__main__":
    unittest.main()
