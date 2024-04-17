import re
from typing import Iterable, Tuple, Optional

from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.exceptions import PureVirtualPathError, TestPathWithNonExistentFSPathError
from debputy.intermediate_manifest import PathType
from debputy.manifest_parser.base_types import SymbolicMode, OctalMode, FileSystemMode
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.path_matcher import (
    MATCH_ANYTHING,
    MatchRule,
    ExactFileSystemPath,
    DirectoryBasedMatch,
    MatchRuleType,
    BasenameGlobMatch,
)
from debputy.substitution import Substitution
from debputy.types import VP
from debputy.util import _normalize_path, perl_module_dirs

# Imported from dh_fixperms
_PERMISSION_NORMALIZATION_SOURCE_DEFINITION = "permission normalization"
attribute_path = AttributePath.builtin_path()[
    _PERMISSION_NORMALIZATION_SOURCE_DEFINITION
]
_STD_FILE_MODE = OctalMode(0o644)
_PATH_FILE_MODE = OctalMode(0o755)
_HAS_BIN_SHBANG_RE = re.compile(rb"^#!\s*/(?:usr/)?s?bin", re.ASCII)


class _UsrShareDocMatchRule(DirectoryBasedMatch):
    def __init__(self) -> None:
        super().__init__(
            MatchRuleType.ANYTHING_BENEATH_DIR,
            _normalize_path("usr/share/doc", with_prefix=True),
            path_type=PathType.FILE,
        )

    def finditer(self, fs_root: VP, *, ignore_paths=None) -> Iterable[VP]:
        doc_dir = fs_root.lookup(self._directory)
        if doc_dir is None:
            return
        for path_in_doc_dir in doc_dir.iterdir:
            if ignore_paths is not None and ignore_paths(path_in_doc_dir):
                continue
            if path_in_doc_dir.is_file:
                yield path_in_doc_dir
            for subpath in path_in_doc_dir.iterdir:
                if subpath.name == "examples" and subpath.is_dir:
                    continue
                if ignore_paths is not None:
                    yield from (
                        f
                        for f in subpath.all_paths()
                        if f.is_file and not ignore_paths(f)
                    )
                else:
                    yield from (f for f in subpath.all_paths() if f.is_file)

    def describe_match_short(self) -> str:
        return f"All files beneath {self._directory}/ except .../<pkg>/examples"

    def describe_match_exact(self) -> str:
        return self.describe_match_short()


class _ShebangScriptFiles(MatchRule):
    def __init__(self) -> None:
        super().__init__(MatchRuleType.GENERIC_GLOB)

    def finditer(self, fs_root: VP, *, ignore_paths=None) -> Iterable[VP]:
        for p in fs_root.all_paths():
            if not p.is_file or (ignore_paths and ignore_paths(p)):
                continue
            try:
                with p.open(byte_io=True) as fd:
                    c = fd.read(32)
            except (PureVirtualPathError, TestPathWithNonExistentFSPathError):
                continue
            if _HAS_BIN_SHBANG_RE.match(c):
                yield p

    @property
    def path_type(self) -> Optional[PathType]:
        return PathType.FILE

    def _full_pattern(self) -> str:
        return "built-in - not a valid pattern"

    def describe_match_short(self) -> str:
        return "All scripts with a absolute #!-line for /(s)bin or /usr/(s)bin"

    def describe_match_exact(self) -> str:
        return self.describe_match_short()


USR_SHARE_DOC_MATCH_RULE = _UsrShareDocMatchRule()
SHEBANG_SCRIPTS = _ShebangScriptFiles()
del _UsrShareDocMatchRule
del _ShebangScriptFiles


def builtin_mode_normalization_rules(
    dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
    dctrl_bin: BinaryPackage,
    substitution: Substitution,
) -> Iterable[Tuple[MatchRule, FileSystemMode]]:
    yield from (
        (
            MatchRule.from_path_or_glob(
                x,
                _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
                path_type=PathType.FILE,
            ),
            _STD_FILE_MODE,
        )
        for x in (
            "*.so.*",
            "*.so",
            "*.la",
            "*.a",
            "*.js",
            "*.css",
            "*.scss",
            "*.sass",
            "*.jpeg",
            "*.jpg",
            "*.png",
            "*.gif",
            "*.cmxs",
            "*.node",
        )
    )

    yield from (
        (
            MatchRule.recursive_beneath_directory(
                x,
                _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
                path_type=PathType.FILE,
            ),
            _STD_FILE_MODE,
        )
        for x in (
            "usr/share/man",
            "usr/include",
            "usr/share/applications",
            "usr/share/lintian/overrides",
        )
    )

    # The dh_fixperms tool recuses for these directories, but probably should not (see #1006927)
    yield from (
        (
            MatchRule.from_path_or_glob(
                f"{x}/*",
                _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
                path_type=PathType.FILE,
            ),
            _PATH_FILE_MODE,
        )
        for x in (
            "usr/bin",
            "usr/bin/mh",
            "bin",
            "usr/sbin",
            "sbin",
            "usr/games",
            "usr/libexec",
            "etc/init.d",
        )
    )

    yield (
        # Strictly speaking, dh_fixperms does a recursive search but in practice, it does not matter.
        MatchRule.from_path_or_glob(
            "etc/sudoers.d/*",
            _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
            path_type=PathType.FILE,
        ),
        OctalMode(0o440),
    )

    # The reportbug rule
    yield (
        ExactFileSystemPath(
            substitution.substitute(
                _normalize_path("usr/share/bug/{{PACKAGE}}"),
                _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
            )
        ),
        OctalMode(0o755),
    )

    yield (
        MatchRule.recursive_beneath_directory(
            "usr/share/bug/{{PACKAGE}}",
            _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
            path_type=PathType.FILE,
            substitution=substitution,
        ),
        OctalMode(0o644),
    )

    yield (
        ExactFileSystemPath(
            substitution.substitute(
                _normalize_path("usr/share/bug/{{PACKAGE}}/script"),
                _PERMISSION_NORMALIZATION_SOURCE_DEFINITION,
            )
        ),
        OctalMode(0o755),
    )

    yield (
        USR_SHARE_DOC_MATCH_RULE,
        OctalMode(0o0644),
    )

    yield from (
        (
            BasenameGlobMatch(
                "*.pm",
                only_when_in_directory=perl_dir,
                path_type=PathType.FILE,
                recursive_match=True,
            ),
            SymbolicMode.parse_filesystem_mode(
                "a-x",
                attribute_path['"*.pm'],
            ),
        )
        for perl_dir in perl_module_dirs(dpkg_architecture_variables, dctrl_bin)
    )

    yield (
        BasenameGlobMatch(
            "*.ali",
            only_when_in_directory=_normalize_path("usr/lib"),
            path_type=PathType.FILE,
            recursive_match=True,
        ),
        SymbolicMode.parse_filesystem_mode(
            "a-w",
            attribute_path['"*.ali"'],
        ),
    )

    yield (
        SHEBANG_SCRIPTS,
        _PATH_FILE_MODE,
    )

    yield (
        MATCH_ANYTHING,
        SymbolicMode.parse_filesystem_mode(
            "go=rX,u+rw,a-s",
            attribute_path["**/*"],
        ),
    )
