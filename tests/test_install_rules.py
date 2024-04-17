import textwrap

import pytest

from debputy.highlevel_manifest_parser import YAMLManifestParser
from debputy.installations import (
    InstallSearchDirContext,
    NoMatchForInstallPatternError,
    SearchDir,
)
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.test_api import build_virtual_file_system


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
        debian_dir=debian_dir,
    )


@pytest.fixture()
def manifest_parser_pkg_foo_w_udeb(
    amd64_dpkg_architecture_variables,
    dpkg_arch_query,
    source_package,
    package_foo_w_udeb_arch_any_cxt_amd64,
    amd64_substitution,
    no_profiles_or_build_options,
    debputy_plugin_feature_set,
) -> YAMLManifestParser:
    # We need an empty directory to avoid triggering packager provided files.
    debian_dir = build_virtual_file_system([])
    return YAMLManifestParser(
        "debian/test-debputy.manifest",
        source_package,
        package_foo_w_udeb_arch_any_cxt_amd64,
        amd64_substitution,
        amd64_dpkg_architecture_variables,
        dpkg_arch_query,
        no_profiles_or_build_options,
        debputy_plugin_feature_set,
        debian_dir=debian_dir,
    )


def test_install_rules(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/tmp/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/tmp/usr/bin/foo",
                content="#!/bin/sh\n",
                mtime=10,
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/tmp/usr/bin/foo-util",
                content="#!/bin/sh\n",
                mtime=10,
            ),
            virtual_path_def(
                "usr/bin/tool.sh",
                fs_path="/nowhere/debian/tmp/usr/bin/tool.sh",
                link_target="./foo",
            ),
            virtual_path_def("usr/share/", fs_path="/nowhere/debian/tmp/usr/share"),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/tmp/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/tmp/usr/share/foo/foo.txt",
                content="A text file",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              - /usr/share/foo
              - /usr/bin/foo
              - /usr/bin/foo-util
          - install:
              source: usr/bin/tool.sh
              as: usr/bin/tool
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    ub_dir = foo_fs_root.lookup("/usr/bin")
    assert ub_dir is not None
    assert ub_dir.is_dir
    assert not ub_dir.has_fs_path  # This will be "generated"

    tool = ub_dir.get("tool")
    assert tool is not None
    assert tool.is_symlink
    assert tool.readlink() == "./foo"

    assert {"foo", "foo-util", "tool"} == {p.name for p in ub_dir.iterdir}
    for n in ["foo", "foo-util"]:
        assert ub_dir[n].mtime == 10
    usf_dir = foo_fs_root.lookup("/usr/share/foo")
    assert usf_dir is not None
    assert usf_dir.is_dir
    # Here we are installing an actual directory, so it should be present too
    assert usf_dir.has_fs_path
    assert usf_dir.fs_path == "/nowhere/debian/tmp/usr/share/foo"
    assert {"foo.txt"} == {p.name for p in usf_dir.iterdir}


