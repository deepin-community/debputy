import atexit
import contextlib
import dataclasses
import errno
import io
import operator
import os
import stat
import subprocess
import tempfile
import time
from abc import ABC
from contextlib import suppress
from typing import (
    List,
    Iterable,
    Dict,
    Optional,
    Tuple,
    Union,
    Iterator,
    Mapping,
    cast,
    Any,
    ContextManager,
    TextIO,
    BinaryIO,
    NoReturn,
    Type,
    Generic,
)
from weakref import ref, ReferenceType

from debputy.exceptions import (
    PureVirtualPathError,
    DebputyFSIsROError,
    DebputyMetadataAccessError,
    TestPathWithNonExistentFSPathError,
    SymlinkLoopError,
)
from debputy.intermediate_manifest import PathType
from debputy.manifest_parser.base_types import (
    ROOT_DEFINITION,
    StaticFileSystemOwner,
    StaticFileSystemGroup,
)
from debputy.plugin.api.spec import (
    VirtualPath,
    PathDef,
    PathMetadataReference,
    PMT,
)
from debputy.types import VP
from debputy.util import (
    generated_content_dir,
    _error,
    escape_shell,
    assume_not_none,
    _normalize_path,
)

BY_BASENAME = operator.attrgetter("name")


class AlwaysEmptyReadOnlyMetadataReference(PathMetadataReference[PMT]):
    __slots__ = ("_metadata_type", "_owning_plugin", "_current_plugin")

    def __init__(
        self,
        owning_plugin: str,
        current_plugin: str,
        metadata_type: Type[PMT],
    ) -> None:
        self._owning_plugin = owning_plugin
        self._current_plugin = current_plugin
        self._metadata_type = metadata_type

    @property
    def is_present(self) -> bool:
        return False

    @property
    def can_read(self) -> bool:
        return self._owning_plugin == self._current_plugin

    @property
    def can_write(self) -> bool:
        return False

    @property
    def value(self) -> Optional[PMT]:
        if self.can_read:
            return None
        raise DebputyMetadataAccessError(
            f"Cannot read the metadata {self._metadata_type.__name__} owned by"
            f" {self._owning_plugin} as the metadata has not been made"
            f" readable to the plugin {self._current_plugin}."
        )

    @value.setter
    def value(self, new_value: PMT) -> None:
        if self._is_owner:
            raise DebputyFSIsROError(
                f"Cannot set the metadata {self._metadata_type.__name__} as the path is read-only"
            )
        raise DebputyMetadataAccessError(
            f"Cannot set the metadata {self._metadata_type.__name__} owned by"
            f" {self._owning_plugin} as the metadata has not been made"
            f" read-write to the plugin {self._current_plugin}."
        )

    @property
    def _is_owner(self) -> bool:
        return self._owning_plugin == self._current_plugin


@dataclasses.dataclass(slots=True)
class PathMetadataValue(Generic[PMT]):
    owning_plugin: str
    metadata_type: Type[PMT]
    value: Optional[PMT] = None

    def can_read_value(self, current_plugin: str) -> bool:
        return self.owning_plugin == current_plugin

    def can_write_value(self, current_plugin: str) -> bool:
        return self.owning_plugin == current_plugin


class PathMetadataReferenceImplementation(PathMetadataReference[PMT]):
    __slots__ = ("_owning_path", "_current_plugin", "_path_metadata_value")

    def __init__(
        self,
        owning_path: VirtualPath,
        current_plugin: str,
        path_metadata_value: PathMetadataValue[PMT],
    ) -> None:
        self._owning_path = owning_path
        self._current_plugin = current_plugin
        self._path_metadata_value = path_metadata_value

    @property
    def is_present(self) -> bool:
        if not self.can_read:
            return False
        return self._path_metadata_value.value is not None

    @property
    def can_read(self) -> bool:
        return self._path_metadata_value.can_read_value(self._current_plugin)

    @property
    def can_write(self) -> bool:
        if not self._path_metadata_value.can_write_value(self._current_plugin):
            return False
        owning_path = self._owning_path
        return owning_path.is_read_write and not owning_path.is_detached

    @property
    def value(self) -> Optional[PMT]:
        if self.can_read:
            return self._path_metadata_value.value
        raise DebputyMetadataAccessError(
            f"Cannot read the metadata {self._metadata_type_name} owned by"
            f" {self._owning_plugin} as the metadata has not been made"
            f" readable to the plugin {self._current_plugin}."
        )

    @value.setter
    def value(self, new_value: PMT) -> None:
        if not self.can_write:
            m = "set" if new_value is not None else "delete"
            raise DebputyMetadataAccessError(
                f"Cannot {m} the metadata {self._metadata_type_name} owned by"
                f" {self._owning_plugin} as the metadata has not been made"
                f" read-write to the plugin {self._current_plugin}."
            )
        owning_path = self._owning_path
        if not owning_path.is_read_write:
            raise DebputyFSIsROError(
                f"Cannot set the metadata {self._metadata_type_name} as the path is read-only"
            )
        if owning_path.is_detached:
            raise TypeError(
                f"Cannot set the metadata {self._metadata_type_name} as the path is detached"
            )
        self._path_metadata_value.value = new_value

    @property
    def _is_owner(self) -> bool:
        return self._owning_plugin == self._current_plugin

    @property
    def _owning_plugin(self) -> str:
        return self._path_metadata_value.owning_plugin

    @property
    def _metadata_type_name(self) -> str:
        return self._path_metadata_value.metadata_type.__name__


def _cp_a(source: str, dest: str) -> None:
    cmd = ["cp", "-a", source, dest]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        full_command = escape_shell(*cmd)
        _error(
            f"The attempt to make an internal copy of {escape_shell(source)} failed. Please review the output of cp"
            f" above to understand what went wrong. The full command was: {full_command}"
        )


def _split_path(path: str) -> Tuple[bool, bool, List[str]]:
    must_be_dir = True if path.endswith("/") else False
    absolute = False
    if path.startswith("/"):
        absolute = True
        path = "." + path
    path_parts = path.rstrip("/").split("/")
    if must_be_dir:
        path_parts.append(".")
    return absolute, must_be_dir, path_parts


def _root(path: VP) -> VP:
    current = path
    while True:
        parent = current.parent_dir
        if parent is None:
            return current
        current = parent


def _check_fs_path_is_file(
    fs_path: str,
    unlink_on_error: Optional["FSPath"] = None,
) -> None:
    had_issue = False
    try:
        # FIXME: Check mode, and use the Virtual Path to cache the result as a side-effect
        st = os.lstat(fs_path)
    except FileNotFoundError:
        had_issue = True
    else:
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            had_issue = True
    if not had_issue:
        return

    if unlink_on_error:
        with suppress(FileNotFoundError):
            os.unlink(fs_path)
    raise TypeError(
        "The provided FS backing file was deleted, replaced with a non-file entry or it was hard"
        " linked to another file. The entry has been disconnected."
    )


