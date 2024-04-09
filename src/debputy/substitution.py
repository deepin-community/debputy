import dataclasses
import os
import re
from enum import IntEnum
from typing import FrozenSet, NoReturn, Optional, Set, Mapping, TYPE_CHECKING, Self

from debputy.architecture_support import (
    dpkg_architecture_table,
    DpkgArchitectureBuildProcessValuesTable,
)
from debputy.exceptions import DebputySubstitutionError
from debputy.util import glob_escape

if TYPE_CHECKING:
    from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
    from debputy.plugin.api import VirtualPath


SUBST_VAR_RE = re.compile(
    r"""
    ([{][{][ ]*)

    (
        _?[A-Za-z0-9]+
        (?:[-_:][A-Za-z0-9]+)*
    )

    ([ ]*[}][}])
""",
    re.VERBOSE,
)


class VariableNameState(IntEnum):
    UNDEFINED = 1
    RESERVED = 2
    DEFINED = 3


@dataclasses.dataclass(slots=True, frozen=True)
class VariableContext:
    debian_dir: "VirtualPath"


class Substitution:
    def substitute(
        self,
        value: str,
        definition_source: str,
        /,
        escape_glob_characters: bool = False,
    ) -> str:
        raise NotImplementedError

    def with_extra_substitutions(self, **extra_substitutions: str) -> "Substitution":
        raise NotImplementedError

    def with_unresolvable_substitutions(
        self, *extra_substitutions: str
    ) -> "Substitution":
        raise NotImplementedError

    def variable_state(self, variable_name: str) -> VariableNameState:
        return VariableNameState.UNDEFINED

    def is_used(self, variable_name: str) -> bool:
        return False

    def _mark_used(self, variable_name: str) -> None:
        pass

    def _replacement(self, matched_key: str, definition_source: str) -> str:
        self._error(
            "Cannot resolve {{" + matched_key + "}}."
            f" The error occurred while trying to process {definition_source}"
        )

    def _error(
        self,
        msg: str,
        *,
        caused_by: Optional[BaseException] = None,
    ) -> NoReturn:
        raise DebputySubstitutionError(msg) from caused_by

    def _apply_substitution(
        self,
        pattern: re.Pattern[str],
        value: str,
        definition_source: str,
        /,
        escape_glob_characters: bool = False,
    ) -> str:
        replacement = value
        offset = 0
        for match in pattern.finditer(value):
            prefix, matched_key, suffix = match.groups()
            replacement_value = self._replacement(matched_key, definition_source)
            self._mark_used(matched_key)
            if escape_glob_characters:
                replacement_value = glob_escape(replacement_value)
            s, e = match.span()
            s += offset
            e += offset
            replacement = replacement[:s] + replacement_value + replacement[e:]
            token_fluff_len = len(prefix) + len(suffix)
            offset += len(replacement_value) - len(matched_key) - token_fluff_len
        return replacement


class NullSubstitution(Substitution):
    def substitute(
        self,
        value: str,
        definition_source: str,
        /,
        escape_glob_characters: bool = False,
    ) -> str:
        return value

    def with_extra_substitutions(self, **extra_substitutions: str) -> "Substitution":
        return self

    def with_unresolvable_substitutions(
        self, *extra_substitutions: str
    ) -> "Substitution":
        return self


NULL_SUBSTITUTION = NullSubstitution()
del NullSubstitution


