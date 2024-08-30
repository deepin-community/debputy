import dataclasses
import os
import stat
import subprocess
import sys
import textwrap
from typing import Optional, List, Union, NoReturn, Mapping

from debputy.commands.debputy_cmd.context import CommandContext
from debputy.commands.debputy_cmd.output import _output_styling, OutputStylingBase
from debputy.filesystem_scan import FSROOverlay
from debputy.linting.lint_util import (
    LinterImpl,
    LintReport,
    LintStateImpl,
    FormatterImpl,
    TermLintReport,
    LintDiagnosticResultState,
)
from debputy.lsp.lsp_debian_changelog import _lint_debian_changelog
from debputy.lsp.lsp_debian_control import (
    _lint_debian_control,
    _reformat_debian_control,
)
from debputy.lsp.lsp_debian_copyright import (
    _lint_debian_copyright,
    _reformat_debian_copyright,
)
from debputy.lsp.lsp_debian_debputy_manifest import _lint_debian_debputy_manifest
from debputy.lsp.lsp_debian_patches_series import _lint_debian_patches_series
from debputy.lsp.lsp_debian_rules import _lint_debian_rules_impl
from debputy.lsp.lsp_debian_tests_control import (
    _lint_debian_tests_control,
    _reformat_debian_tests_control,
)
from debputy.lsp.maint_prefs import (
    MaintainerPreferenceTable,
    EffectiveFormattingPreference,
    determine_effective_preference,
)
from debputy.lsp.quickfixes import provide_standard_quickfixes_from_diagnostics
from debputy.lsp.spellchecking import disable_spellchecking
from debputy.lsp.text_edit import (
    get_well_formatted_edit,
    merge_sort_text_edits,
    apply_text_edits,
    OverLappingTextEditException,
)
from debputy.lsp.vendoring._deb822_repro import Deb822FileElement
from debputy.lsprotocol.types import (
    CodeAction,
    Command,
    CodeActionParams,
    CodeActionContext,
    TextDocumentIdentifier,
    TextEdit,
    Position,
    DiagnosticSeverity,
    Diagnostic,
)
from debputy.packages import SourcePackage, BinaryPackage
from debputy.plugin.api import VirtualPath
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.util import _warn, _error, _info
from debputy.yaml import MANIFEST_YAML, YAMLError
from debputy.yaml.compat import CommentedMap

LINTER_FORMATS = {
    "debian/changelog": _lint_debian_changelog,
    "debian/control": _lint_debian_control,
    "debian/copyright": _lint_debian_copyright,
    "debian/debputy.manifest": _lint_debian_debputy_manifest,
    "debian/rules": _lint_debian_rules_impl,
    "debian/patches/series": _lint_debian_patches_series,
    "debian/tests/control": _lint_debian_tests_control,
}


REFORMAT_FORMATS = {
    "debian/control": _reformat_debian_control,
    "debian/copyright": _reformat_debian_copyright,
    "debian/tests/control": _reformat_debian_tests_control,
}


@dataclasses.dataclass(slots=True)
class LintContext:
    plugin_feature_set: PluginProvidedFeatureSet
    maint_preference_table: MaintainerPreferenceTable
    source_root: Optional[VirtualPath]
    debian_dir: Optional[VirtualPath]
    parsed_deb822_file_content: Optional[Deb822FileElement] = None
    source_package: Optional[SourcePackage] = None
    binary_packages: Optional[Mapping[str, BinaryPackage]] = None
    effective_preference: Optional[EffectiveFormattingPreference] = None
    style_tool: Optional[str] = None
    unsupported_preference_reason: Optional[str] = None
    salsa_ci: Optional[CommentedMap] = None

    def state_for(self, path: str, content: str, lines: List[str]) -> LintStateImpl:
        return LintStateImpl(
            self.plugin_feature_set,
            self.maint_preference_table,
            self.source_root,
            self.debian_dir,
            path,
            content,
            lines,
            self.source_package,
            self.binary_packages,
            self.effective_preference,
        )


