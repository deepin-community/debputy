import random
from typing import cast

import pytest

from debputy.packager_provided_files import detect_all_packager_provided_files
from debputy.plugin.api import DebputyPluginInitializer
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.test_api import (
    InitializedPluginUnderTest,
    build_virtual_file_system,
)
from debputy.plugin.api.test_api.test_impl import initialize_plugin_under_test_preloaded
from tutil import faked_binary_package, binary_package_table


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


def _fetch_debputy_plugin_feature_set(
    plugin: InitializedPluginUnderTest,
) -> PluginProvidedFeatureSet:
    # Very much not public API, but we need it to avoid testing on production data (also, it is hard to find
    # relevant debputy files for all the cases we want to test).
    return cast("InitializedPluginUnderTestImpl", plugin)._feature_set
