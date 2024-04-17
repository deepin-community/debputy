import pytest
from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.packages import DctrlParser
from debputy.util import setup_logging

try:
    from lsprotocol.types import Diagnostic
    from debputy.lsp.spellchecking import disable_spellchecking

    HAS_LSPROTOCOL = True
except ImportError:
    HAS_LSPROTOCOL = False

    def disable_spellchecking() -> None:
        pass


@pytest.fixture(scope="session", autouse=True)
def enable_logging() -> None:
    if not HAS_LSPROTOCOL:
        pytest.skip("Missing python3-lsprotocol")
    setup_logging(reconfigure_logging=True)


@pytest.fixture(scope="session", autouse=True)
def disable_spellchecking_fixture() -> None:
    # CI/The buildd does not install relevant, so this is mostly about ensuring
    # consistent behavior between clean and "unclean" build/test environments
    disable_spellchecking()


@pytest.fixture
def lint_dctrl_parser(
    dpkg_arch_query: DpkgArchTable,
    amd64_dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
    no_profiles_or_build_options: DebBuildOptionsAndProfiles,
) -> DctrlParser:
    return DctrlParser(
        frozenset(),
        frozenset(),
        True,
        True,
        amd64_dpkg_architecture_variables,
        dpkg_arch_query,
        no_profiles_or_build_options,
    )