def gather_lint_info(context: CommandContext) -> LintContext:
    source_root = FSROOverlay.create_root_dir(".", ".")
    debian_dir = source_root.get("debian")
    if debian_dir is not None and not debian_dir.is_dir:
        debian_dir = None
    lint_context = LintContext(
        context.load_plugins(),
        MaintainerPreferenceTable.load_preferences(),
        source_root,
        debian_dir,
    )
    try:
        with open("debian/control") as fd:
            deb822_file, source_package, binary_packages = (
                context.dctrl_parser.parse_source_debian_control(fd, ignore_errors=True)
            )
    except FileNotFoundError:
        source_package = None
    else:
        lint_context.parsed_deb822_file_content = deb822_file
        lint_context.source_package = source_package
        lint_context.binary_packages = binary_packages
    salsa_ci_map: Optional[CommentedMap] = None
    for ci_file in ("debian/salsa-ci.yml", ".gitlab-ci.yml"):
        try:
            with open(ci_file) as fd:
                salsa_ci_map = MANIFEST_YAML.load(fd)
                if not isinstance(salsa_ci_map, CommentedMap):
                    salsa_ci_map = None
                break
        except FileNotFoundError:
            pass
        except YAMLError:
            break
    if source_package is not None or salsa_ci_map is not None:
        pref, tool, pref_reason = determine_effective_preference(
            lint_context.maint_preference_table,
            source_package,
            salsa_ci_map,
        )
        lint_context.effective_preference = pref
        lint_context.style_tool = tool
        lint_context.unsupported_preference_reason = pref_reason

    return lint_context


def initialize_lint_report(context: CommandContext) -> LintReport:
    lint_report_format = context.parsed_args.lint_report_format
    report_output = context.parsed_args.report_output

    if lint_report_format == "term":
        fo = _output_styling(context.parsed_args, sys.stdout)
        if report_output is not None:
            _warn("--report-output is redundant for the `term` report")
        return TermLintReport(fo)
    if lint_report_format == "junit4-xml":
        try:
            import junit_xml
        except ImportError:
            _error(
                "The `junit4-xml` report format requires `python3-junit.xml` to be installed"
            )

        from debputy.linting.lint_report_junit import JunitLintReport

        if report_output is None:
            report_output = "debputy-lint-junit.xml"

        return JunitLintReport(report_output)

    raise AssertionError(f"Missing case for lint_report_format: {lint_report_format}")


def perform_linting(context: CommandContext) -> None:
    parsed_args = context.parsed_args
    if not parsed_args.spellcheck:
        disable_spellchecking()
    linter_exit_code = parsed_args.linter_exit_code
    lint_report = initialize_lint_report(context)
    lint_context = gather_lint_info(context)

    for name_stem in LINTER_FORMATS:
        filename = f"./{name_stem}"
        if not os.path.isfile(filename):
            continue
        perform_linting_of_file(
            lint_context,
            filename,
            name_stem,
            context.parsed_args.auto_fix,
            lint_report,
        )
    if lint_report.number_of_invalid_diagnostics:
        _warn(
            "Some diagnostics did not explicitly set severity. Please report the bug and include the output"
        )
    if lint_report.number_of_broken_diagnostics:
        _error(
            "Some sub-linters reported issues. Please report the bug and include the output"
        )

    if parsed_args.warn_about_check_manifest and os.path.isfile(
        "debian/debputy.manifest"
    ):
        _info("Note: Due to a limitation in the linter, debian/debputy.manifest is")
        _info("only **partially** checked by this command at the time of writing.")
        _info("Please use `debputy check-manifest` to fully check the manifest.")

    lint_report.finish_report()

    if linter_exit_code:
        _exit_with_lint_code(lint_report)


