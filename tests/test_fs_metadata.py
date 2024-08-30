import dataclasses
import textwrap
from typing import Tuple, List, Optional, Union, Sequence

import pytest

from debputy.filesystem_scan import PathDef, build_virtual_fs
from debputy.highlevel_manifest_parser import (
    YAMLManifestParser,
)
from debputy.intermediate_manifest import PathType, IntermediateManifest, TarMember
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.test_api import build_virtual_file_system
from debputy.transformation_rules import TransformationRuntimeError


@pytest.fixture()
def manifest_parser_pkg_foo(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
    source_package,
    package_single_foo_arch_all_cxt_amd64,
    amd64_substitution,
    no_profiles_or_build_options,
    debputy_plugin_feature_set,
) -> YAMLManifestParser:
    # We need an empty directory to avoid triggering packager provided files.
    debian_dir = build_virtual_file_system([])
    return YAMLManifestParser(
        "debian/test-debputy.manifest",
        source_package,
        package_single_foo_arch_all_cxt_amd64,
        amd64_substitution,
        amd64_dpkg_architecture_variables,
        dpkg_arch_query,
        no_profiles_or_build_options,
        debputy_plugin_feature_set,
        "full",
        debian_dir=debian_dir,
    )


@dataclasses.dataclass(slots=True, kw_only=True)
class Expected:
    mtime: Optional[float]
    mode: Optional[int] = None
    link_target: Optional[str] = None
    owner: str = "root"
    group: str = "root"
    has_fs_path: bool = True


def _show_name_on_error(_: str) -> bool:
    return False


def _has_fs_path(tm: TarMember) -> bool:
    return tm.fs_path is not None


def verify_paths(
    intermediate_manifest: IntermediateManifest,
    expected_results: Sequence[Tuple[Union[str, PathDef], Expected]],
) -> None:
    result = {tm.member_path: tm for tm in intermediate_manifest}
    expected_table = {
        f"./{p}" if isinstance(p, str) else f"./{p.path_name}": e
        for p, e in expected_results
    }

    for path_name, expected in expected_table.items():
        tm = result[path_name]
        if tm.path_type == PathType.SYMLINK:
            assert tm.link_target == expected.link_target or _show_name_on_error(
                path_name
            )
        else:
            assert tm.link_target == "" or _show_name_on_error(path_name)
        if expected.mode is not None:
            assert oct(tm.mode) == oct(expected.mode) or _show_name_on_error(path_name)
        if expected.mtime is not None:
            assert tm.mtime == expected.mtime or _show_name_on_error(path_name)
        assert tm.owner == expected.owner or _show_name_on_error(path_name)
        assert tm.group == expected.group or _show_name_on_error(path_name)
        assert _has_fs_path(tm) == expected.has_fs_path or _show_name_on_error(
            path_name
        )

    del result["./"]
    if len(result) != len(expected_results):
        for tm in result.values():
            assert tm.member_path in expected_table


def test_mtime_clamp_and_builtin_dir_mode(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    manifest = manifest_parser_pkg_foo.build_manifest()
    claim_mtime_to = 255
    path_defs: List[Tuple[PathDef, Expected]] = [
        (
            virtual_path_def("usr/", mode=0o700, mtime=10, fs_path="/nowhere/usr/"),
            Expected(mode=0o755, mtime=10),
        ),
        (
            virtual_path_def(
                "usr/bin/", mode=0o2534, mtime=5000, fs_path="/nowhere/usr/bin/"
            ),
            Expected(mode=0o755, mtime=claim_mtime_to),
        ),
        (
            virtual_path_def(
                "usr/bin/my-exec",
                mtime=5000,
                fs_path="/nowhere/usr/bin/my-exec",
                link_target="../../some/where/else",
            ),
            # Implementation detail; symlinks do not refer to their FS path in the intermediate manifest.
            Expected(
                mtime=claim_mtime_to, link_target="/some/where/else", has_fs_path=False
            ),
        ),
    ]

    fs_root = build_virtual_fs([d[0] for d in path_defs], read_write_fs=True)

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )
    verify_paths(intermediate_manifest, path_defs)


