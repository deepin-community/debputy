import os
import random
from typing import cast, TYPE_CHECKING, Sequence, Optional, Mapping

import pytest

from debputy.packager_provided_files import detect_all_packager_provided_files
from debputy.plugin.api import DebputyPluginInitializer
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.plugin.api.impl_types import (
    PackagerProvidedFileClassSpec,
    DebputyPluginMetadata,
)
from debputy.plugin.api.test_api import (
    InitializedPluginUnderTest,
    build_virtual_file_system,
)
from debputy.plugin.api.test_api.test_impl import (
    initialize_plugin_under_test_preloaded,
)
from lint_tests.lint_tutil import requires_levenshtein
from tutil import faked_binary_package, binary_package_table

if TYPE_CHECKING:
    from debputy.plugin.api.test_api.test_impl import InitializedPluginUnderTestImpl

    # Irrelevant, but makes the import not "unused" for things that does not parse `cast("...", ...)` expressions
    assert InitializedPluginUnderTestImpl is not None


def ppf_test_plugin(api: DebputyPluginInitializer) -> None:
    api.packager_provided_file(
        "arch-specific-dash",
        "/some/test-directory/{name}.conf",
        allow_architecture_segment=True,
    )
    api.packager_provided_file(
        "arch.specific.dot",
        "/some/test.directory/{name}",
        allow_architecture_segment=True,
    )

    api.packager_provided_file(
        "arch.specific.with.priority",
        "/some/test.priority.directory/{priority:02}-{name}",
        allow_architecture_segment=True,
        default_priority=60,
    )

    api.packager_provided_file(
        "packageless-fallback",
        "/some/test-plfb/{name}",
        packageless_is_fallback_for_all_packages=True,
    )
    api.packager_provided_file(
        "packageless.fallback",
        "/some/test.plfb/{name}",
        packageless_is_fallback_for_all_packages=True,
    )


@pytest.mark.parametrize(
    "package_name,basename,install_target,is_main_binary",
    [
        ("foo", "foo.arch-specific-dash", "./some/test-directory/foo.conf", True),
        # main package match
        ("foo", "arch-specific-dash", "./some/test-directory/foo.conf", True),
        # arch match
        ("foo", "foo.arch-specific-dash.amd64", "./some/test-directory/foo.conf", True),
        # Matches with periods in both package name and in the file type
        ("foo.bar", "foo.bar.arch.specific.dot", "./some/test.directory/foo.bar", True),
        ("foo.bar", "arch.specific.dot", "./some/test.directory/foo.bar", True),
        (
            "foo.bar",
            "foo.bar.arch.specific.dot.amd64",
            "./some/test.directory/foo.bar",
            True,
        ),
        # Priority
        (
            "foo.bar",
            "foo.bar.arch.specific.with.priority",
            "./some/test.priority.directory/60-foo.bar",
            True,
        ),
        (
            "foo.bar",
            "arch.specific.with.priority",
            "./some/test.priority.directory/60-foo.bar",
            True,
        ),
        (
            "foo.bar",
            "foo.bar.arch.specific.with.priority.amd64",
            "./some/test.priority.directory/60-foo.bar",
            True,
        ),
        # Name
        (
            "foo.bar",
            "foo.bar.special.name.arch.specific.with.priority",
            "./some/test.priority.directory/60-special.name",
            True,
        ),
        (
            "foo.bar",
            "foo.bar.special.name.arch.specific.with.priority.amd64",
            "./some/test.priority.directory/60-special.name",
            True,
        ),
        (
            "foo.bar",
            "packageless-fallback",
            "./some/test-plfb/foo.bar",
            False,
        ),
        (
            "foo.bar",
            "packageless.fallback",
            "./some/test.plfb/foo.bar",
            False,
        ),
    ],
)
def test_packager_provided_files(
    package_name: str, basename: str, install_target: str, is_main_binary: bool
) -> None:
    # Inject our custom files
    plugin = initialize_plugin_under_test_preloaded(
        1,
        ppf_test_plugin,
        plugin_name="pff-test-plugin",
    )
    debputy_plugin_feature_set = _fetch_debputy_plugin_feature_set(plugin)

    debian_dir = build_virtual_file_system([basename])
    binary_under_test = faked_binary_package(
        package_name, is_main_package=is_main_binary
    )
    main_package = (
        binary_under_test if is_main_binary else faked_binary_package("main-pkg")
    )
    binaries = [main_package]
    if not is_main_binary:
        binaries.append(binary_under_test)
    binary_packages = binary_package_table(*binaries)

    ppf = detect_all_packager_provided_files(
        debputy_plugin_feature_set.packager_provided_files,
        debian_dir,
        binary_packages,
    )
    assert package_name in ppf
    all_matched = ppf[package_name].auto_installable
    assert len(all_matched) == 1
    matched = all_matched[0]
    assert basename == matched.path.name
    actual_install_target = "/".join(matched.compute_dest())
    assert actual_install_target == install_target


