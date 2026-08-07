import pytest

import coredb


@pytest.fixture
def db(tmp_path):
    database = coredb.open(str(tmp_path / "test.db"))
    yield database
    database.close()
