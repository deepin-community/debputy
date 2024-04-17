from typing import List, Optional, Callable

import pytest

from debputy.packages import DctrlParser
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from lint_tests.lint_tutil import (
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


def test_debputy_lint_conflicting_keys(line_linter: LintWrapper) -> None:
    lines = textwrap.dedent(
        """\
    manifest-version: 0.1
    installations:
    - install-docs:
        sources:
        - foo
        - bar
        as: baz      # Conflicts with "sources" (#85)
    - install:
        source: foo
        sources:     # Conflicts with "source" (#85)
        - bar
        - baz
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

    msg = 'The "sources" cannot be used with "as".'
    assert first_error.message == msg
    assert f"{first_error.range}" == "3:4-3:11"

    msg = 'The "as" cannot be used with "sources".'
    assert second_error.message == msg
    assert f"{second_error.range}" == "6:4-6:6"

    msg = 'The "source" cannot be used with "sources".'
    assert third_error.message == msg
    assert f"{third_error.range}" == "8:4-8:10"

    msg = 'The "sources" cannot be used with "source".'
    assert fourth_error.message == msg
    assert f"{fourth_error.range}" == "9:4-9:11"


import textwrap
from typing import List, Optional, Callable

import pytest

from debputy.lsp.lsp_debian_debputy_manifest import _lint_debian_debputy_manifest
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

TestLintWrapper = Callable[[List[str]], Optional[List["Diagnostic"]]]


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