def perform_reformat(
    context: CommandContext,
    *,
    named_style: Optional[str] = None,
) -> None:
    parsed_args = context.parsed_args
    fo = _output_styling(context.parsed_args, sys.stdout)
    lint_context = gather_lint_info(context)
    if named_style is not None:
        style = lint_context.maint_preference_table.named_styles.get(named_style)
        if style is None:
            styles = ", ".join(lint_context.maint_preference_table.named_styles)
            _error(f'There is no style named "{style}". Options include: {styles}')
        if (
            lint_context.effective_preference is not None
            and lint_context.effective_preference != style
        ):
            _info(
                f'Note that the style "{named_style}" does not match the style that `debputy` was configured to use.'
            )
            _info("This may be a non-issue (if the configuration is out of date).")
        lint_context.effective_preference = style

    if lint_context.effective_preference is None:
        if lint_context.unsupported_preference_reason is not None:
            _warn(
                "While `debputy` could identify a formatting for this package, it does not support it."
            )
            _warn(f"{lint_context.unsupported_preference_reason}")
            if lint_context.style_tool is not None:
                _info(
                    f"The following tool might be able to apply the style: {lint_context.style_tool}"
                )
            if parsed_args.supported_style_required:
                _error(
                    "Sorry; `debputy` does not support the style. Use --unknown-or-unsupported-style-is-ok to make"
                    " this a non-error (note that `debputy` will not reformat the packaging in this case; just not"
                    " exit with an error code)."
                )
        else:
            print(
                textwrap.dedent(
                    """\
                You can enable set a style by doing either of:

                 * You can set `X-Style: black` in the source stanza of `debian/control` to pick
                   `black` as the preferred style for this package.
                   - Note: `black` is an opinionated style that follows the spirit of the `black` code formatter
                     for Python.
                   - If you use `pre-commit`, then there is a formatting hook at
                     https://salsa.debian.org/debian/debputy-pre-commit-hooks

                 * If you use the Debian Salsa CI pipeline, then you can set SALSA_CI_DISABLE_WRAP_AND_SORT
                   to a "no" or 0 and `debputy` will pick up the configuration from there.
                   - Note: The option must be in `.gitlab-ci.yml` or `debian/salsa-ci.yml` to work. The Salsa CI
                     pipeline will use `wrap-and-sort` while `debputy` uses its own emulation of `wrap-and-sort`
                     (`debputy` also needs to apply the style via `debputy lsp server`).

                 * The `debputy` code also comes with a built-in style database. This may be interesting for
                   packaging teams, so set a default team style that applies to all packages maintained by
                   that packaging team.
                   - Individuals can also add their style, which can useful for ad-hoc packaging teams, where
                     `debputy` will automatically apply a style if *all* co-maintainers agree to it.

                Note the above list is an ordered list of how `debputy` determines which style to use in case
                multiple options are available.
                """
                )
            )
            if parsed_args.supported_style_required:
                if lint_context.style_tool is not None:
                    _error(
                        "Sorry, `debputy reformat` does not support the packaging style. However, the"
                        f" formatting is supposedly handled by: {lint_context.style_tool}"
                    )
                _error(
                    "Sorry; `debputy` does not know which style to use for this package. Please either set a"
                    "style or use --unknown-or-unsupported-style-is-ok to make this a non-error"
                )
        _info("")
        _info(
            "Doing nothing since no supported style could be identified as requested."
            " See above how to set a style."
        )
        _info("Use --supported-style-is-required if this should be an error instead.")
        sys.exit(0)

    changes = False
    auto_fix = context.parsed_args.auto_fix
    for name_stem in REFORMAT_FORMATS:
        formatter = REFORMAT_FORMATS.get(name_stem)
        filename = f"./{name_stem}"
        if formatter is None or not os.path.isfile(filename):
            continue

        reformatted = perform_reformat_of_file(
            fo,
            lint_context,
            filename,
            formatter,
            auto_fix,
        )
        if reformatted:
            changes = True

    if changes and parsed_args.linter_exit_code:
        sys.exit(2)


