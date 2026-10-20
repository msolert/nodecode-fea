"""Where the fake Abaqus lives, and which launcher to use on this platform."""

import os
from pathlib import Path

FAKE_DIR = Path(__file__).parent / "fake_abaqus"
# shutil.which() only finds files with a PATHEXT extension on Windows, hence the .bat twin.
LAUNCHER = FAKE_DIR / "bin" / ("abaqus.bat" if os.name == "nt" else "abaqus")