class CurrentPluginContextManager:
    __slots__ = ("_plugin_names",)

    def __init__(self, initial_plugin_name: str) -> None:
        self._plugin_names = [initial_plugin_name]

    @property
    def current_plugin_name(self) -> str:
        return self._plugin_names[-1]

    @contextlib.contextmanager
    def change_plugin_context(self, new_plugin_name: str) -> Iterator[str]:
        self._plugin_names.append(new_plugin_name)
        yield new_plugin_name
        self._plugin_names.pop()


class VirtualPathBase(VirtualPath, ABC):
    __slots__ = ()

    def _orphan_safe_path(self) -> str:
        return self.path

    def _rw_check(self) -> None:
        if not self.is_read_write:
            raise DebputyFSIsROError(
                f'Attempt to write to "{self._orphan_safe_path()}" failed:'
                " Debputy Virtual File system is R/O."
            )

    def lookup(self, path: str) -> Optional["VirtualPathBase"]:
        match, missing = self.attempt_lookup(path)
        if missing:
            return None
        return match

    def attempt_lookup(self, path: str) -> Tuple["VirtualPathBase", List[str]]:
        if self.is_detached:
            raise ValueError(
                f'Cannot perform lookup via "{self._orphan_safe_path()}": The path is detached'
            )
        absolute, must_be_dir, path_parts = _split_path(path)
        current = _root(self) if absolute else self
        path_parts.reverse()
        link_expansions = set()
        while path_parts:
            dir_part = path_parts.pop()
            if dir_part == ".":
                continue
            if dir_part == "..":
                p = current.parent_dir
                if p is None:
                    raise ValueError(f'The path "{path}" escapes the root dir')
                current = p
                continue
            try:
                current = current[dir_part]
            except KeyError:
                path_parts.append(dir_part)
                path_parts.reverse()
                if must_be_dir:
                    path_parts.pop()
                return current, path_parts
            if current.is_symlink and path_parts:
                if current.path in link_expansions:
                    # This is our loop detection for now. It might have some false positives where you
                    # could safely resolve the same symlink twice. However, given that this use-case is
                    # basically non-existent in practice for packaging, we just stop here for now.
                    raise SymlinkLoopError(
                        f'The path "{path}" traversed the symlink "{current.path}" multiple'
                        " times. Currently, traversing the same symlink twice is considered"
                        " a loop by `debputy` even if the path would eventually resolve."
                        " Consider filing a feature request if you have a benign case that"
                        " triggers this error."
                    )
                link_expansions.add(current.path)
                link_target = current.readlink()
                link_absolute, _, link_path_parts = _split_path(link_target)
                if link_absolute:
                    current = _root(current)
                else:
                    current = assume_not_none(current.parent_dir)
                link_path_parts.reverse()
                path_parts.extend(link_path_parts)
        return current, []

    def mkdirs(self, path: str) -> "VirtualPath":
        current: VirtualPath
        current, missing_parts = self.attempt_lookup(
            f"{path}/" if not path.endswith("/") else path
        )
        if not current.is_dir:
            raise ValueError(
                f'mkdirs of "{path}" failed: This would require {current.path} to not exist OR be'
                " a directory. However, that path exist AND is a not directory."
            )
        for missing_part in missing_parts:
            assert missing_part not in (".", "..")
            current = current.mkdir(missing_part)
        return current

    def prune_if_empty_dir(self) -> None:
        """Remove this and all (now) empty parent directories

        Same as: `rmdir --ignore-fail-on-non-empty --parents`

        This operation may cause the path (and any of its parent directories) to become "detached"
        and therefore unsafe to use in further operations.
        """
        self._rw_check()

        if not self.is_dir:
            raise TypeError(f"{self._orphan_safe_path()} is not a directory")
        if any(self.iterdir):
            return
        parent_dir = assume_not_none(self.parent_dir)

        # Recursive does not matter; we already know the directory is empty.
        self.unlink()

        # Note: The root dir must never be deleted. This works because when delegating it to the root
        # directory, its implementation of this method is a no-op. If this is later rewritten to an
        # inline loop (rather than recursion), be sure to preserve this feature.
        parent_dir.prune_if_empty_dir()

    def _current_plugin(self) -> str:
        if self.is_detached:
            raise TypeError("Cannot resolve the current plugin; path is detached")
        current = self
        while True:
            next_parent = current.parent_dir
            if next_parent is None:
                break
            current = next_parent
        assert current is not None
        return cast("FSRootDir", current)._current_plugin()