def test_transformations_create_symlink(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
             -  create-symlink:
                      path: 'usr/bin/my-exec'
                      target: '../../some/where/else'
             - create-symlink:
                      path: 'usr/bin/{{PACKAGE}}'
                      target: '/usr/lib/{{DEB_HOST_MULTIARCH}}/{{PACKAGE}}/tool'
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    fs_root = build_virtual_fs(["./"], read_write_fs=True)
    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        ("usr/bin/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        (
            "usr/bin/my-exec",
            Expected(
                mtime=claim_mtime_to, link_target="/some/where/else", has_fs_path=False
            ),
        ),
        (
            "usr/bin/foo",
            Expected(
                mtime=claim_mtime_to,
                # Test is using a "static" dpkg-architecture, so it will always be `x86_64-linux-gnu`
                link_target="../lib/x86_64-linux-gnu/foo/tool",
                has_fs_path=False,
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


def test_transformations_create_symlink_replace_success(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
             -  create-symlink:
                      path: 'usr/bin/my-exec'
                      target: '../../some/where/else'
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    clamp_mtime_to = 255
    fs_root = build_virtual_fs(["./usr/bin/my-exec"], read_write_fs=True)
    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        ("usr/bin/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        (
            "usr/bin/my-exec",
            Expected(
                mtime=clamp_mtime_to, link_target="/some/where/else", has_fs_path=False
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, clamp_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


@pytest.mark.parametrize(
    "replacement_rule, reason",
    [
        (
            "abort-on-non-empty-directory",
            "the path is a non-empty directory",
        ),
        (
            "error-if-directory",
            "the path is a directory",
        ),
        (
            "error-if-exists",
            "the path exists",
        ),
    ],
)
def test_transformations_create_symlink_replace_failure(
    manifest_parser_pkg_foo: YAMLManifestParser,
    replacement_rule: str,
    reason: str,
) -> None:
    content = textwrap.dedent(
        f"""\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
             -  create-symlink:
                      path: 'usr/share/foo'
                      target: 'somewhere-else'
                      replacement-rule: {replacement_rule}
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    clamp_mtime_to = 255
    fs_root = build_virtual_fs(["./usr/share/foo/bar"], read_write_fs=True)

    assert [p.name for p in manifest.all_packages] == ["foo"]

    with pytest.raises(TransformationRuntimeError) as e_info:
        manifest.apply_to_binary_staging_directory("foo", fs_root, clamp_mtime_to)

    msg = (
        f"Refusing to replace ./usr/share/foo with a symlink; {reason} and the active"
        f" replacement-rule was {replacement_rule}.  You can set the replacement-rule to"
        ' "discard-existing", if you are not interested in the contents of ./usr/share/foo. This error'
        # Ideally, this would be reported for line 5.
        " was triggered by packages.foo.transformations[0].create-symlink [Line 6 column 18]."
    )
    assert e_info.value.args[0] == msg


def test_transformations_create_symlink_replace_with_explicit_remove(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove: usr/share/foo
            - create-symlink:
                      path: 'usr/share/foo'
                      target: 'somewhere-else'
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    clamp_mtime_to = 255
    fs_root = build_virtual_fs(["./usr/share/foo/bar"], read_write_fs=True)
    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        ("usr/share/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        (
            "usr/share/foo",
            Expected(
                mtime=clamp_mtime_to, link_target="somewhere-else", has_fs_path=False
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]
    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, clamp_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


def test_transformations_create_symlink_replace_with_replacement_rule(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove: usr/share/foo
            - create-symlink:
                      path: 'usr/share/foo'
                      target: 'somewhere-else'
                      replacement-rule: 'discard-existing'
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    clamp_mtime_to = 255
    fs_root = build_virtual_fs(["./usr/share/foo/bar"], read_write_fs=True)
    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        ("usr/share/", Expected(mode=0o755, mtime=clamp_mtime_to, has_fs_path=False)),
        (
            "usr/share/foo",
            Expected(
                mtime=clamp_mtime_to, link_target="somewhere-else", has_fs_path=False
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]
    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, clamp_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


def test_transformations_path_metadata(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
             - path-metadata:
                    path: 'usr/bin/my-exec'
                    mode: "-x"
                    owner: "bin"
                    group: 2
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    fs_root = build_virtual_fs(
        [
            virtual_path_def(
                "./usr/bin/my-exec", fs_path="/no-where", mode=0o755, mtime=10
            ),
        ],
        read_write_fs=True,
    )
    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        ("usr/bin/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        (
            "usr/bin/my-exec",
            Expected(
                mtime=10,
                has_fs_path=True,
                mode=0o644,
                owner="bin",
                group="bin",
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


def test_transformations_directories(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
              - create-directories:
                  path: some/empty/directory
                  mode: "0700"
              - create-directories: another/empty/directory
              - create-directories:
                  path: a/third-empty/directory
                  owner: www-data
                  group: www-data
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mtime=10, fs_path="/nowhere/some"),
        virtual_path_def("some/empty/", mtime=10, fs_path="/nowhere/some/empty"),
        virtual_path_def(
            "some/empty/directory/",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/some/empty/directory",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    expected_results = [
        ("some/", Expected(mode=0o755, mtime=10, has_fs_path=True)),
        ("some/empty/", Expected(mode=0o755, mtime=10, has_fs_path=True)),
        (
            "some/empty/directory/",
            Expected(mode=0o700, mtime=10, has_fs_path=True),
        ),
        ("another/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        (
            "another/empty/",
            Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False),
        ),
        (
            "another/empty/directory/",
            Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False),
        ),
        ("a/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        (
            "a/third-empty/",
            Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False),
        ),
        (
            "a/third-empty/directory/",
            Expected(
                mode=0o755,
                mtime=claim_mtime_to,
                owner="www-data",
                group="www-data",
                has_fs_path=False,
            ),
        ),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )
    verify_paths(intermediate_manifest, expected_results)


def test_transformation_remove(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove: some/empty
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/empty/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/empty/directory/",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/some/empty/directory",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    expected_results = []

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    verify_paths(intermediate_manifest, expected_results)


def test_transformation_remove_keep_empty(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove:
                path: some/empty
                keep-empty-parent-dirs: true
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/empty/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/empty/directory/",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/some/empty/directory",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    expected_results = [
        ("some/", Expected(mode=0o755, mtime=10)),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    verify_paths(intermediate_manifest, expected_results)


def test_transformation_remove_glob(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove: some/*.json
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/foo.json",
            mode=0o600,
            mtime=10,
        ),
        virtual_path_def(
            "some/bar.json",
            mode=0o600,
            mtime=10,
        ),
        virtual_path_def(
            "some/empty/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/bar.txt", mode=0o600, mtime=10, fs_path="/nowhere/some/bar.txt"
        ),
        virtual_path_def(
            "some/bar.JSON", mode=0o600, mtime=10, fs_path="/nowhere/some/bar.JSON"
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    expected_results = [
        ("some/", Expected(mode=0o755, mtime=10)),
        ("some/empty/", Expected(mode=0o755, mtime=10)),
        ("some/bar.txt", Expected(mode=0o644, mtime=10)),
        # Survives because pattern is case-sensitive
        ("some/bar.JSON", Expected(mode=0o644, mtime=10)),
    ]

    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    verify_paths(intermediate_manifest, expected_results)


def test_transformation_remove_no_match(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - remove: some/non-existing-path
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/empty/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/empty/directory/",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/some/empty/directory",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    assert [p.name for p in manifest.all_packages] == ["foo"]

    with pytest.raises(TransformationRuntimeError) as e_info:
        manifest.apply_to_binary_staging_directory("foo", fs_root, claim_mtime_to)
    expected = (
        'The match rule "./some/non-existing-path" in transformation'
        ' "packages.foo.transformations[0].remove [Line 5 column 18]" did not match any paths. Either'
        " the definition is redundant (and can be omitted) or the match rule is incorrect."
    )
    assert expected == e_info.value.args[0]


def test_transformation_move_basic(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - move:
                source: some/dir
                target: new/dir/where-else
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/dir/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/dir/some-dir-symlink1", mtime=10, link_target="/abs/some-target1"
        ),
        virtual_path_def(
            "some/dir/some-dir-symlink2", mtime=10, link_target="../some-target2"
        ),
        virtual_path_def(
            "some/dir/some-dir-symlink3",
            mtime=10,
            link_target="/new/dir/where-else/some-target3",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    assert [p.name for p in manifest.all_packages] == ["foo"]

    expected_results = [
        ("some/", Expected(mode=0o755, mtime=10)),
        ("new/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        ("new/dir/", Expected(mode=0o755, mtime=claim_mtime_to, has_fs_path=False)),
        ("new/dir/where-else/", Expected(mode=0o755, mtime=10)),
        # FIXME: should be 10
        (
            "new/dir/where-else/some-dir-symlink1",
            Expected(mtime=None, link_target="/abs/some-target1", has_fs_path=False),
        ),
        (
            "new/dir/where-else/some-dir-symlink2",
            Expected(mtime=None, link_target="../some-target2", has_fs_path=False),
        ),
        (
            "new/dir/where-else/some-dir-symlink3",
            Expected(mtime=None, link_target="some-target3", has_fs_path=False),
        ),
    ]
    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    print(intermediate_manifest)

    verify_paths(intermediate_manifest, expected_results)


def test_transformation_move_no_match(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    content = textwrap.dedent(
        """\
    manifest-version: '0.1'
    packages:
        foo:
            transformations:
            - move:
                source: some/non-existing-path
                target: some/where-else
    """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=content)
    claim_mtime_to = 255
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/empty/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/empty/directory/",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/some/empty/directory",
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    assert [p.name for p in manifest.all_packages] == ["foo"]

    with pytest.raises(TransformationRuntimeError) as e_info:
        manifest.apply_to_binary_staging_directory("foo", fs_root, claim_mtime_to)
    expected = (
        'The match rule "./some/non-existing-path" in transformation'
        ' "packages.foo.transformations[0].move [Line 6 column 12]" did not match any paths. Either'
        " the definition is redundant (and can be omitted) or the match rule is incorrect."
    )
    assert expected == e_info.value.args[0]


def test_builtin_mode_normalization_shell_scripts(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    manifest = manifest_parser_pkg_foo.build_manifest()
    claim_mtime_to = 255
    sh_script_content = "#!/bin/sh"
    python_script_content = "#! /usr/bin/python"
    unrelated_content = "... random stuff ..."
    paths = [
        virtual_path_def("some/", mode=0o700, mtime=10, fs_path="/nowhere/some"),
        virtual_path_def(
            "some/dir/", mode=0o700, mtime=10, fs_path="/nowhere/some/empty"
        ),
        virtual_path_def(
            "some/dir/script.sh",
            mode=0o600,
            mtime=10,
            fs_path="/nowhere/script.sh",
            content=sh_script_content,
        ),
        virtual_path_def(
            "some/dir/script.py",
            mode=0o600,
            mtime=10,
            fs_path="/nowhere/script.py",
            content=python_script_content,
        ),
        virtual_path_def(
            "some/dir/non-script-file",
            mode=0o600,
            mtime=10,
            fs_path="/nowhere/non-script-file",
            content=unrelated_content,
        ),
    ]
    fs_root = build_virtual_fs(paths, read_write_fs=True)
    assert [p.name for p in manifest.all_packages] == ["foo"]

    expected_results = [
        ("some/", Expected(mode=0o755, mtime=10)),
        ("some/dir/", Expected(mode=0o755, mtime=10)),
        ("some/dir/script.sh", Expected(mode=0o755, mtime=10)),
        ("some/dir/script.py", Expected(mode=0o755, mtime=10)),
        ("some/dir/non-script-file", Expected(mode=0o644, mtime=10)),
    ]
    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    print(intermediate_manifest)

    verify_paths(intermediate_manifest, expected_results)


def test_builtin_mode_normalization(
    manifest_parser_pkg_foo: YAMLManifestParser,
) -> None:
    manifest = manifest_parser_pkg_foo.build_manifest()
    claim_mtime_to = 255

    paths = [
        virtual_path_def("usr/", mode=0o755, mtime=10, fs_path="/nowhere/usr"),
        virtual_path_def(
            "usr/share/", mode=0o755, mtime=10, fs_path="/nowhere/usr/share"
        ),
        virtual_path_def(
            "usr/share/perl5/", mode=0o755, mtime=10, fs_path="/nowhere/usr/share/perl5"
        ),
        virtual_path_def(
            "usr/share/perl5/Foo.pm",
            # #1076346
            mode=0o444,
            mtime=10,
            fs_path="/nowhere/Foo.pm",
        ),
        virtual_path_def(
            "usr/share/perl5/Bar.pm",
            mode=0o755,
            mtime=10,
            fs_path="/nowhere/Bar.pm",
        ),
    ]

    fs_root = build_virtual_fs(paths, read_write_fs=True)
    assert [p.name for p in manifest.all_packages] == ["foo"]

    expected_results = [
        ("usr/", Expected(mode=0o755, mtime=10)),
        ("usr/share/", Expected(mode=0o755, mtime=10)),
        ("usr/share/perl5/", Expected(mode=0o755, mtime=10)),
        ("usr/share/perl5/Bar.pm", Expected(mode=0o644, mtime=10)),
        ("usr/share/perl5/Foo.pm", Expected(mode=0o644, mtime=10)),
    ]
    assert [p.name for p in manifest.all_packages] == ["foo"]

    intermediate_manifest = manifest.apply_to_binary_staging_directory(
        "foo", fs_root, claim_mtime_to
    )

    print(intermediate_manifest)

    verify_paths(intermediate_manifest, expected_results)
