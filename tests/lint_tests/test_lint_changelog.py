import textwrap
from typing import List, Optional

import pytest

from debputy.lsp.lsp_debian_changelog import _lint_debian_changelog
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


@pytest.fixture
def line_linter(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lint_dctrl_parser: DctrlParser,
) -> LintWrapper:
    return LintWrapper(
        "/nowhere/debian/changelog",
        _lint_debian_changelog,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


def test_dctrl_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    foo (0.2) unstable; urgency=medium

     * Renamed to foo
    
     -- Niels Thykier <niels@thykier.net>  Mon, 08 Apr 2024 16:00:00 +0000

    bar (0.2) unstable; urgency=medium

     * Initial release
    
     -- Niels Thykier <niels@thykier.net>  Mon, 01 Apr 2024 00:00:00 +0000
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    # Without a control file, this is fine
    assert not diagnostics

    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: foo
    
    Package: something-else
    """
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    # Also fine, because d/control and d/changelog agrees
    assert not diagnostics

    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: bar

    Package: something-else
    """
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    # This should be problematic though
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = (
        "The first entry must use the same source name as debian/control."
        ' Changelog uses: "foo" while d/control uses: "bar"'
    )
    assert diag.severity == DiagnosticSeverity.Error
    assert diag.message == msg
    assert f"{diag.range}" == "0:0-0:3"
