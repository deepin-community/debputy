import itertools
from typing import Any, Optional, Tuple, IO, Iterable

from debputy.plugin.api import DebputyPluginInitializer, VirtualPath

_BUF_SIZE = 8096


def initialize(api: DebputyPluginInitializer) -> None:
    register_processing_steps_via_non_public_api(api)


def register_processing_steps_via_non_public_api(api: Any) -> None:
    api.package_processor(
        "deduplicate-lua-files",
        deduplicate_lua_files,
    )


def find_lowest_lua5_version_dir(fs_root: VirtualPath) -> Optional[Tuple[int, VirtualPath]]:
    for lua5_minor_version in range(1, 9):
        lua_dir = fs_root.lookup(f"/usr/share/lua/5.{lua5_minor_version}/")
        if not lua_dir:
            continue
        return lua5_minor_version, lua_dir
    return None


def _compute_rel_path(reference_point: VirtualPath, target_file: VirtualPath) -> str:
    assert target_file.path.startswith(reference_point.path + "/")
    return target_file.path[len(reference_point.path) + 1:]


def _chunk(fd: IO[bytes]) -> Iterable[bytes]:
    c = fd.read(_BUF_SIZE)
    while c:
        yield c
        c = fd.read(_BUF_SIZE)


def _same_content(a: VirtualPath, b: VirtualPath) -> bool:
    if a.size != b.size:
        return False
    # It might be tempting to use `cmp {a.fs_path} {b.fs_path}`, but this method makes it easier to test
    # as the test will not have to create physical files.
    #
    # We could also replace this with a checksum check, then we could store them in a table and deduplicate
    # between multiple versions (not just against the lowest version). Decided against it because this is
    # are more faithful translation of the original code.
    with a.open(byte_io=True, buffering=_BUF_SIZE) as afd, b.open(byte_io=True, buffering=_BUF_SIZE) as bfd:
        for a_chunk, b_chunk in itertools.zip_longest(afd, bfd, fillvalue=None):
            if a_chunk is None or b_chunk is None or a_chunk != b_chunk:
                return False
    return True


def deduplicate_lua_files(fs_root: VirtualPath, _unused1: Any, _unused2: Any) -> None:
    res_tuple = find_lowest_lua5_version_dir(fs_root)
    if res_tuple is None:
        return
    lua5_minor_version, lua5_reference_dir = res_tuple
    all_lua5_ref_files = [
        lua_file
        for lua_file in lua5_reference_dir.all_paths()
        if lua_file.is_file and lua_file.name.endswith(".lua")
    ]
    for lua_file in all_lua5_ref_files:
        rel_path = _compute_rel_path(lua5_reference_dir, lua_file)
        for newer_lua_minor_version in range(lua5_minor_version + 1, 9):
            alt_lua_file = fs_root.lookup(f"/usr/share/lua/5.{newer_lua_minor_version}/{rel_path}")
            if alt_lua_file is None or not alt_lua_file.is_file or not _same_content(lua_file, alt_lua_file):
                continue
            parent_dir = alt_lua_file.parent_dir
            assert parent_dir is not None
            alt_lua_file.unlink()
            # We do not have to normalize the link; debputy will handle that later.
            parent_dir.add_symlink(alt_lua_file.name, lua_file.absolute)
