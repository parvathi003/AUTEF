import sys
from pathlib import Path

# The package lives under src/ and is not installed during development.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SAMPLE_PROJECT = Path(__file__).resolve().parent / "sample_project"

# The fixture project's suite is deliberately broken; it is input to these
# tests, not part of them.
collect_ignore = ["sample_project"]
