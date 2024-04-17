import contextlib
import functools
import gzip
import os
import re
import subprocess
from contextlib import ExitStack
from typing import Optional, Iterator, IO, Any, List, Dict, Callable, Union

from debputy.plugin.api import VirtualPath
from debputy.util import _error, xargs, escape_shell, _info, assume_not_none


@contextlib.contextmanager
def _open_maybe_gzip(path: VirtualPath) -> Iterator[Union[IO[bytes], gzip.GzipFile]]:
    if path.name.endswith(".gz"):
        with gzip.GzipFile(path.fs_path, "rb") as fd:
            yield fd
    else:
        with path.open(byte_io=True) as fd:
            yield fd


_SO_LINK_RE = re.compile(rb"[.]so\s+(.*)\s*")
_LA_DEP_LIB_RE = re.compile(rb"'.+'")


def _detect_so_link(path: VirtualPath) -> Optional[str]:
    so_link_re = _SO_LINK_RE
    with _open_maybe_gzip(path) as fd:
        for line in fd:
            m = so_link_re.search(line)
            if m:
                return m.group(1).decode("utf-8")
    return None


def _replace_with_symlink(path: VirtualPath, so_link_target: str) -> None:
    adjusted_target = so_link_target
    parent_dir = path.parent_dir
    assert parent_dir is not None  # For the type checking
    if parent_dir.name == os.path.dirname(adjusted_target):
        # Avoid man8/../man8/foo links
        adjusted_target = os.path.basename(adjusted_target)
    elif "/" in so_link_target:
        # symlinks and so links have a different base directory when the link has a "/".
        # Adjust with an extra "../" to align the result
        adjusted_target = "../" + adjusted_target

    path.unlink()
    parent_dir.add_symlink(path.name, adjusted_target)


