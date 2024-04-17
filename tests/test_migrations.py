import io
import textwrap
from typing import Iterable, Callable, Optional, List, Tuple, Sequence

import pytest

from debputy.dh_migration.migrators import Migrator
from debputy.dh_migration.migrators_impl import (
    migrate_tmpfile,
    migrate_lintian_overrides_files,
    detect_pam_files,
    migrate_doc_base_files,
    migrate_installexamples_file,
    migrate_installdocs_file,
    migrate_install_file,
    migrate_maintscript,
    migrate_links_files,
    detect_dh_addons,
    migrate_not_installed_file,
    migrate_installman_file,
    migrate_bash_completion,
    migrate_installinfo_file,
    migrate_dh_installsystemd_files,
    detect_obsolete_substvars,
    MIGRATION_TARGET_DH_DEBPUTY,
    MIGRATION_TARGET_DH_DEBPUTY_RRR,
    detect_dh_addons_zz_debputy_rrr,
)
from debputy.dh_migration.models import (
    FeatureMigration,
    AcceptableMigrationIssues,
    UnsupportedFeature,
)
from debputy.highlevel_manifest import HighLevelManifest
from debputy.highlevel_manifest_parser import YAMLManifestParser
from debputy.plugin.api import virtual_path_def, VirtualPath
from debputy.plugin.api.test_api import (
    build_virtual_file_system,
)


DEBIAN_DIR_ENTRY = virtual_path_def(".", fs_path="/nowhere/debian")


@pytest.fixture()
def manifest_parser_pkg_foo_factory(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
    source_package,
    package_single_foo_arch_all_cxt_amd64,
    amd64_substitution,
    no_profiles_or_build_options,
    debputy_plugin_feature_set,
) -> Callable[[], YAMLManifestParser]:
    # We need an empty directory to avoid triggering packager provided files.
    debian_dir = build_virtual_file_system([])

    def _factory():
        return YAMLManifestParser(
            "debian/test-debputy.manifest",
            source_package,
            package_single_foo_arch_all_cxt_amd64,
            amd64_substitution,
            amd64_dpkg_architecture_variables,
            dpkg_arch_query,
            no_profiles_or_build_options,
            debputy_plugin_feature_set,
            debian_dir=debian_dir,
        )

    return _factory


@pytest.fixture(scope="session")
def accept_no_migration_issues() -> AcceptableMigrationIssues:
    return AcceptableMigrationIssues(frozenset())


@pytest.fixture(scope="session")
def accept_any_migration_issues() -> AcceptableMigrationIssues:
    return AcceptableMigrationIssues(frozenset(["ALL"]))


@pytest.fixture
def empty_manifest_pkg_foo(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
) -> HighLevelManifest:
    return manifest_parser_pkg_foo_factory().build_manifest()


def run_migrator(
    migrator: Migrator,
    path: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
    *,
    migration_target=MIGRATION_TARGET_DH_DEBPUTY,
) -> FeatureMigration:
    feature_migration = FeatureMigration(migrator.__name__)
    migrator(
        path,
        manifest,
        acceptable_migration_issues,
        feature_migration,
        migration_target,
    )
    return feature_migration


def _assert_unsupported_feature(
    migrator: Migrator,
    path: VirtualPath,
    manifest: HighLevelManifest,
    acceptable_migration_issues: AcceptableMigrationIssues,
):
    with pytest.raises(UnsupportedFeature) as e:
        run_migrator(migrator, path, manifest, acceptable_migration_issues)
    return e


def _write_manifest(manifest: HighLevelManifest) -> str:
    with io.StringIO() as fd:
        manifest.mutable_manifest.write_to(fd)
        return fd.getvalue()