@pytest.mark.parametrize(
    "package_name,expected_basename,non_matched",
    [
        ("foo", "foo.arch-specific-dash", ["arch-specific-dash"]),
        (
            "foo",
            "foo.arch-specific-dash.amd64",
            [
                "foo.arch-specific-dash",
                "arch-specific-dash",
                "foo.arch-specific-dash.i386",
            ],
        ),
        (
            "foo",
            "foo.arch-specific-dash",
            ["arch-specific-dash", "foo.arch-specific-dash.i386"],
        ),
    ],
)
def test_packager_provided_files_priority(
    package_name, expected_basename, non_matched
) -> None:
    assert len(non_matched) > 0
    # Inject our custom files
    plugin = initialize_plugin_under_test_preloaded(
        1,
        ppf_test_plugin,
        plugin_name="pff-test-plugin",
    )
    debputy_plugin_feature_set = _fetch_debputy_plugin_feature_set(plugin)
    binary_packages = binary_package_table(faked_binary_package(package_name))
    all_entries_base = [x for x in non_matched]

    for order in (0, len(all_entries_base), None):
        all_entries = all_entries_base.copy()
        print(f"Order: {order}")
        if order is not None:
            all_entries.insert(order, expected_basename)
        else:
            all_entries.append(expected_basename)
            # Ensure there are no order dependencies in the test by randomizing it.
            random.shuffle(all_entries)

        debian_dir = build_virtual_file_system(all_entries)
        ppf = detect_all_packager_provided_files(
            debputy_plugin_feature_set.packager_provided_files,
            debian_dir,
            binary_packages,
        )
        assert package_name in ppf
        all_matched = ppf[package_name].auto_installable
        assert len(all_matched) == 1
        matched = all_matched[0]
        assert expected_basename == matched.path.name


@pytest.mark.parametrize(
    "main_package,secondary_package",
    [
        ("foo", "foo-doc"),
        ("foo.bar", "foo.bar-doc"),
    ],
)
def test_detect_ppf_simple(main_package: str, secondary_package: str) -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package(main_package, is_main_package=True),
        faked_binary_package(secondary_package),
    )
    debian_dir = build_virtual_file_system(
        [
            "install",
            f"{secondary_package}.install",
            f"{main_package}.docs",
            f"{secondary_package}.docs",
            "copyright",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "install",
        ),
        _fake_PPFClassSpec(
            plugin_metadata,
            "docs",
        ),
        _fake_PPFClassSpec(
            plugin_metadata,
            "copyright",
            packageless_is_fallback_for_all_packages=True,
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
    )
    assert main_package in results
    main_matches = results[main_package].auto_installable
    assert {m.path.name for m in main_matches} == {
        "install",
        f"{main_package}.docs",
        "copyright",
    }

    assert secondary_package in results
    sec_matches = results[secondary_package].auto_installable
    expected = {
        f"{secondary_package}.install",
        f"{secondary_package}.docs",
        "copyright",
    }
    assert {m.path.name for m in sec_matches} == expected


def test_detect_ppf_name_segment() -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package("foo", is_main_package=True),
        faked_binary_package("bar"),
    )
    debian_dir = build_virtual_file_system(
        [
            "fuu.install",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "install",
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {
        "fuu.install",
    }
    foo_install = foo_matches[0]
    assert foo_install.name_segment == "fuu"
    assert foo_install.package_name == "foo"
    assert foo_install.architecture_restriction is None
    assert not foo_install.fuzzy_match

    assert "bar" in results
    assert not results["bar"].auto_installable

    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "install",
            allow_name_segment=False,
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
        allow_fuzzy_matches=True,
    )

    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"fuu.install"}
    foo_install = foo_matches[0]
    assert foo_install.name_segment == "fuu"
    assert foo_install.package_name == "foo"
    assert foo_install.architecture_restriction is None

    assert "bar" in results
    assert not results["bar"].auto_installable


