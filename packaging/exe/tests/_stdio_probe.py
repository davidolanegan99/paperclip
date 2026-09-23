"""Helper subprocess used by test_runner to prove stdio is inherited, not piped.

The launcher spawns the app with ``stdin/stdout/stderr = None`` so the child owns
the console. This probe re-implements that spawn and asserts the grandchild's
output reaches *our* captured streams -- i.e. nothing was swallowed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        child = Path(tmp) / "child.sh"
        child.write_text(
            "#!/bin/sh\necho STDOUT_OK\necho STDERR_OK >&2\nexit 0\n",
            encoding="utf-8",
        )
        child.chmod(0o755)

        proc = subprocess.Popen(
            ["/bin/sh", str(child)],
            stdin=None,
            stdout=None,
            stderr=None,
            env=dict(os.environ),
            cwd=tmp,
        )
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
