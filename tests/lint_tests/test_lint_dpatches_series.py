import textwrap

import pytest

from debputy.lsp.lsp_debian_patches_series import _lint_debian_patches_series
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.test_api import build_virtual_file_system
from lint_tests.lint_tutil import (
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
        "/nowhere/debian/patches/series",
        _lint_debian_patches_series,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


def test_dpatches_series_files_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    # Some leading comment

    ../some.patch

    .//.//./subdir/another-delta.diff # foo

    subdir/no-issues.patch # bar
    """
    ).splitlines(keepends=True)

    fs = build_virtual_file_system(
        [
            "./debian/patches/series",
            "./debian/some.patch",
            "./debian/patches/subdir/another-delta.diff",
            "./debian/patches/subdir/no-issues.patch",
        ]
    )

    line_linter.source_root = fs

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert len(diagnostics) == 2

    first_issue, second_issue = diagnostics

    msg = 'Disallowed prefix "../"'
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "2:0-2:3"
    assert first_issue.severity == DiagnosticSeverity.Error

    msg = 'Unnecessary prefix ".//.//./"'
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "4:0-4:8"
    assert second_issue.severity == DiagnosticSeverity.Warning


def test_dpatches_series_files_file_mismatch_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    # Some leading comment

    some/used-twice.patch

    some/missing-file.patch

    some/used-twice.patch
    """
    ).splitlines(keepends=True)

    fs = build_virtual_file_system(
        [
            "./debian/patches/series",
            "./debian/ignored.patch",
            "./debian/patches/some/unused-file.diff",
            "./debian/patches/some/used-twice.patch",
        ]
    )

    line_linter.source_root = fs

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert len(diagnostics) == 3

    first_issue, second_issue, third_issue = diagnostics

    msg = 'Non-existing patch "some/missing-file.patch"'
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "4:0-4:23"
    assert first_issue.severity == DiagnosticSeverity.Error

    msg = 'Duplicate patch: "some/used-twice.patch"'
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "6:0-6:21"
    assert second_issue.severity == DiagnosticSeverity.Error

    msg = 'Unused patch: "some/unused-file.diff"'
    assert third_issue.message == msg
    assert f"{third_issue.range}" == "0:0-7:22"
    assert third_issue.severity == DiagnosticSeverity.Warning


def test_dpatches_series_files_ext_lint(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    # Some leading comment

    some/ok.diff

    some/ok.patch

    some/no-extension
    """
    ).splitlines(keepends=True)

    fs = build_virtual_file_system(
        [
            "./debian/patches/series",
            "./debian/patches/some/ok.diff",
            "./debian/patches/some/ok.patch",
            "./debian/patches/some/no-extension",
        ]
    )

    line_linter.source_root = fs

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert len(diagnostics) == 1

    issue = diagnostics[0]

    msg = 'Patch not using ".patch" or ".diff" as extension: "some/no-extension"'
    assert issue.message == msg
    assert f"{issue.range}" == "6:0-6:17"
    assert issue.severity == DiagnosticSeverity.Hint
