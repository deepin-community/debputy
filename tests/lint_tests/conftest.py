import pytest

from debputy.lsp.lsp_features import lsp_set_plugin_features
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.util import setup_logging

try:
    from lsprotocol.types import Diagnostic

    HAS_LSPROTOCOL = True
except ImportError:
    HAS_LSPROTOCOL = False


@pytest.fixture(scope="session", autouse=True)
def enable_logging() -> None:
    if not HAS_LSPROTOCOL:
        pytest.skip("Missing python3-lsprotocol")
    setup_logging(reconfigure_logging=True)


@pytest.fixture(autouse=True)
def setup_feature_set(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
) -> None:
    lsp_set_plugin_features(debputy_plugin_feature_set)
    try:
        yield
    finally:
        lsp_set_plugin_features(None)
