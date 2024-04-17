from typing import cast

import pytest

from debputy.exceptions import SymlinkLoopError
from debputy.filesystem_scan import VirtualPathBase
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.test_api import build_virtual_file_system


def test_symlink_lookup() -> None:
    fs = build_virtual_file_system(
        [
            virtual_path_def("./usr/share/doc/bar", link_target="foo"),
            "./usr/share/doc/foo/copyright",
            virtual_path_def("./usr/share/bar/data", link_target="../foo/data"),
            "./usr/share/foo/data/foo.dat",
            virtual_path_def("./usr/share/baz/data", link_target="/usr/share/foo/data"),
            virtual_path_def(
                "./usr/share/test/loop-a", link_target="/usr/share/test/loop-b"
            ),
            virtual_path_def("./usr/share/test/loop-b", link_target="./loop-c"),
            virtual_path_def("./usr/share/test/loop-c", link_target="../test/loop-a"),
        ]
    )
    assert fs.lookup("/usr/share/doc/bar/copyright") is not None
    assert fs.lookup("/usr/share/bar/data/foo.dat") is not None
    assert fs.lookup("/usr/share/baz/data/foo.dat") is not None

    vp_fs: VirtualPathBase = cast("VirtualPathBase", fs)
    p, missing = vp_fs.attempt_lookup("/usr/share/doc/foo/non-existent")
    assert p.path == "./usr/share/doc/foo"
    assert missing == ["non-existent"]

    p, missing = vp_fs.attempt_lookup("/usr/share/bar/data/non-existent")
    assert p.path == "./usr/share/foo/data"
    assert missing == ["non-existent"]

    p, missing = vp_fs.attempt_lookup("/usr/share/baz/data/non-existent")
    assert p.path == "./usr/share/foo/data"
    assert missing == ["non-existent"]

    # The symlink can be looked up
    assert fs.lookup("./usr/share/test/loop-a") is not None
    with pytest.raises(SymlinkLoopError):
        # But resolving it will cause issues
        fs.lookup("./usr/share/test/loop-a/")