def test_detect_ppf_fuzzy_match_bug_950723() -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package("foo", is_main_package=True),
        faked_binary_package("bar"),
    )
    debian_dir = build_virtual_file_system(
        [
            "bar.service",
            "foo@.service",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "service",
            bug_950723=True,
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"foo@.service"}
    foo_at_service = foo_matches[0]
    # Without bug#950723 AND fuzzy_match, it counts a name segment for the main package.
    assert foo_at_service.name_segment == "foo@"
    assert foo_at_service.package_name == "foo"
    assert foo_at_service.architecture_restriction is None
    assert not foo_at_service.fuzzy_match

    assert "bar" in results
    bar_matches = results["bar"].auto_installable
    assert {m.path.name for m in bar_matches} == {"bar.service"}
    bar_service = bar_matches[0]
    assert bar_service.name_segment is None
    assert bar_service.package_name == "bar"
    assert bar_service.architecture_restriction is None
    assert not bar_service.fuzzy_match

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
        allow_fuzzy_matches=True,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"foo@.service"}
    foo_at_service = foo_matches[0]
    assert foo_at_service.name_segment is None
    assert foo_at_service.package_name == "foo"
    assert foo_at_service.architecture_restriction is None

    assert "bar" in results
    bar_matches = results["bar"].auto_installable
    assert {m.path.name for m in bar_matches} == {"bar.service"}
    bar_service = bar_matches[0]
    assert bar_service.name_segment is None
    assert bar_service.package_name == "bar"
    assert bar_service.architecture_restriction is None
    assert not bar_service.fuzzy_match


@requires_levenshtein
def test_detect_ppf_typo() -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package("foo", is_main_package=True),
        faked_binary_package("bar"),
    )
    debian_dir = build_virtual_file_system(
        [
            "fuu.install",
            "bar.intsall",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "install",
        ),
        _fake_PPFClassSpec(
            plugin_metadata,
            "logcheck.violations.d",
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
        detect_typos=True,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"fuu.install"}
    foo_logcheck = foo_matches[0]
    # Not a typo due to how name segments works with debhelper compat <= 13
    # (but should probably have been one.
    assert foo_logcheck.name_segment == "fuu"
    assert foo_logcheck.package_name == "foo"
    assert foo_logcheck.architecture_restriction is None

    assert "bar" in results
    bar_matches = results["bar"].auto_installable
    assert {m.path.name for m in bar_matches} == {"bar.intsall"}
    bar_logcheck = bar_matches[0]
    assert os.path.basename(bar_logcheck.expected_path) == "bar.install"
    assert bar_logcheck.name_segment is None
    assert bar_logcheck.package_name == "bar"
    assert bar_logcheck.architecture_restriction is None

    debian_dir = build_virtual_file_system(
        [
            # Typo'ed by intention
            "logchcek.violations.d",
            "bar.logchcek.violations.d",
        ]
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
        detect_typos=True,
    )

    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"logchcek.violations.d"}
    foo_logcheck = foo_matches[0]
    assert foo_logcheck.name_segment is None
    assert foo_logcheck.package_name == "foo"
    assert foo_logcheck.architecture_restriction is None
    assert os.path.basename(foo_logcheck.expected_path) == "logcheck.violations.d"

    assert "bar" in results
    bar_matches = results["bar"].auto_installable
    assert {m.path.name for m in bar_matches} == {
        "bar.logchcek.violations.d",
    }
    bar_logcheck = bar_matches[0]
    assert bar_logcheck.name_segment is None
    assert bar_logcheck.package_name == "bar"
    assert bar_logcheck.architecture_restriction is None
    assert os.path.basename(bar_logcheck.expected_path) == "bar.logcheck.violations.d"