def test_multi_dest_install_rules(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere"),
            virtual_path_def("source/", fs_path="/nowhere/source"),
            virtual_path_def("source/foo/", fs_path="/nowhere/foo"),
            virtual_path_def(
                "source/foo/foo-a.data",
                fs_path="/nowhere/foo/foo-a.data",
                content="data file",
            ),
            virtual_path_def(
                "source/foo/foo-b.data",
                fs_path="/nowhere/foo/foo-b.data",
                link_target="./foo-a.data",
            ),
            virtual_path_def("source/bar/", fs_path="/nowhere/bar"),
            virtual_path_def(
                "source/bar/bar-a.data",
                fs_path="/nowhere/bar/bar-a.data",
                content="data file",
            ),
            virtual_path_def(
                "source/bar/bar-b.data",
                fs_path="/nowhere/bar/bar-b.data",
                content="data file",
            ),
            virtual_path_def(
                "source/tool.sh",
                fs_path="/nowhere/source/tool.sh",
                content="#!/bin/sh\n# run some command ...",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - multi-dest-install:
              sources:
              - source/foo/*
              - source/bar
              dest-dirs:
              - usr/share/foo
              - usr/share/foo2
          - multi-dest-install:
              source: source/tool.sh
              as:
               - usr/share/foo/tool
               - usr/share/foo2/tool
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    for stem in ["foo", "foo2"]:
        foo_dir = foo_fs_root.lookup(f"/usr/share/{stem}")
        assert foo_dir is not None
        assert foo_dir.is_dir

        assert {"foo-a.data", "foo-b.data", "bar", "tool"} == {
            p.name for p in foo_dir.iterdir
        }

        tool = foo_dir["tool"]
        assert tool.is_file
        with tool.open() as fd:
            content = fd.read()
        assert content.startswith("#!/bin/sh")
        foo_a = foo_dir["foo-a.data"]
        assert foo_a.is_file
        assert foo_a.fs_path == "/nowhere/foo/foo-a.data"
        with foo_a.open() as fd:
            content = fd.read()
        assert "data" in content
        foo_b = foo_dir["foo-b.data"]
        assert foo_b.is_symlink
        assert foo_b.readlink() == "./foo-a.data"

        bar = foo_dir["bar"]
        assert bar.is_dir
        assert {"bar-a.data", "bar-b.data"} == {p.name for p in bar.iterdir}
        assert {"/nowhere/bar/bar-a.data", "/nowhere/bar/bar-b.data"} == {
            p.fs_path for p in bar.iterdir
        }


def test_install_rules_with_glob(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/tmp/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/tmp/usr/bin/foo",
                content="#!/bin/sh\n",
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/tmp/usr/bin/foo-util",
                content="#!/bin/sh\n",
            ),
            virtual_path_def(
                "usr/bin/tool.sh",
                fs_path="/nowhere/debian/tmp/usr/bin/tool.sh",
                link_target="./foo",
            ),
            virtual_path_def("usr/share/", fs_path="/nowhere/debian/tmp/usr/share"),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/tmp/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/tmp/usr/share/foo/foo.txt",
                content="A text file",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              source: usr/bin/tool.sh
              as: usr/bin/tool
          - install:
              - /usr/share/foo
              - /usr/bin/foo*
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    ub_dir = foo_fs_root.lookup("/usr/bin")
    assert ub_dir is not None
    assert ub_dir.is_dir
    assert not ub_dir.has_fs_path  # This will be "generated"

    tool = ub_dir.get("tool")
    assert tool is not None
    assert tool.is_symlink
    assert tool.readlink() == "./foo"

    assert {"foo", "foo-util", "tool"} == {p.name for p in ub_dir.iterdir}
    usf_dir = foo_fs_root.lookup("/usr/share/foo")
    assert usf_dir is not None
    assert usf_dir.is_dir
    # Here we are installing an actual directory, so it should be present too
    assert usf_dir.has_fs_path
    assert usf_dir.fs_path == "/nowhere/debian/tmp/usr/share/foo"
    assert {"foo.txt"} == {p.name for p in usf_dir.iterdir}


def test_install_rules_auto_discard_rules_dir(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp/usr"),
            virtual_path_def("usr/lib/", fs_path="/nowhere/debian/tmp/usr/lib"),
            virtual_path_def(
                "usr/lib/libfoo.so.1.0.0",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0",
                content="Not really an ELF FILE",
            ),
            virtual_path_def(
                "usr/lib/libfoo.la",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.la",
                content="Not really a LA FILE",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              - /usr/lib
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)

    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    lib_dir = foo_fs_root.lookup("/usr/lib")
    assert lib_dir is not None
    assert lib_dir.is_dir
    assert lib_dir.has_fs_path
    assert lib_dir.fs_path == "/nowhere/debian/tmp/usr/lib"

    so_file = lib_dir.get("libfoo.so.1.0.0")
    assert so_file is not None
    assert so_file.is_file
    assert so_file.has_fs_path
    assert so_file.fs_path == "/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0"

    assert {"libfoo.so.1.0.0"} == {p.name for p in lib_dir.iterdir}


def test_install_rules_auto_discard_rules_glob(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp/usr"),
            virtual_path_def("usr/lib/", fs_path="/nowhere/debian/tmp/usr/lib"),
            virtual_path_def(
                "usr/lib/libfoo.so.1.0.0",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0",
                content="Not really an ELF FILE",
            ),
            virtual_path_def(
                "usr/lib/libfoo.la",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.la",
                content="Not really an ELF FILE",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              - /usr/lib/*
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    lib_dir = foo_fs_root.lookup("/usr/lib")
    assert lib_dir is not None
    assert lib_dir.is_dir
    assert not lib_dir.has_fs_path

    so_file = lib_dir.get("libfoo.so.1.0.0")
    assert so_file is not None
    assert so_file.is_file
    assert so_file.has_fs_path
    assert so_file.fs_path == "/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0"

    assert {"libfoo.so.1.0.0"} == {p.name for p in lib_dir.iterdir}


def test_install_rules_auto_discard_rules_overruled_by_explicit_install_rule(
    manifest_parser_pkg_foo,
) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp/usr"),
            virtual_path_def("usr/lib/", fs_path="/nowhere/debian/tmp/usr/lib"),
            virtual_path_def(
                "usr/lib/libfoo.so.1.0.0",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0",
                content="Not really an ELF FILE",
            ),
            virtual_path_def(
                "usr/lib/libfoo.la",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.la",
                content="Not really an ELF FILE",
            ),
            virtual_path_def(
                "usr/lib/libfoo.so",
                fs_path="/nowhere/debian/tmp/usr/lib/libfoo.so",
                link_target="libfoo.so.1.0.0",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              - /usr/lib
              - /usr/lib/libfoo.la
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    lib_dir = foo_fs_root.lookup("/usr/lib")
    assert lib_dir is not None
    assert lib_dir.is_dir
    assert lib_dir.has_fs_path
    assert lib_dir.fs_path == "/nowhere/debian/tmp/usr/lib"

    so_file = lib_dir.get("libfoo.so.1.0.0")
    assert so_file is not None
    assert so_file.is_file
    assert so_file.has_fs_path
    assert so_file.fs_path == "/nowhere/debian/tmp/usr/lib/libfoo.so.1.0.0"

    la_file = lib_dir.get("libfoo.la")
    assert la_file is not None
    assert la_file.is_file
    assert la_file.has_fs_path
    assert la_file.fs_path == "/nowhere/debian/tmp/usr/lib/libfoo.la"

    so_link = lib_dir.get("libfoo.so")
    assert so_link is not None
    assert so_link.is_symlink
    assert so_link.readlink() == "libfoo.so.1.0.0"

    assert {"libfoo.so.1.0.0", "libfoo.so", "libfoo.la"} == {
        p.name for p in lib_dir.iterdir
    }


def test_install_rules_install_as_with_var(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere"),
            virtual_path_def("build/", fs_path="/nowhere/build"),
            virtual_path_def(
                "build/private-arch-tool.sh",
                fs_path="/nowhere/build/private-arch-tool.sh",
                content="#!/bin/sh\n",
            ),
        ]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              source: build/private-arch-tool.sh
              as: /usr/lib/{{DEB_HOST_MULTIARCH}}/foo/private-arch-tool
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_dir, all_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_dir],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    # The variable is always resolved in amd64 context, so we can hard code the resolved
    # variable
    tool = foo_fs_root.lookup("/usr/lib/x86_64-linux-gnu/foo/private-arch-tool")
    assert tool is not None
    assert tool.is_file
    assert tool.fs_path == "/nowhere/build/private-arch-tool.sh"


def test_install_rules_no_matches(manifest_parser_pkg_foo) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere"),
            virtual_path_def("build/", fs_path="/nowhere/build"),
            virtual_path_def(
                "build/private-arch-tool.sh",
                fs_path="/nowhere/build/private-arch-tool.sh",
                content="#!/bin/sh\n",
            ),
        ]
    )
    debian_tmp_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp"),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              # Typo: the path should have ended with ".sh"
              source: build/private-arch-tool
              as: /usr/lib/foo/private-arch-tool
        """
    )
    manifest = manifest_parser_pkg_foo.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    with pytest.raises(NoMatchForInstallPatternError) as e_info:
        manifest.perform_installations(
            install_request_context=InstallSearchDirContext(
                [
                    SearchDir(debian_tmp_dir, all_pkgs),
                    SearchDir(debian_source_root_dir, all_pkgs),
                ],
                [debian_tmp_dir],
            )
        )
    expected_msg = (
        "There were no matches for build/private-arch-tool in /nowhere/debian/tmp, /nowhere"
        " (definition: installations[0].install <Search for: build/private-arch-tool>)."
        " Match rule: ./build/private-arch-tool (the exact path / no globbing)"
    )
    assert e_info.value.message == expected_msg


def test_install_rules_per_package_search_dirs(manifest_parser_pkg_foo_w_udeb) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_tmp_deb_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp-deb"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp-deb/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/tmp-deb/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/tmp-deb/usr/bin/foo",
                content="#!/bin/sh\ndeb",
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/tmp-deb/usr/bin/foo-util",
                content="#!/bin/sh\ndeb",
            ),
            virtual_path_def(
                "usr/bin/tool.sh",
                fs_path="/nowhere/debian/tmp-deb/usr/bin/tool.sh",
                link_target="./foo",
            ),
            virtual_path_def("usr/share/", fs_path="/nowhere/debian/tmp-deb/usr/share"),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/tmp-deb/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/tmp-deb/usr/share/foo/foo.txt",
                content="A deb text file",
            ),
        ]
    )
    debian_tmp_udeb_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/tmp-udeb"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/tmp-udeb/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/tmp-udeb/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/tmp-udeb/usr/bin/foo",
                content="#!/bin/sh\nudeb",
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/tmp-udeb/usr/bin/foo-util",
                content="#!/bin/sh\nudeb",
            ),
            virtual_path_def(
                "usr/bin/tool.sh",
                fs_path="/nowhere/debian/tmp-udeb/usr/bin/tool.sh",
                link_target="./foo",
            ),
            virtual_path_def(
                "usr/share/", fs_path="/nowhere/debian/tmp-udeb/usr/share"
            ),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/tmp-udeb/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/tmp-udeb/usr/share/foo/foo.txt",
                content="A udeb text file",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              source: usr/bin/tool.sh
              as: usr/bin/tool
              into:
                - foo
                - foo-udeb
          - install:
              sources:
                - /usr/share/foo
                - /usr/bin/foo*
              into:
                - foo
                - foo-udeb
        """
    )
    manifest = manifest_parser_pkg_foo_w_udeb.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)
    all_deb_pkgs = frozenset({p for p in all_pkgs if not p.is_udeb})
    all_udeb_pkgs = frozenset({p for p in all_pkgs if p.is_udeb})

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_tmp_deb_dir, all_deb_pkgs),
                SearchDir(debian_tmp_udeb_dir, all_udeb_pkgs),
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_tmp_deb_dir],
        )
    )
    for pkg, ptype in [
        ("foo", "deb"),
        ("foo-udeb", "udeb"),
    ]:
        assert pkg in result
        fs_root = result[pkg].fs_root

        ub_dir = fs_root.lookup("/usr/bin")
        assert ub_dir is not None
        assert ub_dir.is_dir
        assert not ub_dir.has_fs_path  # This will be "generated"

        tool = ub_dir.get("tool")
        assert tool is not None
        assert tool.is_symlink
        assert tool.readlink() == "./foo"

        assert {"foo", "foo-util", "tool"} == {p.name for p in ub_dir.iterdir}

        for p in ub_dir.iterdir:
            assert p.has_fs_path
            assert f"/nowhere/debian/tmp-{ptype}/usr/bin" in p.fs_path

        usf_dir = fs_root.lookup("/usr/share/foo")
        assert usf_dir is not None
        assert usf_dir.is_dir
        # Here we are installing an actual directory, so it should be present too
        assert usf_dir.has_fs_path
        assert usf_dir.fs_path == f"/nowhere/debian/tmp-{ptype}/usr/share/foo"
        assert {"foo.txt"} == {p.name for p in usf_dir.iterdir}
        foo_txt = usf_dir["foo.txt"]
        assert foo_txt.fs_path == f"/nowhere/debian/tmp-{ptype}/usr/share/foo/foo.txt"


def test_install_rules_multi_into(manifest_parser_pkg_foo_w_udeb) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere"),
            virtual_path_def("source/", fs_path="/nowhere/source"),
            virtual_path_def("source/foo/", fs_path="/nowhere/foo"),
            virtual_path_def(
                "source/foo/foo-a.data",
                fs_path="/nowhere/foo/foo-a.data",
                content="data file",
            ),
            virtual_path_def(
                "source/foo/foo-b.data",
                fs_path="/nowhere/foo/foo-b.data",
                link_target="./foo-a.data",
            ),
            virtual_path_def("source/bar/", fs_path="/nowhere/bar"),
            virtual_path_def(
                "source/bar/bar-a.data",
                fs_path="/nowhere/bar/bar-a.data",
                content="data file",
            ),
            virtual_path_def(
                "source/bar/bar-b.data",
                fs_path="/nowhere/bar/bar-b.data",
                content="data file",
            ),
            virtual_path_def(
                "source/tool.sh",
                fs_path="/nowhere/source/tool.sh",
                content="#!/bin/sh\n# run some command ...",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install:
              sources:
              - source/foo/*
              - source/bar
              dest-dir: usr/share/foo
              into:
              - foo
              - foo-udeb
          - install:
              source: source/tool.sh
              as: usr/share/foo/tool
              into:
              - foo
              - foo-udeb
        """
    )
    manifest = manifest_parser_pkg_foo_w_udeb.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [],
        )
    )
    for pkg in ["foo", "foo-udeb"]:
        assert pkg in result
        foo_fs_root = result[pkg].fs_root

        foo_dir = foo_fs_root.lookup("/usr/share/foo")
        assert foo_dir is not None
        assert foo_dir.is_dir

        assert {"foo-a.data", "foo-b.data", "bar", "tool"} == {
            p.name for p in foo_dir.iterdir
        }

        tool = foo_dir["tool"]
        assert tool.is_file
        with tool.open() as fd:
            content = fd.read()
        assert content.startswith("#!/bin/sh")
        foo_a = foo_dir["foo-a.data"]
        assert foo_a.is_file
        assert foo_a.fs_path == "/nowhere/foo/foo-a.data"
        with foo_a.open() as fd:
            content = fd.read()
        assert "data" in content
        foo_b = foo_dir["foo-b.data"]
        assert foo_b.is_symlink
        assert foo_b.readlink() == "./foo-a.data"

        bar = foo_dir["bar"]
        assert bar.is_dir
        assert {"bar-a.data", "bar-b.data"} == {p.name for p in bar.iterdir}
        assert {"/nowhere/bar/bar-a.data", "/nowhere/bar/bar-b.data"} == {
            p.fs_path for p in bar.iterdir
        }


def test_auto_install_d_pkg(manifest_parser_pkg_foo_w_udeb) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [virtual_path_def(".", fs_path="/nowhere")]
    )
    debian_foo_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/foo"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/foo/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/foo/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/foo/usr/bin/foo",
                content="#!/bin/sh\ndeb",
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/foo/usr/bin/foo-util",
                content="#!/bin/sh\ndeb",
            ),
            virtual_path_def(
                "usr/bin/tool",
                fs_path="/nowhere/debian/foo/usr/bin/tool",
                link_target="./foo",
            ),
            virtual_path_def("usr/share/", fs_path="/nowhere/debian/foo/usr/share"),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/foo/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/foo/usr/share/foo/foo.txt",
                content="A deb text file",
            ),
        ]
    )
    debian_foo_udeb_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere/debian/foo-udeb"),
            virtual_path_def("usr/", fs_path="/nowhere/debian/foo-udeb/usr"),
            virtual_path_def("usr/bin/", fs_path="/nowhere/debian/foo-udeb/usr/bin"),
            virtual_path_def(
                "usr/bin/foo",
                fs_path="/nowhere/debian/foo-udeb/usr/bin/foo",
                content="#!/bin/sh\nudeb",
            ),
            virtual_path_def(
                "usr/bin/foo-util",
                fs_path="/nowhere/debian/foo-udeb/usr/bin/foo-util",
                content="#!/bin/sh\nudeb",
            ),
            virtual_path_def(
                "usr/bin/tool",
                fs_path="/nowhere/debian/foo-udeb/usr/bin/tool",
                link_target="./foo",
            ),
            virtual_path_def(
                "usr/share/", fs_path="/nowhere/debian/foo-udeb/usr/share"
            ),
            virtual_path_def(
                "usr/share/foo/", fs_path="/nowhere/debian/foo-udeb/usr/share/foo"
            ),
            virtual_path_def(
                "usr/share/foo/foo.txt",
                fs_path="/nowhere/debian/foo-udeb/usr/share/foo/foo.txt",
                content="A udeb text file",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        """
    )
    manifest = manifest_parser_pkg_foo_w_udeb.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [debian_foo_dir],
            {
                "foo": debian_foo_dir,
                "foo-udeb": debian_foo_udeb_dir,
            },
        )
    )
    for pkg in ["foo", "foo-udeb"]:
        assert pkg in result
        fs_root = result[pkg].fs_root
        ub_dir = fs_root.lookup("/usr/bin")
        assert ub_dir is not None
        assert ub_dir.is_dir
        assert ub_dir.has_fs_path
        assert ub_dir.fs_path == f"/nowhere/debian/{pkg}/usr/bin"

        assert {"foo", "foo-util", "tool"} == {p.name for p in ub_dir.iterdir}

        tool = ub_dir.get("tool")
        assert tool is not None
        assert tool.is_symlink
        assert tool.readlink() == "./foo"

        for p in ub_dir.iterdir:
            assert p.has_fs_path
            assert f"/nowhere/debian/{pkg}/usr/bin" in p.fs_path

        usf_dir = fs_root.lookup("/usr/share/foo")
        assert usf_dir is not None
        assert usf_dir.is_dir
        # Here we are installing an actual directory, so it should be present too
        assert usf_dir.has_fs_path
        assert usf_dir.fs_path == f"/nowhere/debian/{pkg}/usr/share/foo"
        assert {"foo.txt"} == {p.name for p in usf_dir.iterdir}
        foo_txt = usf_dir["foo.txt"]
        assert foo_txt.fs_path == f"/nowhere/debian/{pkg}/usr/share/foo/foo.txt"


def test_install_doc_rules_ignore_udeb(manifest_parser_pkg_foo_w_udeb) -> None:
    debian_source_root_dir = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="/nowhere"),
            virtual_path_def("source/", fs_path="/nowhere/source"),
            virtual_path_def("source/foo/", fs_path="/nowhere/foo"),
            virtual_path_def(
                "source/foo/foo-a.txt",
                fs_path="/nowhere/foo/foo-a.txt",
                content="data file",
            ),
            virtual_path_def(
                "source/foo/foo-b.txt",
                fs_path="/nowhere/foo/foo-b.txt",
                link_target="./foo-a.txt",
            ),
            virtual_path_def("source/html/", fs_path="/nowhere/html"),
            virtual_path_def(
                "source/html/bar-a.html",
                fs_path="/nowhere/html/bar-a.html",
                content="data file",
            ),
            virtual_path_def(
                "source/html/bar-b.html",
                fs_path="/nowhere/html/bar-b.html",
                content="data file",
            ),
        ]
    )
    manifest_content = textwrap.dedent(
        """\
        manifest-version: "0.1"
        installations:
          - install-doc:
              sources:
              - source/foo/*
              - source/html
        """
    )
    manifest = manifest_parser_pkg_foo_w_udeb.parse_manifest(fd=manifest_content)
    all_pkgs = frozenset(manifest.all_packages)

    result = manifest.perform_installations(
        install_request_context=InstallSearchDirContext(
            [
                SearchDir(debian_source_root_dir, all_pkgs),
            ],
            [],
        )
    )
    assert "foo" in result
    foo_fs_root = result["foo"].fs_root

    foo_dir = foo_fs_root.lookup("/usr/share/doc/foo")
    assert foo_dir is not None
    assert foo_dir.is_dir

    assert {"foo-a.txt", "foo-b.txt", "html"} == {p.name for p in foo_dir.iterdir}

    foo_a = foo_dir["foo-a.txt"]
    assert foo_a.is_file
    assert foo_a.fs_path == "/nowhere/foo/foo-a.txt"
    foo_b = foo_dir["foo-b.txt"]
    assert foo_b.is_symlink
    assert foo_b.readlink() == "./foo-a.txt"

    html_dir = foo_dir["html"]
    assert html_dir.is_dir
    assert {"bar-a.html", "bar-b.html"} == {p.name for p in html_dir.iterdir}
    assert {"/nowhere/html/bar-a.html", "/nowhere/html/bar-b.html"} == {
        p.fs_path for p in html_dir.iterdir
    }

    assert "foo-udeb" in result
    foo_udeb_fs_root = result["foo-udeb"].fs_root

    udeb_doc_dir = foo_udeb_fs_root.lookup("/usr/share/doc")
    assert udeb_doc_dir is None
