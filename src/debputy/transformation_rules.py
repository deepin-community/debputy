import dataclasses
import os
from typing import (
    NoReturn,
    Optional,
    Callable,
    Sequence,
    Tuple,
    List,
    Literal,
    Dict,
    TypeVar,
    cast,
)

from debputy.exceptions import (
    DebputyRuntimeError,
    PureVirtualPathError,
    TestPathWithNonExistentFSPathError,
)
from debputy.filesystem_scan import FSPath
from debputy.interpreter import (
    extract_shebang_interpreter_from_file,
)
from debputy.manifest_conditions import ConditionContext, ManifestCondition
from debputy.manifest_parser.base_types import (
    FileSystemMode,
    StaticFileSystemOwner,
    StaticFileSystemGroup,
    DebputyDispatchableType,
)
from debputy.manifest_parser.util import AttributePath
from debputy.path_matcher import MatchRule
from debputy.plugin.api import VirtualPath
from debputy.plugin.debputy.types import DebputyCapability
from debputy.util import _warn


class TransformationRuntimeError(DebputyRuntimeError):
    pass


CreateSymlinkReplacementRule = Literal[
    "error-if-exists",
    "error-if-directory",
    "abort-on-non-empty-directory",
    "discard-existing",
]


VP = TypeVar("VP", bound=VirtualPath)


@dataclasses.dataclass(frozen=True, slots=True)
class PreProvidedExclusion:
    tag: str
    description: str
    pruner: Callable[[FSPath], None]


class TransformationRule(DebputyDispatchableType):
    __slots__ = ()

    def transform_file_system(
        self, fs_root: FSPath, condition_context: ConditionContext
    ) -> None:
        raise NotImplementedError

    def _evaluate_condition(
        self,
        condition: Optional[ManifestCondition],
        condition_context: ConditionContext,
        result_if_condition_is_missing: bool = True,
    ) -> bool:
        if condition is None:
            return result_if_condition_is_missing
        return condition.evaluate(condition_context)

    def _error(
        self,
        msg: str,
        *,
        caused_by: Optional[BaseException] = None,
    ) -> NoReturn:
        raise TransformationRuntimeError(msg) from caused_by

    def _match_rule_had_no_matches(
        self, match_rule: MatchRule, definition_source: str
    ) -> NoReturn:
        self._error(
            f'The match rule "{match_rule.describe_match_short()}" in transformation "{definition_source}" did'
            " not match any paths. Either the definition is redundant (and can be omitted) or the match rule is"
            " incorrect."
        )

    def _fs_path_as_dir(
        self,
        path: VP,
        definition_source: str,
    ) -> VP:
        if path.is_dir:
            return path
        path_type = "file" if path.is_file else 'symlink/"special file system object"'
        self._error(
            f"The path {path.path} was expected to be a directory (or non-existing) due to"
            f" {definition_source}. However that path existed and is a {path_type}."
            f" You may need a `remove: {path.path}` prior to {definition_source} to"
            " to make this transformation succeed."
        )

    def _ensure_is_directory(
        self,
        fs_root: FSPath,
        path_to_directory: str,
        definition_source: str,
    ) -> FSPath:
        current, missing_parts = fs_root.attempt_lookup(path_to_directory)
        current = self._fs_path_as_dir(cast("FSPath", current), definition_source)
        if missing_parts:
            return current.mkdirs("/".join(missing_parts))
        return current


class RemoveTransformationRule(TransformationRule):
    __slots__ = (
        "_match_rules",
        "_keep_empty_parent_dirs",
        "_definition_source",
    )

    def __init__(
        self,
        match_rules: Sequence[MatchRule],
        keep_empty_parent_dirs: bool,
        definition_source: AttributePath,
    ) -> None:
        self._match_rules = match_rules
        self._keep_empty_parent_dirs = keep_empty_parent_dirs
        self._definition_source = definition_source.path

    def transform_file_system(
        self,
        fs_root: FSPath,
        condition_context: ConditionContext,
    ) -> None:
        matched_any = False
        for match_rule in self._match_rules:
            # Fully resolve the matches to avoid RuntimeError caused by collection changing size as a
            # consequence of the removal: https://salsa.debian.org/debian/debputy/-/issues/52
            matches = list(match_rule.finditer(fs_root))
            for m in matches:
                matched_any = True
                parent = m.parent_dir
                if parent is None:
                    self._error(
                        f"Cannot remove the root directory (triggered by {self._definition_source})"
                    )
                m.unlink(recursive=True)
                if not self._keep_empty_parent_dirs:
                    parent.prune_if_empty_dir()
            # FIXME: `rm` should probably be forgiving or at least support a condition to avoid failures
            if not matched_any:
                self._match_rule_had_no_matches(match_rule, self._definition_source)