def test_debhelper_overlapping_stems() -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package("foo", is_main_package=True),
    )
    debian_dir = build_virtual_file_system(
        [
            "foo.user.service",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "service",
        ),
        _fake_PPFClassSpec(
            plugin_metadata,
            "user.service",
        ),
    )

    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {
        "foo.user.service",
    }
    matched_file = foo_matches[0]
    assert matched_file.definition.stem == "user.service"
    assert matched_file.name_segment is None
    assert matched_file.package_name == "foo"
    assert matched_file.architecture_restriction is None

    debian_dir = build_virtual_file_system(
        [
            "foo.named.service",
        ]
    )
    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"foo.named.service"}
    matched_file = foo_matches[0]
    assert matched_file.definition.stem == "service"
    assert matched_file.name_segment == "named"
    assert matched_file.package_name == "foo"
    assert matched_file.architecture_restriction is None


@requires_levenshtein
def test_debhelper_overlapping_stems_typo_check() -> None:
    plugin_metadata = plugin_metadata_for_debputys_own_plugin()
    binary_packages = binary_package_table(
        faked_binary_package("foo", is_main_package=True),
    )
    debian_dir = build_virtual_file_system(
        [
            "foo.uxxr.service",
        ]
    )
    ppf_defs = _ppfs(
        _fake_PPFClassSpec(
            plugin_metadata,
            "service",
        ),
        _fake_PPFClassSpec(
            plugin_metadata,
            "user.service",
        ),
    )
    results = detect_all_packager_provided_files(
        ppf_defs,
        debian_dir,
        binary_packages,
        detect_typos=True,
    )
    assert "foo" in results
    foo_matches = results["foo"].auto_installable
    assert {m.path.name for m in foo_matches} == {"foo.uxxr.service"}
    foo_install = foo_matches[0]
    # While it could just as well be a named segment, we assume it is a typo.
    assert foo_install.definition.stem == "user.service"
    assert foo_install.name_segment is None
    assert foo_install.package_name == "foo"
    assert foo_install.architecture_restriction is None
    assert os.path.basename(foo_install.expected_path) == "foo.user.service"


def _fetch_debputy_plugin_feature_set(
    plugin: InitializedPluginUnderTest,
) -> PluginProvidedFeatureSet:
    # Very much not public API, but we need it to avoid testing on production data (also, it is hard to find
    # relevant debputy files for all the cases we want to test).
    return cast("InitializedPluginUnderTestImpl", plugin)._feature_set


def _fake_PPFClassSpec(
    debputy_plugin_metadata: DebputyPluginMetadata,
    stem: str,
    *,
    default_priority: Optional[int] = None,
    packageless_is_fallback_for_all_packages: bool = False,
    bug_950723: bool = False,
    has_active_command: bool = False,
    allow_name_segment: bool = True,
) -> PackagerProvidedFileClassSpec:
    return PackagerProvidedFileClassSpec(
        debputy_plugin_metadata,
        stem,
        "not-a-real-ppf",
        allow_architecture_segment=True,
        allow_name_segment=allow_name_segment,
        default_priority=default_priority,
        default_mode=0o644,
        post_formatting_rewrite=None,
        packageless_is_fallback_for_all_packages=packageless_is_fallback_for_all_packages,
        reservation_only=False,
        formatting_callback=None,
        bug_950723=bug_950723,
        has_active_command=has_active_command,
    )


def _ppfs(
    *ppfs: PackagerProvidedFileClassSpec,
) -> Mapping[str, PackagerProvidedFileClassSpec]:
    return {ppf.stem: ppf for ppf in ppfs}
