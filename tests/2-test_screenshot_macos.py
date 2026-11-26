"""Unit tests for the macOS app-name handling. No macOS needed: subprocess is mocked."""
import subprocess
import unittest
from unittest import mock

from pcmcp_swarm.screenshot import macos


class AppNameTests(unittest.TestCase):
    def test_normal_names_pass_unchanged(self):
        for n in ["Finder", "Google Chrome", "Visual Studio Code", "Notes", "Xcode-beta", "App.name",
                  "Caf\u00e9 \u00dc", "Bob's App", 'Say "hi"', "back\\slash", "A" * 100]:
            self.assertEqual(macos.clean_app_name(n), n)

    def test_bad_names_refused(self):
        for n in ["", "   ", "A" * 101, "a\nb", "a\rb", "a\x00b", "a\tb", "-e", "--help", None, 5, ["x"]]:
            with self.assertRaises(ValueError, msg=repr(n)):
                macos.clean_app_name(n)

    def test_name_is_passed_as_argument_not_script_text(self):
        evil = ['x" to activate\ndo shell script "touch /tmp/pwned', 'x\\', '" & (do shell script "id") & "',
                "Finder\" to quit\n", "a\\\" b"]
        for n in evil:
            calls = []

            def fake(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="1,2,3,4\n", stderr="")
            with mock.patch.object(macos.subprocess, "run", fake):
                try:
                    macos.capture({"type": "app", "name": n}, "/tmp/out.png")
                except ValueError:
                    continue  # refused before any process ran
            for cmd in calls:
                if cmd[0] == "osascript":
                    self.assertEqual(cmd[1], "-e")
                    self.assertNotIn("do shell script", cmd[2])  # script text is the fixed constant
                    self.assertEqual(cmd[2], macos._APP_BOUNDS_SCRIPT)
                    self.assertEqual(cmd[3], n)  # the name travels only as the final argument

    def test_script_has_no_string_interpolation(self):
        self.assertNotIn("{", macos._APP_BOUNDS_SCRIPT.replace("{x, y}", "").replace("{w, h}", ""))
        self.assertIn("item 1 of argv", macos._APP_BOUNDS_SCRIPT)

    def test_normal_capture_builds_region(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="10,20,300,400\n", stderr="")
        with mock.patch.object(macos.subprocess, "run", fake):
            macos.capture({"type": "app", "name": "Google Chrome"}, "/tmp/out.png")
        self.assertEqual(calls[0][:3], ["osascript", "-e", macos._APP_BOUNDS_SCRIPT])
        self.assertEqual(calls[0][3], "Google Chrome")
        self.assertEqual(calls[1], ["screencapture", "-x", "-R", "10,20,300,400", "/tmp/out.png"])


if __name__ == "__main__":
    unittest.main()