@functools.lru_cache(1)
def _has_man_recode() -> bool:
    # Ideally, we would just use shutil.which or something like that.
    # Unfortunately, in debhelper, we experienced problems with which
    # returning "yes" for a man tool that actually could not be run
    # on salsa CI.
    #
    # Therefore, we adopt the logic of dh_installman to run the tool
    # with --help to confirm it is not broken, because no one could
    # figure out what happened in the salsa CI and my life is still
    # too short to figure it out.
    try:
        subprocess.check_call(
            ["man-recode", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            restore_signals=True,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def process_manpages(fs_root: VirtualPath, _unused1: Any, _unused2: Any) -> None:
    man_dir = fs_root.lookup("./usr/share/man")
    if not man_dir:
        return

    re_encode = []
    for path in (p for p in man_dir.all_paths() if p.is_file and p.has_fs_path):
        size = path.size
        if size == 0:
            continue
        so_link_target = None
        if size <= 1024:
            # debhelper has a 1024 byte guard on the basis that ".so file tend to be small".
            # That guard worked well for debhelper, so lets keep it for now on that basis alone.
            so_link_target = _detect_so_link(path)
        if so_link_target:
            _replace_with_symlink(path, so_link_target)
        else:
            re_encode.append(path)

    if not re_encode or not _has_man_recode():
        return

    with ExitStack() as manager:
        manpages = [
            manager.enter_context(p.replace_fs_path_content()) for p in re_encode
        ]
        static_cmd = ["man-recode", "--to-code", "UTF-8", "--suffix", ".encoded"]
        for cmd in xargs(static_cmd, manpages):
            _info(f"Ensuring manpages have utf-8 encoding via: {escape_shell(*cmd)}")
            try:
                subprocess.check_call(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    restore_signals=True,
                )
            except subprocess.CalledProcessError:
                _error(
                    "The man-recode process failed. Please review the output of `man-recode` to understand"
                    " what went wrong."
                )
        for manpage in manpages:
            dest_name = manpage
            if dest_name.endswith(".gz"):
                dest_name = dest_name[:-3]
            os.rename(f"{dest_name}.encoded", manpage)


def _filter_compress_paths() -> Callable[[VirtualPath], Iterator[VirtualPath]]:
    ignore_dir_basenames = {
        "_sources",
    }
    ignore_basenames = {
        ".htaccess",
        "index.sgml",
        "objects.inv",
        "search_index.json",
        "copyright",
    }
    ignore_extensions = {
        ".htm",
        ".html",
        ".xhtml",
        ".gif",
        ".png",
        ".jpg",
        ".jpeg",
        ".gz",
        ".taz",
        ".tgz",
        ".z",
        ".bz2",
        ".epub",
        ".jar",
        ".zip",
        ".odg",
        ".odp",
        ".odt",
        ".css",
        ".xz",
        ".lz",
        ".lzma",
        ".haddock",
        ".hs",
        ".woff",
        ".woff2",
        ".svg",
        ".svgz",
        ".js",
        ".devhelp2",
        ".map",  # Technically, dh_compress has this one case-sensitive
    }
    ignore_special_cases = ("-gz", "-z", "_z")

    def _filtered_walk(path: VirtualPath) -> Iterator[VirtualPath]:
        for path, children in path.walk():
            if path.name in ignore_dir_basenames:
                children.clear()
                continue
            if path.is_dir and path.name == "examples":
                # Ignore anything beneath /usr/share/doc/*/examples
                parent = path.parent_dir
                grand_parent = parent.parent_dir if parent else None
                if grand_parent and grand_parent.absolute == "/usr/share/doc":
                    children.clear()
                    continue
            name = path.name
            if (
                path.is_symlink
                or not path.is_file
                or name in ignore_basenames
                or not path.has_fs_path
            ):
                continue

            name_lc = name.lower()
            _, ext = os.path.splitext(name_lc)

            if ext in ignore_extensions or name_lc.endswith(ignore_special_cases):
                continue
            yield path

    return _filtered_walk


def _find_compressable_paths(fs_root: VirtualPath) -> Iterator[VirtualPath]:
    path_filter = _filter_compress_paths()

    for p, compress_size_threshold in (
        ("./usr/share/info", 0),
        ("./usr/share/man", 0),
        ("./usr/share/doc", 4096),
    ):
        path = fs_root.lookup(p)
        if path is None:
            continue
        paths = path_filter(path)
        if compress_size_threshold:
            # The special-case for changelog and NEWS is from dh_compress. Generally these files
            # have always been compressed regardless of their size.
            paths = (
                p
                for p in paths
                if p.size > compress_size_threshold
                or p.name.startswith(("changelog", "NEWS"))
            )
        yield from paths
    x11_path = fs_root.lookup("./usr/share/fonts/X11")
    if x11_path:
        yield from (
            p for p in x11_path.all_paths() if p.is_file and p.name.endswith(".pcf")
        )


def apply_compression(fs_root: VirtualPath, _unused1: Any, _unused2: Any) -> None:
    # TODO: Support hardlinks
    compressed_files: Dict[str, str] = {}
    for path in _find_compressable_paths(fs_root):
        parent_dir = assume_not_none(path.parent_dir)
        with parent_dir.add_file(f"{path.name}.gz", mtime=path.mtime) as new_file, open(
            new_file.fs_path, "wb"
        ) as fd:
            try:
                subprocess.check_call(["gzip", "-9nc", path.fs_path], stdout=fd)
            except subprocess.CalledProcessError:
                full_command = f"gzip -9nc {escape_shell(path.fs_path)} > {escape_shell(new_file.fs_path)}"
                _error(
                    f"The compression of {path.path} failed. Please review the error message from gzip to"
                    f" understand what went wrong.  Full command was: {full_command}"
                )
            compressed_files[path.path] = new_file.path
        del parent_dir[path.name]

    all_remaining_symlinks = {p.path: p for p in fs_root.all_paths() if p.is_symlink}
    changed = True
    while changed:
        changed = False
        remaining: List[VirtualPath] = list(all_remaining_symlinks.values())
        for symlink in remaining:
            target = symlink.readlink()
            dir_target, basename_target = os.path.split(target)
            new_basename_target = f"{basename_target}.gz"
            symlink_parent_dir = assume_not_none(symlink.parent_dir)
            dir_path = symlink_parent_dir
            if dir_target != "":
                dir_path = dir_path.lookup(dir_target)
            if (
                not dir_path
                or basename_target in dir_path
                or new_basename_target not in dir_path
            ):
                continue
            del all_remaining_symlinks[symlink.path]
            changed = True

            new_link_name = (
                f"{symlink.name}.gz"
                if not symlink.name.endswith(".gz")
                else symlink.name
            )
            symlink_parent_dir.add_symlink(
                new_link_name, os.path.join(dir_target, new_basename_target)
            )
            symlink.unlink()


def _la_files(fs_root: VirtualPath) -> Iterator[VirtualPath]:
    lib_dir = fs_root.lookup("/usr/lib")
    if not lib_dir:
        return
    # Original code only iterators directly in /usr/lib. To be a faithful conversion, we do the same
    # here.
    # Eagerly resolve the list as the replacement can trigger a runtime error otherwise
    paths = list(lib_dir.iterdir)
    yield from (p for p in paths if p.is_file and p.name.endswith(".la"))


# Conceptually, the same feature that dh_gnome provides.
# The clean_la_files function based on the dh_gnome version written by Luca Falavigna in 2010,
# who in turn references a Makefile version of the feature.
# https://salsa.debian.org/gnome-team/gnome-pkg-tools/-/commit/2868e1e41ea45443b0fb340bf4c71c4de87d4a5b
def clean_la_files(
    fs_root: VirtualPath,
    _unused1: Any,
    _unused2: Any,
) -> None:
    for path in _la_files(fs_root):
        buffer = []
        with path.open(byte_io=True) as fd:
            replace_file = False
            for line in fd:
                if line.startswith(b"dependency_libs"):
                    replacement = _LA_DEP_LIB_RE.sub(b"''", line)
                    if replacement != line:
                        replace_file = True
                        line = replacement
                buffer.append(line)

            if not replace_file:
                continue
            _info(f"Clearing the dependency_libs line in {path.path}")
            with path.replace_fs_path_content() as fs_path, open(fs_path, "wb") as wfd:
                wfd.writelines(buffer)
