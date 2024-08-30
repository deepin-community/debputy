import collections
from typing import List, Optional, Mapping, Any, Callable, Sequence

import pytest

from debputy.filesystem_scan import VirtualPathBase
from debputy.linting.lint_util import (
    LinterPositionCodec,
    LintStateImpl,
    LintState,
)
from debputy.lsp.maint_prefs import (
    MaintainerPreferenceTable,
    EffectiveFormattingPreference,
)
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet

from debputy.lsprotocol.types import Diagnostic, DiagnosticSeverity, Range


try:
    from Levenshtein import distance

    HAS_LEVENSHTEIN = True
except ImportError:
    HAS_LEVENSHTEIN = False


LINTER_POSITION_CODEC = LinterPositionCodec()


class LintWrapper:

    def __init__(
        self,
        path: str,
        handler: Callable[[LintState], Optional[List[Diagnostic]]],
        debputy_plugin_feature_set: PluginProvidedFeatureSet,
        dctrl_parser: DctrlParser,
    ) -> None:
        self._debputy_plugin_feature_set = debputy_plugin_feature_set
        self._handler = handler
        self.dctrl_lines: Optional[List[str]] = None
        self.path = path
        self._dctrl_parser = dctrl_parser
        self.source_root: Optional[VirtualPathBase] = None
        self.lint_maint_preference_table = MaintainerPreferenceTable({}, {})
        self.effective_preference: Optional[EffectiveFormattingPreference] = None

    def __call__(self, lines: List[str]) -> Optional[List["Diagnostic"]]:
        source_package = None
        binary_packages = None
        dctrl_lines = self.dctrl_lines
        if dctrl_lines is not None:
            _, source_package, binary_packages = (
                self._dctrl_parser.parse_source_debian_control(
                    dctrl_lines, ignore_errors=True
                )
            )
        source_root = self.source_root
        debian_dir = source_root.get("debian") if source_root is not None else None
        state = LintStateImpl(
            self._debputy_plugin_feature_set,
            self.lint_maint_preference_table,
            source_root,
            debian_dir,
            self.path,
            "".join(dctrl_lines) if dctrl_lines is not None else "",
            lines,
            source_package,
            binary_packages,
            self.effective_preference,
        )
        return check_diagnostics(self._handler(state))


def requires_levenshtein(func: Any) -> Any:
    return pytest.mark.skipif(
        not HAS_LEVENSHTEIN, reason="Missing python3-levenshtein"
    )(func)


def check_diagnostics(
    diagnostics: Optional[List["Diagnostic"]],
) -> Optional[List["Diagnostic"]]:
    if diagnostics:
        for diagnostic in diagnostics:
            assert diagnostic.severity is not None
            assert diagnostic.source is not None
    return diagnostics


def by_range_sort_key(diagnostic: Diagnostic) -> Any:
    start_pos = diagnostic.range.start
    end_pos = diagnostic.range.end
    return start_pos.line, start_pos.character, end_pos.line, end_pos.character


def group_diagnostics_by_severity(
    diagnostics: Optional[List["Diagnostic"]],
) -> Mapping["DiagnosticSeverity", List["Diagnostic"]]:
    if not diagnostics:
        return {}

    by_severity = collections.defaultdict(list)

    for diagnostic in sorted(diagnostics, key=by_range_sort_key):
        severity = diagnostic.severity
        assert severity is not None
        by_severity[severity].append(diagnostic)

    return by_severity


def diag_range_to_text(lines: Sequence[str], range_: "Range") -> str:
    parts = []
    for line_no in range(range_.start.line, range_.end.line + 1):
        line = lines[line_no]
        chunk = line
        if line_no == range_.start.line and line_no == range_.end.line:
            chunk = line[range_.start.character : range_.end.character]
        elif line_no == range_.start.line:
            chunk = line[range_.start.character :]
        elif line_no == range_.end.line:
            chunk = line[: range_.end.character]
        parts.append(chunk)
    return "".join(parts)