class FSPath(VirtualPathBase, ABC):
    __slots__ = (
        "_basename",
        "_parent_dir",
        "_children",
        "_path_cache",
        "_parent_path_cache",
        "_last_known_parent_path",
        "_mode",
        "_owner",
        "_group",
        "_mtime",
        "_stat_cache",
        "_metadata",
        "__weakref__",
    )

    def __init__(
        self,
        basename: str,
        parent: Optional["FSPath"],
        children: Optional[Dict[str, "FSPath"]] = None,
        initial_mode: Optional[int] = None,
        mtime: Optional[float] = None,
        stat_cache: Optional[os.stat_result] = None,
    ) -> None:
        self._basename = basename
        self._path_cache: Optional[str] = None
        self._parent_path_cache: Optional[str] = None
        self._children = children
        self._last_known_parent_path: Optional[str] = None
        self._mode = initial_mode
        self._mtime = mtime
        self._stat_cache = stat_cache
        self._metadata: Dict[Tuple[str, Type[Any]], PathMetadataValue[Any]] = {}
        self._owner = ROOT_DEFINITION
        self._group = ROOT_DEFINITION

        # The self._parent_dir = None is to create `_parent_dir` because the parent_dir setter calls
        # is_orphaned, which assumes self._parent_dir is an attribute.
        self._parent_dir: Optional[ReferenceType["FSPath"]] = None
        if parent is not None:
            self.parent_dir = parent

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self._orphan_safe_path()!r},"
            f" is_file={self.is_file},"
            f" is_dir={self.is_dir},"
            f" is_symlink={self.is_symlink},"
            f" has_fs_path={self.has_fs_path},"
            f" children_len={len(self._children) if self._children else 0})"
        )

    @property
    def name(self) -> str:
        return self._basename

    @name.setter
    def name(self, new_name: str) -> None:
        self._rw_check()
        if new_name == self._basename:
            return
        if self.is_detached:
            self._basename = new_name
            return
        self._rw_check()
        parent = self.parent_dir
        # This little parent_dir dance ensures the parent dir detects the rename properly
        self.parent_dir = None
        self._basename = new_name
        self.parent_dir = parent

    @property
    def iterdir(self) -> Iterable["FSPath"]:
        if self._children is not None:
            yield from self._children.values()

    def all_paths(self) -> Iterable["FSPath"]:
        yield self
        if not self.is_dir:
            return
        by_basename = BY_BASENAME
        stack = sorted(self.iterdir, key=by_basename, reverse=True)
        while stack:
            current = stack.pop()
            yield current
            if current.is_dir and not current.is_detached:
                stack.extend(sorted(current.iterdir, key=by_basename, reverse=True))

    def walk(self) -> Iterable[Tuple["FSPath", List["FSPath"]]]:
        # FIXME: can this be more "os.walk"-like without making it harder to implement?
        if not self.is_dir:
            yield self, []
            return
        by_basename = BY_BASENAME
        stack = [self]
        while stack:
            current = stack.pop()
            children = sorted(current.iterdir, key=by_basename)
            assert not children or current.is_dir
            yield current, children
            # Removing the directory counts as discarding the children.
            if not current.is_detached:
                stack.extend(reversed(children))

    def _orphan_safe_path(self) -> str:
        if not self.is_detached or self._last_known_parent_path is not None:
            return self.path
        return f"<orphaned>/{self.name}"

    @property
    def is_detached(self) -> bool:
        parent = self._parent_dir
        if parent is None:
            return True
        resolved_parent = parent()
        if resolved_parent is None:
            return True
        return resolved_parent.is_detached

    # The __getitem__ behaves like __getitem__ from Dict but __iter__ would ideally work like a Sequence.
    # However, that does not feel compatible, so lets force people to use .children instead for the Sequence
    # behaviour to avoid surprises for now.
    # (Maybe it is a non-issue, but it is easier to add the API later than to remove it once we have committed
    # to using it)
    __iter__ = None

    def __getitem__(self, key) -> "FSPath":
        if self._children is None:
            raise KeyError(
                f"{key} (note: {self._orphan_safe_path()!r} has no children)"
            )
        if isinstance(key, FSPath):
            key = key.name
        return self._children[key]

    def __delitem__(self, key) -> None:
        self._rw_check()
        children = self._children
        if children is None:
            raise KeyError(key)
        del children[key]

    def get(self, key: str) -> "Optional[FSPath]":
        try:
            return self[key]
        except KeyError:
            return None

    def __contains__(self, item: object) -> bool:
        if isinstance(item, VirtualPath):
            return item.parent_dir is self
        if not isinstance(item, str):
            return False
        m = self.get(item)
        return m is not None

    def _add_child(self, child: "FSPath") -> None:
        self._rw_check()
        if not self.is_dir:
            raise TypeError(f"{self._orphan_safe_path()!r} is not a directory")
        if self._children is None:
            self._children = {}

        conflict_child = self.get(child.name)
        if conflict_child is not None:
            conflict_child.unlink(recursive=True)
        self._children[child.name] = child

    @property
    def tar_path(self) -> str:
        path = self.path
        if self.is_dir:
            return path + "/"
        return path

    @property
    def path(self) -> str:
        parent_path = self.parent_dir_path
        if (
            self._parent_path_cache is not None
            and self._parent_path_cache == parent_path
        ):
            return assume_not_none(self._path_cache)
        if parent_path is None:
            raise ReferenceError(
                f"The path {self.name} is detached! {self.__class__.__name__}"
            )
        self._parent_path_cache = parent_path
        ret = os.path.join(parent_path, self.name)
        self._path_cache = ret
        return ret

    @property
    def parent_dir(self) -> Optional["FSPath"]:
        p_ref = self._parent_dir
        p = p_ref() if p_ref is not None else None
        if p is None:
            raise ReferenceError(
                f"The path {self.name} is detached! {self.__class__.__name__}"
            )
        return p

    @parent_dir.setter
    def parent_dir(self, new_parent: Optional["FSPath"]) -> None:
        self._rw_check()
        if new_parent is not None:
            if not new_parent.is_dir:
                raise ValueError(
                    f"The parent {new_parent._orphan_safe_path()} must be a directory"
                )
            new_parent._rw_check()
        old_parent = None
        self._last_known_parent_path = None
        if not self.is_detached:
            old_parent = self.parent_dir
            old_parent_children = assume_not_none(assume_not_none(old_parent)._children)
            del old_parent_children[self.name]
        if new_parent is not None:
            self._parent_dir = ref(new_parent)
            new_parent._add_child(self)
        else:
            if old_parent is not None and not old_parent.is_detached:
                self._last_known_parent_path = old_parent.path
            self._parent_dir = None
        self._parent_path_cache = None

    @property
    def parent_dir_path(self) -> Optional[str]:
        if self.is_detached:
            return self._last_known_parent_path
        return assume_not_none(self.parent_dir).path

    def chown(
        self,
        owner: Optional[StaticFileSystemOwner],
        group: Optional[StaticFileSystemGroup],
    ) -> None:
        """Change the owner/group of this path

        :param owner: The desired owner definition for this path. If None, then no change of owner is performed.
        :param group: The desired  group definition for this path. If None, then no change of group is performed.
        """
        self._rw_check()

        if owner is not None:
            self._owner = owner.ownership_definition
        if group is not None:
            self._group = group.ownership_definition

    def stat(self) -> os.stat_result:
        st = self._stat_cache
        if st is None:
            st = self._uncached_stat()
            self._stat_cache = st
        return st

    def _uncached_stat(self) -> os.stat_result:
        return os.lstat(self.fs_path)

    @property
    def mode(self) -> int:
        current_mode = self._mode
        if current_mode is None:
            current_mode = stat.S_IMODE(self.stat().st_mode)
            self._mode = current_mode
        return current_mode

    @mode.setter
    def mode(self, new_mode: int) -> None:
        self._rw_check()
        min_bit = 0o500 if self.is_dir else 0o400
        if (new_mode & min_bit) != min_bit:
            omode = oct(new_mode)[2:]
            omin = oct(min_bit)[2:]
            raise ValueError(
                f'Attempt to set mode of path "{self._orphan_safe_path()}" to {omode} rejected;'
                f" Minimum requirements are {omin} (read-bit and, for dirs, exec bit for user)."
                " There are no paths that do not need these requirements met and they can cause"
                " problems during build or on the final system."
            )
        self._mode = new_mode

    @property
    def mtime(self) -> float:
        mtime = self._mtime
        if mtime is None:
            mtime = self.stat().st_mtime
            self._mtime = mtime
        return mtime

    @mtime.setter
    def mtime(self, new_mtime: float) -> None:
        self._rw_check()
        self._mtime = new_mtime

    @property
    def tar_owner_info(self) -> Tuple[str, int, str, int]:
        owner = self._owner
        group = self._group
        return (
            owner.entity_name,
            owner.entity_id,
            group.entity_name,
            group.entity_id,
        )

    @property
    def _can_replace_inline(self) -> bool:
        return False

    @contextlib.contextmanager
    def add_file(
        self,
        name: str,
        *,
        unlink_if_exists: bool = True,
        use_fs_path_mode: bool = False,
        mode: int = 0o0644,
        mtime: Optional[float] = None,
        # Special-case parameters that are not exposed in the API
        fs_basename_matters: bool = False,
        subdir_key: Optional[str] = None,
    ) -> Iterator["FSPath"]:
        if "/" in name or name in {".", ".."}:
            raise ValueError(f'Invalid file name: "{name}"')
        if not self.is_dir:
            raise TypeError(
                f"Cannot create {self._orphan_safe_path()}/{name}:"
                f" {self._orphan_safe_path()} is not a directory"
            )
        self._rw_check()
        existing = self.get(name)
        if existing is not None:
            if not unlink_if_exists:
                raise ValueError(
                    f'The path "{self._orphan_safe_path()}" already contains a file called "{name}"'
                    f" and exist_ok was False"
                )
            existing.unlink(recursive=False)

        if fs_basename_matters and subdir_key is None:
            raise ValueError(
                "When fs_basename_matters is True, a subdir_key must be provided"
            )

        directory = generated_content_dir(subdir_key=subdir_key)

        if fs_basename_matters:
            fs_path = os.path.join(directory, name)
            with open(fs_path, "xb") as _:
                # Ensure that the fs_path exists
                pass
            child = FSBackedFilePath(
                name,
                self,
                fs_path,
                replaceable_inline=True,
                mtime=mtime,
            )
            yield child
        else:
            with tempfile.NamedTemporaryFile(
                dir=directory, suffix=f"__{name}", delete=False
            ) as fd:
                fs_path = fd.name
                child = FSBackedFilePath(
                    name,
                    self,
                    fs_path,
                    replaceable_inline=True,
                    mtime=mtime,
                )
                fd.close()
                yield child

        if use_fs_path_mode:
            # Ensure the caller can see the current mode
            os.chmod(fs_path, mode)
        _check_fs_path_is_file(fs_path, unlink_on_error=child)
        child._reset_caches()
        if not use_fs_path_mode:
            child.mode = mode

    def insert_file_from_fs_path(
        self,
        name: str,
        fs_path: str,
        *,
        exist_ok: bool = True,
        use_fs_path_mode: bool = False,
        mode: int = 0o0644,
        require_copy_on_write: bool = True,
        follow_symlinks: bool = True,
        reference_path: Optional[VirtualPath] = None,
    ) -> "FSPath":
        if "/" in name or name in {".", ".."}:
            raise ValueError(f'Invalid file name: "{name}"')
        if not self.is_dir:
            raise TypeError(
                f"Cannot create {self._orphan_safe_path()}/{name}:"
                f" {self._orphan_safe_path()} is not a directory"
            )
        self._rw_check()
        if name in self and not exist_ok:
            raise ValueError(
                f'The path "{self._orphan_safe_path()}" already contains a file called "{name}"'
                f" and exist_ok was False"
            )
        new_fs_path = fs_path
        if follow_symlinks:
            if reference_path is not None:
                raise ValueError(
                    "The reference_path cannot be used with follow_symlinks"
                )
            new_fs_path = os.path.realpath(new_fs_path, strict=True)

        fmode: Optional[int] = mode
        if use_fs_path_mode:
            fmode = None

        st = None
        if reference_path is None:
            st = os.lstat(new_fs_path)
            if stat.S_ISDIR(st.st_mode):
                raise ValueError(
                    f'The provided path "{fs_path}" is a directory. However, this'
                    " method does not support directories"
                )

            if not stat.S_ISREG(st.st_mode):
                if follow_symlinks:
                    raise ValueError(
                        f"The resolved fs_path ({new_fs_path}) was not a file."
                    )
                raise ValueError(f"The provided fs_path ({fs_path}) was not a file.")
        return FSBackedFilePath(
            name,
            self,
            new_fs_path,
            initial_mode=fmode,
            stat_cache=st,
            replaceable_inline=not require_copy_on_write,
            reference_path=reference_path,
        )

    def add_symlink(
        self,
        link_name: str,
        link_target: str,
        *,
        reference_path: Optional[VirtualPath] = None,
    ) -> "FSPath":
        if "/" in link_name or link_name in {".", ".."}:
            raise ValueError(
                f'Invalid file name: "{link_name}" (it must be a valid basename)'
            )
        if not self.is_dir:
            raise TypeError(
                f"Cannot create {self._orphan_safe_path()}/{link_name}:"
                f" {self._orphan_safe_path()} is not a directory"
            )
        self._rw_check()

        existing = self.get(link_name)
        if existing:
            # Emulate ln -sf with attempts a non-recursive unlink first.
            existing.unlink(recursive=False)

        return SymlinkVirtualPath(
            link_name,
            self,
            link_target,
            reference_path=reference_path,
        )

    def mkdir(
        self,
        name: str,
        *,
        reference_path: Optional[VirtualPath] = None,
    ) -> "FSPath":
        if "/" in name or name in {".", ".."}:
            raise ValueError(
                f'Invalid file name: "{name}" (it must be a valid basename)'
            )
        if not self.is_dir:
            raise TypeError(
                f"Cannot create {self._orphan_safe_path()}/{name}:"
                f" {self._orphan_safe_path()} is not a directory"
            )
        if reference_path is not None and not reference_path.is_dir:
            raise ValueError(
                f'The provided fs_path "{reference_path.fs_path}" exist but it is not a directory!'
            )
        self._rw_check()

        existing = self.get(name)
        if existing:
            raise ValueError(f"Path {existing.path} already exist")
        return VirtualDirectoryFSPath(name, self, reference_path=reference_path)

    def mkdirs(self, path: str) -> "FSPath":
        return cast("FSPath", super().mkdirs(path))

    @property
    def is_read_write(self) -> bool:
        """When true, the file system entry may be mutated

        :return: Whether file system mutations are permitted.
        """
        if self.is_detached:
            return True
        return assume_not_none(self.parent_dir).is_read_write

    def unlink(self, *, recursive: bool = False) -> None:
        """Unlink a file or a directory

        This operation will detach the path from the file system (causing "is_detached" to return True).

        Note that the root directory cannot be deleted.

        :param recursive: If True, then non-empty directories will be unlinked as well removing everything inside them
          as well.  When False, an error is raised if the path is a non-empty directory
        """
        if self.is_detached:
            return
        if not recursive and any(self.iterdir):
            raise ValueError(
                f'Refusing to unlink "{self.path}": The directory was not empty and recursive was False'
            )
        # The .parent_dir setter does a _rw_check() for us.
        self.parent_dir = None

    def _reset_caches(self) -> None:
        self._mtime = None
        self._stat_cache = None

    def metadata(
        self,
        metadata_type: Type[PMT],
        *,
        owning_plugin: Optional[str] = None,
    ) -> PathMetadataReference[PMT]:
        current_plugin = self._current_plugin()
        if owning_plugin is None:
            owning_plugin = current_plugin
        metadata_key = (owning_plugin, metadata_type)
        metadata_value = self._metadata.get(metadata_key)
        if metadata_value is None:
            if self.is_detached:
                raise TypeError(
                    f"Cannot access the metadata {metadata_type.__name__}: The path is detached."
                )
            if not self.is_read_write:
                return AlwaysEmptyReadOnlyMetadataReference(
                    owning_plugin,
                    current_plugin,
                    metadata_type,
                )
            metadata_value = PathMetadataValue(owning_plugin, metadata_type)
            self._metadata[metadata_key] = metadata_value
        return PathMetadataReferenceImplementation(
            self,
            current_plugin,
            metadata_value,
        )

    @contextlib.contextmanager
    def replace_fs_path_content(
        self,
        *,
        use_fs_path_mode: bool = False,
    ) -> Iterator[str]:
        if not self.is_file:
            raise TypeError(
                f'Cannot replace contents of "{self._orphan_safe_path()}" as it is not a file'
            )
        self._rw_check()
        fs_path = self.fs_path
        if not self._can_replace_inline:
            fs_path = self.fs_path
            directory = generated_content_dir()
            with tempfile.NamedTemporaryFile(
                dir=directory, suffix=f"__{self.name}", delete=False
            ) as new_path_fd:
                new_path_fd.close()
                _cp_a(fs_path, new_path_fd.name)
                fs_path = new_path_fd.name
                self._replaced_path(fs_path)
                assert self.fs_path == fs_path

        current_mtime = self._mtime
        if current_mtime is not None:
            os.utime(fs_path, (current_mtime, current_mtime))

        current_mode = self.mode
        yield fs_path
        _check_fs_path_is_file(fs_path, unlink_on_error=self)
        if not use_fs_path_mode:
            os.chmod(fs_path, current_mode)
        self._reset_caches()

    def _replaced_path(self, new_fs_path: str) -> None:
        raise NotImplementedError


