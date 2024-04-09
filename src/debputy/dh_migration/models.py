import dataclasses
import re
from typing import Sequence, Optional, FrozenSet, Tuple, List, cast

from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.highlevel_manifest import MutableYAMLManifest
from debputy.substitution import Substitution

_DH_VAR_RE = re.compile(r"([$][{])([A-Za-z0-9][-_:0-9A-Za-z]*)([}])")


class AcceptableMigrationIssues:
    def __init__(self, values: FrozenSet[str]):
        self._values = values

    def __contains__(self, item: str) -> bool:
        return item in self._values or "ALL" in self._values


class UnsupportedFeature(RuntimeError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])

    @property
    def issue_keys(self) -> Optional[Sequence[str]]:
        if len(self.args) < 2:
            return None
        return cast("Sequence[str]", self.args[1])


class ConflictingChange(RuntimeError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])


@dataclasses.dataclass(slots=True)
class FeatureMigration:
    tagline: str
    successful_manifest_changes: int = 0
    already_present: int = 0
    warnings: List[str] = dataclasses.field(default_factory=list)
    remove_paths_on_success: List[str] = dataclasses.field(default_factory=list)
    rename_paths_on_success: List[Tuple[str, str]] = dataclasses.field(
        default_factory=list
    )
    assumed_compat: Optional[int] = None
    required_plugins: List[str] = dataclasses.field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def rename_on_success(self, source: str, dest: str) -> None:
        self.rename_paths_on_success.append((source, dest))

    def remove_on_success(self, path: str) -> None:
        self.remove_paths_on_success.append(path)

    def require_plugin(self, debputy_plugin: str) -> None:
        self.required_plugins.append(debputy_plugin)

    @property
    def anything_to_do(self) -> bool:
        return bool(self.total_changes_involved)

    @property
    def performed_changes(self) -> int:
        return (
            self.successful_manifest_changes
            + len(self.remove_paths_on_success)
            + len(self.rename_paths_on_success)
        )

    @property
    def total_changes_involved(self) -> int:
        return (
            self.successful_manifest_changes
            + len(self.warnings)
            + len(self.remove_paths_on_success)
            + len(self.rename_paths_on_success)
        )


class DHMigrationSubstitution(Substitution):
    def __init__(
        self,
        dpkg_arch_table: DpkgArchitectureBuildProcessValuesTable,
        acceptable_migration_issues: AcceptableMigrationIssues,
        feature_migration: FeatureMigration,
        mutable_manifest: MutableYAMLManifest,
    ) -> None:
        self._acceptable_migration_issues = acceptable_migration_issues
        self._dpkg_arch_table = dpkg_arch_table
        self._feature_migration = feature_migration
        self._mutable_manifest = mutable_manifest
        # TODO: load 1:1 variables from the real subst instance (less stuff to keep in sync)
        one2one = [
            "DEB_SOURCE",
            "DEB_VERSION",
            "DEB_VERSION_EPOCH_UPSTREAM",
            "DEB_VERSION_UPSTREAM_REVISION",
            "DEB_VERSION_UPSTREAM",
            "SOURCE_DATE_EPOCH",
        ]
        self._builtin_substs = {
            "Tab": "{{token:TAB}}",
            "Space": " ",
            "Newline": "{{token:NEWLINE}}",
            "Dollar": "${}",
        }
        self._builtin_substs.update((x, "{{" + x + "}}") for x in one2one)

    def _replacement(self, key: str, definition_source: str) -> str:
        if key in self._builtin_substs:
            return self._builtin_substs[key]
        if key in self._dpkg_arch_table:
            return "{{" + key + "}}"
        if key.startswith("env:"):
            if "dh-subst-env" not in self._acceptable_migration_issues:
                raise UnsupportedFeature(
                    "Use of environment based substitution variable {{"
                    + key
                    + "}} is not"
                    f" supported in debputy. The variable was spotted at {definition_source}",
                    ["dh-subst-env"],
                )
        elif "dh-subst-unknown-variable" not in self._acceptable_migration_issues:
            raise UnsupportedFeature(
                "Unknown substitution variable {{"
                + key
                + "}}, which does not have a known"
                f" counter part in debputy. The variable was spotted at {definition_source}",
                ["dh-subst-unknown-variable"],
            )
        manifest_definitions = self._mutable_manifest.manifest_definitions(
            create_if_absent=False
        )
        manifest_variables = manifest_definitions.manifest_variables(
            create_if_absent=False
        )
        if key not in manifest_variables.variables:
            manifest_definitions.create_definition_if_missing()
            manifest_variables[key] = "TODO: Provide variable value for " + key
            self._feature_migration.warn(
                "TODO: MANUAL MIGRATION of unresolved substitution variable {{"
                + key
                + "}} from"
                + f" {definition_source}"
            )
            self._feature_migration.successful_manifest_changes += 1

        return "{{" + key + "}}"

    def substitute(
        self,
        value: str,
        definition_source: str,
        /,
        escape_glob_characters: bool = False,
    ) -> str:
        if "${" not in value:
            return value
        replacement = self._apply_substitution(
            _DH_VAR_RE,
            value,
            definition_source,
            escape_glob_characters=escape_glob_characters,
        )
        return replacement.replace("${}", "$")

    def with_extra_substitutions(self, **extra_substitutions: str) -> "Substitution":
        return self