class SubstitutionImpl(Substitution):
    __slots__ = (
        "_used",
        "_env",
        "_plugin_feature_set",
        "_static_variables",
        "_unresolvable_substitutions",
        "_dpkg_arch_table",
        "_parent",
        "_variable_context",
    )

    def __init__(
        self,
        /,
        plugin_feature_set: Optional["PluginProvidedFeatureSet"] = None,
        static_variables: Optional[Mapping[str, str]] = None,
        unresolvable_substitutions: FrozenSet[str] = frozenset(),
        dpkg_arch_table: Optional[DpkgArchitectureBuildProcessValuesTable] = None,
        environment: Optional[Mapping[str, str]] = None,
        parent: Optional["SubstitutionImpl"] = None,
        variable_context: Optional[VariableContext] = None,
    ) -> None:
        self._used: Set[str] = set()
        self._plugin_feature_set = plugin_feature_set
        self._static_variables = (
            dict(static_variables) if static_variables is not None else None
        )
        self._unresolvable_substitutions = unresolvable_substitutions
        self._dpkg_arch_table = (
            dpkg_arch_table
            if dpkg_arch_table is not None
            else dpkg_architecture_table()
        )
        self._env = environment if environment is not None else os.environ
        self._parent = parent
        if variable_context is not None:
            self._variable_context = variable_context
        elif self._parent is not None:
            self._variable_context = self._parent._variable_context
        else:
            raise ValueError(
                "variable_context is required either directly or via the parent"
            )

    def copy_for_subst_test(
        self,
        plugin_feature_set: "PluginProvidedFeatureSet",
        variable_context: VariableContext,
        *,
        extra_substitutions: Optional[Mapping[str, str]] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "Self":
        extra_substitutions_impl = (
            dict(self._static_variables.items()) if self._static_variables else {}
        )
        if extra_substitutions:
            extra_substitutions_impl.update(extra_substitutions)
        return self.__class__(
            plugin_feature_set=plugin_feature_set,
            variable_context=variable_context,
            static_variables=extra_substitutions_impl,
            unresolvable_substitutions=self._unresolvable_substitutions,
            dpkg_arch_table=self._dpkg_arch_table,
            environment=environment if environment is not None else {},
        )

    def variable_state(self, key: str) -> VariableNameState:
        if key.startswith("DEB_"):
            if key in self._dpkg_arch_table:
                return VariableNameState.DEFINED
            return VariableNameState.RESERVED
        plugin_feature_set = self._plugin_feature_set
        if (
            plugin_feature_set is not None
            and key in plugin_feature_set.manifest_variables
        ):
            return VariableNameState.DEFINED
        if key.startswith("env:"):
            k = key[4:]
            if k in self._env:
                return VariableNameState.DEFINED
            return VariableNameState.RESERVED
        if self._static_variables is not None and key in self._static_variables:
            return VariableNameState.DEFINED
        if key in self._unresolvable_substitutions:
            return VariableNameState.RESERVED
        if self._parent is not None:
            return self._parent.variable_state(key)
        return VariableNameState.UNDEFINED

    def is_used(self, variable_name: str) -> bool:
        if variable_name in self._used:
            return True
        parent = self._parent
        if parent is not None:
            return parent.is_used(variable_name)
        return False

    def _mark_used(self, variable_name: str) -> None:
        p = self._parent
        while p:
            # Find the parent that has the variable if possible. This ensures that is_used works
            # correctly.
            if p._static_variables is not None and variable_name in p._static_variables:
                p._mark_used(variable_name)
                break
            plugin_feature_set = p._plugin_feature_set
            if (
                plugin_feature_set is not None
                and variable_name in plugin_feature_set.manifest_variables
                and not plugin_feature_set.manifest_variables[
                    variable_name
                ].is_documentation_placeholder
            ):
                p._mark_used(variable_name)
                break
            p = p._parent
        self._used.add(variable_name)

    def _replacement(self, key: str, definition_source: str) -> str:
        if key.startswith("DEB_") and key in self._dpkg_arch_table:
            return self._dpkg_arch_table[key]
        if key.startswith("env:"):
            k = key[4:]
            if k in self._env:
                return self._env[k]
            self._error(
                f'The environment does not contain the variable "{key}" '
                f"(error occurred while trying to process {definition_source})"
            )

        # The order between extra_substitution and plugin_feature_set is leveraged by
        # the tests to implement mocking variables. If the order needs tweaking,
        # you will need a custom resolver for the tests to support mocking.
        static_variables = self._static_variables
        if static_variables and key in static_variables:
            return static_variables[key]
        plugin_feature_set = self._plugin_feature_set
        if plugin_feature_set is not None:
            provided_var = plugin_feature_set.manifest_variables.get(key)
            if (
                provided_var is not None
                and not provided_var.is_documentation_placeholder
            ):
                v = provided_var.resolve(self._variable_context)
                # cache it for next time.
                if static_variables is None:
                    static_variables = {}
                    self._static_variables = static_variables
                static_variables[key] = v
                return v
        if key in self._unresolvable_substitutions:
            self._error(
                "The variable {{" + key + "}}"
                f" is not available while processing {definition_source}."
            )
        parent = self._parent
        if parent is not None:
            return parent._replacement(key, definition_source)
        self._error(
            "Cannot resolve {{" + key + "}}: it is not a known key."
            f" The error occurred while trying to process {definition_source}"
        )

    def with_extra_substitutions(self, **extra_substitutions: str) -> "Substitution":
        if not extra_substitutions:
            return self
        return SubstitutionImpl(
            dpkg_arch_table=self._dpkg_arch_table,
            environment=self._env,
            static_variables=extra_substitutions,
            parent=self,
        )

    def with_unresolvable_substitutions(
        self,
        *extra_substitutions: str,
    ) -> "Substitution":
        if not extra_substitutions:
            return self
        return SubstitutionImpl(
            dpkg_arch_table=self._dpkg_arch_table,
            environment=self._env,
            unresolvable_substitutions=frozenset(extra_substitutions),
            parent=self,
        )

    def substitute(
        self,
        value: str,
        definition_source: str,
        /,
        escape_glob_characters: bool = False,
    ) -> str:
        if "{{" not in value:
            return value
        return self._apply_substitution(
            SUBST_VAR_RE,
            value,
            definition_source,
            escape_glob_characters=escape_glob_characters,
        )
