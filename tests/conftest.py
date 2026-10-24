import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fresh_db(tmp_path):
    import database

    database.init_db(tmp_path / "agent.db")
    return database


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
