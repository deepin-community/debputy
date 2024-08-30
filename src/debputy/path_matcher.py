import fnmatch
import glob
import itertools
import os
import re
from enum import Enum
from typing import (
    Callable,
    Optional,
    TypeVar,
    Iterable,
    Union,
    Sequence,
    Tuple,
)

from debputy.intermediate_manifest import PathType
from debputy.plugin.api import VirtualPath
from debputy.substitution import Substitution, NULL_SUBSTITUTION
from debputy.types import VP
from debputy.util import _normalize_path, _error, escape_shell

MR = TypeVar("MR")
_GLOB_PARTS = re.compile(r"[*?]|\[]?[^]]+]")


def _lookup_path(fs_root: VP, path: str) -> Optional[VP]:
    if not path.startswith("./"):
        raise ValueError("Directory must be normalized (and not the root directory)")
    if fs_root.name != "." or fs_root.parent_dir is not None:
        raise ValueError("Provided fs_root must be the root directory")
    # TODO: Strictly speaking, this is unsound. (E.g., FSRootDir does not return FSRootDir on a lookup)
    return fs_root.lookup(path[2:])


def _compile_basename_glob(
    basename_glob: str,
) -> Tuple[Optional[str], Callable[[str], bool]]:
    remainder = None
    if not glob.has_magic(basename_glob):
        return escape_shell(basename_glob), lambda x: x == basename_glob

    if basename_glob.startswith("*"):
        if basename_glob.endswith("*"):
            remainder = basename_glob[1:-1]
            possible_quick_match = lambda x: remainder in x
            escaped_pattern = "*" + escape_shell(remainder) + "*"
        else:
            remainder = basename_glob[1:]
            possible_quick_match = lambda x: x.endswith(remainder)
            escaped_pattern = "*" + escape_shell(remainder)
    else:
        remainder = basename_glob[:-1]
        possible_quick_match = lambda x: x.startswith(remainder)
        escaped_pattern = escape_shell(remainder) + "*"

    if not glob.has_magic(remainder):
        return escaped_pattern, possible_quick_match
    slow_pattern = re.compile(fnmatch.translate(basename_glob))
    return None, lambda x: bool(slow_pattern.match(x))


def _apply_match(
    fs_path: VP,
    match_part: Union[Callable[[str], bool], str],
) -> Iterable[VP]:
    if isinstance(match_part, str):
        m = fs_path.lookup(match_part)
        if m:
            yield m
    else:
        yield from (p for p in fs_path.iterdir if match_part(p.name))


class MatchRuleType(Enum):
    EXACT_MATCH = "exact"
    BASENAME_GLOB = "basename-glob"
    DIRECT_CHILDREN_OF_DIR = "direct-children-of-dir"
    ANYTHING_BENEATH_DIR = "anything-beneath-dir"
    GENERIC_GLOB = "generic-glob"
    MATCH_ANYTHING = "match-anything"