class VirtualFSPathBase(FSPath, ABC):
    __slots__ = ()

    def __init__(
        self,
        basename: str,
        parent: Optional["FSPath"],
        children: Optional[Dict[str, "FSPath"]] = None,
        initial_mode: Optional[int] = None,
        mtime: Optional[float] = None,
        stat_cache: Optional[os.stat_result] = None,
    ) -> None:
        super().__init__(
            basename,
            parent,
            children,
            initial_mode=initial_mode,
            mtime=mtime,
            stat_cache=stat_cache,
        )

    @property
    def mtime(self) -> float:
        mtime = self._mtime
        if mtime is None:
            mtime = time.time()
            self._mtime = mtime
        return mtime

    @property
    def has_fs_path(self) -> bool:
        return False

    def stat(self) -> os.stat_result:
        if not self.has_fs_path:
            raise PureVirtualPathError(
                "stat() is only applicable to paths backed by the file system. The path"
                f" {self._orphan_safe_path()!r} is purely virtual"
            )
        return super().stat()

    @property
    def fs_path(self) -> str:
        if not self.has_fs_path:
            raise PureVirtualPathError(
                "fs_path is only applicable to paths backed by the file system. The path"
                f" {self._orphan_safe_path()!r} is purely virtual"
            )
        return self.fs_path


