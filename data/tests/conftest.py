"""Import bootstrap for the V3 monorepo layout.

V2 ran this suite with ``1_DATA`` as the working directory, so cwd put
``scripts/`` on the import path and the stewardship scripts' documented
fallback import ``from scripts.methodology_contract import ...`` resolved when
tests loaded them by file path. The V3 gate runs pytest from the repo root, so
``data/`` (the ``scripts`` package's parent) is put on ``sys.path`` here instead.
"""
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.append(str(REPOSITORY_ROOT))
