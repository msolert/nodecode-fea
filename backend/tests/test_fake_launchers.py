"""The fake Abaqus used by the tests must work on Linux, macOS and Windows."""

import os
import subprocess
import sys

from .fake_paths import FAKE_DIR, LAUNCHER


def test_launcher_for_this_platform_exists():
    assert LAUNCHER.exists()
    assert LAUNCHER.name == ("abaqus.bat" if os.name == "nt" else "abaqus")


def test_windows_launcher_has_crlf_line_endings():
    raw = (FAKE_DIR / "bin" / "abaqus.bat").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")  # cmd mishandles labels/goto in LF-only files


def test_fake_cae_accepts_script_argument_split_at_equals(tmp_path):
    """Windows cmd turns 'script=boot.py' into two arguments; the simulated CAE must cope."""
    (tmp_path / "boot.py").write_text("open('booted', 'w').write('yes')\n")
    env = dict(os.environ, PYTHONPATH=str(FAKE_DIR), FAKE_CAE_SECONDS="0")
    for args in (["script=boot.py"], ["script", "boot.py"]):
        (tmp_path / "booted").unlink(missing_ok=True)
        subprocess.run([sys.executable, str(FAKE_DIR / "fake_cae.py"), *args], cwd=tmp_path, env=env, check=True,
                       timeout=30, capture_output=True)
        assert (tmp_path / "booted").read_text() == "yes"