class MoveTransformationRule(TransformationRule):
    __slots__ = (
        "_match_rule",
        "_dest_path",
        "_dest_is_dir",
        "_definition_source",
        "_condition",
    )

    def __init__(
        self,
        match_rule: MatchRule,
        dest_path: str,
        dest_is_dir: bool,
        definition_source: AttributePath,
        condition: Optional[ManifestCondition],
    ) -> None:
        self._match_rule = match_rule
        self._dest_path = dest_path
        self._dest_is_dir = dest_is_dir
        self._definition_source = definition_source.path
        self._condition = condition

    def transform_file_system(
        self, fs_root: FSPath, condition_context: ConditionContext
    ) -> None:
        if not self._evaluate_condition(self._condition, condition_context):
            return
        # Eager resolve is necessary to avoid "self-recursive" matching in special cases (e.g., **/*.la)
        matches = list(self._match_rule.finditer(fs_root))
        if not matches:
            self._match_rule_had_no_matches(self._match_rule, self._definition_source)

        target_dir: Optional[VirtualPath]
        if self._dest_is_dir:
            target_dir = self._ensure_is_directory(
                fs_root,
                self._dest_path,
                self._definition_source,
            )
        else:
            dir_part, basename = os.path.split(self._dest_path)
            target_parent_dir = self._ensure_is_directory(
                fs_root,
                dir_part,
                self._definition_source,
            )
            target_dir = target_parent_dir.get(basename)

            if target_dir is None or not target_dir.is_dir:
                if len(matches) > 1:
                    self._error(
                        f"Could not rename {self._match_rule.describe_match_short()} to {self._dest_path}"
                        f" (from: {self._definition_source}).  Multiple paths matched the pattern and the"
                        " destination was not a directory. Either correct the pattern to only match only source"
                        " OR define the destination to be a directory (E.g., add a trailing slash - example:"
                        f' "{self._dest_path}/")'
                    )
                p = matches[0]
                if p.path == self._dest_path:
                    self._error(
                        f"Error in {self._definition_source}, the source"
                        f" {self._match_rule.describe_match_short()} matched {self._dest_path} making the"
                        " rename redundant!?"
                    )
                p.parent_dir = target_parent_dir
                p.name = basename
                return

        assert target_dir is not None and target_dir.is_dir
        basenames: Dict[str, VirtualPath] = dict()
        target_dir_path = target_dir.path

        for m in matches:
            if m.path == target_dir_path:
                self._error(
                    f"Error in {self._definition_source}, the source {self._match_rule.describe_match_short()}"
                    f"matched {self._dest_path} (among other), but it is not possible to copy a directory into"
                    " itself"
                )
            if m.name in basenames:
                alt_path = basenames[m.name]
                # We document "two *distinct*" paths.  However, as the glob matches are written, it should not be
                # possible for a *single* glob to match the same path twice.
                assert alt_path is not m
                self._error(
                    f"Could not rename {self._match_rule.describe_match_short()} to {self._dest_path}"
                    f" (from: {self._definition_source}).  Multiple paths matched the pattern had the"
                    f' same basename "{m.name}" ("{m.path}" vs. "{alt_path.path}").  Please correct the'
                    f" pattern, so it only matches one path with that basename to avoid this conflict."
                )
            existing = m.get(m.name)
            if existing and existing.is_dir:
                self._error(
                    f"Could not rename {self._match_rule.describe_match_short()} to {self._dest_path}"
                    f" (from: {self._definition_source}).  The pattern matched {m.path} which would replace"
                    f" the existing directory {existing.path}.  If this replacement is intentional, then please"
                    f' remove "{existing.path}" first (e.g., via `- remove: "{existing.path}"`)'
                )
            basenames[m.name] = m
            m.parent_dir = target_dir


