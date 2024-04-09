import collections
import dataclasses
import os.path
import re
from enum import IntEnum
from typing import (
    List,
    Dict,
    FrozenSet,
    Callable,
    Union,
    Iterator,
    Tuple,
    Set,
    Sequence,
    Optional,
    Iterable,
    TYPE_CHECKING,
    cast,
    Any,
    Mapping,
)

from debputy.exceptions import DebputyRuntimeError
from debputy.filesystem_scan import FSPath
from debputy.manifest_conditions import (
    ConditionContext,
    ManifestCondition,
    _BUILD_DOCS_BDO,
)
from debputy.manifest_parser.base_types import (
    FileSystemMatchRule,
    FileSystemExactMatchRule,
    DebputyDispatchableType,
)
from debputy.packages import BinaryPackage
from debputy.path_matcher import MatchRule, ExactFileSystemPath, MATCH_ANYTHING
from debputy.substitution import Substitution
from debputy.util import _error, _warn

if TYPE_CHECKING:
    from debputy.packager_provided_files import PackagerProvidedFile
    from debputy.plugin.api import VirtualPath
    from debputy.plugin.api.impl_types import PluginProvidedDiscardRule


_MAN_TH_LINE = re.compile(r'^[.]TH\s+\S+\s+"?(\d+[^"\s]*)"?')
_MAN_DT_LINE = re.compile(r"^[.]Dt\s+\S+\s+(\d+\S*)")
_MAN_SECTION_BASENAME = re.compile(r"[.]([1-9]\w*)(?:[.]gz)?$")
_MAN_REAL_SECTION = re.compile(r"^(\d+)")
_MAN_INST_BASENAME = re.compile(r"[.][^.]+$")
MAN_GUESS_LANG_FROM_PATH = re.compile(
    r"(?:^|/)man/(?:([a-z][a-z](?:_[A-Z][A-Z])?)(?:\.[^/]+)?)?/man[1-9]/"
)
MAN_GUESS_FROM_BASENAME = re.compile(r"[.]([a-z][a-z](?:_[A-Z][A-Z])?)[.](?:[1-9]|man)")


class InstallRuleError(DebputyRuntimeError):
    pass


class PathAlreadyInstalledOrDiscardedError(InstallRuleError):
    @property
    def path(self) -> str:
        return cast("str", self.args[0])

    @property
    def into(self) -> FrozenSet[BinaryPackage]:
        return cast("FrozenSet[BinaryPackage]", self.args[1])

    @property
    def definition_source(self) -> str:
        return cast("str", self.args[2])


class ExactPathMatchTwiceError(InstallRuleError):
    @property
    def path(self) -> str:
        return cast("str", self.args[1])

    @property
    def into(self) -> BinaryPackage:
        return cast("BinaryPackage", self.args[2])

    @property
    def definition_source(self) -> str:
        return cast("str", self.args[3])


class NoMatchForInstallPatternError(InstallRuleError):
    @property
    def pattern(self) -> str:
        return cast("str", self.args[1])

    @property
    def search_dirs(self) -> Sequence["SearchDir"]:
        return cast("Sequence[SearchDir]", self.args[2])

    @property
    def definition_source(self) -> str:
        return cast("str", self.args[3])


@dataclasses.dataclass(slots=True, frozen=True)
class SearchDir:
    search_dir: "VirtualPath"
    applies_to: FrozenSet[BinaryPackage]


@dataclasses.dataclass(slots=True, frozen=True)
class BinaryPackageInstallRuleContext:
    binary_package: BinaryPackage
    fs_root: FSPath
    doc_main_package: BinaryPackage

    def replace(self, **changes: Any) -> "BinaryPackageInstallRuleContext":
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(slots=True, frozen=True)
class InstallSearchDirContext:
    search_dirs: Sequence[SearchDir]
    check_for_uninstalled_dirs: Sequence["VirtualPath"]
    # TODO: Support search dirs per-package
    debian_pkg_dirs: Mapping[str, "VirtualPath"] = dataclasses.field(
        default_factory=dict
    )


@dataclasses.dataclass(slots=True)
class InstallRuleContext:
    # TODO: Search dirs should be per-package
    search_dirs: Sequence[SearchDir]
    binary_package_contexts: Dict[str, BinaryPackageInstallRuleContext] = (
        dataclasses.field(default_factory=dict)
    )

    def __getitem__(self, item: str) -> BinaryPackageInstallRuleContext:
        return self.binary_package_contexts[item]

    def __setitem__(self, key: str, value: BinaryPackageInstallRuleContext) -> None:
        self.binary_package_contexts[key] = value

    def replace(self, **changes: Any) -> "InstallRuleContext":
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(slots=True, frozen=True)
class PathMatch:
    path: "VirtualPath"
    search_dir: "VirtualPath"
    is_exact_match: bool
    into: FrozenSet[BinaryPackage]


