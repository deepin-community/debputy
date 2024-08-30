import pytest

from debputy.architecture_support import faked_arch_table
from debputy.commands.debputy_cmd.output import no_fancy_output
from debputy.dh_migration.models import (
    DHMigrationSubstitution,
    AcceptableMigrationIssues,
    FeatureMigration,
)
from debputy.filesystem_scan import FSROOverlay
from debputy.highlevel_manifest import MutableYAMLManifest
from debputy.substitution import SubstitutionImpl, VariableContext

MOCK_ENV = {
    # This conflicts with the dpkg arch table intentionally (to ensure we can tell which one is being resolved)
    "DEB_HOST_ARCHITECTURE": "i386",
}
MOCK_DPKG_ARCH_TABLE = faked_arch_table("amd64", build_arch="i386")
MOCK_VARIABLE_CONTEXT = VariableContext(FSROOverlay.create_root_dir("debian", "debian"))


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "unchanged",
            "unchanged",
        ),
        (
            "unchanged\\{{\n}}",
            "unchanged\\{{\n}}",
        ),  # Newline is not an allowed part of a substitution
        (
            "{{token:DOUBLE_OPEN_CURLY_BRACE}}{{token:NL}}{{token:DOUBLE_CLOSE_CURLY_BRACE}}",
            "{{\n}}",
        ),
        (
            "{{token:DOUBLE_OPEN_CURLY_BRACE}}token:TAB}}{{token:TAB{{token:DOUBLE_CLOSE_CURLY_BRACE}}",
            "{{token:TAB}}{{token:TAB}}",
        ),
        (
            "/usr/lib/{{DEB_HOST_MULTIARCH}}",
            f'/usr/lib/{MOCK_DPKG_ARCH_TABLE["DEB_HOST_MULTIARCH"]}',
        ),
    ],
)
def test_substitution_match(debputy_plugin_feature_set, value, expected) -> None:
    subst = SubstitutionImpl(
        plugin_feature_set=debputy_plugin_feature_set,
        dpkg_arch_table=MOCK_DPKG_ARCH_TABLE,
        environment=MOCK_ENV,
        variable_context=MOCK_VARIABLE_CONTEXT,
    )
    replacement = subst.substitute(value, "test def")
    assert replacement == expected


def test_migrate_substitution() -> None:
    feature_migration = FeatureMigration("test migration", no_fancy_output())
    subst = DHMigrationSubstitution(
        MOCK_DPKG_ARCH_TABLE,
        AcceptableMigrationIssues(frozenset()),
        feature_migration,
        MutableYAMLManifest({}),
    )
    replacement = subst.substitute("usr/lib/${DEB_HOST_MULTIARCH}/foo", "test def")
    assert replacement == "usr/lib/{{DEB_HOST_MULTIARCH}}/foo"
