"""Windows code signing and version metadata.

The honest framing
------------------
Nothing can make an *unsigned* PyInstaller binary invisible to antivirus. What
actually moves the needle, in descending order of effect:

1. **Sign it.** A trusted Authenticode signature is the only real fix for
   SmartScreen/AV reputation. This module wires ``signtool`` (and Azure Trusted
   Signing) so it is one environment variable away -- but the certificate must
   be yours; it cannot be generated for you.
2. **Ship onedir, not onefile.** A onefile exe unpacks itself into ``%TEMP%``
   and executes from there, which is the classic malware pattern AV heuristics
   target. ``--onedir`` produces a folder with a normal launcher exe and
   dramatically fewer heuristic hits.
3. **Embed version metadata.** A binary with no ``FileVersionInfo`` is treated
   as suspicious by many engines. :func:`generate_version_info` writes the
   resource PyInstaller needs.
4. UPX off (it corrupts ``node.exe`` *and* trips AV), no debug bootloader.

:func:`av_risk_report` reports which of these are active for a given build so
the outcome is visible rather than assumed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ._platform import WINDOWS, PlatformInfo, current_platform

#: RFC 3161 timestamp servers commonly used for Authenticode.
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
FALLBACK_TIMESTAMP_URLS = (
    "http://timestamp.sectigo.com",
    "http://tsa.starfieldtech.com",
)

_VERSION_PART_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class SigningConfig:
    """How (and whether) to sign, resolved from the environment."""

    enabled: bool
    subject: Optional[str] = None
    pfx_path: Optional[Path] = None
    pfx_password: Optional[str] = None
    timestamp_url: Optional[str] = DEFAULT_TIMESTAMP_URL
    signtool: Optional[str] = None
    extra_args: Tuple[str, ...] = ()
    sha1_hash: Optional[str] = None

    @property
    def method(self) -> str:
        if not self.enabled:
            return "none"
        if self.pfx_path:
            return "pfx"
        if self.subject:
            return "store-subject"
        if self.sha1_hash:
            return "store-sha1"
        return "auto"


def signing_config_from_env() -> SigningConfig:
    """Build a :class:`SigningConfig` from ``PAPERCLIP_SIGN*`` variables.

    Recognised:
      ``PAPERCLIP_SIGN=1``                enable signing
      ``PAPERCLIP_SIGN_SUBJECT``          cert subject name in the machine store
      ``PAPERCLIP_SIGN_SHA1``             cert thumbprint in the machine store
      ``PAPERCLIP_PFX``                   path to a .pfx file
      ``PAPERCLIP_PFX_PASSWORD``          password for that .pfx
      ``PAPERCLIP_TIMESTAMP_URL``         RFC3161 URL (``none`` to disable)
      ``PAPERCLIP_SIGNTOOL``              explicit signtool path
      ``PAPERCLIP_SIGN_ARGS``             extra arguments, space-separated
    """
    enabled = os.environ.get("PAPERCLIP_SIGN", "").strip().lower() in {"1", "true", "yes", "on"}
    subject = os.environ.get("PAPERCLIP_SIGN_SUBJECT") or None
    sha1 = os.environ.get("PAPERCLIP_SIGN_SHA1") or None
    pfx_raw = os.environ.get("PAPERCLIP_PFX") or None
    pfx_path = Path(pfx_raw).expanduser() if pfx_raw else None
    password = os.environ.get("PAPERCLIP_PFX_PASSWORD") or None
    timestamp = os.environ.get("PAPERCLIP_TIMESTAMP_URL", DEFAULT_TIMESTAMP_URL) or None
    if timestamp and timestamp.strip().lower() == "none":
        timestamp = None
    signtool = os.environ.get("PAPERCLIP_SIGNTOOL") or None
    extra = tuple((os.environ.get("PAPERCLIP_SIGN_ARGS") or "").split())

    # Having a credential implies intent to sign, even without PAPERCLIP_SIGN=1.
    if pfx_path or subject or sha1:
        enabled = True

    return SigningConfig(
        enabled=enabled,
        subject=subject,
        pfx_path=pfx_path,
        pfx_password=password,
        timestamp_url=timestamp,
        signtool=signtool,
        extra_args=extra,
        sha1_hash=sha1,
    )


def find_signtool(explicit: Optional[str] = None) -> Optional[str]:
    """Locate ``signtool.exe``.

    Checks the explicit override, PATH, then the usual Windows SDK locations
    (newest SDK first, since older signtool builds lack SHA-256 support).
    """
    if explicit:
        return explicit if Path(explicit).is_file() or shutil.which(explicit) else None
    found = shutil.which("signtool") or shutil.which("signtool.exe")
    if found:
        return found
    if current_platform().os != WINDOWS:
        return None

    candidates: List[Path] = []
    for root_env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(root_env)
        if not root:
            continue
        sdk = Path(root) / "Windows Kits" / "10" / "bin"
        if not sdk.is_dir():
            continue
        for version_dir in sorted(sdk.iterdir(), reverse=True):
            for arch in ("x64", "x86"):
                candidates.append(version_dir / arch / "signtool.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def build_signtool_command(
    binary: Path, config: SigningConfig, signtool: str
) -> List[str]:
    """Assemble the ``signtool sign`` argv for a credential configuration."""
    command: List[str] = [signtool, "sign"]

    if config.pfx_path:
        command += ["/f", str(config.pfx_path)]
        if config.pfx_password:
            command += ["/p", config.pfx_password]
    elif config.sha1_hash:
        command += ["/sha1", config.sha1_hash]
    elif config.subject:
        command += ["/n", config.subject]
    else:
        # Let signtool pick the best certificate in the store.
        command += ["/a"]

    # SHA-256 file digest and timestamp digest: required by modern Windows.
    command += ["/fd", "SHA256"]
    if config.timestamp_url:
        command += ["/tr", config.timestamp_url, "/td", "SHA256"]
    command += ["/d", "Paperclip", "/du", "https://github.com/paperclipai/paperclip"]
    command += list(config.extra_args)
    command.append(str(binary))
    return command


class SigningError(RuntimeError):
    """Signing was requested but could not be completed."""


def sign_binary(binary: Path, config: Optional[SigningConfig] = None) -> Optional[str]:
    """Sign ``binary`` in place. Returns a human-readable summary, or ``None``.

    Raises :class:`SigningError` when signing was explicitly requested but could
    not be performed -- a silent skip would ship an unsigned binary the user
    believed was signed, which is the worst outcome.
    """
    config = config or signing_config_from_env()
    if not config.enabled:
        return None
    if not binary.is_file():
        raise SigningError(f"cannot sign a missing file: {binary}")

    signtool = find_signtool(config.signtool)
    if not signtool:
        raise SigningError(
            "signing was requested but signtool.exe was not found.\n"
            "  Install the Windows SDK, or set PAPERCLIP_SIGNTOOL to its full path.\n"
            "  To skip signing, unset PAPERCLIP_SIGN / PAPERCLIP_PFX / PAPERCLIP_SIGN_SUBJECT."
        )
    if config.pfx_path and not config.pfx_path.is_file():
        raise SigningError(f"PAPERCLIP_PFX points at a missing file: {config.pfx_path}")

    command = build_signtool_command(binary, config, signtool)
    printable = " ".join(
        "<password>" if part == (config.pfx_password or "") else part for part in command
    )
    print(f"  $ {printable}", file=sys.stderr)

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        # Retry against fallback timestamp servers: a single TSA outage should
        # not fail an otherwise valid signing operation.
        if config.timestamp_url:
            for fallback in FALLBACK_TIMESTAMP_URLS:
                if fallback == config.timestamp_url:
                    continue
                retry = SigningConfig(
                    enabled=True,
                    subject=config.subject,
                    pfx_path=config.pfx_path,
                    pfx_password=config.pfx_password,
                    timestamp_url=fallback,
                    signtool=config.signtool,
                    extra_args=config.extra_args,
                    sha1_hash=config.sha1_hash,
                )
                retry_command = build_signtool_command(binary, retry, signtool)
                print(f"  $ retrying with timestamp server {fallback}", file=sys.stderr)
                completed = subprocess.run(retry_command, capture_output=True, text=True, check=False)
                if completed.returncode == 0:
                    return f"signed ({config.method}, timestamp {fallback})"
        raise SigningError(
            f"signtool failed (exit {completed.returncode}):\n{output[-1200:]}"
        )
    return f"signed ({config.method})"


def parse_version_tuple(version: str, parts: int = 4) -> Tuple[int, ...]:
    """``"0.3.1"`` -> ``(0, 3, 1, 0)`` for a VERSIONINFO resource."""
    numbers = [int(match) for match in _VERSION_PART_RE.findall(version or "")][:parts]
    while len(numbers) < parts:
        numbers.append(0)
    return tuple(numbers[:parts])


def generate_version_info(
    destination: Path,
    *,
    version: str,
    product_name: str = "Paperclip",
    exe_name: str = "paperclip.exe",
    company_name: str = "Paperclip",
    description: str = "Paperclip - orchestrate AI agent teams",
    copyright_text: str = "MIT License - Copyright (c) Paperclip contributors",
) -> Path:
    """Write a PyInstaller ``version_info.txt`` VERSIONINFO resource.

    Embedding this is one of the cheap, real AV mitigations: binaries with no
    version metadata are treated as suspicious by many engines and SmartScreen
    shows "Unknown publisher" with no product context.
    """
    numeric = parse_version_tuple(version)
    dotted = ".".join(str(part) for part in numeric)
    content = f"""# UTF-8