class FSRootDir(FSPath):
    __slots__ = ("_fs_path", "_fs_read_write", "_plugin_context")

    def __init__(self, fs_path: Optional[str] = None) -> None:
        self._fs_path = fs_path
        self._fs_read_write = True
        super().__init__(
            ".",
            None,
            children={},
            initial_mode=0o755,
        )
        self._plugin_context = CurrentPluginContextManager("debputy")

    @property
    def is_detached(self) -> bool:
        return False

    def _orphan_safe_path(self) -> str:
        return self.name

    @property
    def path(self) -> str:
        return self.name

    @property
    def parent_dir(self) -> Optional["FSPath"]:
        return None

    @parent_dir.setter
    def parent_dir(self, new_parent: Optional[FSPath]) -> None:
        if new_parent is not None:
            raise ValueError("The root directory cannot become a non-root directory")

    @property
    def parent_dir_path(self) -> Optional[str]:
        return None

    @property
    def is_dir(self) -> bool:
        return True

    @property
    def is_file(self) -> bool:
        return False

    @property
    def is_symlink(self) -> bool:
        return False

    def readlink(self) -> str:
        raise TypeError(f'"{self._orphan_safe_path()!r}" is a directory; not a symlink')

    @property
    def has_fs_path(self) -> bool:
        return self._fs_path is not None

    def stat(self) -> os.stat_result:
        if not self.has_fs_path:
            raise PureVirtualPathError(
                "stat() is only applicable to paths backed by the file system. The path"
                f" {self._orphan_safe_path()!r} is purely virtual"
            )
        return os.stat(self.fs_path)

    @property
    def fs_path(self) -> str:
        if not self.has_fs_path:
            raise PureVirtualPathError(
                "fs_path is only applicable to paths backed by the file system. The path"
                f" {self._orphan_safe_path()!r} is purely virtual"
            )
        return assume_not_none(self._fs_path)

    @property
    def is_read_write(self) -> bool:
        return self._fs_read_write

    @is_read_write.setter
    def is_read_write(self, new_value: bool) -> None:
        self._fs_read_write = new_value

    def prune_if_empty_dir(self) -> None:
        # No-op for the root directory. There is never a case where you want to delete this directory
        # (and even if you could, debputy will need it for technical reasons, so the root dir stays)
        return

    def unlink(self, *, recursive: bool = False) -> None:
        # There is never a case where you want to delete this directory (and even if you could,
        # debputy will need it for technical reasons, so the root dir stays)
        raise TypeError("Cannot delete the root directory")

    def _current_plugin(self) -> str:
        return self._plugin_context.current_plugin_name

    @contextlib.contextmanager
    def change_plugin_context(self, new_plugin: str) -> Iterator[str]:
        with self._plugin_context.change_plugin_context(new_plugin) as r:
            yield r


