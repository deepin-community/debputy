from typing import Optional

from debputy.plugin.api import virtual_path_def, VirtualPath
from debputy.plugin.api.test_api import (
    initialize_plugin_under_test,
    build_virtual_file_system,
)


def test_lua_plugin_deduplicate_lua_files_no_lua() -> None:
    plugin = initialize_plugin_under_test()
    no_lua_files_fs = build_virtual_file_system(
        [
            "usr/bin/foo",
            "usr/share/lua5.2/foo/",  # Said no lua files, not no lua paths :)
        ]
    )
    path_count = sum((1 for _ in no_lua_files_fs.all_paths()), start=0)
    plugin.run_package_processor("deduplicate-lua-files", no_lua_files_fs)
    # The plugin does nothing if there are no lua files
    assert path_count == sum((1 for _ in no_lua_files_fs.all_paths()), start=0)
    assert not any(f.is_symlink for f in no_lua_files_fs.all_paths())


def _resolve_lua_symlink(v: VirtualPath) -> Optional[VirtualPath]:
    # TODO: This should probably be in the VirtualPath API (after some corner case tests/improvements)
    assert v.is_symlink
    target = v.readlink()
    parent = v.parent_dir
    assert parent is not None
    return parent.lookup(target)


def test_lua_plugin_deduplicate_lua_files_lua_differ() -> None:
    plugin = initialize_plugin_under_test()
    # Long to test more than one iteration
    base_content = "a" * 10000
    fs = build_virtual_file_system(
        [
            virtual_path_def("usr/share/lua/5.2/foo.lua", content=base_content),
            virtual_path_def("usr/share/lua/5.3/foo.lua", content=base_content),
            virtual_path_def("usr/share/lua/5.4/foo.lua", content=base_content),
            virtual_path_def("usr/share/lua/5.2/bar.lua", content=base_content + "unique for 5.2"),
            virtual_path_def("usr/share/lua/5.3/bar.lua", content=base_content + "unique for 5.3"),
            # Deliberately shorter to test different parse lengths
            virtual_path_def("usr/share/lua/5.4/bar.lua", content="unique for 5.4"),
            virtual_path_def("usr/share/lua/5.2/foo5.2.lua", content="unrelated"),
            virtual_path_def("usr/share/lua/5.3/foo5.3.lua", content="unrelated"),
            virtual_path_def("usr/share/lua/5.4/foo5.4.lua", content="unrelated"),
        ]
    )
    plugin.run_package_processor("deduplicate-lua-files", fs)

    foo52_lua = fs.lookup("/usr/share/lua/5.2/foo.lua")
    # This should be there and unchanged
    assert foo52_lua is not None and foo52_lua.is_file
    # These should be symlinks to foo52_lua
    for foo5x_lua_path in ["/usr/share/lua/5.3/foo.lua", "/usr/share/lua/5.4/foo.lua"]:
        foo5x_lua = fs.lookup(foo5x_lua_path)
        assert foo5x_lua_path is not None and foo5x_lua.is_symlink
        target_path = _resolve_lua_symlink(foo5x_lua)
        assert target_path.path == foo52_lua.path

    # These should be unchanged
    for path_stem in [
        "5.2/bar.lua",
        "5.3/bar.lua",
        "5.4/bar.lua",
        "5.2/foo5.2.lua",
        "5.3/foo5.3.lua",
        "5.4/foo5.4.lua",
    ]:
        unique_file = fs.lookup(f"/usr/share/lua/{path_stem}")
        assert unique_file is not None and unique_file.is_file
