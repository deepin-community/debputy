import textwrap
from typing import List, Optional

import pytest

from debputy.lsp.lsp_debian_control import _lint_debian_control
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from lint_tests.lint_tutil import (
    group_diagnostics_by_severity,
    requires_levenshtein,
    LintWrapper,
)

try:
    from lsprotocol.types import Diagnostic, DiagnosticSeverity
except ImportError:
    pass


class DctrlLintWrapper(LintWrapper):

    def __call__(self, lines: List[str]) -> Optional[List["Diagnostic"]]:
        try:
            self.dctrl_lines = lines
            return super().__call__(lines)
        finally:
            self.dctrl_lines = None


@pytest.fixture
def line_linter(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lint_dctrl_parser: DctrlParser,
) -> LintWrapper:
    return DctrlLintWrapper(
        "/nowhere/debian/control",
        _lint_debian_control,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


def test_dctrl_lint(line_linter: LintWrapper) -> None:
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
def test_dctrl_lint_typos(line_linter: LintWrapper) -> None:
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
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = 'The "Build-Dpends" looks like a typo of "Build-Depends".'
    assert diag.message == msg
    assert diag.severity == DiagnosticSeverity.Warning
    assert f"{diag.range}" == "6:0-6:12"


@requires_levenshtein
def test_dctrl_lint_mx_value_with_typo(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Source: foo
    Standards-Version: 4.5.2
    Priority: optional
    Section: devel
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    # Typo of `all`
    Architecture: linux-any alle
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert len(diagnostics) == 2
    by_severity = group_diagnostics_by_severity(diagnostics)
    assert DiagnosticSeverity.Error in by_severity
    assert DiagnosticSeverity.Warning in by_severity

    typo_diag = by_severity[DiagnosticSeverity.Warning][0]
    mx_diag = by_severity[DiagnosticSeverity.Error][0]
    mx_msg = 'The value "all" cannot be used with other values.'
    typo_msg = 'It is possible that the value is a typo of "all".'
    assert mx_diag.message == mx_msg
    assert typo_diag.message == typo_msg
    assert f"{mx_diag.range}" == "10:24-10:28"
    assert f"{typo_diag.range}" == "10:24-10:28"


def test_dctrl_lint_mx_value(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Source: foo
    Standards-Version: 4.5.2
    Priority: optional
    Section: devel
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all linux-any
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = 'The value "all" cannot be used with other values.'
    assert diag.message == msg
    assert diag.severity == DiagnosticSeverity.Error
    assert f"{diag.range}" == "8:14-8:17"

    lines = textwrap.dedent(
        """\
    Source: foo
    Standards-Version: 4.5.2
    Priority: optional
    Section: devel
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: linux-any any
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = 'The value "any" cannot be used with other values.'
    assert diag.message == msg
    assert diag.severity == DiagnosticSeverity.Error
    assert f"{diag.range}" == "8:24-8:27"