class DiscardState(IntEnum):
    UNCHECKED = 0
    NOT_DISCARDED = 1
    DISCARDED_BY_PLUGIN_PROVIDED_RULE = 2
    DISCARDED_BY_MANIFEST_RULE = 3


def _determine_manpage_section(
    match_rule: PathMatch,
    provided_section: Optional[int],
    definition_source: str,
) -> Optional[str]:
    section = str(provided_section) if provided_section is not None else None
    if section is None:
        detected_section = None
        with open(match_rule.path.fs_path, "r") as fd:
            for line in fd:
                if not line.startswith((".TH", ".Dt")):
                    continue

                m = _MAN_DT_LINE.match(line)
                if not m:
                    m = _MAN_TH_LINE.match(line)
                    if not m:
                        continue
                detected_section = m.group(1)
                if "." in detected_section:
                    _warn(
                        f"Ignoring detected section {detected_section} in {match_rule.path.fs_path}"
                        f" (detected via {definition_source}): It looks too much like a version"
                    )
                    detected_section = None
                break
        if detected_section is None:
            m = _MAN_SECTION_BASENAME.search(os.path.basename(match_rule.path.path))
            if m:
                detected_section = m.group(1)
        section = detected_section

    return section


def _determine_manpage_real_section(
    match_rule: PathMatch,
    section: Optional[str],
    definition_source: str,
) -> int:
    real_section = None
    if section is not None:
        m = _MAN_REAL_SECTION.match(section)
        if m:
            real_section = int(m.group(1))
    if real_section is None or real_section < 0 or real_section > 9:
        if real_section is not None:
            _warn(
                f"Computed section for {match_rule.path.fs_path} was {real_section} (section: {section}),"
                f" which is not a valid section (must be between 1 and 9 incl.)"
            )
        _error(
            f"Could not determine the section for {match_rule.path.fs_path} automatically.  The man page"
            f" was detected via {definition_source}. Consider using `section: <number>` to"
            " explicitly declare the section. Keep in mind that it applies to all man pages for that"
            " rule and you may have to split the rule into two for this reason."
        )
    return real_section


def _determine_manpage_language(
    match_rule: PathMatch,
    provided_language: Optional[str],
) -> Optional[str]:
    if provided_language is not None:
        if provided_language not in ("derive-from-basename", "derive-from-path"):
            return provided_language if provided_language != "C" else None
        if provided_language == "derive-from-basename":
            m = MAN_GUESS_FROM_BASENAME.search(match_rule.path.name)
            if m is None:
                return None
            return m.group(1)
        # Fall-through for derive-from-path case
    m = MAN_GUESS_LANG_FROM_PATH.search(match_rule.path.path)
    if m is None:
        return None
    return m.group(1)


def _dest_path_for_manpage(
    provided_section: Optional[int],
    provided_language: Optional[str],
    definition_source: str,
) -> Callable[["PathMatch"], str]:
    def _manpage_dest_path(match_rule: PathMatch) -> str:
        inst_basename = _MAN_INST_BASENAME.sub("", match_rule.path.name)
        section = _determine_manpage_section(
            match_rule, provided_section, definition_source
        )
        real_section = _determine_manpage_real_section(
            match_rule, section, definition_source
        )
        assert section is not None
        language = _determine_manpage_language(match_rule, provided_language)
        if language is None:
            maybe_language = ""
        else:
            maybe_language = f"{language}/"
            lang_suffix = f".{language}"
            if inst_basename.endswith(lang_suffix):
                inst_basename = inst_basename[: -len(lang_suffix)]

        return (
            f"usr/share/man/{maybe_language}man{real_section}/{inst_basename}.{section}"
        )

    return _manpage_dest_path


