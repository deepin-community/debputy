import textwrap
from typing import List, Optional

import pytest

from debputy.lsp.lsp_debian_control import _lint_debian_control
from debputy.lsp.lsp_debian_control_reference_data import CURRENT_STANDARDS_VERSION
from debputy.packages import DctrlParser
from debputy.plugin.api import virtual_path_def
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.test_api import build_virtual_file_system
from lint_tests.lint_tutil import (
    group_diagnostics_by_severity,
    requires_levenshtein,
    LintWrapper,
    diag_range_to_text,
)

from debputy.lsprotocol.types import Diagnostic, DiagnosticSeverity
from tutil import build_time_only


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
    assert f"{second_warn.range}" == "7:9-7:13"


@requires_levenshtein
def test_dctrl_lint_typos(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Standards-Version: {CURRENT_STANDARDS_VERSION}
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
        f"""\
    Source: foo
    Standards-Version: {CURRENT_STANDARDS_VERSION}
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
    assert diagnostics is not None
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
    assert f"{mx_diag.range}" == "9:24-9:28"
    assert f"{typo_diag.range}" == "9:24-9:28"


def test_dctrl_lint_mx_value(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Standards-Version: {CURRENT_STANDARDS_VERSION}
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
        f"""\
    Source: foo
    Standards-Version: {CURRENT_STANDARDS_VERSION}
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


def test_dctrl_lint_dup_sep(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar,
     , baz
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
    error = diagnostics[0]

    msg = "Duplicate separator"
    assert error.message == msg
    assert f"{error.range}" == "10:1-10:2"
    assert error.severity == DiagnosticSeverity.Error


def test_dctrl_lint_ma(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Multi-Arch: same
    Depends: bar, baz
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
    error = diagnostics[0]

    msg = "Multi-Arch: same is not valid for Architecture: all packages. Maybe you want foreign?"
    assert error.message == msg
    assert f"{error.range}" == "9:12-9:16"
    assert error.severity == DiagnosticSeverity.Error


def test_dctrl_lint_udeb(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    XB-Installer-Menu-Item: 1234
    Depends: bar, baz
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.

    Package: bar-udeb
    Architecture: all
    Section: debian-installer
    Package-Type: udeb
    XB-Installer-Menu-Item: golf
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 2
    first, second = diagnostics

    msg = "The XB-Installer-Menu-Item field is only applicable to udeb packages (`Package-Type: udeb`)"
    assert first.message == msg
    assert f"{first.range}" == "9:0-9:22"
    assert first.severity == DiagnosticSeverity.Warning

    msg = r'The value "golf" does not match the regex ^[1-9]\d{3,4}$.'
    assert second.message == msg
    assert f"{second.range}" == "21:24-21:28"
    assert second.severity == DiagnosticSeverity.Error


def test_dctrl_lint_arch_only_fields(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    X-DH-Build-For-Type: target
    Depends: bar, baz
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
    issue = diagnostics[0]

    msg = "The X-DH-Build-For-Type field is not applicable to arch:all packages (`Architecture: all`)"
    assert issue.message == msg
    assert f"{issue.range}" == "9:0-9:19"
    assert issue.severity == DiagnosticSeverity.Warning


def test_dctrl_lint_sv(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: 4.6.2
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
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
    issue = diagnostics[0]

    msg = f"Latest Standards-Version is {CURRENT_STANDARDS_VERSION}"
    assert issue.message == msg
    assert f"{issue.range}" == "3:19-3:24"
    assert issue.severity == DiagnosticSeverity.Information

    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: Golf
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
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
    issue = diagnostics[0]

    msg = f'Not a valid version. Current version is "{CURRENT_STANDARDS_VERSION}"'
    assert issue.message == msg
    assert f"{issue.range}" == "3:19-3:23"
    assert issue.severity == DiagnosticSeverity.Warning

    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}.0
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
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
    issue = diagnostics[0]

    msg = "Unnecessary version segment. This part of the version is only used for editorial changes"
    assert issue.message == msg
    assert f"{issue.range}" == "3:24-3:26"
    assert issue.severity == DiagnosticSeverity.Information


def test_dctrl_lint_sv_udeb_only(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo-udeb
    Architecture: all
    Package-Type: udeb
    Section: debian-installer
    Depends: bar, baz
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert not diagnostics


def test_dctrl_lint_udeb_menu_iten(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    Source: foo
    Section: devel
    Priority: optional
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo-udeb
    Architecture: all
    Package-Type: udeb
    Section: debian-installer
    XB-Installer-Menu-Item: 12345
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.

    Package: bar-udeb
    Architecture: all
    Package-Type: udeb
    Section: debian-installer
    XB-Installer-Menu-Item: ${foo}
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert not diagnostics


def test_dctrl_lint_multiple_vcs(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)
    Vcs-Git: https://salsa.debian.org/debian/foo
    Vcs-Svn: https://svn.debian.org/debian/foo
    Vcs-Browser: https://salsa.debian.org/debian/foo

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: Some very interesting synopsis
     A very interesting description
     that spans multiple lines
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 2
    first_issue, second_issue = diagnostics

    msg = f'Multiple Version Control fields defined ("Vcs-Git")'
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "6:0-7:0"
    assert first_issue.severity == DiagnosticSeverity.Warning

    msg = f'Multiple Version Control fields defined ("Vcs-Svn")'
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "7:0-8:0"
    assert second_issue.severity == DiagnosticSeverity.Warning


def test_dctrl_lint_synopsis_empty(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description:
     A very interesting description
     without a synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = "Package synopsis is missing"
    assert issue.message == msg
    assert f"{issue.range}" == "10:0-10:11"
    assert issue.severity == DiagnosticSeverity.Warning


def test_dctrl_lint_synopsis_basis(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: The synopsis is not the best because it starts with an article and also the synopsis goes on and on
     A very interesting description
     with a poor synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 2
    first_issue, second_issue = diagnostics

    msg = "Package synopsis starts with an article (a/an/the)."
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "10:13-10:16"
    assert first_issue.severity == DiagnosticSeverity.Warning

    msg = "Package synopsis is too long."
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "10:92-10:112"
    assert second_issue.severity == DiagnosticSeverity.Warning


def test_dctrl_lint_synopsis_template(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: <insert up to 60 chars description>
     A very interesting description
     with a poor synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = "Package synopsis is a placeholder"
    assert issue.message == msg
    assert f"{issue.range}" == "10:13-10:48"
    assert issue.severity == DiagnosticSeverity.Warning


def test_dctrl_lint_synopsis_too_short(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: short
     A very interesting description
     with a poor synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = "Package synopsis is too short"
    assert issue.message == msg
    assert f"{issue.range}" == "10:13-10:18"
    assert issue.severity == DiagnosticSeverity.Warning


@build_time_only
def test_dctrl_lint_ambiguous_pkgfile(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    # FIXME: This relies on "cwd" being a valid debian directory using debhelper. Fix and
    # remove the `build_time_only` restriction
    line_linter.source_root = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="."),
            "./debian/bar.service",
        ]
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = (
        'Possible typo in "./debian/bar.service". Consider renaming the file to "debian/foo.service"'
        ' (or maybe "debian/foo.bar.service") if it is intended for foo'
    )
    assert issue.message == msg
    assert f"{issue.range}" == "7:0-8:0"
    assert issue.severity == DiagnosticSeverity.Warning
    diag_data = issue.data
    assert isinstance(diag_data, dict)
    assert diag_data.get("report_for_related_file") in (
        "./debian/bar.service",
        "debian/bar.service",
    )


@build_time_only
def test_dctrl_lint_ambiguous_pkgfile_no_name_segment(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13), dh-sequence-zz-debputy,

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    # FIXME: This relies on "cwd" being a valid debian directory using debhelper. Fix and
    # remove the `build_time_only` restriction
    line_linter.source_root = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="."),
            "./debian/bar.alternatives",
        ]
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = (
        'Possible typo in "./debian/bar.alternatives". Consider renaming the file to "debian/foo.alternatives"'
        " if it is intended for foo"
    )
    assert issue.message == msg
    assert f"{issue.range}" == "7:0-8:0"
    assert issue.severity == DiagnosticSeverity.Warning
    diag_data = issue.data
    assert isinstance(diag_data, dict)
    assert diag_data.get("report_for_related_file") in (
        "./debian/bar.alternatives",
        "debian/bar.alternatives",
    )


@requires_levenshtein
@build_time_only
def test_dctrl_lint_stem_typo_pkgfile(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    # FIXME: This relies on "cwd" being a valid debian directory using debhelper. Fix and
    # remove the `build_time_only` restriction
    line_linter.source_root = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="."),
            "./debian/foo.intsall",
        ]
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = 'The file "./debian/foo.intsall" is likely a typo of "./debian/foo.install"'
    assert issue.message == msg
    assert f"{issue.range}" == "7:0-8:0"
    assert issue.severity == DiagnosticSeverity.Warning
    diag_data = issue.data
    assert isinstance(diag_data, dict)
    assert diag_data.get("report_for_related_file") in (
        "./debian/foo.intsall",
        "debian/foo.intsall",
    )


@build_time_only
def test_dctrl_lint_stem_inactive_pkgfile_fp(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13), dh-sequence-zz-debputy,

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    # FIXME: This relies on "cwd" being a valid debian directory using debhelper. Fix and
    # remove the `build_time_only` restriction
    #
    # Note: The "positive" test of this one is missing; suspect because it cannot (reliably)
    # load the `zz-debputy` sequence.
    line_linter.source_root = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="."),
            "./debian/foo.install",
            virtual_path_def(
                "./debian/rules",
                content=textwrap.dedent(
                    """\
            #! /usr/bin/make -f

            binary binary-arch binary-indep build build-arch build-indep clean:
                foo $@
            """
                ),
            ),
        ]
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    # We should not emit diagnostics when the package is not using dh!
    assert not diagnostics


@requires_levenshtein
@build_time_only
def test_dctrl_lint_stem_typo_pkgfile_ignored_exts_or_files(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    # FIXME: This relies on "cwd" being a valid debian directory using debhelper. Fix and
    # remove the `build_time_only` restriction
    line_linter.source_root = build_virtual_file_system(
        [
            virtual_path_def(".", fs_path="."),
            "debian/salsa-ci.yml",
            "debian/gbp.conf",
            "debian/foo.conf",
            "debian/foo.sh",
            "debian/foo.yml",
            # One wrong one to ensure the test works.
            "debian/foo.intsall",
        ]
    )

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = 'The file "./debian/foo.intsall" is likely a typo of "./debian/foo.install"'
    assert issue.message == msg
    assert f"{issue.range}" == "7:0-8:0"
    assert issue.severity == DiagnosticSeverity.Warning
    diag_data = issue.data
    assert isinstance(diag_data, dict)
    assert diag_data.get("report_for_related_file") in (
        "./debian/foo.intsall",
        "debian/foo.intsall",
    )


def test_dctrl_lint_dep_field_missing_sep(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    # Missing separator between baz and libfubar1
     libfubar1,
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]
    msg = (
        "Trailing data after a relationship that might be a second relationship."
        " Is a separator missing before this part?"
    )
    problem_text = diag_range_to_text(lines, issue.range)
    assert issue.message == msg
    assert problem_text == "libfubar1"
    assert f"{issue.range}" == "11:1-11:10"
    assert issue.severity == DiagnosticSeverity.Error


def test_dctrl_lint_dep_field_missing_sep_or_syntax_error(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz
    # Missing separator between baz and libfubar1
     _libfubar1,
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]
    msg = "Parse error of the relationship. Either a syntax error or a missing separator somewhere."
    problem_text = diag_range_to_text(lines, issue.range)
    assert issue.message == msg
    assert problem_text == "_libfubar1"
    assert f"{issue.range}" == "11:1-11:11"
    assert issue.severity == DiagnosticSeverity.Error


def test_dctrl_lint_dep_field_completely_busted(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: bar, baz, _asd
    # This is just busted
     _libfubar1,
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]
    msg = 'Could not parse "_asd _libfubar1" as a dependency relation.'
    problem_text = diag_range_to_text(lines, issue.range)
    expected_problem_text = "\n".join((" _asd", "# This is just busted", " _libfubar1"))
    assert issue.message == msg
    assert problem_text == expected_problem_text
    assert f"{issue.range}" == "9:18-11:11"
    assert issue.severity == DiagnosticSeverity.Error


def test_dctrl_lint_dep_field_completely_busted_first_line(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    # A wild field comment appeared!
    Depends: _bar,
     asd,
    # This is fine (but the _bar part is not)
     libfubar1,
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]
    msg = 'Could not parse "_bar" as a dependency relation.'
    problem_text = diag_range_to_text(lines, issue.range)
    assert issue.message == msg
    assert problem_text == " _bar"
    assert f"{issue.range}" == "10:8-10:13"
    assert issue.severity == DiagnosticSeverity.Error


def test_dctrl_lint_dep_field_restricted_operator(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    # Some random field comment
    Provides: bar (>= 2),
     bar
    # Inline comment to spice up things
     (<= 1),
    # This one is valid
     fubar (= 2),
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 2
    first_issue, second_issue = diagnostics

    msg = 'The version operator ">=" is not allowed in Provides'
    problem_text = diag_range_to_text(lines, first_issue.range)
    assert first_issue.message == msg
    assert problem_text == ">="
    assert f"{first_issue.range}" == "10:15-10:17"
    assert first_issue.severity == DiagnosticSeverity.Error

    msg = 'The version operator "<=" is not allowed in Provides'
    problem_text = diag_range_to_text(lines, second_issue.range)
    assert second_issue.message == msg
    assert problem_text == "<="
    assert f"{second_issue.range}" == "13:2-13:4"
    assert second_issue.severity == DiagnosticSeverity.Error


def test_dctrl_lint_dep_field_restricted_or_relations(
    line_linter: LintWrapper,
) -> None:
    lines = textwrap.dedent(
        f"""\
    Source: foo
    Section: devel
    Priority: optional
    Standards-Version: {CURRENT_STANDARDS_VERSION}
    Maintainer: Jane Developer <jane@example.com>
    Build-Depends: debhelper-compat (= 13)

    Package: foo
    Architecture: all
    Depends: pkg-a
     | pkg-b
    # What goes in Depends do not always work in Provides
    Provides: foo-a
     | foo-b
    Description: some short synopsis
     A very interesting description
     with a valid synopsis
     .
     Just so be clear, this is for a test.
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    print(diagnostics)
    assert diagnostics and len(diagnostics) == 1
    issue = diagnostics[0]

    msg = 'The field Provides does not support "|" (OR) in relations.'
    problem_text = diag_range_to_text(lines, issue.range)
    assert issue.message == msg
    assert problem_text == "|"
    assert f"{issue.range}" == "13:1-13:2"
    assert issue.severity == DiagnosticSeverity.Error


def test_dctrl_duplicate_key(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        f"""\
        Source: jquery-tablesorter
        Section: javascript
        Priority: optional
        Maintainer: Debian Javascript Maintainers <pkg-javascript-devel@lists.alioth.de\
        bian.org>
        Uploaders: Paul Gevers <elbrus@debian.org>
        Build-Depends:
         debhelper-compat (=13),
         grunt,
         libjs-qunit,
         node-grunt-contrib-clean,
         node-grunt-contrib-copy,
         node-grunt-contrib-uglify,
         node-grunt-contrib-concat,
        Standards-Version: {CURRENT_STANDARDS_VERSION}
        Homepage: https://github.com/Mottie/tablesorter
        Vcs-Git: https://salsa.debian.org/js-team/jquery-tablesorter.git
        Vcs-Browser: https://salsa.debian.org/js-team/jquery-tablesorter
        Rules-Requires-Root: no

        Package: libjs-jquery-tablesorter
        Architecture: all
        Multi-Arch: foreign
        Depends:
         ${{misc:Depends}},
         libjs-jquery,
         libjs-jquery-metadata,
        Recommends: javascript-common
        Multi-Arch: foreign
        Description: jQuery flexible client-side table sorting plugin
         Tablesorter is a jQuery plugin for turning a standard HTML table with THEAD
         and TBODY tags into a sortable table without page refreshes. Tablesorter can
         successfully parse and sort many types of data including linked data in a
         cell. It has many useful features including:
         .
           * Multi-column alphanumeric sorting and filtering.
           * Multi-tbody sorting
           * Supports Bootstrap v2-4.
           * Parsers for sorting text, alphanumeric text, URIs, integers, currency,
             floats, IP addresses, dates (ISO, long and short formats) and time.
             Add your own easily.
           * Inline editing
           * Support for ROWSPAN and COLSPAN on TH elements.
           * Support secondary "hidden" sorting (e.g., maintain alphabetical sort when
             sorting on other criteria).
           * Extensibility via widget system.
           * Cross-browser: IE 6.0+, FF 2+, Safari 2.0+, Opera 9.0+, Chrome 5.0+.

    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    assert len(diagnostics) == 1

    issue = diagnostics[0]

    msg = (
        "The Multi-Arch field name was used multiple times in this stanza."
        " Please ensure the field is only used once per stanza. Note that Multi-Arch and"
        " X[BCS]-Multi-Arch are considered the same field."
    )
    assert issue.message == msg
    assert f"{issue.range}" == "27:0-27:10"
    assert issue.severity == DiagnosticSeverity.Error
