"""Tests for argument splitting: launcher flags must never leak into the app."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe.launcher import split_launcher_flags  # noqa: E402


class SplitLauncherFlagsTests(unittest.TestCase):
    def test_no_flags_passes_everything_through(self):
        flags, app, dash = split_launcher_flags(["issue", "list", "--company", "acme"])
        self.assertEqual(flags, [])
        self.assertEqual(app, ["issue", "list", "--company", "acme"])
        self.assertFalse(dash)

    def test_leading_launcher_flag_is_consumed(self):
        flags, app, _ = split_launcher_flags(["--launcher-fetch-node", "run"])
        self.assertEqual(flags, ["--launcher-fetch-node"])
        self.assertEqual(app, ["run"])

    def test_app_flags_are_never_inspected(self):
        # Once a real subcommand appears, everything after it belongs to the app,
        # including things that look like launcher flags.
        flags, app, _ = split_launcher_flags(["prompt", "--launcher-fetch-node"])
        self.assertEqual(flags, [])
        self.assertEqual(app, ["prompt", "--launcher-fetch-node"])

    def test_double_dash_forces_passthrough(self):
        flags, app, dash = split_launcher_flags(["--", "--launcher-info"])
        self.assertEqual(flags, [])
        self.assertEqual(app, ["--launcher-info"])
        self.assertTrue(dash)

    def test_flag_with_inline_value(self):
        flags, app, _ = split_launcher_flags(["--launcher-node=/opt/node", "doctor"])
        self.assertEqual(flags, ["--launcher-node=/opt/node"])
        self.assertEqual(app, ["doctor"])

    def test_flag_with_separate_value(self):
        flags, app, _ = split_launcher_flags(["--launcher-node", "/opt/node", "doctor"])
        self.assertIn("--launcher-node", flags)
        self.assertIn("/opt/node", flags)
        self.assertEqual(app, ["doctor"])

    def test_empty_argv(self):
        flags, app, dash = split_launcher_flags([])
        self.assertEqual((flags, app, dash), ([], [], False))

    def test_multiple_launcher_flags_then_app_args(self):
        flags, app, _ = split_launcher_flags(
            ["--launcher-info", "--launcher-fetch-node", "company", "list"]
        )
        self.assertEqual(flags, ["--launcher-info", "--launcher-fetch-node"])
        self.assertEqual(app, ["company", "list"])

    def test_short_flags_belong_to_the_app(self):
        flags, app, _ = split_launcher_flags(["-h"])
        self.assertEqual(flags, [])
        self.assertEqual(app, ["-h"])


class FlagValueTests(unittest.TestCase):
    def test_inline_and_separate_forms_agree(self):
        from paperclip_exe.launcher import _flag_value

        self.assertEqual(_flag_value(["--launcher-node=/a"], "--launcher-node"), "/a")
        self.assertEqual(_flag_value(["--launcher-node", "/a"], "--launcher-node"), "/a")
        self.assertIsNone(_flag_value(["--launcher-info"], "--launcher-node"))
        # Trailing flag with no value must not crash or swallow the next run.
        self.assertIsNone(_flag_value(["--launcher-node"], "--launcher-node"))


if __name__ == "__main__":
    unittest.main()