class SourcePathMatcher:
    def __init__(self, auto_discard_rules: List["PluginProvidedDiscardRule"]) -> None:
        self._already_matched: Dict[
            str,
            Tuple[FrozenSet[BinaryPackage], str],
        ] = {}
        self._exact_match_request: Set[Tuple[str, str]] = set()
        self._discarded: Dict[str, DiscardState] = {}
        self._auto_discard_rules = auto_discard_rules
        self.used_auto_discard_rules: Dict[str, Set[str]] = collections.defaultdict(set)

    def is_reserved(self, path: "VirtualPath") -> bool:
        fs_path = path.fs_path
        if fs_path in self._already_matched:
            return True
        result = self._discarded.get(fs_path, DiscardState.UNCHECKED)
        if result == DiscardState.UNCHECKED:
            result = self._check_plugin_provided_exclude_state_for(path)
        if result == DiscardState.NOT_DISCARDED:
            return False

        return True

    def exclude(self, path: str) -> None:
        self._discarded[path] = DiscardState.DISCARDED_BY_MANIFEST_RULE

    def _run_plugin_provided_discard_rules_on(self, path: "VirtualPath") -> bool:
        for dr in self._auto_discard_rules:
            verdict = dr.should_discard(path)
            if verdict:
                self.used_auto_discard_rules[dr.name].add(path.fs_path)
                return True
        return False

    def _check_plugin_provided_exclude_state_for(
        self,
        path: "VirtualPath",
    ) -> DiscardState:
        cache_misses = []
        current_path = path
        while True:
            fs_path = current_path.fs_path
            exclude_state = self._discarded.get(fs_path, DiscardState.UNCHECKED)
            if exclude_state != DiscardState.UNCHECKED:
                verdict = exclude_state
                break
            cache_misses.append(fs_path)
            if self._run_plugin_provided_discard_rules_on(current_path):
                verdict = DiscardState.DISCARDED_BY_PLUGIN_PROVIDED_RULE
                break
            # We cannot trust a "NOT_DISCARDED" until we check its parent (the directory could
            # be excluded without the files in it triggering the rule).
            parent_dir = current_path.parent_dir
            if not parent_dir:
                verdict = DiscardState.NOT_DISCARDED
                break
            current_path = parent_dir
        if cache_misses:
            for p in cache_misses:
                self._discarded[p] = verdict
        return verdict

    def may_match(
        self,
        match: PathMatch,
        *,
        is_exact_match: bool = False,
    ) -> Tuple[FrozenSet[BinaryPackage], bool]:
        m = self._already_matched.get(match.path.fs_path)
        if m:
            return m[0], False
        current_path = match.path.fs_path
        discard_state = self._discarded.get(current_path, DiscardState.UNCHECKED)

        if discard_state == DiscardState.UNCHECKED:
            discard_state = self._check_plugin_provided_exclude_state_for(match.path)

        assert discard_state is not None and discard_state != DiscardState.UNCHECKED

        is_discarded = discard_state != DiscardState.NOT_DISCARDED
        if (
            is_exact_match
            and discard_state == DiscardState.DISCARDED_BY_PLUGIN_PROVIDED_RULE
        ):
            is_discarded = False
        return frozenset(), is_discarded

    def reserve(
        self,
        path: "VirtualPath",
        reserved_by: FrozenSet[BinaryPackage],
        definition_source: str,
        *,
        is_exact_match: bool = False,
    ) -> None:
        fs_path = path.fs_path
        self._already_matched[fs_path] = reserved_by, definition_source
        if not is_exact_match:
            return
        for pkg in reserved_by:
            m_key = (pkg.name, fs_path)
            self._exact_match_request.add(m_key)
        try:
            del self._discarded[fs_path]
        except KeyError:
            pass
        for discarded_paths in self.used_auto_discard_rules.values():
            discarded_paths.discard(fs_path)

    def detect_missing(self, search_dir: "VirtualPath") -> Iterator["VirtualPath"]:
        stack = list(search_dir.iterdir)
        while stack:
            m = stack.pop()
            if m.is_dir:
                s_len = len(stack)
                stack.extend(m.iterdir)

                if s_len == len(stack) and not self.is_reserved(m):
                    # "Explicitly" empty dir
                    yield m
            elif not self.is_reserved(m):
                yield m

    def find_and_reserve_all_matches(
        self,
        match_rule: MatchRule,
        search_dirs: Sequence[SearchDir],
        dir_only_match: bool,
        match_filter: Optional[Callable[["VirtualPath"], bool]],
        reserved_by: FrozenSet[BinaryPackage],
        definition_source: str,
    ) -> Tuple[List[PathMatch], Tuple[int, ...]]:
        matched = []
        already_installed_paths = 0
        already_excluded_paths = 0
        glob_expand = False if isinstance(match_rule, ExactFileSystemPath) else True

        for match in _resolve_path(
            match_rule,
            search_dirs,
            dir_only_match,
            match_filter,
            reserved_by,
        ):
            installed_into, excluded = self.may_match(
                match, is_exact_match=not glob_expand
            )
            if installed_into:
                if glob_expand:
                    already_installed_paths += 1
                    continue
                packages = ", ".join(p.name for p in installed_into)
                raise PathAlreadyInstalledOrDiscardedError(
                    f'The "{match.path.fs_path}" has been reserved by and installed into {packages}.'
                    f" The definition that triggered this issue is {definition_source}.",
                    match,
                    installed_into,
                    definition_source,
                )
            if excluded:
                if glob_expand:
                    already_excluded_paths += 1
                    continue
                raise PathAlreadyInstalledOrDiscardedError(
                    f'The "{match.path.fs_path}" has been excluded. If you want this path installed, move it'
                    f" above the exclusion rule that excluded it. The definition that triggered this"
                    f" issue is {definition_source}.",
                    match,
                    installed_into,
                    definition_source,
                )
            if not glob_expand:
                for pkg in match.into:
                    m_key = (pkg.name, match.path.fs_path)
                    if m_key in self._exact_match_request:
                        raise ExactPathMatchTwiceError(
                            f'The path "{match.path.fs_path}" (via exact match) has already been installed'
                            f" into {pkg.name}. The second installation triggered by {definition_source}",
                            match.path,
                            pkg,
                            definition_source,
                        )
                    self._exact_match_request.add(m_key)

            if reserved_by:
                self._already_matched[match.path.fs_path] = (
                    match.into,
                    definition_source,
                )
            else:
                self.exclude(match.path.fs_path)
            matched.append(match)
        exclude_counts = already_installed_paths, already_excluded_paths
        return matched, exclude_counts


