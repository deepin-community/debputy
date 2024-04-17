from debputy.filesystem_scan import build_virtual_fs
from debputy.plugin.api import virtual_path_def
from debputy.plugin.debputy.package_processors import apply_compression


def test_apply_compression():
    # TODO: This test should be a proper plugin test
    fs_root = build_virtual_fs(
        [
            virtual_path_def(
                "./usr/share/man/man1/foo.1",
                materialized_content="man page content",
            ),
            virtual_path_def("./usr/share/man/man1/bar.1", link_target="foo.1"),
            virtual_path_def(
                "./usr/share/man/de/man1/bar.1", link_target="../../man1/foo.1"
            ),
        ],
        read_write_fs=True,
    )
    apply_compression(fs_root, None, None)

    assert fs_root.lookup("./usr/share/man/man1/foo.1") is None
    assert fs_root.lookup("./usr/share/man/man1/foo.1.gz") is not None
    assert fs_root.lookup("./usr/share/man/man1/bar.1") is None
    bar_symlink = fs_root.lookup("./usr/share/man/man1/bar.1.gz")
    assert bar_symlink is not None
    assert bar_symlink.readlink() == "foo.1.gz"

    assert fs_root.lookup("./usr/share/man/de/man1/bar.1") is None
    de_bar_symlink = fs_root.lookup("./usr/share/man/de/man1/bar.1.gz")
    assert de_bar_symlink is not None
    assert de_bar_symlink.readlink() == "../../man1/foo.1.gz"
