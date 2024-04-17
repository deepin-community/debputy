import pytest

from debputy.deb_packaging_support import install_upstream_changelog
from debputy.filesystem_scan import build_virtual_fs
from debputy.plugin.api import virtual_path_def


@pytest.mark.parametrize(
    "upstream_changelog_name,other_files",
    [
        (
            "changelog.txt",
            [
                "changelog.md",
                "CHANGELOG.rst",
                "random-file",
            ],
        ),
        (
            "CHANGELOG.rst",
            [
                "doc/CHANGELOG.txt",
                "docs/CHANGELOG.md",
            ],
        ),
        (
            "docs/CHANGELOG.rst",
            [
                "docs/history.md",
            ],
        ),
        (
            "changelog",
            [],
        ),
    ],
)
def test_upstream_changelog_from_source(
    package_single_foo_arch_all_cxt_amd64,
    upstream_changelog_name,
    other_files,
) -> None:
    upstream_changelog_content = "Some upstream changelog"
    dctrl = package_single_foo_arch_all_cxt_amd64["foo"]
    data_fs_root = build_virtual_fs([], read_write_fs=True)
    upstream_fs_contents = [
        virtual_path_def("CHANGELOG", materialized_content="Some upstream changelog")
    ]
    upstream_fs_contents.extend(
        virtual_path_def(x, materialized_content="Wrong file!") for x in other_files
    )
    source_fs_root = build_virtual_fs(upstream_fs_contents)

    install_upstream_changelog(dctrl, data_fs_root, source_fs_root)

    upstream_changelog = data_fs_root.lookup(f"usr/share/doc/{dctrl.name}/changelog")
    assert upstream_changelog is not None
    assert upstream_changelog.is_file
    with upstream_changelog.open() as fd:
        content = fd.read()
    assert upstream_changelog_content == content


@pytest.mark.parametrize(
    "upstream_changelog_basename,other_data_files,other_source_files",
    [
        (
            "CHANGELOG",
            [
                "history.txt",
                "changes.md",
            ],
            [
                "changelog",
                "doc/CHANGELOG.txt",
                "docs/CHANGELOG.md",
            ],
        ),
        (
            "changelog",
            [
                "history.txt",
                "changes.md",
            ],
            [
                "changelog",
                "doc/CHANGELOG.txt",
                "docs/CHANGELOG.md",
            ],
        ),
        (
            "changes.md",
            [
                "changelog.rst",
            ],
            ["changelog"],
        ),
    ],
)
def test_upstream_changelog_from_data_fs(
    package_single_foo_arch_all_cxt_amd64,
    upstream_changelog_basename,
    other_data_files,
    other_source_files,
) -> None:
    upstream_changelog_content = "Some upstream changelog"
    dctrl = package_single_foo_arch_all_cxt_amd64["foo"]
    doc_dir = f"./usr/share/doc/{dctrl.name}"
    data_fs_contents = [
        virtual_path_def(
            f"{doc_dir}/{upstream_changelog_basename}",
            materialized_content="Some upstream changelog",
        )
    ]
    data_fs_contents.extend(
        virtual_path_def(
            f"{doc_dir}/{x}",
            materialized_content="Wrong file!",
        )
        for x in other_data_files
    )
    data_fs_root = build_virtual_fs(data_fs_contents, read_write_fs=True)
    source_fs_root = build_virtual_fs(
        [
            virtual_path_def(
                x,
                materialized_content="Wrong file!",
            )
            for x in other_source_files
        ]
    )

    install_upstream_changelog(dctrl, data_fs_root, source_fs_root)

    upstream_changelog = data_fs_root.lookup(f"usr/share/doc/{dctrl.name}/changelog")
    assert upstream_changelog is not None
    assert upstream_changelog.is_file
    with upstream_changelog.open() as fd:
        content = fd.read()
    assert upstream_changelog_content == content


def test_upstream_changelog_pre_installed_compressed(
    package_single_foo_arch_all_cxt_amd64,
) -> None:
    dctrl = package_single_foo_arch_all_cxt_amd64["foo"]
    changelog = f"./usr/share/doc/{dctrl.name}/changelog.gz"
    data_fs_root = build_virtual_fs(
        [virtual_path_def(changelog, fs_path="/nowhere/should/not/be/resolved")],
        read_write_fs=True,
    )
    source_fs_root = build_virtual_fs(
        [virtual_path_def("changelog", materialized_content="Wrong file!")]
    )

    install_upstream_changelog(dctrl, data_fs_root, source_fs_root)

    upstream_ch_compressed = data_fs_root.lookup(
        f"usr/share/doc/{dctrl.name}/changelog.gz"
    )
    assert upstream_ch_compressed is not None
    assert upstream_ch_compressed.is_file
    upstream_ch_uncompressed = data_fs_root.lookup(
        f"usr/share/doc/{dctrl.name}/changelog"
    )
    assert upstream_ch_uncompressed is None


def test_upstream_changelog_no_matches(
    package_single_foo_arch_all_cxt_amd64,
) -> None:
    dctrl = package_single_foo_arch_all_cxt_amd64["foo"]
    doc_dir = f"./usr/share/doc/{dctrl.name}"
    data_fs_root = build_virtual_fs(
        [
            virtual_path_def(
                f"{doc_dir}/random-file", materialized_content="Wrong file!"
            ),
            virtual_path_def(
                f"{doc_dir}/changelog.Debian", materialized_content="Wrong file!"
            ),
        ],
        read_write_fs=True,
    )
    source_fs_root = build_virtual_fs(
        [virtual_path_def("some-random-file", materialized_content="Wrong file!")]
    )

    install_upstream_changelog(dctrl, data_fs_root, source_fs_root)

    upstream_ch_compressed = data_fs_root.lookup(
        f"usr/share/doc/{dctrl.name}/changelog.gz"
    )
    assert upstream_ch_compressed is None
    upstream_ch_uncompressed = data_fs_root.lookup(
        f"usr/share/doc/{dctrl.name}/changelog"
    )
    assert upstream_ch_uncompressed is None


def test_upstream_changelog_salsa_issue_49(
    package_single_foo_arch_all_cxt_amd64,
) -> None:
    # https://salsa.debian.org/debian/debputy/-/issues/49
    dctrl = package_single_foo_arch_all_cxt_amd64["foo"]
    doc_dir = f"./usr/share/doc/{dctrl.name}"
    data_fs_root = build_virtual_fs(
        [virtual_path_def(f"{doc_dir}", link_target="foo-data")], read_write_fs=True
    )
    source_fs_root = build_virtual_fs(
        [virtual_path_def("changelog", materialized_content="Wrong file!")]
    )

    install_upstream_changelog(dctrl, data_fs_root, source_fs_root)

    doc_dir = data_fs_root.lookup(f"usr/share/doc/{dctrl.name}")
    assert doc_dir is not None
    assert doc_dir.is_symlink