def _resolve_path(
    match_rule: MatchRule,
    search_dirs: Iterable["SearchDir"],
    dir_only_match: bool,
    match_filter: Optional[Callable[["VirtualPath"], bool]],
    into: FrozenSet[BinaryPackage],
) -> Iterator[PathMatch]:
    missing_matches = set(into)
    for sdir in search_dirs:
        matched = False
        if into and missing_matches.isdisjoint(sdir.applies_to):
            # All the packages, where this search dir applies, already got a match
            continue
        applicable = sdir.applies_to & missing_matches
        for matched_path in match_rule.finditer(
            sdir.search_dir,
            ignore_paths=match_filter,
        ):
            if dir_only_match and not matched_path.is_dir:
                continue
            if matched_path.parent_dir is None:
                if match_rule is MATCH_ANYTHING:
                    continue
                _error(
                    f"The pattern {match_rule.describe_match_short()} matched the root dir."
                )
            yield PathMatch(matched_path, sdir.search_dir, False, applicable)
            matched = True
            # continue; we want to match everything we can from this search directory.

        if matched:
            missing_matches -= applicable
            if into and not missing_matches:
                # For install rules, we can stop as soon as all packages had a match
                # For discard rules, all search directories must be visited.  Otherwise,
                # you would have to repeat the discard rule once per search dir to be
                # sure something is fully discarded
                break


def _resolve_dest_paths(
    match: PathMatch,
    dest_paths: Sequence[Tuple[str, bool]],
    install_context: "InstallRuleContext",
) -> Sequence[Tuple[str, "FSPath"]]:
    dest_and_roots = []
    for dest_path, dest_path_is_format in dest_paths:
        if dest_path_is_format:
            for pkg in match.into:
                parent_dir = match.path.parent_dir
                pkg_install_context = install_context[pkg.name]
                fs_root = pkg_install_context.fs_root
                dpath = dest_path.format(
                    basename=match.path.name,
                    dirname=parent_dir.path if parent_dir is not None else "",
                    package_name=pkg.name,
                    doc_main_package_name=pkg_install_context.doc_main_package.name,
                )
                if dpath.endswith("/"):
                    raise ValueError(
                        f'Provided destination (when resolved for {pkg.name}) for "{match.path.path}" ended'
                        f' with "/" ("{dest_path}"), which it must not!'
                    )
                dest_and_roots.append((dpath, fs_root))
        else:
            if dest_path.endswith("/"):
                raise ValueError(
                    f'Provided destination for "{match.path.path}" ended with "/" ("{dest_path}"),'
                    " which it must not!"
                )
            dest_and_roots.extend(
                (dest_path, install_context[pkg.name].fs_root) for pkg in match.into
            )
    return dest_and_roots