def _verify_migrator_generates_parsable_manifest(
    migrator: Migrator,
    parser_factory: Callable[[], YAMLManifestParser],
    acceptable_migration_issues: AcceptableMigrationIssues,
    dh_config_name: str,
    dh_config_content: str,
    expected_manifest_content: str,
    expected_warnings: Optional[List[str]] = None,
    expected_renamed_paths: Optional[List[Tuple[str, str]]] = None,
    expected_removals: Optional[List[str]] = None,
    required_plugins: Optional[Sequence[str]] = tuple(),
    dh_config_mode: Optional[int] = None,
) -> None:
    # No file, no changes
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    migration = run_migrator(
        migrator,
        empty_fs,
        parser_factory().build_manifest(),
        acceptable_migration_issues,
    )

    assert not migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    if dh_config_mode is None:
        if dh_config_content.startswith(("#!/usr/bin/dh-exec", "#! /usr/bin/dh-exec")):
            dh_config_mode = 0o755
        else:
            dh_config_mode = 0o644

    # Test with a dh_config file now
    fs_w_dh_config = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                dh_config_name,
                fs_path=f"/nowhere/debian/{dh_config_name}",
                content=dh_config_content,
                mode=dh_config_mode,
            ),
        ]
    )
    manifest = parser_factory().build_manifest()

    migration = run_migrator(
        migrator,
        fs_w_dh_config,
        manifest,
        acceptable_migration_issues,
    )

    assert migration.anything_to_do
    if expected_warnings is not None:
        assert migration.warnings == expected_warnings
    else:
        assert not migration.warnings
    assert migration.remove_paths_on_success == [f"/nowhere/debian/{dh_config_name}"]
    if expected_removals is None:
        assert migration.remove_paths_on_success == [
            f"/nowhere/debian/{dh_config_name}"
        ]
    else:
        assert migration.remove_paths_on_success == expected_removals
    if expected_renamed_paths is not None:
        assert migration.rename_paths_on_success == expected_renamed_paths
    else:
        assert not migration.rename_paths_on_success
    assert tuple(migration.required_plugins) == tuple(required_plugins)
    actual_manifest = _write_manifest(manifest)
    assert actual_manifest == expected_manifest_content

    # Verify that the output is actually parsable
    parser_factory().parse_manifest(fd=actual_manifest)


