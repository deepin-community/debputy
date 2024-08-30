import os
from typing import Iterator

import pytest

from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
    package_metadata_context,
)

DATA_FILE = os.path.join(os.path.dirname(__file__), "numpy3_test.data")


@pytest.fixture(scope="session")
def numpy3_stub_data_file() -> Iterator[None]:
    os.environ["_NUMPY_TEST_PATH"] = DATA_FILE
    yield
    try:
        del os.environ["_NUMPY_TEST_PATH"]
    except KeyError:
        pass


def test_numpy3_plugin_arch_all(numpy3_stub_data_file) -> None:
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    context = package_metadata_context(package_fields={"Architecture": "all"})
    metadata = plugin.run_metadata_detector("numpy-depends", fs, context)
    assert metadata.substvars["python3:Depends"] == "python3-numpy"


def test_numpy3_plugin_arch_any(numpy3_stub_data_file) -> None:
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    context = package_metadata_context(package_fields={"Architecture": "any"})
    metadata = plugin.run_metadata_detector("numpy-depends", fs, context)
    expected = "python3-numpy (>= 1:1.22.0), python3-numpy-abi9"
    assert metadata.substvars["python3:Depends"] == expected
