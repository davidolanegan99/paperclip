"""Regression tests for the standalone Windows Setup.exe contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
ISS = REPO / "packaging" / "windows-installer" / "paperclip.iss"
BUILD = REPO / "packaging" / "windows-installer" / "build-installer.ps1"


class WindowsInstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iss = ISS.read_text(encoding="utf-8")
        cls.build = BUILD.read_text(encoding="utf-8")

    def test_installer_sources_the_complete_onedir_bundle(self):
        self.assertIn(
            'Source: "..\\exe\\dist\\paperclip\\*"; DestDir: "{app}"',
            self.iss,
        )
        self.assertIn("recursesubdirs createallsubdirs", self.iss)
        self.assertIn("runtime/node/node.exe", self.iss)

    def test_installer_has_no_system_node_prerequisite_check(self):
        # Node is deliberately not an Inno prerequisite. The executable's
        # bundled runtime is installed as part of the same Setup.exe.
        self.assertNotIn("IsNodeInstalled", self.iss)
        self.assertNotIn("nodejs.org/", self.iss)
        self.assertNotIn("Node.js >=", self.iss)

    def test_build_script_enforces_embedded_runtime(self):
        self.assertIn('"--embed-node"', self.build)
        self.assertIn("_internal\\runtime\\node\\node.exe", self.build)
        self.assertIn("Standalone installer ready", self.build)

    def test_build_script_packages_with_inno_setup(self):
        self.assertIn("ISCC.exe", self.build)
        self.assertIn("paperclip.iss", self.build)
        self.assertIn("Paperclip-Setup-0.3.1.exe", self.build)


if __name__ == "__main__":
    unittest.main()
