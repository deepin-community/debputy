import textwrap

import pytest

from debputy.lsp.lsp_debian_debputy_manifest import _lint_debian_debputy_manifest
from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from lint_tests.lint_tutil import (
    requires_levenshtein,
    LintWrapper,
    group_diagnostics_by_severity,
)

from debputy.lsprotocol.types import DiagnosticSeverity


@pytest.fixture
def line_linter(
    debputy_plugin_feature_set: PluginProvidedFeatureSet,
    lint_dctrl_parser: DctrlParser,
) -> LintWrapper:
    return LintWrapper(
        "/nowhere/debian/debputy.manifest",
        _lint_debian_debputy_manifest,
        debputy_plugin_feature_set,
        lint_dctrl_parser,
    )


def test_debputy_lint_unknown_keys(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    installations:
    - install-something:
        sources:
        - abc
        - def
    - install-docs:
        source: foo
        puff: true   # Unknown keyword (assuming install-docs)
        when:
          negated: cross-compiling
    - install-docs:
        source: bar
        when: ross-compiling  # Typo of "cross-compiling"; FIXME not caught
    packages:
      foo:
        blah: qwe    # Unknown keyword
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    by_severity = group_diagnostics_by_severity(diagnostics)
    # This example triggers errors only
    assert DiagnosticSeverity.Error in by_severity

    assert DiagnosticSeverity.Warning not in by_severity
    assert DiagnosticSeverity.Hint not in by_severity
    assert DiagnosticSeverity.Information not in by_severity

    errors = by_severity[DiagnosticSeverity.Error]
    print(errors)
    assert len(errors) == 4

    first_error, second_error, third_error, fourth_error = errors

    msg = 'Unknown or unsupported key "install-something".'
    assert first_error.message == msg
    assert f"{first_error.range}" == "2:2-2:19"

    msg = 'Unknown or unsupported key "puff".'
    assert second_error.message == msg
    assert f"{second_error.range}" == "8:4-8:8"

    msg = 'Unknown or unsupported key "negated".'
    assert third_error.message == msg
    assert f"{third_error.range}" == "10:6-10:13"

    msg = 'Unknown or unsupported key "blah".'
    assert fourth_error.message == msg
    assert f"{fourth_error.range}" == "16:4-16:8"


def test_debputy_lint_null_keys(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: '0.1'
    installations:
    - install-docs:
        :
        - GETTING-STARTED-WITH-dh-debputy.md
        - MANIFEST-FORMAT.md
        - MIGRATING-A-DH-PLUGIN.md
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    assert len(diagnostics) == 1
    issue = diagnostics[0]

    msg = "Missing key"
    assert issue.message == msg
    assert f"{issue.range}" == "3:4-3:5"
    assert issue.severity == DiagnosticSeverity.Error


@requires_levenshtein
def test_debputy_lint_unknown_keys_spelling(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    installations:
    - install-dcoss:  # typo
        sources:
        - abc
        - def
        puff: true   # Unknown keyword (assuming install-docs)
        when:
          nut: cross-compiling  # Typo of "not"
    - install-docs:
        source: bar
        when: ross-compiling  # Typo of "cross-compiling"; FIXME not caught
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    by_severity = group_diagnostics_by_severity(diagnostics)
    # This example triggers errors only
    assert DiagnosticSeverity.Error in by_severity

    assert DiagnosticSeverity.Warning not in by_severity
    assert DiagnosticSeverity.Hint not in by_severity
    assert DiagnosticSeverity.Information not in by_severity

    errors = by_severity[DiagnosticSeverity.Error]
    print(errors)
    assert len(errors) == 3

    first_error, second_error, third_error = errors

    msg = 'Unknown or unsupported key "install-dcoss". It looks like a typo of "install-docs".'
    assert first_error.message == msg
    assert f"{first_error.range}" == "2:2-2:15"

    msg = 'Unknown or unsupported key "puff".'
    assert second_error.message == msg
    assert f"{second_error.range}" == "6:4-6:8"

    msg = 'Unknown or unsupported key "nut". It looks like a typo of "not".'
    assert third_error.message == msg
    assert f"{third_error.range}" == "8:6-8:9"


def test_debputy_lint_check_package_names(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    packages:
        unknown-package:
            binary-version: '1:{{DEB_VERSION_UPSTREAM_REVISION}}'
    """
    ).splitlines(keepends=True)

    line_linter.dctrl_lines = None
    diagnostics = line_linter(lines)
    print(diagnostics)
    # Does nothing without a control file
    assert not diagnostics

    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: foo

    Package: foo
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    assert diagnostics and len(diagnostics) == 1
    diag = diagnostics[0]

    msg = 'Unknown package "unknown-package".'
    assert diag.message == msg
    assert f"{diag.range}" == "2:4-2:19"


def test_debputy_lint_integration_mode(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    installations: []
    packages:
        foo:
            services:
            - service: foo
    """
    ).splitlines(keepends=True)
    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: dh-sequence-zz-debputy-rrr,

    Package: foo
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    assert diagnostics and len(diagnostics) == 2
    first_issue, second_issue = diagnostics

    msg = 'Feature "installations" not supported in integration mode dh-sequence-zz-debputy-rrr'
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "1:0-1:13"
    assert first_issue.severity == DiagnosticSeverity.Error

    msg = 'Feature "services" not supported in integration mode dh-sequence-zz-debputy-rrr'
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "4:8-4:16"
    assert second_issue.severity == DiagnosticSeverity.Error

    # Changing the integration mode should fix both
    line_linter.dctrl_lines = textwrap.dedent(
        """\
    Source: foo
    Build-Depends: dh-sequence-zz-debputy,

    Package: foo
    """
    ).splitlines(keepends=True)
    diagnostics = line_linter(lines)
    assert not diagnostics


def test_debputy_lint_attr_value_checks(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    packages:
        foo:
            services:
            - service: foo
              enable-on-install: "true"
              on-upgrade: "bar"
    """
    ).splitlines(keepends=True)

    diagnostics = line_linter(lines)
    assert diagnostics and len(diagnostics) == 2
    first_issue, second_issue = diagnostics

    msg = 'Not a supported value for "enable-on-install"'
    assert first_issue.message == msg
    assert f"{first_issue.range}" == "5:29-5:35"
    assert first_issue.severity == DiagnosticSeverity.Error

    msg = 'Not a supported value for "on-upgrade"'
    assert second_issue.message == msg
    assert f"{second_issue.range}" == "6:22-6:27"
    assert second_issue.severity == DiagnosticSeverity.Error
