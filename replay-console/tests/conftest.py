import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ROOT = PROJECT.parent
for path in (str(PROJECT), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
