import textwrap
from typing import List, Optional, Callable

import pytest

from debputy.lsp.lsp_debian_control import _lint_debian_control
from lint_tests.lint_tutil import (
    run_linter,
    group_diagnostics_by_severity,
    requires_levenshtein,
    exactly_one_diagnostic,
)

try:
    from lsprotocol.types import Diagnostic, DiagnosticSeverity
except ImportError:
    pass


TestLinter = Callable[[List[str]], Optional[List["Diagnostic"]]]


@pytest.fixture
def line_linter() -> TestLinter:
    path = "/nowhere/debian/control"

    def _linter(lines: List[str]) -> Optional[List["Diagnostic"]]:
        return run_linter(path, lines, _lint_debian_control)

    return _linter


def test_dctrl_lint(line_linter: TestLinter) -> None:
    lines = textwrap.dedent(
        """\
    Source: foo
    Some-Other-Field: bar
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    # Unknown section
    Section: base
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    by_severity = group_diagnostics_by_severity(diagnostics)
    # This example triggers errors and warnings, but no hint of info
    assert DiagnosticSeverity.Error in by_severity
    assert DiagnosticSeverity.Warning in by_severity

    assert DiagnosticSeverity.Hint not in by_severity
    assert DiagnosticSeverity.Information not in by_severity

    errors = by_severity[DiagnosticSeverity.Error]
    print(errors)
    assert len(errors) == 3

    first_error, second_error, third_error = errors

    msg = "Stanza is missing field Standards-Version"
    assert first_error.message == msg
    assert f"{first_error.range}" == "0:0-1:0"

    msg = "Stanza is missing field Maintainer"
    assert second_error.message == msg
    assert f"{second_error.range}" == "0:0-1:0"

    msg = "Stanza is missing field Priority"
    assert third_error.message == msg
    assert f"{third_error.range}" == "4:0-5:0"

    warnings = by_severity[DiagnosticSeverity.Warning]
    assert len(warnings) == 2

    first_warn, second_warn = warnings

    msg = "Stanza is missing field Description"
    assert first_warn.message == msg
    assert f"{first_warn.range}" == "4:0-5:0"

    msg = 'The value "base" is not supported in Section.'
    assert second_warn.message == msg
    assert f"{second_warn.range}" == "8:9-8:13"


@requires_levenshtein
def test_dctrl_lint_typos(line_linter: TestLinter) -> None:
    lines = textwrap.dedent(
        """\
    Source: foo
    Standards-Version: 4.5.2
    Priority: optional
    Section: devel
    Maintainer: Jane Developer <jane@example.com>
    # Typo
    Build-Dpends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    diag = exactly_one_diagnostic(diagnostics)

    msg = 'The "Build-Dpends" looks like a typo of "Build-Depends".'
    assert diag.message == msg
    assert diag.severity == DiagnosticSeverity.Warning
    assert f"{diag.range}" == "6:0-6:12"