def _resolve_matches(
    matches: List[PathMatch],
    dest_paths: Union[Sequence[Tuple[str, bool]], Callable[[PathMatch], str]],
    install_context: "InstallRuleContext",
) -> Iterator[Tuple[PathMatch, Sequence[Tuple[str, "FSPath"]]]]:
    if callable(dest_paths):
        compute_dest_path = dest_paths
        for match in matches:
            dpath = compute_dest_path(match)
            if dpath.endswith("/"):
                raise ValueError(
                    f'Provided destination for "{match.path.path}" ended with "/" ("{dpath}"), which it must not!'
                )
            dest_and_roots = [
                (dpath, install_context[pkg.name].fs_root) for pkg in match.into
            ]
            yield match, dest_and_roots
    else:
        for match in matches:
            dest_and_roots = _resolve_dest_paths(
                match,
                dest_paths,
                install_context,
            )
            yield match, dest_and_roots


class InstallRule(DebputyDispatchableType):
    __slots__ = (
        "_already_matched",
        "_exact_match_request",
        "_condition",
        "_match_filter",
        "_definition_source",
    )

    def __init__(
        self,
        condition: Optional[ManifestCondition],
        definition_source: str,
        *,
        match_filter: Optional[Callable[["VirtualPath"], bool]] = None,
    ) -> None:
        self._condition = condition
        self._definition_source = definition_source
        self._match_filter = match_filter

    def _check_single_match(
        self, source: FileSystemMatchRule, matches: List[PathMatch]
    ) -> None:
        seen_pkgs = set()
        problem_pkgs = frozenset()
        for m in matches:
            problem_pkgs = seen_pkgs & m.into
            if problem_pkgs:
                break
            seen_pkgs.update(problem_pkgs)
        if problem_pkgs:
            pkg_names = ", ".join(sorted(p.name for p in problem_pkgs))
            _error(
                f'The pattern "{source.raw_match_rule}" matched multiple entries for the packages: {pkg_names}.'
                "However, it should matched exactly one item. Please tighten the pattern defined"
                f" in {self._definition_source}"
            )

    def _match_pattern(
        self,
        path_matcher: SourcePathMatcher,
        fs_match_rule: FileSystemMatchRule,
        condition_context: ConditionContext,
        search_dirs: Sequence[SearchDir],
        into: FrozenSet[BinaryPackage],
    ) -> List[PathMatch]:
        (matched, exclude_counts) = path_matcher.find_and_reserve_all_matches(
            fs_match_rule.match_rule,
            search_dirs,
            fs_match_rule.raw_match_rule.endswith("/"),
            self._match_filter,
            into,
            self._definition_source,
        )

        already_installed_paths, already_excluded_paths = exclude_counts

        if into:
            allow_empty_match = all(not p.should_be_acted_on for p in into)
        else:
            # discard rules must match provided at least one search dir exist.  If none of them
            # exist, then we assume the discard rule is for a package that will not be built
            allow_empty_match = any(s.search_dir.is_dir for s in search_dirs)
        if self._condition is not None and not self._condition.evaluate(
            condition_context
        ):
            allow_empty_match = True

        if not matched and not allow_empty_match:
            search_dir_text = ", ".join(x.search_dir.fs_path for x in search_dirs)
            if already_excluded_paths and already_installed_paths:
                total_paths = already_excluded_paths + already_installed_paths
                msg = (
                    f"There were no matches for {fs_match_rule.raw_match_rule} in {search_dir_text} after ignoring"
                    f" {total_paths} path(s) already been matched previously either by install or"
                    f" exclude rules. If you wanted to install some of these paths into multiple"
                    f" packages, please tweak the definition that installed them to install them"
                    f' into multiple packages (usually change "into: foo" to "into: [foo, bar]".'
                    f" If you wanted to install these paths and exclude rules are getting in your"
                    f" way, then please move this install rule before the exclusion rule that causes"
                    f" issue or, in case of built-in excludes, list the paths explicitly (without"
                    f" using patterns). Source for this issue is {self._definition_source}. Match rule:"
                    f" {fs_match_rule.match_rule.describe_match_exact()}"
                )
            elif already_excluded_paths:
                msg = (
                    f"There were no matches for {fs_match_rule.raw_match_rule} in {search_dir_text} after ignoring"
                    f" {already_excluded_paths} path(s) that have been excluded."
                    " If you wanted to install some of these paths, please move the install rule"
                    " before the exclusion rule or, in case of built-in excludes, list the paths explicitly"
                    f" (without using patterns). Source for this issue is {self._definition_source}. Match rule:"
                    f" {fs_match_rule.match_rule.describe_match_exact()}"
                )
            elif already_installed_paths:
                msg = (
                    f"There were no matches for {fs_match_rule.raw_match_rule} in {search_dir_text} after ignoring"
                    f" {already_installed_paths} path(s) already been matched previously."
                    " If you wanted to install some of these paths into multiple packages,"
                    f" please tweak the definition that installed them to install them into"
                    f' multiple packages (usually change "into: foo" to "into: [foo, bar]".'
                    f" Source for this issue is {self._definition_source}. Match rule:"
                    f" {fs_match_rule.match_rule.describe_match_exact()}"
                )
            else:
                # TODO: Try harder to find the match and point out possible typos
                msg = (
                    f"There were no matches for {fs_match_rule.raw_match_rule} in {search_dir_text} (definition:"
                    f" {self._definition_source}). Match rule: {fs_match_rule.match_rule.describe_match_exact()}"
                )
            raise NoMatchForInstallPatternError(
                msg,
                fs_match_rule,
                search_dirs,
                self._definition_source,
            )
        return matched

    def _install_matches(
        self,
        path_matcher: SourcePathMatcher,
        matches: List[PathMatch],
        dest_paths: Union[Sequence[Tuple[str, bool]], Callable[[PathMatch], str]],
        install_context: "InstallRuleContext",
        into: FrozenSet[BinaryPackage],
        condition_context: ConditionContext,
    ) -> None:
        if (
            self._condition is not None
            and not self._condition.evaluate(condition_context)
        ) or not any(p.should_be_acted_on for p in into):
            # Rule is disabled; skip all its actions - also allow empty matches
            # for this particular case.
            return

        if not matches:
            raise ValueError("matches must not be empty")

        for match, dest_paths_and_roots in _resolve_matches(
            matches,
            dest_paths,
            install_context,
        ):
            install_recursively_into_dirs = []
            for dest, fs_root in dest_paths_and_roots:
                dir_part, basename = os.path.split(dest)
                # We do not associate these with the FS path.  First off,
                # it is complicated to do in most cases (indeed, debhelper
                # does not preserve these directories either) and secondly,
                # it is "only" mtime and mode - mostly irrelevant as the
                # directory is 99.9% likely to be 0755 (we are talking
                # directories like "/usr", "/usr/share").
                dir_path = fs_root.mkdirs(dir_part)
                existing_path = dir_path.get(basename)

                if match.path.is_dir:
                    if existing_path is not None and not existing_path.is_dir:
                        existing_path.unlink()
                        existing_path = None
                    current_dir = existing_path

                    if current_dir is None:
                        current_dir = dir_path.mkdir(
                            basename, reference_path=match.path
                        )
                    install_recursively_into_dirs.append(current_dir)
                else:
                    if existing_path is not None and existing_path.is_dir:
                        _error(
                            f"Cannot install {match.path} ({match.path.fs_path}) as {dest}. That path already exist"
                            f" and is a directory.  This error was triggered via {self._definition_source}."
                        )

                    if match.path.is_symlink:
                        dir_path.add_symlink(
                            basename, match.path.readlink(), reference_path=match.path
                        )
                    else:
                        dir_path.insert_file_from_fs_path(
                            basename,
                            match.path.fs_path,
                            follow_symlinks=False,
                            use_fs_path_mode=True,
                            reference_path=match.path,
                        )
            if install_recursively_into_dirs:
                self._install_dir_recursively(
                    path_matcher, install_recursively_into_dirs, match, into
                )

    def _install_dir_recursively(
        self,
        path_matcher: SourcePathMatcher,
        parent_dirs: Sequence[FSPath],
        match: PathMatch,
        into: FrozenSet[BinaryPackage],
    ) -> None:
        stack = [
            (parent_dirs, e)
            for e in match.path.iterdir
            if not path_matcher.is_reserved(e)
        ]

        while stack:
            current_dirs, dir_entry = stack.pop()
            path_matcher.reserve(
                dir_entry,
                into,
                self._definition_source,
                is_exact_match=False,
            )
            if dir_entry.is_dir:
                new_dirs = [
                    d.mkdir(dir_entry.name, reference_path=dir_entry)
                    for d in current_dirs
                ]
                stack.extend(
                    (new_dirs, de)
                    for de in dir_entry.iterdir
                    if not path_matcher.is_reserved(de)
                )
            elif dir_entry.is_symlink:
                for current_dir in current_dirs:
                    current_dir.add_symlink(
                        dir_entry.name,
                        dir_entry.readlink(),
                        reference_path=dir_entry,
                    )
            elif dir_entry.is_file:
                for current_dir in current_dirs:
                    current_dir.insert_file_from_fs_path(
                        dir_entry.name,
                        dir_entry.fs_path,
                        use_fs_path_mode=True,
                        follow_symlinks=False,
                        reference_path=dir_entry,
                    )
            else:
                _error(
                    f"Unsupported file type: {dir_entry.fs_path} - neither a file, directory or symlink"
                )

    def perform_install(
        self,
        path_matcher: SourcePathMatcher,
        install_context: InstallRuleContext,
        condition_context: ConditionContext,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def install_as(
        cls,
        source: FileSystemMatchRule,
        dest_path: str,
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        return GenericInstallationRule(
            [source],
            [(dest_path, False)],
            into,
            condition,
            definition_source,
            require_single_match=True,
        )

    @classmethod
    def install_dest(
        cls,
        sources: Sequence[FileSystemMatchRule],
        dest_dir: Optional[str],
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        if dest_dir is None:
            dest_dir = "{dirname}/{basename}"
        else:
            dest_dir = os.path.join(dest_dir, "{basename}")
        return GenericInstallationRule(
            sources,
            [(dest_dir, True)],
            into,
            condition,
            definition_source,
        )

    @classmethod
    def install_multi_as(
        cls,
        source: FileSystemMatchRule,
        dest_paths: Sequence[str],
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        if len(dest_paths) < 2:
            raise ValueError(
                "Please use `install_as` when there is less than 2 dest path"
            )
        dps = tuple((dp, False) for dp in dest_paths)
        return GenericInstallationRule(
            [source],
            dps,
            into,
            condition,
            definition_source,
            require_single_match=True,
        )

    @classmethod
    def install_multi_dest(
        cls,
        sources: Sequence[FileSystemMatchRule],
        dest_dirs: Sequence[str],
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        if len(dest_dirs) < 2:
            raise ValueError(
                "Please use `install_dest` when there is less than 2 dest dir"
            )
        dest_paths = tuple((os.path.join(dp, "{basename}"), True) for dp in dest_dirs)
        return GenericInstallationRule(
            sources,
            dest_paths,
            into,
            condition,
            definition_source,
        )

    @classmethod
    def install_doc(
        cls,
        sources: Sequence[FileSystemMatchRule],
        dest_dir: Optional[str],
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        cond: ManifestCondition = _BUILD_DOCS_BDO
        if condition is not None:
            cond = ManifestCondition.all_of([cond, condition])
        dest_path_is_format = False
        if dest_dir is None:
            dest_dir = "usr/share/doc/{doc_main_package_name}/{basename}"
            dest_path_is_format = True

        return GenericInstallationRule(
            sources,
            [(dest_dir, dest_path_is_format)],
            into,
            cond,
            definition_source,
        )

    @classmethod
    def install_doc_as(
        cls,
        source: FileSystemMatchRule,
        dest_path: str,
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        cond: ManifestCondition = _BUILD_DOCS_BDO
        if condition is not None:
            cond = ManifestCondition.all_of([cond, condition])

        return GenericInstallationRule(
            [source],
            [(dest_path, False)],
            into,
            cond,
            definition_source,
            require_single_match=True,
        )

    @classmethod
    def install_examples(
        cls,
        sources: Sequence[FileSystemMatchRule],
        into: FrozenSet[BinaryPackage],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        cond: ManifestCondition = _BUILD_DOCS_BDO
        if condition is not None:
            cond = ManifestCondition.all_of([cond, condition])
        return GenericInstallationRule(
            sources,
            [("usr/share/doc/{doc_main_package_name}/examples/{basename}", True)],
            into,
            cond,
            definition_source,
        )

    @classmethod
    def install_man(
        cls,
        sources: Sequence[FileSystemMatchRule],
        into: FrozenSet[BinaryPackage],
        section: Optional[int],
        language: Optional[str],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> "InstallRule":
        cond: ManifestCondition = _BUILD_DOCS_BDO
        if condition is not None:
            cond = ManifestCondition.all_of([cond, condition])

        dest_path_computer = _dest_path_for_manpage(
            section, language, definition_source
        )

        return GenericInstallationRule(
            sources,
            dest_path_computer,
            into,
            cond,
            definition_source,
            match_filter=lambda m: not m.is_file,
        )

    @classmethod
    def discard_paths(
        cls,
        paths: Sequence[FileSystemMatchRule],
        definition_source: str,
        condition: Optional[ManifestCondition],
        *,
        limit_to: Optional[Sequence[FileSystemExactMatchRule]] = None,
    ) -> "InstallRule":
        return DiscardRule(
            paths,
            condition,
            tuple(limit_to) if limit_to is not None else tuple(),
            definition_source,
        )


class PPFInstallRule(InstallRule):
    __slots__ = (
        "_ppfs",
        "_substitution",
        "_into",
    )

    def __init__(
        self,
        into: BinaryPackage,
        substitution: Substitution,
        ppfs: Sequence["PackagerProvidedFile"],
    ) -> None:
        super().__init__(
            None,
            "<built-in; PPF install rule>",
        )
        self._substitution = substitution
        self._ppfs = ppfs
        self._into = into

    def perform_install(
        self,
        path_matcher: SourcePathMatcher,
        install_context: InstallRuleContext,
        condition_context: ConditionContext,
    ) -> None:
        binary_install_context = install_context[self._into.name]
        fs_root = binary_install_context.fs_root
        for ppf in self._ppfs:
            source_path = ppf.path.fs_path
            dest_dir, name = ppf.compute_dest()
            dir_path = fs_root.mkdirs(dest_dir)

            dir_path.insert_file_from_fs_path(
                name,
                source_path,
                follow_symlinks=True,
                use_fs_path_mode=False,
                mode=ppf.definition.default_mode,
            )


class GenericInstallationRule(InstallRule):
    __slots__ = (
        "_sources",
        "_into",
        "_dest_paths",
        "_require_single_match",
    )

    def __init__(
        self,
        sources: Sequence[FileSystemMatchRule],
        dest_paths: Union[Sequence[Tuple[str, bool]], Callable[[PathMatch], str]],
        into: FrozenSet[BinaryPackage],
        condition: Optional[ManifestCondition],
        definition_source: str,
        *,
        require_single_match: bool = False,
        match_filter: Optional[Callable[["VirtualPath"], bool]] = None,
    ) -> None:
        super().__init__(
            condition,
            definition_source,
            match_filter=match_filter,
        )
        self._sources = sources
        self._into = into
        self._dest_paths = dest_paths
        self._require_single_match = require_single_match
        if self._require_single_match and len(sources) != 1:
            raise ValueError("require_single_match implies sources must have len 1")

    def perform_install(
        self,
        path_matcher: SourcePathMatcher,
        install_context: InstallRuleContext,
        condition_context: ConditionContext,
    ) -> None:
        for source in self._sources:
            matches = self._match_pattern(
                path_matcher,
                source,
                condition_context,
                install_context.search_dirs,
                self._into,
            )
            if self._require_single_match and len(matches) > 1:
                self._check_single_match(source, matches)
            self._install_matches(
                path_matcher,
                matches,
                self._dest_paths,
                install_context,
                self._into,
                condition_context,
            )


class DiscardRule(InstallRule):
    __slots__ = ("_fs_match_rules", "_limit_to")

    def __init__(
        self,
        fs_match_rules: Sequence[FileSystemMatchRule],
        condition: Optional[ManifestCondition],
        limit_to: Sequence[FileSystemExactMatchRule],
        definition_source: str,
    ) -> None:
        super().__init__(condition, definition_source)
        self._fs_match_rules = fs_match_rules
        self._limit_to = limit_to

    def perform_install(
        self,
        path_matcher: SourcePathMatcher,
        install_context: InstallRuleContext,
        condition_context: ConditionContext,
    ) -> None:
        into = frozenset()
        limit_to = self._limit_to
        if limit_to:
            matches = {x.match_rule.path for x in limit_to}
            search_dirs = tuple(
                s
                for s in install_context.search_dirs
                if s.search_dir.fs_path in matches
            )
            if len(limit_to) != len(search_dirs):
                matches.difference(s.search_dir.fs_path for s in search_dirs)
                paths = ":".join(matches)
                _error(
                    f"The discard rule defined at {self._definition_source} mentions the following"
                    f" search directories that were not known to debputy: {paths}."
                    " Either the search dir is missing somewhere else or it should be removed from"
                    " the discard rule."
                )
        else:
            search_dirs = install_context.search_dirs

        for fs_match_rule in self._fs_match_rules:
            self._match_pattern(
                path_matcher,
                fs_match_rule,
                condition_context,
                search_dirs,
                into,
            )
