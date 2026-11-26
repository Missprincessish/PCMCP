"""The screenshot tool validates its input before any capture runs. Capture is mocked."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcmcp_swarm import mcp_server


class ScreenshotInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = {"screenshots": self.tmp}
        self.cfg = {"mcp": {"allow_screenshots": True}}
        self.seen = []

        def fake_capture(target, out):
            self.seen.append(target)
            Path(out).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.patches = [mock.patch.object(mcp_server, "_ctx", lambda: (self.cfg, self.paths)),
                        mock.patch("pcmcp_swarm.screenshot.capture", fake_capture)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_good_app_name_reaches_capture(self):
        mcp_server.tool_screenshot({"target": "app", "name": "Google Chrome"})
        self.assertEqual(self.seen, [{"type": "app", "name": "Google Chrome"}])

    def test_bad_app_names_refused_before_capture(self):
        for n in ["a\nb", "-e", "x" * 101, 5, ["x"], "a\x00"]:
            with self.assertRaises(mcp_server.Gate, msg=repr(n)):
                mcp_server.tool_screenshot({"target": "app", "name": n})
        self.assertEqual(self.seen, [])

    def test_region_must_be_whole_numbers(self):
        with self.assertRaises(mcp_server.Gate):
            mcp_server.tool_screenshot({"target": "region", "x": "1;rm", "y": 0, "w": 1, "h": 1})
        mcp_server.tool_screenshot({"target": "region", "x": "1", "y": 2, "w": 3, "h": 4})
        self.assertEqual(self.seen, [{"type": "region", "x": 1, "y": 2, "w": 3, "h": 4}])

    def test_gate_still_off_by_default(self):
        self.cfg = {}
        mcp_server._ctx = lambda: (self.cfg, self.paths)
        with self.assertRaises(mcp_server.Gate):
            mcp_server.tool_screenshot({"target": "full"})


if __name__ == "__main__":
    unittest.main()
