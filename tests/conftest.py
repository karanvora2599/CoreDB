import json
import shutil
import sys
from pathlib import Path

import pytest

import coredb

# tools/ is developer tooling, not part of the installed `coredb` package, so
# it isn't importable from an editable install alone - put the repo root on
# the path so tests can reach the dataset generator they share constants with.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def db(tmp_path):
    database = coredb.open(str(tmp_path / "test.db"))
    yield database
    database.close()


# ----------------------------------------------------------------------
# The Meridian dataset (tests/test_large_dataset.py). Generated outside the
# repository and never committed - see tools/gen_test_dataset.py - so every
# fixture here degrades to a skip rather than a failure when it is absent.
# ----------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--deep", action="store_true", default=False,
        help="also run the slow whole-graph tests against the Meridian dataset "
             "(global centrality, full dump/restore round-trip)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "deep: slow whole-graph test over the large dataset; needs --deep")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--deep"):
        return
    skip_deep = pytest.mark.skip(reason="needs --deep")
    for item in items:
        if "deep" in item.keywords:
            item.add_marker(skip_deep)


class MeridianDataset:
    """An opened copy of the generated dataset, plus the manifest that
    describes it. `ground_truth` is what the generator planted by
    construction; `manifest["stats"]` is what the engine reported at build
    time (a regression signal, not an independent oracle)."""

    def __init__(self, db, manifest, path):
        self.db = db
        self.manifest = manifest
        self.path = path
        self.ground_truth = manifest["ground_truth"]

    def gt(self, *keys):
        node = self.ground_truth
        for key in keys:
            node = node[key]
        return node


@pytest.fixture(scope="session")
def meridian_manifest():
    from tools.dataset_spec import SPEC_VERSION
    from tools.gen_test_dataset import MANIFEST_NAME, dataset_dir

    directory = dataset_dir()
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.exists():
        pytest.skip(
            f"no Meridian dataset at {directory} - build it with "
            "`python -m tools.gen_test_dataset` (or point COREDB_TEST_DATASET elsewhere)"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("spec_version") != SPEC_VERSION:
        pytest.skip(
            f"dataset at {directory} was built from spec_version="
            f"{manifest.get('spec_version')}, this checkout expects {SPEC_VERSION} - "
            "rebuild it with `python -m tools.gen_test_dataset --force`"
        )
    manifest["_dir"] = str(directory)
    return manifest


@pytest.fixture(scope="session")
def meridian(meridian_manifest, tmp_path_factory):
    """The dataset's LMDB database, opened from a temporary copy.

    Copied rather than opened in place for two reasons: opening an LMDB
    environment immediately grows its file to `map_size` (so opening the
    shipped copy would inflate a 51 MB directory to hundreds of MB, every
    run), and a test that writes must not mutate the shared dataset.
    """
    from coredb.engine import SCHEMA_VERSION

    source = Path(meridian_manifest["_dir"]) / meridian_manifest["files"]["db"]
    if meridian_manifest.get("schema_version") != SCHEMA_VERSION:
        pytest.skip(
            f"dataset was built against engine schema_version="
            f"{meridian_manifest.get('schema_version')}, this checkout is at "
            f"{SCHEMA_VERSION} - rebuild it with "
            "`python -m tools.gen_test_dataset --force`"
        )

    target = tmp_path_factory.mktemp("meridian") / "graph.db"
    shutil.copytree(source, target)
    database = coredb.open(str(target), map_size=meridian_manifest["db"]["recommended_map_size"])
    yield MeridianDataset(database, meridian_manifest, Path(meridian_manifest["_dir"]))
    database.close()