# PyInstaller VERSIONINFO resource, generated by packaging/exe.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric!r},
    prodvers={numeric!r},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          "040904B0",
          [
            StringStruct("CompanyName", {company_name!r}),
            StringStruct("FileDescription", {description!r}),
            StringStruct("FileVersion", {dotted!r}),
            StringStruct("InternalName", "paperclip"),
            StringStruct("LegalCopyright", {copyright_text!r}),
            StringStruct("OriginalFilename", {exe_name!r}),
            StringStruct("ProductName", {product_name!r}),
            StringStruct("ProductVersion", {dotted!r}),
          ],
        )
      ]
    ),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


@dataclass
class AvRiskReport:
    """Which AV/SmartScreen mitigations are active for a build."""

    signed: bool = False
    signing_method: str = "none"
    onefile: bool = True
    has_version_info: bool = False
    upx: bool = False
    console: bool = True
    notes: List[str] = field(default_factory=list)

    @property
    def risk(self) -> str:
        """Coarse assessment, used to warn the operator before distributing."""
        if self.signed and not self.onefile:
            return "low"
        if self.signed or not self.onefile:
            return "moderate"
        return "high"

    def lines(self) -> List[str]:
        out = [
            f"  signed          : {'yes (' + self.signing_method + ')' if self.signed else 'NO'}",
            f"  layout          : {'onefile (self-extracting)' if self.onefile else 'onedir (folder)'}",
            f"  version metadata: {'yes' if self.has_version_info else 'NO'}",
            f"  UPX packed      : {'yes (avoid!)' if self.upx else 'no'}",
            f"  assessed risk   : {self.risk}",
        ]
        out.extend(f"  - {note}" for note in self.notes)
        return out