def perform_reformat_of_file(
    fo: OutputStylingBase,
    lint_context: LintContext,
    filename: str,
    formatter: FormatterImpl,
    auto_fix: bool,
) -> bool:
    with open(filename, "rt", encoding="utf-8") as fd:
        text = fd.read()

    lines = text.splitlines(keepends=True)
    lint_state = lint_context.state_for(
        filename,
        text,
        lines,
    )
    edits = formatter(lint_state)
    if not edits:
        return False

    try:
        replacement = apply_text_edits(text, lines, edits)
    except OverLappingTextEditException:
        _error(
            f"The reformatter for {filename} produced overlapping edits (which is broken and will not work)"
        )

    output_filename = f"{filename}.tmp"
    with open(output_filename, "wt", encoding="utf-8") as fd:
        fd.write(replacement)

    r = subprocess.run(["diff", "-u", filename, output_filename]).returncode
    if r != 0 and r != 1:
        _warn(f"diff -u {filename} {output_filename} failed!?")
    if auto_fix:
        orig_mode = stat.S_IMODE(os.stat(filename).st_mode)
        os.chmod(output_filename, orig_mode)
        os.rename(output_filename, filename)
        print(
            fo.colored(
                f"Reformatted {filename}.",
                fg="green",
                style="bold",
            )
        )
    else:
        os.unlink(output_filename)

    return True


def _exit_with_lint_code(lint_report: LintReport) -> NoReturn:
    diagnostics_count = lint_report.diagnostics_count
    if (
        diagnostics_count[DiagnosticSeverity.Error]
        or diagnostics_count[DiagnosticSeverity.Warning]
    ):
        sys.exit(2)
    sys.exit(0)


def perform_linting_of_file(
    lint_context: LintContext,
    filename: str,
    file_format: str,
    auto_fixing_enabled: bool,
    lint_report: LintReport,
) -> None:
    handler = LINTER_FORMATS.get(file_format)
    if handler is None:
        return
    with open(filename, "rt", encoding="utf-8") as fd:
        text = fd.read()

    if auto_fixing_enabled:
        _auto_fix_run(
            lint_context,
            filename,
            text,
            handler,
            lint_report,
        )
    else:
        _diagnostics_run(
            lint_context,
            filename,
            text,
            handler,
            lint_report,
        )


def _edit_happens_before_last_fix(
    last_edit_pos: Position,
    last_fix_position: Position,
) -> bool:
    if last_edit_pos.line < last_fix_position.line:
        return True
    return (
        last_edit_pos.line == last_fix_position.character
        and last_edit_pos.character < last_fix_position.character
    )


