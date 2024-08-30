import stat
import os
from typing import Iterator

import pytest

from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
    package_metadata_context,
)

STUB_CMD = os.path.join(os.path.dirname(__file__), "perl-ssl_test.sh")


@pytest.fixture(scope="session")
def perl_ssl_stub_cmd() -> Iterator[None]:
    os.environ["_PERL_SSL_DEFAULTS_TEST_PATH"] = STUB_CMD
    mode = stat.S_IMODE(os.stat(STUB_CMD).st_mode)
    if (mode & 0o500) != 0o500:
        os.chmod(STUB_CMD, mode | 0o500)
    yield
    try:
        del os.environ["_PERL_SSL_DEFAULTS_TEST_PATH"]
    except KeyError:
        pass


def test_perl_openssl(perl_ssl_stub_cmd) -> None:
    plugin = initialize_plugin_under_test()
    fs = build_virtual_file_system([])
    context = package_metadata_context(package_fields={"Architecture": "all"})
    metadata = plugin.run_metadata_detector("perl-openssl-abi", fs, context)
    assert metadata.substvars["perl:Depends"] == "perl-openssl-abi-3"