class MatchRule:
    __slots__ = ("_rule_type",)

    def __init__(self, rule_type: MatchRuleType) -> None:
        self._rule_type = rule_type

    @property
    def rule_type(self) -> MatchRuleType:
        return self._rule_type

    def finditer(
        self,
        fs_root: VP,
        *,
        ignore_paths: Optional[Callable[[VP], bool]] = None,
    ) -> Iterable[VP]:
        # TODO: Strictly speaking, this is unsound. (E.g., FSRootDir does not return FSRootDir on a lookup)
        raise NotImplementedError

    def _full_pattern(self) -> str:
        raise NotImplementedError

    @property
    def path_type(self) -> Optional[PathType]:
        return None

    def describe_match_short(self) -> str:
        return self._full_pattern()

    def describe_match_exact(self) -> str:
        raise NotImplementedError

    def shell_escape_pattern(self) -> str:
        raise TypeError("Pattern not suitable or not supported for shell escape")

    @classmethod
    def recursive_beneath_directory(
        cls,
        directory: str,
        definition_source: str,
        path_type: Optional[PathType] = None,
        substitution: Substitution = NULL_SUBSTITUTION,
    ) -> "MatchRule":
        if directory in (".", "/"):
            return MATCH_ANYTHING
        assert not glob.has_magic(directory)
        return DirectoryBasedMatch(
            MatchRuleType.ANYTHING_BENEATH_DIR,
            substitution.substitute(_normalize_path(directory), definition_source),
            path_type=path_type,
        )

    @classmethod
    def from_path_or_glob(
        cls,
        path_or_glob: str,
        definition_source: str,
        path_type: Optional[PathType] = None,
        substitution: Substitution = NULL_SUBSTITUTION,
    ) -> "MatchRule":
        # TODO: Handle '{a,b,c}' patterns too
        # FIXME: Better error handling!
        normalized_no_prefix = _normalize_path(path_or_glob, with_prefix=False)
        if path_or_glob in ("*", "**/*", ".", "/"):
            assert path_type is None
            return MATCH_ANYTHING

        # We do not support {a,b} at the moment. This check is not perfect, but it should catch the most obvious
        # unsupported usage.
        if (
            "{" in path_or_glob
            and ("," in path_or_glob or ".." in path_or_glob)
            and re.search(r"[{][^},.]*(?:,|[.][.])[^},.]*[}]", path_or_glob)
        ):
            m = re.search(r"(.*)[{]([^},.]*(?:,|[.][.])[^},.]*[}])", path_or_glob)
            assert m is not None
            replacement = m.group(1) + "{{OPEN_CURLY_BRACE}}" + m.group(2)
            _error(
                f'The pattern "{path_or_glob}" (defined in {definition_source}) looks like it contains a'
                f' brace expansion (such as "{{a,b}}" or "{{a..b}}").  Brace expansions are not supported.'
                " If you wanted to match the literal path with a brace in it, please use a substitution to insert"
                f' the opening brace.  As an example: "{replacement}"'
            )

        normalized_with_prefix = "./" + normalized_no_prefix
        # TODO: Check for escapes here  "foo[?]/bar" can be written as an exact match for foo?/bar
        # - similar holds for "foo[?]/*" being a directory match (etc.).
        if not glob.has_magic(normalized_with_prefix):
            assert path_type is None
            return ExactFileSystemPath(
                substitution.substitute(normalized_with_prefix, definition_source)
            )

        directory = os.path.dirname(normalized_with_prefix)
        basename = os.path.basename(normalized_with_prefix)

        if ("**" in directory and directory != "./**") or "**" in basename:
            raise ValueError(
                f'Cannot process pattern "{path_or_glob}" from {definition_source}: The double-star'
                ' glob ("**") is not supported in general.  Only "**/<basename-glob>" supported.'
            )

        if basename == "*" and not glob.has_magic(directory):
            return DirectoryBasedMatch(
                MatchRuleType.DIRECT_CHILDREN_OF_DIR,
                substitution.substitute(directory, definition_source),
                path_type=path_type,
            )
        elif directory == "./**" or not glob.has_magic(directory):
            basename_glob = substitution.substitute(
                basename, definition_source, escape_glob_characters=True
            )
            if directory in (".", "./**"):
                return BasenameGlobMatch(
                    basename_glob,
                    path_type=path_type,
                    recursive_match=True,
                )
            return BasenameGlobMatch(
                basename_glob,
                only_when_in_directory=substitution.substitute(
                    directory, definition_source
                ),
                path_type=path_type,
                recursive_match=False,
            )

        return GenericGlobImplementation(normalized_with_prefix, path_type=path_type)


def _match_file_type(path_type: PathType, path: VirtualPath) -> bool:
    if path_type == PathType.FILE and path.is_file:
        return True
    if path_type == PathType.DIRECTORY and path.is_dir:
        return True
    if path_type == PathType.SYMLINK and path.is_symlink:
        return True
    assert path_type in (PathType.FILE, PathType.DIRECTORY, PathType.SYMLINK)
    return False


class MatchAnything(MatchRule):
    def __init__(self) -> None:
        super().__init__(MatchRuleType.MATCH_ANYTHING)

    def _full_pattern(self) -> str:
        return "**/*"

    def finditer(
        self, fs_root: VP, *, ignore_paths: Optional[Callable[[VP], bool]] = None
    ) -> Iterable[VP]:
        if ignore_paths is not None:
            yield from (p for p in fs_root.all_paths() if not ignore_paths(p))
        yield from fs_root.all_paths()

    def describe_match_exact(self) -> str:
        return "**/* (Match anything)"


MATCH_ANYTHING: MatchRule = MatchAnything()

del MatchAnything


