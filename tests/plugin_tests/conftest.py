import os
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def workaround_debputys_own_test_suite() -> Iterator[None]:
    # This fixture is only required as long as the tests are run inside `debputy`'s
    # own test suite.  If you copy out a plugin + tests, you should *not* need this
    # fixture.
    #
    # The problem appears because in the debputy source package, these plugins are
    # always provided in their "installed" location.
    orig = os.environ.get("DEBPUTY_TEST_PLUGIN_LOCATION")
    os.environ["DEBPUTY_TEST_PLUGIN_LOCATION"] = "installed"
    yield
    if orig is None:
        del os.environ["DEBPUTY_TEST_PLUGIN_LOCATION"]
    else:
        os.environ["DEBPUTY_TEST_PLUGIN_LOCATION"] = orig
