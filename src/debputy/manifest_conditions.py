import dataclasses
from enum import Enum
from typing import List, Callable, Optional, Sequence

from debian.debian_support import DpkgArchTable

from debputy._deb_options_profiles import DebBuildOptionsAndProfiles
from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.manifest_parser.base_types import DebputyDispatchableType
from debputy.packages import BinaryPackage
from debputy.substitution import Substitution
from debputy.util import active_profiles_match


@dataclasses.dataclass(slots=True, frozen=True)
class ConditionContext:
    binary_package: Optional[BinaryPackage]
    build_env: DebBuildOptionsAndProfiles
    substitution: Substitution
    dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable
    dpkg_arch_query_table: DpkgArchTable


class ManifestCondition(DebputyDispatchableType):
    __slots__ = ()

    def describe(self) -> str:
        raise NotImplementedError

    def negated(self) -> "ManifestCondition":
        return NegatedManifestCondition(self)

    def evaluate(self, context: ConditionContext) -> bool:
        raise NotImplementedError

    @classmethod
    def _manifest_group(
        cls,
        match_type: "_ConditionGroupMatchType",
        conditions: "Sequence[ManifestCondition]",
    ) -> "ManifestCondition":
        condition = conditions[0]
        if (
            isinstance(condition, ManifestConditionGroup)
            and condition.match_type == match_type
        ):
            return condition.extend(conditions[1:])
        return ManifestConditionGroup(match_type, conditions)

    @classmethod
    def any_of(cls, conditions: "Sequence[ManifestCondition]") -> "ManifestCondition":
        return cls._manifest_group(_ConditionGroupMatchType.ANY_OF, conditions)

    @classmethod
    def all_of(cls, conditions: "Sequence[ManifestCondition]") -> "ManifestCondition":
        return cls._manifest_group(_ConditionGroupMatchType.ALL_OF, conditions)

    @classmethod
    def is_cross_building(cls) -> "ManifestCondition":
        return _IS_CROSS_BUILDING

    @classmethod
    def can_execute_compiled_binaries(cls) -> "ManifestCondition":
        return _CAN_EXECUTE_COMPILED_BINARIES

    @classmethod
    def run_build_time_tests(cls) -> "ManifestCondition":
        return _RUN_BUILD_TIME_TESTS


class NegatedManifestCondition(ManifestCondition):
    __slots__ = ("_condition",)

    def __init__(self, condition: ManifestCondition) -> None:
        self._condition = condition

    def negated(self) -> "ManifestCondition":
        return self._condition

    def describe(self) -> str:
        return f"not ({self._condition.describe()})"

    def evaluate(self, context: ConditionContext) -> bool:
        return not self._condition.evaluate(context)


class _ConditionGroupMatchType(Enum):
    ANY_OF = (any, "At least one of: [{conditions}]")
    ALL_OF = (all, "All of: [{conditions}]")

    def describe(self, conditions: Sequence[ManifestCondition]) -> str:
        return self.value[1].format(
            conditions=", ".join(x.describe() for x in conditions)
        )

    def evaluate(
        self, conditions: Sequence[ManifestCondition], context: ConditionContext
    ) -> bool:
        return self.value[0](c.evaluate(context) for c in conditions)


class ManifestConditionGroup(ManifestCondition):
    __slots__ = ("match_type", "_conditions")

    def __init__(
        self,
        match_type: _ConditionGroupMatchType,
        conditions: Sequence[ManifestCondition],
    ) -> None:
        self.match_type = match_type
        self._conditions = conditions

    def describe(self) -> str:
        return self.match_type.describe(self._conditions)

    def evaluate(self, context: ConditionContext) -> bool:
        return self.match_type.evaluate(self._conditions, context)

    def extend(
        self,
        conditions: Sequence[ManifestCondition],
    ) -> "ManifestConditionGroup":
        combined = list(self._conditions)
        combined.extend(conditions)
        return ManifestConditionGroup(
            self.match_type,
            combined,
        )


