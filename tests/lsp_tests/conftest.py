import pytest
from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.util import setup_logging

try:
    from pygls.server import LanguageServer
    from lsprotocol.types import (
        InitializeParams,
        ClientCapabilities,
        GeneralClientCapabilities,
        PositionEncodingKind,
        TextDocumentItem,
        Position,
        CompletionParams,
        TextDocumentIdentifier,
        HoverParams,
        MarkupContent,
    )
    from debputy.lsp.debputy_ls import DebputyLanguageServer

    HAS_PYGLS = True
except ImportError:
    HAS_PYGLS = False


@pytest.fixture(scope="session", autouse=True)
def enable_logging() -> None:
    setup_logging(log_only_to_stderr=True, reconfigure_logging=True)


@pytest.fixture
def lsp_dctrl_parser(
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


@pytest.fixture()
def ls(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lsp_dctrl_parser: DctrlParser,
) -> "DebputyLanguageServer":
    if not HAS_PYGLS:
        pytest.skip("Missing pygls")
    ls = DebputyLanguageServer("debputy", "v<test>")
    ls.lsp.lsp_initialize(
        InitializeParams(
            ClientCapabilities(
                general=GeneralClientCapabilities(
                    position_encodings=[PositionEncodingKind.Utf32],
                )
            )
        )
    )
    ls.plugin_feature_set = debputy_plugin_feature_set
    ls.dctrl_parser = lsp_dctrl_parser
    return ls
