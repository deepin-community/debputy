import os
from typing import Mapping

import pytest
from debian.deb822 import Deb822
from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import (
    DpkgArchitectureBuildProcessValuesTable,
    faked_arch_table,
)
from debputy.filesystem_scan import FSROOverlay
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage, SourcePackage
from debputy.plugin.debputy.debputy_plugin import initialize_debputy_features
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl import DebputyPluginInitializerProvider
from debputy.substitution import (
    NULL_SUBSTITUTION,
    Substitution,
    SubstitutionImpl,
    VariableContext,
)

# Disable dpkg's translation layer.  It is very slow and disabling it makes it easier to debug
# test-failure reports from systems with translations active.
os.environ["DPKG_NLS"] = "0"


@pytest.fixture(scope="session")
def amd64_dpkg_architecture_variables() -> DpkgArchitectureBuildProcessValuesTable:
    return faked_arch_table("amd64")


@pytest.fixture(scope="session")
def dpkg_arch_query() -> DpkgArchTable:
    return DpkgArchTable.load_arch_table()


@pytest.fixture()
def source_package() -> SourcePackage:
    return SourcePackage(
        {
            "Source": "foo",
        }
    )


@pytest.fixture()
def package_single_foo_arch_all_cxt_amd64(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
) -> Mapping[str, BinaryPackage]:
    return {
        p.name: p
        for p in [
            BinaryPackage(
                Deb822(
                    {
                        "Package": "foo",
                        "Architecture": "all",
                    }
                ),
                amd64_dpkg_architecture_variables,
                dpkg_arch_query,
                is_main_package=True,
            )
        ]
    }


@pytest.fixture()
def package_foo_w_udeb_arch_any_cxt_amd64(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
) -> Mapping[str, BinaryPackage]:
    return {
        p.name: p
        for p in [
            BinaryPackage(
                Deb822(
                    {
                        "Package": "foo",
                        "Architecture": "any",
                    }
                ),
                amd64_dpkg_architecture_variables,
                dpkg_arch_query,
                is_main_package=True,
            ),
            BinaryPackage(
                Deb822(
                    {
                        "Package": "foo-udeb",
                        "Architecture": "any",
                        "Package-Type": "udeb",
                    }
                ),
                amd64_dpkg_architecture_variables,
                dpkg_arch_query,
            ),
        ]
    }


@pytest.fixture(scope="session")
def null_substitution() -> Substitution:
    return NULL_SUBSTITUTION


@pytest.fixture(scope="session")
def _empty_debputy_plugin_feature_set() -> PluginProvidedFeatureSet:
    return PluginProvidedFeatureSet()


@pytest.fixture(scope="session")
def amd64_substitution(
    amd64_dpkg_architecture_variables,
    _empty_debputy_plugin_feature_set,
) -> Substitution:
    debian_dir = FSROOverlay.create_root_dir("debian", "debian")
    variable_context = VariableContext(
        debian_dir,
    )
    return SubstitutionImpl(
        plugin_feature_set=_empty_debputy_plugin_feature_set,
        dpkg_arch_table=amd64_dpkg_architecture_variables,
        static_variables=None,
        environment={},
        unresolvable_substitutions=frozenset(["SOURCE_DATE_EPOCH", "PACKAGE"]),
        variable_context=variable_context,
    )


@pytest.fixture(scope="session")
def no_profiles_or_build_options() -> DebBuildOptionsAndProfiles:
    return DebBuildOptionsAndProfiles(environ={})


@pytest.fixture(scope="session")
def debputy_plugin_feature_set(
    _empty_debputy_plugin_feature_set, amd64_substitution
) -> PluginProvidedFeatureSet:
    plugin_metadata = DebputyPluginMetadata(
        plugin_name="debputy",
        api_compat_version=1,
        plugin_initializer=initialize_debputy_features,
        plugin_loader=None,
        plugin_path="<loaded-via-test>",
    )
    feature_set = _empty_debputy_plugin_feature_set
    api = DebputyPluginInitializerProvider(
        plugin_metadata,
        feature_set,
        amd64_substitution,
    )
    api.load_plugin()
    return feature_set


@pytest.fixture
def attribute_path(request) -> AttributePath:
    return AttributePath.builtin_path()[request.node.nodeid]