class VirtualPathWithReference(VirtualFSPathBase, ABC):
    __slots__ = ("_reference_path",)

    def __init__(
        self,
        basename: str,
        parent: FSPath,
        *,
        default_mode: int,
        reference_path: Optional[VirtualPath] = None,
    ) -> None:
        super().__init__(
            basename,
            parent=parent,
            initial_mode=reference_path.mode if reference_path else default_mode,
        )
        self._reference_path = reference_path

    @property
    def has_fs_path(self) -> bool:
        ref_path = self._reference_path
        return ref_path is not None and ref_path.has_fs_path

    @property
    def mtime(self) -> float:
        mtime = self._mtime
        if mtime is None:
            ref_path = self._reference_path
            if ref_path:
                mtime = ref_path.mtime
            else:
                mtime = super().mtime
            self._mtime = mtime
        return mtime

    @mtime.setter
    def mtime(self, new_mtime: float) -> None:
        self._rw_check()
        self._mtime = new_mtime

    @property
    def fs_path(self) -> str:
        ref_path = self._reference_path
        if ref_path is not None and (
            not super().has_fs_path or super().fs_path == ref_path.fs_path
        ):
            return ref_path.fs_path
        return super().fs_path

    def stat(self) -> os.stat_result:
        ref_path = self._reference_path
        if ref_path is not None and (
            not super().has_fs_path or super().fs_path == ref_path.fs_path
        ):
            return ref_path.stat()
        return super().stat()

    def open(
        self,
        *,
        byte_io: bool = False,
        buffering: int = -1,
    ) -> Union[TextIO, BinaryIO]:
        reference_path = self._reference_path
        if reference_path is not None and reference_path.fs_path == self.fs_path:
            return reference_path.open(byte_io=byte_io, buffering=buffering)
        return super().open(byte_io=byte_io, buffering=buffering)


class VirtualDirectoryFSPath(VirtualPathWithReference):
    __slots__ = ("_reference_path",)

    def __init__(
        self,
        basename: str,
        parent: FSPath,
        *,
        reference_path: Optional[VirtualPath] = None,
    ) -> None:
        super().__init__(
            basename,
            parent,
            reference_path=reference_path,
            default_mode=0o755,
        )
        self._reference_path = reference_path
        assert reference_path is None or reference_path.is_dir

    @property
    def is_dir(self) -> bool:
        return True

    @property
    def is_file(self) -> bool:
        return False

    @property
    def is_symlink(self) -> bool:
        return False

    def readlink(self) -> str:
        raise TypeError(f'"{self._orphan_safe_path()!r}" is a directory; not a symlink')


class SymlinkVirtualPath(VirtualPathWithReference):
    __slots__ = ("_link_target",)

    def __init__(
        self,
        basename: str,
        parent_dir: FSPath,
        link_target: str,
        *,
        reference_path: Optional[VirtualPath] = None,
    ) -> None:
        super().__init__(
            basename,
            parent=parent_dir,
            default_mode=_SYMLINK_MODE,
            reference_path=reference_path,
        )
        self._link_target = link_target

    @property
    def is_dir(self) -> bool:
        return False

    @property
    def is_file(self) -> bool:
        return False

    @property
    def is_symlink(self) -> bool:
        return True

    def readlink(self) -> str:
        return self._link_target


class FSBackedFilePath(VirtualPathWithReference):
    __slots__ = ("_fs_path", "_replaceable_inline")

    def __init__(
        self,
        basename: str,
        parent_dir: FSPath,
        fs_path: str,
        *,
        replaceable_inline: bool = False,
        initial_mode: Optional[int] = None,
        mtime: Optional[float] = None,
        stat_cache: Optional[os.stat_result] = None,
        reference_path: Optional[VirtualPath] = None,
    ) -> None:
        super().__init__(
            basename,
            parent_dir,
            default_mode=0o644,
            reference_path=reference_path,
        )
        self._fs_path = fs_path
        self._replaceable_inline = replaceable_inline
        if initial_mode is not None:
            self.mode = initial_mode
        if mtime is not None:
            self._mtime = mtime
        self._stat_cache = stat_cache
        assert (
            not replaceable_inline or "debputy/scratch-dir/" in fs_path
        ), f"{fs_path} should not be inline-replaceable -- {self.path}"

    @property
    def is_dir(self) -> bool:
        return False

    @property
    def is_file(self) -> bool:
        return True

    @property
    def is_symlink(self) -> bool:
        return False

    def readlink(self) -> str:
        raise TypeError(f'"{self._orphan_safe_path()!r}" is a file; not a symlink')

    @property
    def has_fs_path(self) -> bool:
        return True

    @property
    def fs_path(self) -> str:
        return self._fs_path

    @property
    def _can_replace_inline(self) -> bool:
        return self._replaceable_inline

    def _replaced_path(self, new_fs_path: str) -> None:
        self._fs_path = new_fs_path
        self._reference_path = None
        self._replaceable_inline = True


_SYMLINK_MODE = 0o777