def test_migrate_tmpfile(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = migrate_tmpfile
    empty_debian_dir = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    migration = run_migrator(
        migrator,
        empty_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert not migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success

    tmpfile_debian_dir = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("tmpfile", fs_path="/nowhere/debian/tmpfile"),
        ]
    )

    migration = run_migrator(
        migrator,
        tmpfile_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert migration.rename_paths_on_success == [
        ("/nowhere/debian/tmpfile", "/nowhere/debian/tmpfiles")
    ]

    tmpfile_debian_dir = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            # Use real paths here to make `cmp -s` discover that they are the same
            virtual_path_def("tmpfile", fs_path="debian/control"),
            virtual_path_def("tmpfiles", fs_path="debian/control"),
        ]
    )

    migration = run_migrator(
        migrator,
        tmpfile_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    assert migration.anything_to_do
    assert not migration.warnings
    assert migration.remove_paths_on_success == ["debian/control"]
    assert not migration.rename_paths_on_success

    conflict_tmpfile_debian_dir = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            # Use real paths here to make `cmp -s` discover a difference
            virtual_path_def("tmpfile", fs_path="debian/control"),
            virtual_path_def("tmpfiles", fs_path="debian/changelog"),
        ]
    )

    migration = run_migrator(
        migrator,
        conflict_tmpfile_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert len(migration.warnings) == 1
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success

    conflict_tmpfile_debian_dir = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("tmpfile", fs_path="/nowhere/debian/tmpfile"),
            virtual_path_def("tmpfiles/", fs_path="/nowhere/debian/tmpfiles"),
        ]
    )

    migration = run_migrator(
        migrator,
        conflict_tmpfile_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert len(migration.warnings) == 1
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success

    conflict_tmpfile_debian_dir = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "tmpfile",
                link_target="/nowhere/debian/tmpfiles",
                fs_path="/nowhere/debian/tmpfile",
            ),
        ]
    )

    migration = run_migrator(
        migrator,
        conflict_tmpfile_debian_dir,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert len(migration.warnings) == 1
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success


def test_migrate_lintian_overrides_files(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
    accept_any_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = migrate_lintian_overrides_files
    no_override_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    single_noexec_override_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "foo.lintian-overrides",
                fs_path="/nowhere/no-exec/debian/foo.lintian-overrides",
            ),
        ]
    )
    single_exec_override_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "foo.lintian-overrides",
                fs_path="/nowhere/exec/debian/foo.lintian-overrides",
                mode=0o755,
            ),
        ]
    )
    for no_issue_fs in [no_override_fs, single_noexec_override_fs]:
        migration = run_migrator(
            migrator,
            no_issue_fs,
            empty_manifest_pkg_foo,
            accept_no_migration_issues,
        )

        assert not migration.anything_to_do
        assert not migration.warnings
        assert not migration.remove_paths_on_success
        assert not migration.rename_paths_on_success

    _assert_unsupported_feature(
        migrator,
        single_exec_override_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    migration = run_migrator(
        migrator,
        single_exec_override_fs,
        empty_manifest_pkg_foo,
        accept_any_migration_issues,
    )

    assert migration.anything_to_do
    assert len(migration.warnings) == 1
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success


def test_detect_pam_files(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_pam_files
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    pam_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("pam", fs_path="/nowhere/debian/foo.pam"),
        ]
    )

    migration = run_migrator(
        migrator,
        empty_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert not migration.anything_to_do
    assert not migration.warnings
    assert migration.assumed_compat is None
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success

    migration = run_migrator(
        migrator,
        pam_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert not migration.anything_to_do
    assert not migration.warnings
    assert migration.assumed_compat == 14
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success


def test_migrate_doc_base_files(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = migrate_doc_base_files
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    doc_base_ok_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("doc-base", fs_path="/nowhere/debian/doc-base"),
            virtual_path_def(
                "foo.doc-base.EX", fs_path="/nowhere/debian/foo.doc-base.EX"
            ),
        ]
    )
    doc_base_migration_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "foo.doc-base.bar", fs_path="/nowhere/debian/foo.doc-base.bar"
            ),
        ]
    )

    for no_change_fs in [empty_fs, doc_base_ok_fs]:
        migration = run_migrator(
            migrator,
            no_change_fs,
            empty_manifest_pkg_foo,
            accept_no_migration_issues,
        )

        assert not migration.anything_to_do
        assert not migration.warnings
        assert not migration.remove_paths_on_success
        assert not migration.rename_paths_on_success

    migration = run_migrator(
        migrator,
        doc_base_migration_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert migration.rename_paths_on_success == [
        ("/nowhere/debian/foo.doc-base.bar", "/nowhere/debian/foo.bar.doc-base")
    ]


def test_migrate_dh_installsystemd_files(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = migrate_dh_installsystemd_files
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])
    files_ok_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("@service", fs_path="/nowhere/debian/@service"),
            virtual_path_def("foo.@service", fs_path="/nowhere/debian/foo.@service"),
        ]
    )
    migration_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def("foo@.service", fs_path="/nowhere/debian/foo@.service"),
        ]
    )

    for no_change_fs in [empty_fs, files_ok_fs]:
        migration = run_migrator(
            migrator,
            no_change_fs,
            empty_manifest_pkg_foo,
            accept_no_migration_issues,
        )

        assert not migration.anything_to_do
        assert not migration.warnings
        assert not migration.remove_paths_on_success
        assert not migration.rename_paths_on_success

    migration = run_migrator(
        migrator,
        migration_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert migration.rename_paths_on_success == [
        ("/nowhere/debian/foo@.service", "/nowhere/debian/foo.@service")
    ]


def test_migrate_installexamples_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
        bar
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-examples:
                sources:
                - foo/*
                - bar
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installexamples_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "examples",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_installinfo_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
        bar
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-docs:
                sources:
                - foo/*
                - bar
                dest-dir: '{{path:GNU_INFO_DIR}}'
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installinfo_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "info",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_installinfo_file_conditionals(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        #!/usr/bin/dh-exec
        foo/* <!pkg.foo.noinfo>
        bar <!pkg.foo.noinfo>
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-docs:
                sources:
                - foo/*
                - bar
                dest-dir: '{{path:GNU_INFO_DIR}}'
                when:
                  build-profiles-matches: <!pkg.foo.noinfo>
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installinfo_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "info",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_installexamples_file_single_source(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-examples:
                source: foo/*
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installexamples_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "examples",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_installdocs_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
        bar
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-docs:
                sources:
                - foo/*
                - bar
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installdocs_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "docs",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_installdocs_file_single_source(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-docs:
                source: foo/*
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_installdocs_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "docs",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        bar usr/bin
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                source: bar
                dest-dir: usr/bin
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file_conditionals_simple_arch(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        #!/usr/bin/dh-exec
        bar usr/bin  [linux-any]
        foo usr/bin  [linux-any]
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - bar
                - foo
                dest-dir: usr/bin
                when:
                  arch-matches: linux-any
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file_util_linux_locales(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    # Parts of the `d/util-linux-locales.install` file. It uses d/tmp for most of its paths
    # and that breaks the default dest-dir (dh_install always strips --sourcedir, `debputy`
    # currently does not)
    dh_config_content = textwrap.dedent(
        """\
        #!/usr/bin/dh-exec
        usr/share/locale/*/*/util-linux.mo

        # bsdextrautils
        debian/tmp/usr/share/man/*/man1/col.1 <!nodoc>

        debian/tmp/usr/share/man/*/man3/libblkid.3 <!nodoc>
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - usr/share/man/*/man1/col.1
                - usr/share/man/*/man3/libblkid.3
                when:
                  build-profiles-matches: <!nodoc>
            - install:
                source: usr/share/locale/*/*/util-linux.mo
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file_conditionals_simple_combined_cond(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    for cond in ["<!foo> <!bar> [linux-any]", "[linux-any] <!foo> <!bar>"]:
        dh_config_content = textwrap.dedent(
            """\
            #!/usr/bin/dh-exec
            bar usr/bin  {CONDITION}
            foo usr/bin  {CONDITION}
        """
        ).format(CONDITION=cond)
        expected_manifest_content = textwrap.dedent(
            """\
                manifest-version: '0.1'
                installations:
                - install:
                    sources:
                    - bar
                    - foo
                    dest-dir: usr/bin
                    when:
                      all-of:
                      - arch-matches: linux-any
                      - build-profiles-matches: <!foo> <!bar>
        """
        )
        _verify_migrator_generates_parsable_manifest(
            migrate_install_file,
            manifest_parser_pkg_foo_factory,
            accept_no_migration_issues,
            "install",
            dh_config_content,
            expected_manifest_content,
        )


def test_migrate_install_file_conditionals_unknown_subst(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_any_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        #!/usr/bin/dh-exec
        bar ${unknown_substvar}
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            definitions:
              variables:
                unknown_substvar: 'TODO: Provide variable value for unknown_substvar'
            installations:
            - install:
                source: bar
                dest-dir: '{{unknown_substvar}}'
    """
    )
    expected_warning = (
        "TODO: MANUAL MIGRATION of unresolved substitution variable {{unknown_substvar}}"
        ' from ./install line 2 token "${unknown_substvar}"'
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_any_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
        expected_warnings=[expected_warning],
    )


def test_migrate_install_file_multidest(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        # Issue #66
        # - observed in kafs-client / the original install file copied in here.

        src/aklog-kafs        usr/bin
        src/kafs-check-config usr/bin
        #
        src/kafs-preload usr/sbin
        #
        src/kafs-dns     usr/libexec
        #
        conf/cellservdb.conf usr/share/kafs-client
        conf/client.conf     etc/kafs
        #
        conf/kafs_dns.conf etc/request-key.d
        #
        conf/cellservdb.conf usr/share/kafs
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - src/aklog-kafs
                - src/kafs-check-config
                dest-dir: usr/bin
            - install:
                source: src/kafs-preload
                dest-dir: usr/sbin
            - install:
                source: src/kafs-dns
                dest-dir: usr/libexec
            - install:
                source: conf/client.conf
                dest-dir: etc/kafs
            - install:
                source: conf/kafs_dns.conf
                dest-dir: etc/request-key.d
            - multi-dest-install:
                source: conf/cellservdb.conf
                dest-dirs:
                - usr/share/kafs-client
                - usr/share/kafs
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file_multidest_default_dest(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        # Relaed to issue #66 - testing corner case not present in the original install file

        src/aklog-kafs        usr/bin
        src/kafs-check-config usr/bin
        #
        src/kafs-preload usr/sbin
        #
        src/kafs-dns     usr/libexec
        #
        usr/share/kafs-client/cellservdb.conf
        conf/client.conf     etc/kafs
        #
        conf/kafs_dns.conf etc/request-key.d
        #
        usr/share/kafs-client/cellservdb.conf usr/share/kafs
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - src/aklog-kafs
                - src/kafs-check-config
                dest-dir: usr/bin
            - install:
                source: src/kafs-preload
                dest-dir: usr/sbin
            - install:
                source: src/kafs-dns
                dest-dir: usr/libexec
            - install:
                source: conf/client.conf
                dest-dir: etc/kafs
            - install:
                source: conf/kafs_dns.conf
                dest-dir: etc/request-key.d
            - multi-dest-install:
                source: usr/share/kafs-client/cellservdb.conf
                dest-dirs:
                - usr/share/kafs
                - usr/share/kafs-client
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_install_file_multidest_default_dest_warning(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        # Relaed to issue #66 - testing corner case not present in the original install file

        src/aklog-kafs        usr/bin
        src/kafs-check-config usr/bin
        #
        src/kafs-preload usr/sbin
        #
        src/kafs-dns     usr/libexec
        #
        usr/share/kafs-*/cellservdb.conf
        conf/client.conf     etc/kafs
        #
        conf/kafs_dns.conf etc/request-key.d
        #
        usr/share/kafs-*/cellservdb.conf usr/share/kafs
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - src/aklog-kafs
                - src/kafs-check-config
                dest-dir: usr/bin
            - install:
                source: src/kafs-preload
                dest-dir: usr/sbin
            - install:
                source: src/kafs-dns
                dest-dir: usr/libexec
            - install:
                source: conf/client.conf
                dest-dir: etc/kafs
            - install:
                source: conf/kafs_dns.conf
                dest-dir: etc/request-key.d
            - multi-dest-install:
                source: usr/share/kafs-*/cellservdb.conf
                dest-dirs:
                - usr/share/kafs
                - 'FIXME: usr/share/kafs-* (could not reliably compute the dest dir)'
    """
    )
    warnings = [
        "TODO: FIXME left in dest-dir(s) of some installation rules."
        " Please review these and remove the FIXME (plus correct as necessary)"
    ]
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
        expected_warnings=warnings,
    )


def test_migrate_installman_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        man/foo.1 man/bar.1
        man2/*.2
        man3/bar.3 man3/bar.de.3
        man/de/man3/bar.pl.3
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install-man:
                sources:
                - man/foo.1
                - man/bar.1
                - man2/*.2
                - man/de/man3/bar.pl.3
            - install-man:
                sources:
                - man3/bar.3
                - man3/bar.de.3
                language: derive-from-basename
    """
    )
    expected_warnings = [
        'Detected man pages that might rely on "derive-from-basename" logic.  Please double check'
        " that the generated `install-man` rules are correct"
    ]
    _verify_migrator_generates_parsable_manifest(
        migrate_installman_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "manpages",
        dh_config_content,
        expected_manifest_content,
        expected_warnings=expected_warnings,
    )


def test_migrate_install_dh_exec_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        #!/usr/bin/dh-exec

        foo/script.sh => usr/bin/script
        => usr/bin/bar
        usr/bin/* usr/share/foo/extra/* usr/share/foo/extra
        another-util usr/share/foo/extra
        # This will not be merged with `=> usr/bin/bar`
        usr/share/foo/features
        usr/share/foo/bugs
        some-file.txt usr/share/foo/online-doc
        # TODO: Support migration of these
        # pathA pathB  conditional/arch [linux-anx]
        # <!pkg.foo.condition>  another-path conditional/profile
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                source: usr/bin/bar
            - install:
                source: foo/script.sh
                as: usr/bin/script
            - install:
                sources:
                - usr/bin/*
                - usr/share/foo/extra/*
                - another-util
                dest-dir: usr/share/foo/extra
            - install:
                source: some-file.txt
                dest-dir: usr/share/foo/online-doc
            - install:
                sources:
                - usr/share/foo/features
                - usr/share/foo/bugs
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_install_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "install",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_maintscript(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        rm_conffile /etc/foo.conf
        mv_conffile /etc/bar.conf /etc/new-foo.conf 1.0~ bar
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            packages:
              foo:
                conffile-management:
                - remove:
                    path: /etc/foo.conf
                - rename:
                    source: /etc/bar.conf
                    target: /etc/new-foo.conf
                    prior-to-version: 1.0~
                    owning-package: bar
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_maintscript,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "maintscript",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_not_installed_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*.txt bar/${DEB_HOST_MULTIARCH}/*.so*
        baz/script.sh
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - discard:
              - foo/*.txt
              - bar/{{DEB_HOST_MULTIARCH}}/*.so*
              - baz/script.sh
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_not_installed_file,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "not-installed",
        dh_config_content,
        expected_manifest_content,
    )


def test_migrate_links_files(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        usr/share/target usr/bin/symlink
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            packages:
              foo:
                transformations:
                - create-symlink:
                    path: usr/bin/symlink
                    target: /usr/share/target
    """
    )
    _verify_migrator_generates_parsable_manifest(
        migrate_links_files,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "links",
        dh_config_content,
        expected_manifest_content,
    )


def test_detect_obsolete_substvars(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_obsolete_substvars

    dctrl_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-foo,

    Package: foo
    Architecture: any
    Description: ...
    Depends: ${misc:Depends},
      ${shlibs:Depends},
      bar (>= 1.0~) | baz, ${so:Depends},
    """
    )
    dctrl_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control",
                content=dctrl_content,
            ),
        ]
    )

    migration = run_migrator(
        migrator,
        dctrl_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    msg = (
        "The following relationship substitution variables can be removed from foo:"
        " ${misc:Depends}, ${shlibs:Depends}, ${so:Depends}"
    )
    assert migration.anything_to_do
    assert migration.warnings == [msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins


def test_detect_obsolete_substvars_remove_field(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_obsolete_substvars

    dctrl_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-foo,

    Package: foo
    Architecture: any
    Description: ...
    Pre-Depends: ${misc:Pre-Depends}
    Depends: bar (>= 1.0~) | baz
    """
    )
    dctrl_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control",
                content=dctrl_content,
            ),
        ]
    )

    migration = run_migrator(
        migrator,
        dctrl_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    msg = (
        "The following relationship fields can be removed from foo: Pre-Depends."
        "  (The content in them would be applied automatically.)"
    )
    assert migration.anything_to_do
    assert migration.warnings == [msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins


def test_detect_obsolete_substvars_remove_field_essential(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_obsolete_substvars

    dctrl_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-foo,

    Package: foo
    Architecture: any
    Description: ...
    Essential: yes
    # Obsolete because the package is essential
    Pre-Depends: ${shlibs:Depends}
    Depends: bar (>= 1.0~) | baz
    """
    )
    dctrl_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control",
                content=dctrl_content,
            ),
        ]
    )

    migration = run_migrator(
        migrator,
        dctrl_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    msg = (
        "The following relationship fields can be removed from foo: Pre-Depends."
        "  (The content in them would be applied automatically.)"
    )
    assert migration.anything_to_do
    assert migration.warnings == [msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins


def test_detect_obsolete_substvars_remove_field_non_essential(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_obsolete_substvars

    dctrl_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-foo,

    Package: foo
    Architecture: any
    Description: ...
    # This is not obsolete since the package is not essential
    Pre-Depends: ${shlibs:Depends}
    Depends: bar (>= 1.0~) | baz
    """
    )
    dctrl_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control",
                content=dctrl_content,
            ),
        ]
    )

    migration = run_migrator(
        migrator,
        dctrl_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    assert not migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins


def test_detect_dh_addons(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
    accept_any_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_dh_addons
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])

    dctrl_no_addons_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Description: ...
    """
    )

    dctrl_w_addons_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-foo,

    Package: foo
    Architecture: all
    Description: ...
    """
    )

    dctrl_w_migrateable_addons_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-debputy,
                   dh-sequence-numpy3,

    Package: foo
    Architecture: all
    Description: ...
    """
    )

    dctrl_no_addons_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control-without-addons",
                content=dctrl_no_addons_content,
            ),
        ]
    )
    dctrl_w_addons_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control-with-addons",
                content=dctrl_w_addons_content,
            ),
        ]
    )
    dctrl_w_migrateable_addons_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control-with-migrateable-addons",
                content=dctrl_w_migrateable_addons_content,
            ),
        ]
    )
    no_ctrl_msg = (
        "Cannot find debian/control. Detection of unsupported/missing dh-sequence addon"
        " could not be performed. Please ensure the package will Build-Depend on dh-sequence-zz-debputy"
        " and not rely on any other debhelper sequence addons except those debputy explicitly supports."
    )
    missing_debputy_bd_msg = "Missing Build-Depends on dh-sequence-zz-debputy"
    unsupported_sequence_msg = (
        'The dh addon "foo" is not known to work with dh-debputy and might malfunction'
    )

    migration = run_migrator(
        migrator,
        empty_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    assert migration.anything_to_do
    assert migration.warnings == [no_ctrl_msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    migration = run_migrator(
        migrator,
        dctrl_no_addons_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    assert migration.anything_to_do
    assert migration.warnings == [missing_debputy_bd_msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    _assert_unsupported_feature(
        migrator,
        dctrl_w_addons_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    migration = run_migrator(
        migrator,
        dctrl_w_addons_fs,
        empty_manifest_pkg_foo,
        accept_any_migration_issues,
    )

    assert migration.anything_to_do
    assert migration.warnings == [unsupported_sequence_msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    migration = run_migrator(
        migrator,
        dctrl_w_migrateable_addons_fs,
        empty_manifest_pkg_foo,
        accept_any_migration_issues,
    )
    assert not migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert migration.required_plugins == ["numpy3"]


def test_detect_dh_addons_rrr(
    empty_manifest_pkg_foo: HighLevelManifest,
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    migrator = detect_dh_addons_zz_debputy_rrr
    empty_fs = build_virtual_file_system([DEBIAN_DIR_ENTRY])

    dctrl_no_addons_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Description: ...
    """
    )

    dctrl_w_addons_content = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: debhelper-compat (= 13),
                   dh-sequence-zz-debputy-rrr,
                   dh-sequence-foo,

    Package: foo
    Architecture: all
    Description: ...
    """
    )

    dctrl_no_addons_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control-without-addons",
                content=dctrl_no_addons_content,
            ),
        ]
    )
    dctrl_w_addons_fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                "control",
                fs_path="/nowhere/debian/control-with-addons",
                content=dctrl_w_addons_content,
            ),
        ]
    )
    no_ctrl_msg = (
        "Cannot find debian/control. Detection of unsupported/missing dh-sequence addon"
        " could not be performed. Please ensure the package will Build-Depend on dh-sequence-zz-debputy-rrr."
    )
    missing_debputy_bd_msg = "Missing Build-Depends on dh-sequence-zz-debputy-rrr"

    migration = run_migrator(
        migrator,
        empty_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
        migration_target=MIGRATION_TARGET_DH_DEBPUTY_RRR,
    )
    assert migration.anything_to_do
    assert migration.warnings == [no_ctrl_msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    migration = run_migrator(
        migrator,
        dctrl_no_addons_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )
    assert migration.anything_to_do
    assert migration.warnings == [missing_debputy_bd_msg]
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins

    migration = run_migrator(
        migrator,
        dctrl_w_addons_fs,
        empty_manifest_pkg_foo,
        accept_no_migration_issues,
    )

    assert not migration.anything_to_do
    assert not migration.warnings
    assert not migration.remove_paths_on_success
    assert not migration.rename_paths_on_success
    assert not migration.required_plugins


def test_migrate_bash_completion_file_no_changes(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        compgen -A
    """
    )
    dh_config_name = "bash-completion"
    fs = build_virtual_file_system(
        [
            DEBIAN_DIR_ENTRY,
            virtual_path_def(
                dh_config_name,
                fs_path=f"/nowhere/debian/{dh_config_name}",
                content=dh_config_content,
            ),
        ]
    )
    migration = run_migrator(
        migrate_bash_completion,
        fs,
        manifest_parser_pkg_foo_factory().build_manifest(),
        accept_no_migration_issues,
    )
    assert not migration.rename_paths_on_success
    assert not migration.remove_paths_on_success
    assert not migration.warnings
    assert not migration.required_plugins


def test_migrate_bash_completion_file(
    manifest_parser_pkg_foo_factory: Callable[[], YAMLManifestParser],
    accept_no_migration_issues: AcceptableMigrationIssues,
) -> None:
    dh_config_content = textwrap.dedent(
        """\
        foo/*
        bar baz
        debian/bar-completion bar
        debian/foo-completion foo
        debian/*.cmpl
    """
    )
    expected_manifest_content = textwrap.dedent(
        """\
            manifest-version: '0.1'
            installations:
            - install:
                sources:
                - foo/*
                - debian/*.cmpl
                dest-dir: '{{path:BASH_COMPLETION_DIR}}'
            - install:
                source: bar
                as: '{{path:BASH_COMPLETION_DIR}}/baz'
    """
    )
    expected_renames = [
        ("debian/bar-completion", "debian/foo.bar.bash-completion"),
        ("debian/foo-completion", "debian/foo.bash-completion"),
    ]
    _verify_migrator_generates_parsable_manifest(
        migrate_bash_completion,
        manifest_parser_pkg_foo_factory,
        accept_no_migration_issues,
        "bash-completion",
        dh_config_content,
        expected_manifest_content,
        expected_renamed_paths=expected_renames,
    )
