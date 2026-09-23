"""Tests for Windows signing configuration and VERSIONINFO generation.

Signing cannot be exercised end-to-end without a real certificate, so these
tests pin the parts that must be right for a certificate to work when the user
supplies one: argv assembly, credential detection, password masking, and that
the generated VERSIONINFO resource is valid Python PyInstaller can consume.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe._platform import LINUX, WINDOWS, X64, platform_from_keys  # noqa: E402
from paperclip_exe.signing import (  # noqa: E402
    DEFAULT_TIMESTAMP_URL,
    SigningConfig,
    SigningError,
    av_risk_report,
    build_signtool_command,
    find_signtool,
    generate_version_info,
    parse_version_tuple,
    sign_binary,
    signing_config_from_env,
)

SIGN_ENV_KEYS = (
    "PAPERCLIP_SIGN",
    "PAPERCLIP_SIGN_SUBJECT",
    "PAPERCLIP_SIGN_SHA1",
    "PAPERCLIP_PFX",
    "PAPERCLIP_PFX_PASSWORD",
    "PAPERCLIP_TIMESTAMP_URL",
    "PAPERCLIP_SIGNTOOL",
    "PAPERCLIP_SIGN_ARGS",
)


class SignEnvGuard:
    """Clear the signing environment for the duration of a test."""

    def __enter__(self):
        self.saved = {key: os.environ.get(key) for key in SIGN_ENV_KEYS}
        for key in SIGN_ENV_KEYS:
            os.environ.pop(key, None)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class SigningConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with SignEnvGuard():
            config = signing_config_from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.method, "none")

    def test_explicit_flag_enables(self):
        with SignEnvGuard():
            os.environ["PAPERCLIP_SIGN"] = "1"
            config = signing_config_from_env()
        self.assertTrue(config.enabled)
        self.assertEqual(config.method, "auto")

    def test_credential_implies_intent_to_sign(self):
        # Setting a certificate without PAPERCLIP_SIGN=1 must still sign: the
        # opposite would silently ship an unsigned binary the user asked for.
        with SignEnvGuard():
            os.environ["PAPERCLIP_SIGN_SUBJECT"] = "Paperclip Inc"
            self.assertEqual(signing_config_from_env().method, "store-subject")

        with SignEnvGuard():
            os.environ["PAPERCLIP_SIGN_SHA1"] = "abc123"
            self.assertEqual(signing_config_from_env().method, "store-sha1")

        with SignEnvGuard():
            os.environ["PAPERCLIP_PFX"] = "cert.pfx"
            self.assertEqual(signing_config_from_env().method, "pfx")

    def test_pfx_path_is_expanded(self):
        with SignEnvGuard():
            os.environ["PAPERCLIP_PFX"] = "~/certs/paperclip.pfx"
            config = signing_config_from_env()
        self.assertEqual(config.pfx_path, Path.home() / "certs" / "paperclip.pfx")

    def test_timestamp_can_be_disabled(self):
        with SignEnvGuard():
            os.environ["PAPERCLIP_SIGN"] = "1"
            self.assertEqual(signing_config_from_env().timestamp_url, DEFAULT_TIMESTAMP_URL)
            os.environ["PAPERCLIP_TIMESTAMP_URL"] = "none"
            self.assertIsNone(signing_config_from_env().timestamp_url)

    def test_extra_args_are_split(self):
        with SignEnvGuard():
            os.environ["PAPERCLIP_SIGN"] = "1"
            os.environ["PAPERCLIP_SIGN_ARGS"] = "/csp provider /kc key"
            config = signing_config_from_env()
        self.assertEqual(config.extra_args, ("/csp", "provider", "/kc", "key"))


class SigntoolCommandTests(unittest.TestCase):
    def _config(self, **kwargs) -> SigningConfig:
        base = dict(enabled=True)
        base.update(kwargs)
        return SigningConfig(**base)

    def test_pfx_includes_file_and_password(self):
        command = build_signtool_command(
            Path("dist/paperclip.exe"),
            self._config(pfx_path=Path("c:/cert.pfx"), pfx_password="s3cret"),
            "signtool",
        )
        self.assertEqual(command[:2], ["signtool", "sign"])
        self.assertIn("/f", command)
        self.assertIn("/p", command)
        self.assertIn("s3cret", command)

    def test_subject_uses_store_lookup(self):
        command = build_signtool_command(
            Path("x.exe"), self._config(subject="Paperclip Inc"), "signtool"
        )
        self.assertEqual(command[command.index("/n") + 1], "Paperclip Inc")

    def test_sha1_thumbprint(self):
        command = build_signtool_command(Path("x.exe"), self._config(sha1_hash="DEAD"), "signtool")
        self.assertEqual(command[command.index("/sha1") + 1], "DEAD")

    def test_auto_selects_best_certificate(self):
        command = build_signtool_command(Path("x.exe"), self._config(), "signtool")
        self.assertIn("/a", command)

    def test_sha256_digests_are_always_set(self):
        # Modern Windows rejects SHA-1 file digests; both must be SHA256.
        command = build_signtool_command(Path("x.exe"), self._config(subject="X"), "signtool")
        self.assertEqual(command[command.index("/fd") + 1], "SHA256")
        self.assertEqual(command[command.index("/td") + 1], "SHA256")

    def test_timestamp_server_is_included(self):
        command = build_signtool_command(
            Path("x.exe"), self._config(timestamp_url=DEFAULT_TIMESTAMP_URL), "signtool"
        )
        self.assertEqual(command[command.index("/tr") + 1], DEFAULT_TIMESTAMP_URL)

    def test_no_timestamp_when_disabled(self):
        command = build_signtool_command(Path("x.exe"), self._config(timestamp_url=None), "signtool")
        self.assertNotIn("/tr", command)

    def test_target_is_last_argument(self):
        binary = Path("dist/paperclip.exe")
        command = build_signtool_command(binary, self._config(subject="X"), "signtool")
        self.assertEqual(command[-1], str(binary))

    def test_extra_args_are_appended(self):
        command = build_signtool_command(
            Path("x.exe"), self._config(subject="X", extra_args=("/v",)), "signtool"
        )
        self.assertIn("/v", command)


class SignBinaryTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        with TemporaryDirectory() as tmp:
            binary = Path(tmp) / "paperclip.exe"
            binary.write_text("MZ", encoding="utf-8")
            self.assertIsNone(sign_binary(binary, SigningConfig(enabled=False)))

    def test_missing_binary_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SigningError):
                sign_binary(Path(tmp) / "absent.exe", SigningConfig(enabled=True))

    def test_missing_signtool_raises_with_guidance(self):
        # Critical: signing was requested but cannot happen. A silent skip would
        # ship an unsigned binary the operator believed was signed.
        with TemporaryDirectory() as tmp:
            binary = Path(tmp) / "paperclip.exe"
            binary.write_text("MZ", encoding="utf-8")
            config = SigningConfig(enabled=True, signtool=str(Path(tmp) / "no-signtool.exe"))
            with self.assertRaises(SigningError) as ctx:
                sign_binary(binary, config)
        message = str(ctx.exception)
        self.assertIn("signtool", message.lower())
        self.assertIn("Windows SDK", message)
        self.assertIn("PAPERCLIP_SIGNTOOL", message)

    def test_missing_pfx_raises(self):
        with TemporaryDirectory() as tmp:
            binary = Path(tmp) / "paperclip.exe"
            binary.write_text("MZ", encoding="utf-8")
            config = SigningConfig(
                enabled=True,
                pfx_path=Path(tmp) / "absent.pfx",
                signtool=sys.executable,  # any existing file, so we reach the pfx check
            )
            with self.assertRaises(SigningError) as ctx:
                sign_binary(binary, config)
            self.assertIn("PFX", str(ctx.exception))

    def test_password_is_masked_in_echoed_command(self):
        # The command is echoed to stderr; the password must never appear.
        with TemporaryDirectory() as tmp:
            binary = Path(tmp) / "paperclip.exe"
            binary.write_text("MZ", encoding="utf-8")
            pfx = Path(tmp) / "cert.pfx"
            pfx.write_text("fake", encoding="utf-8")
            # Use the python interpreter as a stand-in signtool that fails fast.
            config = SigningConfig(
                enabled=True,
                pfx_path=pfx,
                pfx_password="super-secret-value",
                signtool=sys.executable,
                timestamp_url=None,
            )
            completed = subprocess.run(
                [sys.executable, "-c", "pass"], capture_output=True, text=True
            )
            self.assertEqual(completed.returncode, 0)
            # Drive sign_binary and capture what it printed.
            import io
            from contextlib import redirect_stderr

            buffer = io.StringIO()
            # Make the stand-in fail so we exercise the error path deterministically.
            failing = Path(tmp) / "fail.py"
            failing.write_text("import sys; sys.exit(1)", encoding="utf-8")
            config = SigningConfig(
                enabled=True,
                pfx_path=pfx,
                pfx_password="super-secret-value",
                signtool=sys.executable,
                timestamp_url=None,
                extra_args=(str(failing),),
            )
            with redirect_stderr(buffer):
                with self.assertRaises(SigningError):
                    sign_binary(binary, config)
            self.assertNotIn("super-secret-value", buffer.getvalue())
            self.assertIn("<password>", buffer.getvalue())


class FindSigntoolTests(unittest.TestCase):
    def test_non_windows_without_explicit_returns_none(self):
        if os.name == "nt":
            self.skipTest("running on Windows")
        self.assertIsNone(find_signtool())

    def test_explicit_missing_path_returns_none(self):
        self.assertIsNone(find_signtool("/definitely/not/signtool.exe"))

    def test_explicit_existing_path_is_returned(self):
        self.assertEqual(find_signtool(sys.executable), sys.executable)


class VersionTupleTests(unittest.TestCase):
    def test_pads_to_four_components(self):
        self.assertEqual(parse_version_tuple("0.3.1"), (0, 3, 1, 0))
        self.assertEqual(parse_version_tuple("1.2"), (1, 2, 0, 0))
        self.assertEqual(parse_version_tuple("1.2.3.4"), (1, 2, 3, 4))

    def test_truncates_extra_components(self):
        self.assertEqual(parse_version_tuple("1.2.3.4.5"), (1, 2, 3, 4))

    def test_handles_prerelease_suffixes(self):
        self.assertEqual(parse_version_tuple("2026.403.0-beta.1"), (2026, 403, 0, 1))

    def test_empty_and_garbage(self):
        self.assertEqual(parse_version_tuple(""), (0, 0, 0, 0))
        self.assertEqual(parse_version_tuple("unknown"), (0, 0, 0, 0))


class GenerateVersionInfoTests(unittest.TestCase):
    def test_output_is_valid_python(self):
        import ast

        with TemporaryDirectory() as tmp:
            path = generate_version_info(Path(tmp) / "version_info.txt", version="0.3.1")
            source = path.read_text(encoding="utf-8")
            ast.parse(source)  # must not raise

    def test_output_can_be_evaluated_with_pyinstaller_stubs(self):
        # PyInstaller execs this file against its own VSVersionInfo classes; prove
        # the structure is what it expects by evaluating it with stubs.
        with TemporaryDirectory() as tmp:
            path = generate_version_info(Path(tmp) / "version_info.txt", version="0.3.1")
            captured = {}

            def VSVersionInfo(**kwargs):
                captured.update(kwargs)
                return kwargs

            namespace = {
                "VSVersionInfo": VSVersionInfo,
                "FixedFileInfo": lambda **kw: kw,
                "StringFileInfo": lambda kids: kids,
                "StringTable": lambda lang, structs: (lang, structs),
                "StringStruct": lambda name, value: (name, value),
                "VarFileInfo": lambda vars_: vars_,
                "VarStruct": lambda name, value: (name, value),
            }
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)

            self.assertEqual(captured["ffi"]["filevers"], (0, 3, 1, 0))
            self.assertEqual(captured["ffi"]["prodvers"], (0, 3, 1, 0))
            # kids == [StringFileInfo([StringTable(lang, structs)]), VarFileInfo([...])]
            string_file_info = captured["kids"][0]
            language, structs = string_file_info[0]
            strings = dict(structs)
            self.assertEqual(strings["FileVersion"], "0.3.1.0")
            self.assertEqual(strings["ProductVersion"], "0.3.1.0")
            self.assertEqual(strings["ProductName"], "Paperclip")
            self.assertEqual(strings["OriginalFilename"], "paperclip.exe")
            self.assertEqual(strings["CompanyName"], "Paperclip")
            self.assertIn("LegalCopyright", strings)
            # Language/codepage: US English + Unicode.
            self.assertEqual(language, "040904B0")
            self.assertEqual(captured["kids"][1][0], ("Translation", [1033, 1200]))

    def test_custom_metadata(self):
        with TemporaryDirectory() as tmp:
            path = generate_version_info(
                Path(tmp) / "version_info.txt",
                version="2026.403.0",
                exe_name="paperclip-beta.exe",
                company_name="Acme",
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("paperclip-beta.exe", text)
            self.assertIn("Acme", text)
            self.assertIn("2026.403.0.0", text)

    def test_parent_directory_is_created(self):
        with TemporaryDirectory() as tmp:
            path = generate_version_info(Path(tmp) / "deep" / "nested" / "v.txt", version="1.0")
            self.assertTrue(path.is_file())


class AvRiskReportTests(unittest.TestCase):
    """AV posture is Windows-specific, so every case pins the platform."""

    WIN = platform_from_keys(WINDOWS, X64)

    def test_signed_onedir_is_low_risk(self):
        report = av_risk_report(
            signed=True, signing_method="store-subject", onefile=False,
            has_version_info=True, plat=self.WIN,
        )
        self.assertEqual(report.risk, "low")

    def test_unsigned_onefile_is_high_risk(self):
        report = av_risk_report(
            signed=False, onefile=True, has_version_info=False, plat=self.WIN
        )
        self.assertEqual(report.risk, "high")
        notes = "\n".join(report.notes)
        self.assertIn("onefile", notes)
        self.assertIn("VERSIONINFO", notes)
        self.assertIn("unsigned", notes.lower())
        # Must tell the operator how to actually fix it.
        self.assertIn("PAPERCLIP_SIGN_SUBJECT", notes)
        self.assertIn("PAPERCLIP_PFX", notes)
        self.assertIn("--onedir", notes)

    def test_partial_mitigation_is_moderate(self):
        self.assertEqual(
            av_risk_report(signed=True, onefile=True, has_version_info=True, plat=self.WIN).risk,
            "moderate",
        )
        self.assertEqual(
            av_risk_report(signed=False, onefile=False, has_version_info=True, plat=self.WIN).risk,
            "moderate",
        )

    def test_version_info_present_suppresses_that_note(self):
        report = av_risk_report(
            signed=False, onefile=True, has_version_info=True, plat=self.WIN
        )
        self.assertNotIn("VERSIONINFO", "\n".join(report.notes))

    def test_non_windows_target_is_not_warned_about_smartscreen(self):
        report = av_risk_report(
            signed=False, onefile=True, has_version_info=False, plat=platform_from_keys(LINUX, X64)
        )
        notes = "\n".join(report.notes)
        self.assertIn("applies to Windows builds", notes)
        self.assertNotIn("PAPERCLIP_PFX", notes)

    def test_upx_is_reported_off(self):
        # UPX both corrupts node.exe and trips AV; the spec hard-disables it.
        report = av_risk_report(signed=False, onefile=True, plat=self.WIN)
        self.assertFalse(report.upx)
        self.assertIn("UPX packed      : no", "\n".join(report.lines()))

    def test_lines_are_renderable(self):
        report = av_risk_report(signed=False, onefile=True, has_version_info=True, plat=self.WIN)
        lines = report.lines()
        self.assertTrue(all(isinstance(line, str) for line in lines))
        self.assertTrue(any("assessed risk" in line for line in lines))

    def test_signing_method_hidden_when_not_signed(self):
        # A stale method string must not make an unsigned build look signed.
        report = av_risk_report(
            signed=False, signing_method="pfx", onefile=True, plat=self.WIN
        )
        self.assertEqual(report.signing_method, "none")
        self.assertIn("signed          : NO", "\n".join(report.lines()))


if __name__ == "__main__":
    unittest.main()