class CreateSymlinkPathTransformationRule(TransformationRule):
    __slots__ = (
        "_link_dest",
        "_link_target",
        "_replacement_rule",
        "_definition_source",
        "_condition",
    )

    def __init__(
        self,
        link_target: str,
        link_dest: str,
        replacement_rule: CreateSymlinkReplacementRule,
        definition_source: AttributePath,
        condition: Optional[ManifestCondition],
    ) -> None:
        self._link_target = link_target
        self._link_dest = link_dest
        self._replacement_rule = replacement_rule
        self._definition_source = definition_source.path
        self._condition = condition

    def transform_file_system(
        self,
        fs_root: FSPath,
        condition_context: ConditionContext,
    ) -> None:
        if not self._evaluate_condition(self._condition, condition_context):
            return
        dir_path_part, link_name = os.path.split(self._link_dest)
        dir_path = self._ensure_is_directory(
            fs_root,
            dir_path_part,
            self._definition_source,
        )
        existing = dir_path.get(link_name)
        if existing:
            self._handle_existing_path(existing)
        dir_path.add_symlink(link_name, self._link_target)

    def _handle_existing_path(self, existing: VirtualPath) -> None:
        replacement_rule = self._replacement_rule
        if replacement_rule == "abort-on-non-empty-directory":
            unlink = not existing.is_dir or not any(existing.iterdir)
            reason = "the path is a non-empty directory"
        elif replacement_rule == "discard-existing":
            unlink = True
            reason = "<<internal error: you should not see an error with this message>>"
        elif replacement_rule == "error-if-directory":
            unlink = not existing.is_dir
            reason = "the path is a directory"
        else:
            assert replacement_rule == "error-if-exists"
            unlink = False
            reason = "the path exists"

        if unlink:
            existing.unlink(recursive=True)
        else:
            self._error(
                f"Refusing to replace {existing.path} with a symlink; {reason} and"
                f" the active replacement-rule was {self._replacement_rule}.  You can"
                f' set the replacement-rule to "discard-existing", if you are not interested'
                f" in the contents of {existing.path}. This error was triggered by {self._definition_source}."
            )


