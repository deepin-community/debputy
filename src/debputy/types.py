import dataclasses
from typing import (
    TypeVar,
    TYPE_CHECKING,
    Sequence,
    Tuple,
    Mapping,
    Dict,
    Optional,
    TypedDict,
    NotRequired,
    List,
    MutableMapping,
)

if TYPE_CHECKING:
    from debputy.plugin.api import VirtualPath
    from debputy.filesystem_scan import FSPath

    VP = TypeVar("VP", VirtualPath, FSPath)
    S = TypeVar("S", str, bytes)
else:
    VP = TypeVar("VP", "VirtualPath", "FSPath")
    S = TypeVar("S", str, bytes)


class EnvironmentModificationSerialized(TypedDict):
    replacements: NotRequired[Dict[str, str]]
    removals: NotRequired[List[str]]


@dataclasses.dataclass(slots=True, frozen=True)
class EnvironmentModification:
    replacements: Sequence[Tuple[str, str]] = tuple()
    removals: Sequence[str] = tuple()

    def __bool__(self) -> bool:
        return not self.removals and not self.replacements

    def combine(
        self,
        other: "Optional[EnvironmentModification]",
    ) -> "EnvironmentModification":
        if not other:
            return self
        existing_replacements = {k: v for k, v in self.replacements}
        extra_replacements = {
            k: v
            for k, v in other.replacements
            if k not in existing_replacements or existing_replacements[k] != v
        }
        seen_removals = set(self.removals)
        extra_removals = [r for r in other.removals if r not in seen_removals]

        if not extra_replacements and isinstance(self.replacements, tuple):
            new_replacements = self.replacements
        else:
            new_replacements = []
            for k, v in existing_replacements:
                if k not in extra_replacements:
                    new_replacements.append((k, v))

            for k, v in other.replacements:
                if k in extra_replacements:
                    new_replacements.append((k, v))

            new_replacements = tuple(new_replacements)

        if not extra_removals and isinstance(self.removals, tuple):
            new_removals = self.removals
        else:
            new_removals = list(self.removals)
            new_removals.extend(extra_removals)
            new_removals = tuple(new_removals)

        if self.replacements is new_replacements and self.removals is new_removals:
            return self

        return EnvironmentModification(
            new_replacements,
            new_removals,
        )

    def update_inplace(self, env: MutableMapping[str, str]) -> None:
        for k, v in self.replacements:
            existing_value = env.get(k)
            if v == existing_value:
                continue
            env[k] = v

        for k in self.removals:
            if k not in env:
                continue
            del env[k]

    def compute_env(self, base_env: Mapping[str, str]) -> Mapping[str, str]:
        updated_env: Optional[Dict[str, str]] = None
        for k, v in self.replacements:
            existing_value = base_env.get(k)
            if v == existing_value:
                continue

            if updated_env is None:
                updated_env = dict(base_env)
            updated_env[k] = v

        for k in self.removals:
            if k not in base_env:
                continue
            if updated_env is None:
                updated_env = dict(base_env)
            del updated_env[k]

        if updated_env is not None:
            return updated_env
        return base_env
