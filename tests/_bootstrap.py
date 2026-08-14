"""Shared test bootstrap.

Every test file starts with ``import _bootstrap``. It:

* puts the repo's ``opt/rpi-player`` on ``sys.path`` (so tests exercise the
  working copy, not whatever is deployed to /opt),
* points ``RPI_PLAYER_HOME`` at the repo's ``etc/`` so config loads,
* shims ``tomllib`` on Python < 3.11,
* fails with an actionable message rather than a bare ImportError.

``RPI_PLAYER_TEST_ROOT`` overrides the tree under test, e.g. to run the suite
against the deployed copy:

    RPI_PLAYER_TEST_ROOT=/opt/rpi-player ./tests/run-all.sh
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RPI_PLAYER_TEST_ROOT", _HERE.parent / "opt" / "rpi-player"))

if not (ROOT / "player").is_dir():
    sys.exit(
        f"FATAL: no player/ package under {ROOT}\n"
        f"  Run the suite from the repository root, or set RPI_PLAYER_TEST_ROOT."
    )

sys.path.insert(0, str(ROOT))
os.environ.setdefault("RPI_PLAYER_HOME", str(ROOT))

# Python 3.11+ has tomllib in the stdlib (Debian Trixie ships 3.13). Older
# interpreters need the tomli backport.
try:
    import tomllib  # noqa: F401
except ImportError:  # pragma: no cover
    try:
        import tomli

        sys.modules["tomllib"] = tomli
    except ImportError:
        sys.exit(
            "FATAL: Python < 3.11 and tomli is not installed.\n"
            "  pip install tomli   (or run with the deployed venv:\n"
            "  PYTHON=/opt/rpi-player/venv/bin/python ./tests/run-all.sh)"
        )


def require(*modules: str) -> None:
    """Exit with a useful message if a third-party module is missing."""
    hints = {
        "serial": "pyserial",
        "mpd": "python-mpd2",
        "PIL": "Pillow",
        "StreamDeck": "streamdeck",
    }
    missing = []
    for module in modules:
        try:
            __import__(module)
        except ImportError:
            missing.append(hints.get(module, module))
    if missing:
        sys.exit(
            f"SKIP: missing {', '.join(missing)}\n"
            f"  {sys.executable} -m pip install {' '.join(missing)}\n"
            f"  or: PYTHON=/opt/rpi-player/venv/bin/python ./tests/run-all.sh"
        )


def guard_player_package() -> None:
    """Catch the 'deployed tree is a different scaffold' failure early.

    If ``player.config`` will not import but ``player`` will, the tree under
    test is almost certainly a stub — which is exactly the state that produces
    'SyntaxError: invalid syntax' from systemd at runtime.
    """
    try:
        import player  # noqa: F401
    except ImportError as exc:
        sys.exit(f"FATAL: cannot import 'player' from {ROOT}: {exc}")

    try:
        import player.config  # noqa: F401
    except ImportError as exc:
        sys.exit(
            f"FATAL: 'player' imported from {ROOT} but player.config did not: {exc}\n"
            f"  The tree under test looks like a stub, not this repository.\n"
            f"  Check:  head -n1 {ROOT}/player/config.py\n"
            f"  If that prints a shebang for bash, see docs/RECOVERY.md."
        )


guard_player_package()