class CreateDirectoryTransformationRule(TransformationRule):
    __slots__ = (
        "_directories",
        "_owner",
        "_group",
        "_mode",
        "_definition_source",
        "_condition",
    )

    def __init__(
        self,
        directories: Sequence[str],
        owner: Optional[StaticFileSystemOwner],
        group: Optional[StaticFileSystemGroup],
        mode: Optional[FileSystemMode],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> None:
        super().__init__()
        self._directories = directories
        self._owner = owner
        self._group = group
        self._mode = mode
        self._definition_source = definition_source
        self._condition = condition

    def transform_file_system(
        self,
        fs_root: FSPath,
        condition_context: ConditionContext,
    ) -> None:
        if not self._evaluate_condition(self._condition, condition_context):
            return
        owner = self._owner
        group = self._group
        mode = self._mode
        for directory in self._directories:
            dir_path = self._ensure_is_directory(
                fs_root,
                directory,
                self._definition_source,
            )

            if mode is not None:
                try:
                    desired_mode = mode.compute_mode(dir_path.mode, dir_path.is_dir)
                except ValueError as e:
                    self._error(
                        f"Could not compute desired mode for {dir_path.path} as"
                        f" requested in {self._definition_source}: {e.args[0]}",
                        caused_by=e,
                    )
                dir_path.mode = desired_mode
            dir_path.chown(owner, group)


def _apply_owner_and_mode(
    path: VirtualPath,
    owner: Optional[StaticFileSystemOwner],
    group: Optional[StaticFileSystemGroup],
    mode: Optional[FileSystemMode],
    capabilities: Optional[str],
    capability_mode: Optional[FileSystemMode],
    definition_source: str,
) -> None:
    if owner is not None or group is not None:
        path.chown(owner, group)
    if mode is not None:
        try:
            desired_mode = mode.compute_mode(path.mode, path.is_dir)
        except ValueError as e:
            raise TransformationRuntimeError(
                f"Could not compute desired mode for {path.path} as"
                f" requested in {definition_source}: {e.args[0]}"
            ) from e
        path.mode = desired_mode

    if path.is_file and capabilities is not None:
        cap_ref = path.metadata(DebputyCapability)
        cap_value = cap_ref.value
        if cap_value is not None:
            _warn(
                f"Replacing the capabilities set on path {path.path} from {cap_value.definition_source} due"
                f" to {definition_source}."
            )
        assert capability_mode is not None
        cap_ref.value = DebputyCapability(
            capabilities,
            capability_mode,
            definition_source,
        )


class PathMetadataTransformationRule(TransformationRule):
    __slots__ = (
        "_match_rules",
        "_owner",
        "_group",
        "_mode",
        "_capabilities",
        "_capability_mode",
        "_recursive",
        "_definition_source",
        "_condition",
    )

    def __init__(
        self,
        match_rules: Sequence[MatchRule],
        owner: Optional[StaticFileSystemOwner],
        group: Optional[StaticFileSystemGroup],
        mode: Optional[FileSystemMode],
        recursive: bool,
        capabilities: Optional[str],
        capability_mode: Optional[FileSystemMode],
        definition_source: str,
        condition: Optional[ManifestCondition],
    ) -> None:
        super().__init__()
        self._match_rules = match_rules
        self._owner = owner
        self._group = group
        self._mode = mode
        self._capabilities = capabilities
        self._capability_mode = capability_mode
        self._recursive = recursive
        self._definition_source = definition_source
        self._condition = condition
        if self._capabilities is None and self._capability_mode is not None:
            raise ValueError("capability_mode without capabilities")
        if self._capabilities is not None and self._capability_mode is None:
            raise ValueError("capabilities without capability_mode")

    def transform_file_system(
        self,
        fs_root: FSPath,
        condition_context: ConditionContext,
    ) -> None:
        if not self._evaluate_condition(self._condition, condition_context):
            return
        owner = self._owner
        group = self._group
        mode = self._mode
        capabilities = self._capabilities
        capability_mode = self._capability_mode
        definition_source = self._definition_source
        d: Optional[List[FSPath]] = [] if self._recursive else None
        needs_file_match = False
        if self._owner is not None or self._group is not None or self._mode is not None:
            needs_file_match = True

        for match_rule in self._match_rules:
            match_ok = False
            saw_symlink = False
            saw_directory = False

            for path in match_rule.finditer(fs_root):
                if path.is_symlink:
                    saw_symlink = True
                    continue
                if path.is_file or not needs_file_match:
                    match_ok = True
                if path.is_dir:
                    saw_directory = True
                    if not match_ok and needs_file_match and self._recursive:
                        match_ok = any(p.is_file for p in path.all_paths())
                _apply_owner_and_mode(
                    path,
                    owner,
                    group,
                    mode,
                    capabilities,
                    capability_mode,
                    definition_source,
                )
                if path.is_dir and d is not None:
                    d.append(path)

            if not match_ok:
                if needs_file_match and (saw_directory or saw_symlink):
                    _warn(
                        f"The match rule {match_rule.describe_match_short()} (from {self._definition_source})"
                        " did not match any files, but given the attributes it can only apply to files."
                    )
                elif saw_symlink:
                    _warn(
                        f"The match rule {match_rule.describe_match_short()} (from {self._definition_source})"
                        ' matched symlinks, but "path-metadata" cannot apply to symlinks.'
                    )
                self._match_rule_had_no_matches(match_rule, self._definition_source)

        if not d:
            return
        for recurse_dir in d:
            for path in recurse_dir.all_paths():
                if path.is_symlink:
                    continue
                _apply_owner_and_mode(
                    path,
                    owner,
                    group,
                    mode,
                    capabilities,
                    capability_mode,
                    definition_source,
                )


class ModeNormalizationTransformationRule(TransformationRule):
    __slots__ = ("_normalizations",)

    def __init__(
        self,
        normalizations: Sequence[Tuple[MatchRule, FileSystemMode]],
    ) -> None:
        self._normalizations = normalizations

    def transform_file_system(
        self,
        fs_root: FSPath,
        condition_context: ConditionContext,
    ) -> None:
        seen = set()
        for match_rule, fs_mode in self._normalizations:
            for path in match_rule.finditer(
                fs_root, ignore_paths=lambda p: p.path in seen
            ):
                if path.is_symlink or path.path in seen:
                    continue
                seen.add(path.path)
                try:
                    desired_mode = fs_mode.compute_mode(path.mode, path.is_dir)
                except ValueError as e:
                    raise AssertionError(
                        "Error while applying built-in mode normalization rule"
                    ) from e
                path.mode = desired_mode


class NormalizeShebangLineTransformation(TransformationRule):
    def transform_file_system(
        self,
        fs_root: VirtualPath,
        condition_context: ConditionContext,
    ) -> None:
        for path in fs_root.all_paths():
            if not path.is_file:
                continue
            try:
                with path.open(byte_io=True, buffering=4096) as fd:
                    interpreter = extract_shebang_interpreter_from_file(fd)
            except (PureVirtualPathError, TestPathWithNonExistentFSPathError):
                # Do not make tests unnecessarily complex to write
                continue
            if interpreter is None:
                continue

            if interpreter.fixup_needed:
                interpreter.replace_shebang_line(path)
