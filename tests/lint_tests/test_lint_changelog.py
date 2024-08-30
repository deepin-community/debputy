import textwrap

import pytest

from debputy.lsp.lsp_debian_changelog import _lint_debian_changelog
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from lint_tests.lint_tutil import LintWrapper

from debputy.lsprotocol.types import DiagnosticSeverity


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
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    # Also fine, because d/control and d/changelog agrees
    assert not diagnostics

    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: bar

    Package: something-else
    """
    ).splitlines(keepends=True)

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


def test_dctrl_lint_historical(line_linter: LintWrapper) -> None:
    nonsense = "very very very very very very very very very very very very very very "
    lines = textwrap.dedent(
        f"""\
    foo (0.4) unstable; urgency=medium

      * A {nonsense} long line about absolute nothing that should trigger a warning about length.

     -- Niels Thykier <niels@thykier.net>  Mon, 08 Apr 2024 16:00:00 +0000

    foo (0.3) unstable; urgency=medium

      * Another entry that is not too long.

     -- Niels Thykier <niels@thykier.net>  Thu, 04 Apr 2024 00:00:00 +0000

    foo (0.2) unstable; urgency=medium

      * A {nonsense}  long line about absolute nothing that should not trigger a warning about length.

     -- Niels Thykier <niels@thykier.net>  Mon, 01 Apr 2024 00:00:00 +0000
    """
    ).splitlines(keepends=True)
    diagnostics = line_linter(lines)
    print(diagnostics)
    # This should be problematic though
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = "Line exceeds 82 characters"
    assert diag.severity == DiagnosticSeverity.Hint
    assert diag.message == msg
    assert f"{diag.range}" == "2:82-2:153"
