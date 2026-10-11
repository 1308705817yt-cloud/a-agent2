import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node only needed for optional frontend checks")
    def test_rendering_escaping_and_status_controls(self):
        result = subprocess.run([shutil.which("node"), "tests/frontend_check.cjs"], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
