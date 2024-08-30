import textwrap

import pytest

from debputy.lsp.lsp_debian_copyright import _lint_debian_copyright
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.test_api import build_virtual_file_system
from lint_tests.lint_tutil import (
    group_diagnostics_by_severity,
    LintWrapper,
)

from debputy.lsprotocol.types import DiagnosticSeverity


@pytest.fixture
def line_linter(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lint_dctrl_parser: DctrlParser,
) -> LintWrapper:
    return LintWrapper(
        "/nowhere/debian/copyright",
        _lint_debian_copyright,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


def test_dcpy_files_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/

    Files: foo .//unnecessary///many/slashes
    Copyright: Noone <noone@example.com>
    License: something
     yada yada yada
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    by_severity = group_diagnostics_by_severity(diagnostics)
    assert DiagnosticSeverity.Warning in by_severity

    assert DiagnosticSeverity.Error not in by_severity
    assert DiagnosticSeverity.Hint not in by_severity
    assert DiagnosticSeverity.Information not in by_severity

    warnings = by_severity[DiagnosticSeverity.Warning]
    print(warnings)
    assert len(warnings) == 2

    first_warn, second_warn = warnings

    msg = 'Unnecessary prefix ".//"'
    assert first_warn.message == msg
    assert f"{first_warn.range}" == "2:11-2:14"

    msg = 'Simplify to a single "/"'
    assert second_warn.message == msg
    assert f"{second_warn.range}" == "2:25-2:28"


def test_dcpy_files_matches_dir_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/

    Files: foo
    Copyright: Noone <noone@example.com>
    License: something
     yada yada yada
    """
    ).splitlines(keepends=True)

    source_root = build_virtual_file_system(["./foo/bar"])
    line_linter.source_root = source_root

    diagnostics = line_linter(lines)
    assert len(diagnostics) == 1
    issue = diagnostics[0]

    msg = "Directories cannot be a match. Use `dir/*` to match everything in it"
    assert issue.message == msg
    assert f"{issue.range}" == "2:7-2:10"
    assert issue.severity == DiagnosticSeverity.Warning
