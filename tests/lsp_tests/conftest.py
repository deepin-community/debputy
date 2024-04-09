import pytest

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
    from debputy.lsp.lsp_features import lsp_set_plugin_features

    HAS_PYGLS = True
except ImportError:
    HAS_PYGLS = False


@pytest.fixture(scope="session", autouse=True)
def enable_logging() -> None:
    setup_logging(log_only_to_stderr=True, reconfigure_logging=True)


@pytest.fixture()
def ls(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
) -> "LanguageServer":
    if not HAS_PYGLS:
        pytest.skip("Missing pygls")
    ls = LanguageServer("debputy", "v<test>")
    ls.lsp.lsp_initialize(
        InitializeParams(
            ClientCapabilities(
                general=GeneralClientCapabilities(
                    position_encodings=[PositionEncodingKind.Utf32],
                )
            )
        )
    )
    lsp_set_plugin_features(debputy_plugin_feature_set)
    try:
        yield ls
    finally:
        lsp_set_plugin_features(None)