class ArchMatchManifestConditionBase(ManifestCondition):
    __slots__ = ("_arch_spec", "_is_negated")

    def __init__(self, arch_spec: List[str], *, is_negated: bool = False) -> None:
        self._arch_spec = arch_spec
        self._is_negated = is_negated

    def negated(self) -> "ManifestCondition":
        return self.__class__(self._arch_spec, is_negated=not self._is_negated)


class SourceContextArchMatchManifestCondition(ArchMatchManifestConditionBase):
    def describe(self) -> str:
        if self._is_negated:
            return f'architecture (for source package) matches *none* of [{", ".join(self._arch_spec)}]'
        return f'architecture (for source package) matches any of [{", ".join(self._arch_spec)}]'

    def evaluate(self, context: ConditionContext) -> bool:
        arch = context.dpkg_architecture_variables.current_host_arch
        match = context.dpkg_arch_query_table.architecture_is_concerned(
            arch, self._arch_spec
        )
        return not match if self._is_negated else match


class BinaryPackageContextArchMatchManifestCondition(ArchMatchManifestConditionBase):
    def describe(self) -> str:
        if self._is_negated:
            return f'architecture (for binary package) matches *none* of [{", ".join(self._arch_spec)}]'
        return f'architecture (for binary package) matches any of [{", ".join(self._arch_spec)}]'

    def evaluate(self, context: ConditionContext) -> bool:
        binary_package = context.binary_package
        if binary_package is None:
            raise RuntimeError(
                "Condition only applies in the context of a BinaryPackage, but was evaluated"
                " without one"
            )
        arch = binary_package.resolved_architecture
        match = context.dpkg_arch_query_table.architecture_is_concerned(
            arch, self._arch_spec
        )
        return not match if self._is_negated else match


class BuildProfileMatch(ManifestCondition):
    __slots__ = ("_profile_spec", "_is_negated")

    def __init__(self, profile_spec: str, *, is_negated: bool = False) -> None:
        self._profile_spec = profile_spec
        self._is_negated = is_negated

    def negated(self) -> "ManifestCondition":
        return self.__class__(self._profile_spec, is_negated=not self._is_negated)

    def describe(self) -> str:
        if self._is_negated:
            return f"DEB_BUILD_PROFILES matches *none* of [{self._profile_spec}]"
        return f"DEB_BUILD_PROFILES matches any of [{self._profile_spec}]"

    def evaluate(self, context: ConditionContext) -> bool:
        match = active_profiles_match(
            self._profile_spec, context.build_env.deb_build_profiles
        )
        return not match if self._is_negated else match


@dataclasses.dataclass(frozen=True, slots=True)
class _SingletonCondition(ManifestCondition):
    description: str
    implementation: Callable[[ConditionContext], bool]

    def describe(self) -> str:
        return self.description

    def evaluate(self, context: ConditionContext) -> bool:
        return self.implementation(context)


def _can_run_built_binaries(context: ConditionContext) -> bool:
    if not context.dpkg_architecture_variables.is_cross_compiling:
        return True
    # User / Builder asserted that we could even though we are cross-compiling, so we have to assume it is true
    return "crossbuildcanrunhostbinaries" in context.build_env.deb_build_options


_IS_CROSS_BUILDING = _SingletonCondition(
    "Cross Compiling (i.e., DEB_HOST_GNU_TYPE != DEB_BUILD_GNU_TYPE)",
    lambda c: c.dpkg_architecture_variables.is_cross_compiling,
)

_CAN_EXECUTE_COMPILED_BINARIES = _SingletonCondition(
    "Can run built binaries (natively or via transparent emulation)",
    _can_run_built_binaries,
)

_RUN_BUILD_TIME_TESTS = _SingletonCondition(
    "Run build time tests",
    lambda c: "nocheck" not in c.build_env.deb_build_options,
)

_BUILD_DOCS_BDO = _SingletonCondition(
    "Build docs (nodocs not in DEB_BUILD_OPTIONS)",
    lambda c: "nodocs" not in c.build_env.deb_build_options,
)


del _SingletonCondition
del _can_run_built_binaries