class VirtualTestPath(FSPath):
    __slots__ = (
        "_path_type",
        "_has_fs_path",
        "_fs_path",
        "_link_target",
        "_content",
        "_materialized_content",
    )

    def __init__(
        self,
        basename: str,
        parent_dir: Optional[FSPath],
        mode: Optional[int] = None,
        mtime: Optional[float] = None,
        is_dir: bool = False,
        has_fs_path: Optional[bool] = False,
        fs_path: Optional[str] = None,
        link_target: Optional[str] = None,
        content: Optional[str] = None,
        materialized_content: Optional[str] = None,
    ) -> None:
        if is_dir:
            self._path_type = PathType.DIRECTORY
        elif link_target is not None:
            self._path_type = PathType.SYMLINK
            if mode is not None and mode != _SYMLINK_MODE:
                raise ValueError(
                    f'Please do not assign a mode to symlinks. Triggered for "{basename}".'
                )
            assert mode is None or mode == _SYMLINK_MODE
        else:
            self._path_type = PathType.FILE

        if mode is not None:
            initial_mode = mode
        else:
            initial_mode = 0o755 if is_dir else 0o644

        self._link_target = link_target
        if has_fs_path is None:
            has_fs_path = bool(fs_path)
        self._has_fs_path = has_fs_path
        self._fs_path = fs_path
        self._materialized_content = materialized_content
        super().__init__(
            basename,
            parent=parent_dir,
            initial_mode=initial_mode,
            mtime=mtime,
        )
        self._content = content

    @property
    def is_dir(self) -> bool:
        return self._path_type == PathType.DIRECTORY

    @property
    def is_file(self) -> bool:
        return self._path_type == PathType.FILE

    @property
    def is_symlink(self) -> bool:
        return self._path_type == PathType.SYMLINK

    def readlink(self) -> str:
        if not self.is_symlink:
            raise TypeError(f"readlink is only valid for symlinks ({self.path!r})")
        link_target = self._link_target
        assert link_target is not None
        return link_target

    @property
    def mtime(self) -> float:
        if self._mtime is None:
            self._mtime = time.time()
        return self._mtime

    @mtime.setter
    def mtime(self, new_mtime: float) -> None:
        self._rw_check()
        self._mtime = new_mtime

    @property
    def has_fs_path(self) -> bool:
        return self._has_fs_path

    def stat(self) -> os.stat_result:
        if self.has_fs_path:
            path = self.fs_path
            if path is None:
                raise PureVirtualPathError(
                    f"The test wants a real stat of {self._orphan_safe_path()!r}, which this mock path"
                    " cannot provide!"
                )
            try:
                return os.stat(path)
            except FileNotFoundError as e:
                raise PureVirtualPathError(
                    f"The test wants a real stat of {self._orphan_safe_path()!r}, which this mock path"
                    " cannot provide! (An fs_path was provided, but it did not exist)"
                ) from e

        raise PureVirtualPathError(
            "stat() is only applicable to paths backed by the file system. The path"
            f" {self._orphan_safe_path()!r} is purely virtual"
        )

    @property
    def size(self) -> int:
        if self._content is not None:
            return len(self._content.encode("utf-8"))
        if not self.has_fs_path or self.fs_path is None:
            return 0
        return self.stat().st_size

    @property
    def fs_path(self) -> str:
        if self.has_fs_path:
            if self._fs_path is None and self._materialized_content is not None:
                with tempfile.NamedTemporaryFile(
                    mode="w+t",
                    encoding="utf-8",
                    suffix=f"__{self.name}",
                    delete=False,
                ) as fd:
                    filepath = fd.name
                    fd.write(self._materialized_content)
                self._fs_path = filepath
                atexit.register(lambda: os.unlink(filepath))

            path = self._fs_path
            if path is None:
                raise PureVirtualPathError(
                    f"The test wants a real file system entry of {self._orphan_safe_path()!r}, which this "
                    " mock path cannot provide!"
                )
            return path
        raise PureVirtualPathError(
            "fs_path is only applicable to paths backed by the file system. The path"
            f" {self._orphan_safe_path()!r} is purely virtual"
        )

    def replace_fs_path_content(
        self,
        *,
        use_fs_path_mode: bool = False,
    ) -> ContextManager[str]:
        if self._content is not None:
            raise TypeError(
                f"The `replace_fs_path_content()` method was called on {self.path}. Said path was"
                " created with `content` but for this method to work, the path should have been"
                " created with `materialized_content`"
            )
        return super().replace_fs_path_content(use_fs_path_mode=use_fs_path_mode)

    def open(
        self,
        *,
        byte_io: bool = False,
        buffering: int = -1,
    ) -> Union[TextIO, BinaryIO]:
        if self._content is None:
            try:
                return super().open(byte_io=byte_io, buffering=buffering)
            except FileNotFoundError as e:
                raise TestPathWithNonExistentFSPathError(
                    "The test path {self.path} had an fs_path {self._fs_path}, which does not"
                    " exist.  This exception can only occur in the testsuite.  Either have the"
                    " test provide content for the path (`virtual_path_def(..., content=...) or,"
                    " if that is too painful in general, have the code accept this error as a "
                    " test only-case and provide a default."
                ) from e

        if byte_io:
            return io.BytesIO(self._content.encode("utf-8"))
        return io.StringIO(self._content)

    def _replaced_path(self, new_fs_path: str) -> None:
        self._fs_path = new_fs_path


class FSROOverlay(VirtualPathBase):
    __slots__ = (
        "_path",
        "_fs_path",
        "_parent",
        "_stat_cache",
        "_readlink_cache",
        "_children",
        "_stat_failed_cache",
        "__weakref__",
    )

    def __init__(
        self,
        path: str,
        fs_path: str,
        parent: Optional["FSROOverlay"],
    ) -> None:
        self._path: str = path
        self._fs_path: str = _normalize_path(fs_path, with_prefix=False)
        self._parent: Optional[ReferenceType[FSROOverlay]] = (
            ref(parent) if parent is not None else None
        )
        self._stat_cache: Optional[os.stat_result] = None
        self._readlink_cache: Optional[str] = None
        self._stat_failed_cache = False
        self._children: Optional[Mapping[str, FSROOverlay]] = None

    @classmethod
    def create_root_dir(cls, path: str, fs_path: str) -> "FSROOverlay":
        return FSROOverlay(path, fs_path, None)

    @property
    def name(self) -> str:
        return os.path.basename(self._path)

    @property
    def iterdir(self) -> Iterable["FSROOverlay"]:
        if not self.is_dir:
            return
        if self._children is None:
            self._ensure_children_are_resolved()
        yield from assume_not_none(self._children).values()

    def lookup(self, path: str) -> Optional["FSROOverlay"]:
        if not self.is_dir:
            return None
        if self._children is None:
            self._ensure_children_are_resolved()

        absolute, _, path_parts = _split_path(path)
        current = cast("FSROOverlay", _root(self)) if absolute else self
        for no, dir_part in enumerate(path_parts):
            if dir_part == ".":
                continue
            if dir_part == "..":
                p = current.parent_dir
                if current is None:
                    raise ValueError(f'The path "{path}" escapes the root dir')
                current = p
                continue
            try:
                current = current[dir_part]
            except KeyError:
                return None
        return current

    def all_paths(self) -> Iterable["FSROOverlay"]:
        yield self
        if not self.is_dir:
            return
        stack = list(self.iterdir)
        stack.reverse()
        while stack:
            current = stack.pop()
            yield current
            if current.is_dir:
                if current._children is None:
                    current._ensure_children_are_resolved()
                stack.extend(reversed(current._children.values()))

    def _ensure_children_are_resolved(self) -> None:
        if not self.is_dir or self._children:
            return
        dir_path = self.path
        dir_fs_path = self.fs_path
        children = {}
        for name in sorted(os.listdir(dir_fs_path), key=os.path.basename):
            child_path = os.path.join(dir_path, name) if dir_path != "." else name
            child_fs_path = (
                os.path.join(dir_fs_path, name) if dir_fs_path != "." else name
            )
            children[name] = FSROOverlay(
                child_path,
                child_fs_path,
                self,
            )
        self._children = children

    @property
    def is_detached(self) -> bool:
        return False

    def __getitem__(self, key) -> "VirtualPath":
        if not self.is_dir:
            raise KeyError(key)
        if self._children is None:
            self._ensure_children_are_resolved()
        if isinstance(key, FSPath):
            key = key.name
        return self._children[key]

    def __delitem__(self, key) -> None:
        self._error_ro_fs()

    @property
    def is_read_write(self) -> bool:
        return False

    def _rw_check(self) -> None:
        self._error_ro_fs()

    def _error_ro_fs(self) -> NoReturn:
        raise DebputyFSIsROError(
            f'Attempt to write to "{self.path}" failed:'
            " Debputy Virtual File system is R/O."
        )

    @property
    def path(self) -> str:
        return self._path

    @property
    def parent_dir(self) -> Optional["FSROOverlay"]:
        parent = self._parent
        if parent is None:
            return None
        resolved = parent()
        if resolved is None:
            raise RuntimeError("Parent was garbage collected!")
        return resolved

    def stat(self) -> os.stat_result:
        if self._stat_failed_cache:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), self.fs_path
            )

        if self._stat_cache is None:
            try:
                self._stat_cache = os.lstat(self.fs_path)
            except FileNotFoundError:
                self._stat_failed_cache = True
                raise
        return self._stat_cache

    @property
    def mode(self) -> int:
        return stat.S_IMODE(self.stat().st_mode)

    @mode.setter
    def mode(self, _unused: int) -> None:
        self._error_ro_fs()

    @property
    def mtime(self) -> float:
        return self.stat().st_mtime

    @mtime.setter
    def mtime(self, new_mtime: float) -> None:
        self._error_ro_fs()

    def readlink(self) -> str:
        if not self.is_symlink:
            raise TypeError(f"readlink is only valid for symlinks ({self.path!r})")
        if self._readlink_cache is None:
            self._readlink_cache = os.readlink(self.fs_path)
        return self._readlink_cache

    @property
    def fs_path(self) -> str:
        return self._fs_path

    @property
    def is_dir(self) -> bool:
        # The root path can have a non-existent fs_path (such as d/tmp not always existing)
        try:
            return stat.S_ISDIR(self.stat().st_mode)
        except FileNotFoundError:
            return False

    @property
    def is_file(self) -> bool:
        # The root path can have a non-existent fs_path (such as d/tmp not always existing)
        try:
            return stat.S_ISREG(self.stat().st_mode)
        except FileNotFoundError:
            return False

    @property
    def is_symlink(self) -> bool:
        # The root path can have a non-existent fs_path (such as d/tmp not always existing)
        try:
            return stat.S_ISLNK(self.stat().st_mode)
        except FileNotFoundError:
            return False

    @property
    def has_fs_path(self) -> bool:
        return True

    def open(
        self,
        *,
        byte_io: bool = False,
        buffering: int = -1,
    ) -> Union[TextIO, BinaryIO]:
        # Allow symlinks for open here, because we can let the OS resolve the symlink reliably in this
        # case.
        if not self.is_file and not self.is_symlink:
            raise TypeError(
                f"Cannot open {self.path} for reading: It is not a file nor a symlink"
            )

        if byte_io:
            return open(self.fs_path, "rb", buffering=buffering)
        return open(self.fs_path, "rt", encoding="utf-8", buffering=buffering)

    def chown(
        self,
        owner: Optional[StaticFileSystemOwner],
        group: Optional[StaticFileSystemGroup],
    ) -> None:
        self._error_ro_fs()

    def mkdir(self, name: str) -> "VirtualPath":
        self._error_ro_fs()

    def add_file(
        self,
        name: str,
        *,
        unlink_if_exists: bool = True,
        use_fs_path_mode: bool = False,
        mode: int = 0o0644,
        mtime: Optional[float] = None,
    ) -> ContextManager["VirtualPath"]:
        self._error_ro_fs()

    def add_symlink(self, link_name: str, link_target: str) -> "VirtualPath":
        self._error_ro_fs()

    def unlink(self, *, recursive: bool = False) -> None:
        self._error_ro_fs()

    def metadata(
        self,
        metadata_type: Type[PMT],
        *,
        owning_plugin: Optional[str] = None,
    ) -> PathMetadataReference[PMT]:
        current_plugin = self._current_plugin()
        if owning_plugin is None:
            owning_plugin = current_plugin
        return AlwaysEmptyReadOnlyMetadataReference(
            owning_plugin,
            current_plugin,
            metadata_type,
        )