class ExactFileSystemPath(MatchRule):
    __slots__ = "_path"

    def __init__(self, path: str) -> None:
        super().__init__(MatchRuleType.EXACT_MATCH)
        self._path = path

    def _full_pattern(self) -> str:
        return self._path

    def finditer(
        self, fs_root: VP, *, ignore_paths: Optional[Callable[[VP], bool]] = None
    ) -> Iterable[VP]:
        p = _lookup_path(fs_root, self._path)
        if p is not None and (ignore_paths is None or not ignore_paths(p)):
            yield p

    def describe_match_exact(self) -> str:
        return f"{self._path} (the exact path / no globbing)"

    @property
    def path(self) -> str:
        return self._path

    def shell_escape_pattern(self) -> str:
        return escape_shell(self._path.lstrip("."))


class DirectoryBasedMatch(MatchRule):
    __slots__ = "_directory", "_path_type"

    def __init__(
        self,
        rule_type: MatchRuleType,
        directory: str,
        path_type: Optional[PathType] = None,
    ) -> None:
        super().__init__(rule_type)
        self._directory = directory
        self._path_type = path_type
        assert rule_type in (
            MatchRuleType.DIRECT_CHILDREN_OF_DIR,
            MatchRuleType.ANYTHING_BENEATH_DIR,
        )
        assert not self._directory.endswith("/")

    def _full_pattern(self) -> str:
        return self._directory

    def finditer(
        self,
        fs_root: VP,
        *,
        ignore_paths: Optional[Callable[[VP], bool]] = None,
    ) -> Iterable[VP]:
        p = _lookup_path(fs_root, self._directory)
        if p is None or not p.is_dir:
            return
        if self._rule_type == MatchRuleType.ANYTHING_BENEATH_DIR:
            path_iter = p.all_paths()
        else:
            path_iter = p.iterdir
        if ignore_paths is not None:
            path_iter = (p for p in path_iter if not ignore_paths(p))
        if self._path_type is None:
            yield from path_iter
        else:
            yield from (m for m in path_iter if _match_file_type(self._path_type, m))

    def describe_match_short(self) -> str:
        path_type_match = (
            ""
            if self._path_type is None
            else f" <only for path type {self._path_type.manifest_key}>"
        )
        if self._rule_type == MatchRuleType.ANYTHING_BENEATH_DIR:
            return f"{self._directory}/**/*{path_type_match}"
        return f"{self._directory}/*{path_type_match}"

    def describe_match_exact(self) -> str:
        if self._rule_type == MatchRuleType.ANYTHING_BENEATH_DIR:
            return f"{self._directory}/**/* (anything below the directory)"
        return f"{self.describe_match_short()} (anything directly in the directory)"

    @property
    def path_type(self) -> Optional[PathType]:
        return self._path_type

    @property
    def directory(self) -> str:
        return self._directory

    def shell_escape_pattern(self) -> str:
        if self._rule_type == MatchRuleType.ANYTHING_BENEATH_DIR:
            return super().shell_escape_pattern()
        return escape_shell(self._directory.lstrip(".")) + "/*"


