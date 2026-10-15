# Lets `pytest` run from the repo root without setting PYTHONPATH.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
