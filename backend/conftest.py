"""
Pytest configuration: make the ``app`` package importable when running
``pytest`` from the ``implementation/backend/`` directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