class BasenameGlobMatch(MatchRule):
    __slots__ = (
        "_basename_glob",
        "_directory",
        "_matcher",
        "_path_type",
        "_recursive_match",
        "_escaped_basename_pattern",
    )

    def __init__(
        self,
        basename_glob: str,
        only_when_in_directory: Optional[str] = None,
        path_type: Optional[PathType] = None,
        recursive_match: Optional[bool] = None,  # TODO: Can this just be = False (?)
    ) -> None:
        super().__init__(MatchRuleType.BASENAME_GLOB)
        self._basename_glob = basename_glob
        self._directory = only_when_in_directory
        self._path_type = path_type
        self._recursive_match = recursive_match
        if self._directory is None and not recursive_match:
            self._recursive_match = True
        assert self._directory is None or not self._directory.endswith("/")
        assert "/" not in basename_glob  # Not a basename if it contains /
        assert "**" not in basename_glob  # Also not a (true) basename if it has **
        self._escaped_basename_pattern, self._matcher = _compile_basename_glob(
            basename_glob
        )

    def _full_pattern(self) -> str:
        if self._directory is not None:
            maybe_recursive = "**/" if self._recursive_match else ""
            return f"{self._directory}/{maybe_recursive}{self._basename_glob}"
        return self._basename_glob

    def finditer(
        self,
        fs_root: VP,
        *,
        ignore_paths: Optional[Callable[[VP], bool]] = None,
    ) -> Iterable[VP]:
        search_root = fs_root
        if self._directory is not None:
            p = _lookup_path(fs_root, self._directory)
            if p is None or not p.is_dir:
                return
            search_root = p
        path_iter = (
            search_root.all_paths() if self._recursive_match else search_root.iterdir
        )
        if ignore_paths is not None:
            path_iter = (p for p in path_iter if not ignore_paths(p))
        if self._path_type is None:
            yield from (m for m in path_iter if self._matcher(m.name))
        else:
            yield from (
                m
                for m in path_iter
                if self._matcher(m.name) and _match_file_type(self._path_type, m)
            )

    def describe_match_short(self) -> str:
        path_type_match = (
            ""
            if self._path_type is None
            else f" <only for path type {self._path_type.manifest_key}>"
        )
        return (
            self._full_pattern()
            if path_type_match == ""
            else f"{self._full_pattern()}{path_type_match}"
        )

    def describe_match_exact(self) -> str:
        if self._directory is not None:
            return f"{self.describe_match_short()} (glob / directly in the directory)"
        return f"{self.describe_match_short()} (basename match)"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BasenameGlobMatch):
            return NotImplemented
        return (
            self._basename_glob == other._basename_glob
            and self._directory == other._directory
            and self._path_type == other._path_type
            and self._recursive_match == other._recursive_match
        )

    @property
    def path_type(self) -> Optional[PathType]:
        return self._path_type

    @property
    def directory(self) -> Optional[str]:
        return self._directory

    def shell_escape_pattern(self) -> str:
        if self._directory is None or self._escaped_basename_pattern is None:
            return super().shell_escape_pattern()
        return (
            escape_shell(self._directory.lstrip("."))
            + f"/{self._escaped_basename_pattern}"
        )


class GenericGlobImplementation(MatchRule):
    __slots__ = "_glob_pattern", "_path_type", "_match_parts"

    def __init__(
        self,
        glob_pattern: str,
        path_type: Optional[PathType] = None,
    ) -> None:
        super().__init__(MatchRuleType.GENERIC_GLOB)
        if glob_pattern.startswith("./"):
            glob_pattern = glob_pattern[2:]
        self._glob_pattern = glob_pattern
        self._path_type = path_type
        assert "**" not in glob_pattern  # No recursive globs
        assert glob.has_magic(
            glob_pattern
        )  # If it has no glob, then it could have been an exact match
        assert (
            "/" in glob_pattern
        )  # If it does not have a / then a BasenameGlob could have been used instead
        self._match_parts = self._compile_glob()

    def _full_pattern(self) -> str:
        return self._glob_pattern

    def finditer(
        self,
        fs_root: VP,
        *,
        ignore_paths: Optional[Callable[[VP], bool]] = None,
    ) -> Iterable[VP]:
        search_history = [fs_root]
        for part in self._match_parts:
            next_layer = itertools.chain.from_iterable(
                _apply_match(m, part) for m in search_history
            )
            # TODO: Figure out why we need to materialize next_layer into a list for this to work.
            search_history = list(next_layer)
            if not search_history:
                # While we have it as a list, we might as well have an "early exit".
                return

        if self._path_type is None:
            if ignore_paths is None:
                yield from search_history
            else:
                yield from (p for p in search_history if not ignore_paths(p))
        elif ignore_paths is None:
            yield from (
                m for m in search_history if _match_file_type(self._path_type, m)
            )
        else:
            yield from (
                m
                for m in search_history
                if _match_file_type(self._path_type, m) and not ignore_paths(m)
            )

    def describe_match_short(self) -> str:
        path_type_match = (
            ""
            if self._path_type is None
            else f" <only for path type {self._path_type.manifest_key}>"
        )
        return (
            self._full_pattern()
            if path_type_match == ""
            else f"{self._full_pattern()}{path_type_match}"
        )

    def describe_match_exact(self) -> str:
        return f"{self.describe_match_short()} (glob)"

    def _compile_glob(self) -> Sequence[Union[Callable[[str], bool], str]]:
        assert self._glob_pattern.strip("/") == self._glob_pattern
        return [
            _compile_basename_glob(part) if glob.has_magic(part) else part
            for part in self._glob_pattern.split("/")
        ]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GenericGlobImplementation):
            return NotImplemented
        return (
            self._glob_pattern == other._glob_pattern
            and self._path_type == other._path_type
        )

    @property
    def path_type(self) -> Optional[PathType]:
        return self._path_type
