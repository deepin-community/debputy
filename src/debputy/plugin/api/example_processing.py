import dataclasses
from enum import Enum
from typing import Set, Tuple, List, cast, Dict, Sequence

from debputy.filesystem_scan import build_virtual_fs
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.impl_types import (
    AutomaticDiscardRuleExample,
    PluginProvidedDiscardRule,
)
from debputy.util import _normalize_path


class DiscardVerdict(Enum):
    INCONSISTENT_CODE_KEPT = (
        None,
        "INCONSISTENT (code kept the path, but should have discarded)",
    )
    INCONSISTENT_CODE_DISCARDED = (
        None,
        "INCONSISTENT (code discarded the path, but should have kept it)",
    )
    KEPT = (False, "Kept")
    DISCARDED_BY_CODE = (True, "Discarded (directly by the rule)")
    DISCARDED_BY_DIRECTORY = (True, "Discarded (directory was discarded)")

    @property
    def message(self) -> str:
        return cast("str", self.value[1])

    @property
    def is_consistent(self) -> bool:
        return self.value[0] is not None

    @property
    def is_discarded(self) -> bool:
        return self.value[0] is True

    @property
    def is_kept(self) -> bool:
        return self.value[0] is False


@dataclasses.dataclass(slots=True, frozen=True)
class ProcessedDiscardRuleExample:
    rendered_paths: Sequence[Tuple[VirtualPath, DiscardVerdict]]
    inconsistent_paths: Set[VirtualPath]
    # To avoid the parents being garbage collected
    fs_root: VirtualPath


def process_discard_rule_example(
    discard_rule: PluginProvidedDiscardRule,
    example: AutomaticDiscardRuleExample,
) -> ProcessedDiscardRuleExample:
    fs_root: VirtualPath = build_virtual_fs([p for p, _ in example.content])

    actual_discarded: Dict[str, bool] = {}
    expected_output = {
        "/" + _normalize_path(p.path_name, with_prefix=False): v
        for p, v in example.content
    }
    inconsistent_paths = set()
    rendered_paths = []

    for p in fs_root.all_paths():
        parent = p.parent_dir
        discard_carry_over = False
        path_name = p.absolute
        if parent and actual_discarded[parent.absolute]:
            verdict = True
            discard_carry_over = True
        else:
            verdict = discard_rule.should_discard(p)

        actual_discarded[path_name] = verdict
        expected = expected_output.get(path_name)
        if expected is not None:
            inconsistent = expected != verdict
            if inconsistent:
                inconsistent_paths.add(p)
        else:
            continue

        if inconsistent:
            if verdict:
                verdict_code = DiscardVerdict.INCONSISTENT_CODE_DISCARDED
            else:
                verdict_code = DiscardVerdict.INCONSISTENT_CODE_KEPT
        elif verdict:
            if discard_carry_over:
                verdict_code = DiscardVerdict.DISCARDED_BY_DIRECTORY
            else:
                verdict_code = DiscardVerdict.DISCARDED_BY_CODE
        else:
            verdict_code = DiscardVerdict.KEPT
        rendered_paths.append((p, verdict_code))

    return ProcessedDiscardRuleExample(rendered_paths, inconsistent_paths, fs_root)