class FSROOverlayRootDir(FSROOverlay):
    __slots__ = ("_plugin_context",)

    def __init__(self, path: str, fs_path: str) -> None:
        super().__init__(path, fs_path, None)
        self._plugin_context = CurrentPluginContextManager("debputy")

    def _current_plugin(self) -> str:
        return self._plugin_context.current_plugin_name

    @contextlib.contextmanager
    def change_plugin_context(self, new_plugin: str) -> Iterator[str]:
        with self._plugin_context.change_plugin_context(new_plugin) as r:
            yield r


def as_path_def(pd: Union[str, PathDef]) -> PathDef:
    return PathDef(pd) if isinstance(pd, str) else pd


def as_path_defs(paths: Iterable[Union[str, PathDef]]) -> Iterable[PathDef]:
    yield from (as_path_def(p) for p in paths)


def build_virtual_fs(
    paths: Iterable[Union[str, PathDef]],
    read_write_fs: bool = False,
) -> "FSPath":
    root_dir: Optional[FSRootDir] = None
    directories: Dict[str, FSPath] = {}
    non_directories = set()

    def _ensure_parent_dirs(p: str) -> None:
        current = p.rstrip("/")
        missing_dirs = []
        while True:
            current = os.path.dirname(current)
            if current in directories:
                break
            if current in non_directories:
                raise ValueError(
                    f'Conflicting definition for "{current}".  The path "{p}" wants it as a directory,'
                    ' but it is defined as a non-directory.  (Ensure dirs end with "/")'
                )
            missing_dirs.append(current)
        for dir_path in reversed(missing_dirs):
            parent_dir = directories[os.path.dirname(dir_path)]
            d = VirtualTestPath(os.path.basename(dir_path), parent_dir, is_dir=True)
            directories[dir_path] = d

    for path_def in as_path_defs(paths):
        path = path_def.path_name
        if path in directories or path in non_directories:
            raise ValueError(
                f'Duplicate definition of "{path}".  Can be false positive if input is not in'
                ' "correct order" (ensure directories occur before their children)'
            )
        if root_dir is None:
            root_fs_path = None
            if path in (".", "./", "/"):
                root_fs_path = path_def.fs_path
            root_dir = FSRootDir(fs_path=root_fs_path)
            directories["."] = root_dir

        if path not in (".", "./", "/") and not path.startswith("./"):
            path = "./" + path
        if path not in (".", "./", "/"):
            _ensure_parent_dirs(path)
        if path in (".", "./"):
            assert "." in directories
            continue
        is_dir = False
        if path.endswith("/"):
            path = path[:-1]
            is_dir = True
        directory = directories[os.path.dirname(path)]
        assert not is_dir or not bool(
            path_def.link_target
        ), f"is_dir={is_dir} vs. link_target={path_def.link_target}"
        fs_path = VirtualTestPath(
            os.path.basename(path),
            directory,
            is_dir=is_dir,
            mode=path_def.mode,
            mtime=path_def.mtime,
            has_fs_path=path_def.has_fs_path,
            fs_path=path_def.fs_path,
            link_target=path_def.link_target,
            content=path_def.content,
            materialized_content=path_def.materialized_content,
        )
        assert not fs_path.is_detached
        if fs_path.is_dir:
            directories[fs_path.path] = fs_path
        else:
            non_directories.add(fs_path.path)

    if root_dir is None:
        root_dir = FSRootDir()

    root_dir.is_read_write = read_write_fs
    return root_dir
