import collections
from typing import List, Optional, Mapping, Any

import pytest

from debputy.linting.lint_util import LinterImpl, LinterPositionCodec

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


def requires_levenshtein(func: Any) -> Any:
    return pytest.mark.skipif(
        not HAS_LEVENSHTEIN, reason="Missing python3-levenshtein"
    )(func)


def _check_diagnostics(
    diagnostics: Optional[List["Diagnostic"]],
) -> Optional[List["Diagnostic"]]:
    if diagnostics:
        for diagnostic in diagnostics:
            assert diagnostic.severity is not None
    return diagnostics


def run_linter(
    path: str, lines: List[str], linter: LinterImpl
) -> Optional[List["Diagnostic"]]:
    uri = f"file://{path}"
    return _check_diagnostics(linter(uri, path, lines, LINTER_POSITION_CODEC))


def exactly_one_diagnostic(diagnostics: Optional[List["Diagnostic"]]) -> "Diagnostic":
    assert diagnostics and len(diagnostics) == 1
    return diagnostics[0]


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