def _auto_fix_run(
    lint_context: LintContext,
    filename: str,
    text: str,
    linter: LinterImpl,
    lint_report: LintReport,
) -> None:
    another_round = True
    unfixed_diagnostics: List[Diagnostic] = []
    remaining_rounds = 10
    fixed_count = 0
    too_many_rounds = False
    lines = text.splitlines(keepends=True)
    lint_state = lint_context.state_for(
        filename,
        text,
        lines,
    )
    current_issues = linter(lint_state)
    issue_count_start = len(current_issues) if current_issues else 0
    while another_round and current_issues:
        another_round = False
        last_fix_position = Position(0, 0)
        unfixed_diagnostics.clear()
        edits = []
        fixed_diagnostics = []
        for diagnostic in current_issues:
            actions = provide_standard_quickfixes_from_diagnostics(
                CodeActionParams(
                    TextDocumentIdentifier(filename),
                    diagnostic.range,
                    CodeActionContext(
                        [diagnostic],
                    ),
                ),
            )
            auto_fixing_edits = resolve_auto_fixer(filename, actions)

            if not auto_fixing_edits:
                unfixed_diagnostics.append(diagnostic)
                continue

            sorted_edits = merge_sort_text_edits(
                [get_well_formatted_edit(e) for e in auto_fixing_edits],
            )
            last_edit = sorted_edits[-1]
            last_edit_pos = last_edit.range.start
            if _edit_happens_before_last_fix(last_edit_pos, last_fix_position):
                if not another_round:
                    if remaining_rounds > 0:
                        remaining_rounds -= 1
                        print(
                            "Detected overlapping edit; scheduling another edit round."
                        )
                        another_round = True
                    else:
                        _warn(
                            "Too many overlapping edits; stopping after this round (circuit breaker)."
                        )
                        too_many_rounds = True
                continue
            edits.extend(sorted_edits)
            fixed_diagnostics.append(diagnostic)
            last_fix_position = sorted_edits[-1].range.start

        if another_round and not edits:
            _error(
                "Internal error: Detected an overlapping edit and yet had no edits to perform..."
            )

        fixed_count += len(fixed_diagnostics)

        text = apply_text_edits(
            text,
            lines,
            edits,
        )
        lines = text.splitlines(keepends=True)

        with lint_report.line_state(lint_state):
            for diagnostic in fixed_diagnostics:
                lint_report.report_diagnostic(
                    diagnostic,
                    result_state=LintDiagnosticResultState.FIXED,
                )
        lint_state.content = text
        lint_state.lines = lines
        current_issues = linter(lint_state)

    if fixed_count:
        output_filename = f"{filename}.tmp"
        with open(output_filename, "wt", encoding="utf-8") as fd:
            fd.write(text)
        orig_mode = stat.S_IMODE(os.stat(filename).st_mode)
        os.chmod(output_filename, orig_mode)
        os.rename(output_filename, filename)
        lines = text.splitlines(keepends=True)
        lint_state.content = text
        lint_state.lines = lines
        remaining_issues = linter(lint_state) or []
    else:
        remaining_issues = current_issues or []

    with lint_report.line_state(lint_state):
        for diagnostic in remaining_issues:
            lint_report.report_diagnostic(diagnostic)

    if isinstance(lint_report, TermLintReport):
        # TODO: Not optimal, but will do for now.
        fo = lint_report.fo
        print()
        if fixed_count:
            remaining_issues_count = len(remaining_issues)
            print(
                fo.colored(
                    f"Fixes applied to {filename}: {fixed_count}."
                    f" Number of issues went from {issue_count_start} to {remaining_issues_count}",
                    fg="green",
                    style="bold",
                )
            )
        elif remaining_issues:
            print(
                fo.colored(
                    f"None of the issues in {filename} could be fixed automatically. Sorry!",
                    fg="yellow",
                    bg="black",
                    style="bold",
                )
            )
        else:
            assert not current_issues
            print(
                fo.colored(
                    f"No issues detected in {filename}",
                    fg="green",
                    style="bold",
                )
            )
        if too_many_rounds:
            print(
                fo.colored(
                    f"Not all fixes for issues in {filename} could be applied due to overlapping edits.",
                    fg="yellow",
                    bg="black",
                    style="bold",
                )
            )
            print(
                "Running once more may cause more fixes to be applied. However, you may be facing"
                " pathological performance."
            )


def _diagnostics_run(
    lint_context: LintContext,
    filename: str,
    text: str,
    linter: LinterImpl,
    lint_report: LintReport,
) -> None:
    lines = text.splitlines(keepends=True)
    lint_state = lint_context.state_for(filename, text, lines)
    with lint_report.line_state(lint_state):
        issues = linter(lint_state) or []
        for diagnostic in issues:
            actions = provide_standard_quickfixes_from_diagnostics(
                CodeActionParams(
                    TextDocumentIdentifier(filename),
                    diagnostic.range,
                    CodeActionContext(
                        [diagnostic],
                    ),
                ),
            )
            auto_fixer = resolve_auto_fixer(filename, actions)
            has_auto_fixer = bool(auto_fixer)

            result_state = LintDiagnosticResultState.REPORTED
            if has_auto_fixer:
                result_state = LintDiagnosticResultState.FIXABLE

            lint_report.report_diagnostic(diagnostic, result_state=result_state)


def resolve_auto_fixer(
    document_ref: str,
    actions: Optional[List[Union[Command, CodeAction]]],
) -> Optional[List[TextEdit]]:
    if actions is None or len(actions) != 1:
        return None
    action = actions[0]
    if not isinstance(action, CodeAction):
        return None
    workspace_edit = action.edit
    if workspace_edit is None or action.command is not None:
        return None
    if (
        not workspace_edit.changes
        or len(workspace_edit.changes) != 1
        or document_ref not in workspace_edit.changes
    ):
        return None
    return workspace_edit.changes[document_ref]
