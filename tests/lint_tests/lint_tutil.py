import collections
from typing import List, Optional, Mapping, Any, Callable

import pytest

from debputy.linting.lint_util import (
    LinterImpl,
    LinterPositionCodec,
    LintStateImpl,
    LintState,
)
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet

try:
    from lsprotocol.types import Diagnostic, DiagnosticSeverity
except ImportError:
    pass


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

    def __call__(self, lines: List[str]) -> Optional[List["Diagnostic"]]:
        source_package = None
        binary_packages = None
        dctrl_lines = self.dctrl_lines
        if dctrl_lines is not None:
            source_package, binary_packages = (
                self._dctrl_parser.parse_source_debian_control(
                    dctrl_lines, ignore_errors=True
                )
            )
        state = LintStateImpl(
            self._debputy_plugin_feature_set,
            self.path,
            lines,
            source_package,
            binary_packages,
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
