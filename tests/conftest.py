import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures", "synthetic"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture
def store_root(tmp_path):
    return str(tmp_path / "store")


@pytest.fixture
def synthetic_kimi_fixture():
    return os.path.join(FIXTURES, "kimi.json")
