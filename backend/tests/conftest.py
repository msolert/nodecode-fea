import os
import sys

# The fake Abaqus launchers run the fakes with the same Python as the tests (not whatever 'python' is on PATH).
os.environ.setdefault("NOCODE_TEST_PYTHON", sys.executable)