def av_risk_report(
    *,
    signed: bool,
    signing_method: str = "none",
    onefile: bool = True,
    has_version_info: bool = False,
    plat: Optional[PlatformInfo] = None,
) -> AvRiskReport:
    """Summarise mitigation state and produce actionable guidance."""
    plat = plat or current_platform()
    report = AvRiskReport(
        signed=signed,
        signing_method=signing_method if signed else "none",
        onefile=onefile,
        has_version_info=has_version_info,
        upx=False,  # the spec hard-disables UPX
        console=True,
    )
    if plat.os != WINDOWS:
        report.notes.append("AV/SmartScreen guidance applies to Windows builds; this target is "
                            f"{plat.os}.")
        return report

    if onefile:
        report.notes.append(
            "onefile self-extracts into %TEMP% and runs from there, which is the pattern "
            "AV heuristics flag most. Build with --onedir for far fewer false positives."
        )
    if not has_version_info:
        report.notes.append(
            "no VERSIONINFO resource: engines treat metadata-less binaries as suspicious. "
            "Pass --version-resource (on by default for Windows builds)."
        )
    if not signed:
        report.notes.append(
            "unsigned: SmartScreen will warn and some AV will quarantine. To sign, set "
            "PAPERCLIP_SIGN_SUBJECT (cert in the store) or PAPERCLIP_PFX + "
            "PAPERCLIP_PFX_PASSWORD, then rebuild."
        )
        report.notes.append(
            "signing needs a certificate from a CA (DigiCert, Sectigo, SSL.com, Certum). "
            "It cannot be generated locally, and only OV/EV certs build SmartScreen reputation."
        )
    return report
