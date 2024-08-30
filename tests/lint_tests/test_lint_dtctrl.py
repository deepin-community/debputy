import textwrap

import pytest

from debputy.lsp.lsp_debian_control_reference_data import CURRENT_STANDARDS_VERSION
from debputy.lsp.lsp_debian_tests_control import _lint_debian_tests_control
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from lint_tests.lint_tutil import (
    requires_levenshtein,
    LintWrapper,
)


@pytest.fixture
def line_linter(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lint_dctrl_parser: DctrlParser,
) -> LintWrapper:
    return LintWrapper(
        "/nowhere/debian/tests/control",
        _lint_debian_tests_control,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


@requires_levenshtein
def test_dtctrl_lint_live_example_silx(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Tests: no-opencl
    Depends:
     @,
     python3-all,
     python3-pytest,
     python3-pytest-mock,
     python3-pytest-xvfb,
     xauth,
     xvfb,
    Restrictions: allow-stderr

    Tests: opencl
    Depends:
     @,
     clinfo,
     python3-all,
     python3-pytest,
     python3-pytest-mock,
     python3-pytest-xvfb,
     xauth,
     xvfb,
    Architecture: !i386
    Restrictions: allow-stderr

    Test-Command: xvfb-run -s "-screen 0 1024x768x24 -ac +extension GLX +render -noreset" sh debian/tests/gui
    Depends:
     mesa-utils,
     silx,
     xauth,
     xvfb,
    Restrictions: allow-stderr
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert not diagnostics
