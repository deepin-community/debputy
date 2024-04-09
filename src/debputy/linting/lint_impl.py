import os
import stat
import sys
from typing import Optional, List, Union, NoReturn

from lsprotocol.types import (
    CodeAction,
    Command,
    CodeActionParams,
    CodeActionContext,
    TextDocumentIdentifier,
    TextEdit,
    Position,
    DiagnosticSeverity,
)

from debputy.commands.debputy_cmd.context import CommandContext
from debputy.commands.debputy_cmd.output import _output_styling, OutputStylingBase
from debputy.linting.lint_util import (
    LINTER_POSITION_CODEC,
    report_diagnostic,
    LinterImpl,
    LintReport,
)
from debputy.lsp.lsp_debian_changelog import _lint_debian_changelog
from debputy.lsp.lsp_debian_control import _lint_debian_control
from debputy.lsp.lsp_debian_copyright import _lint_debian_copyright
from debputy.lsp.lsp_debian_debputy_manifest import _lint_debian_debputy_manifest
from debputy.lsp.lsp_debian_rules import _lint_debian_rules_impl
from debputy.lsp.lsp_debian_tests_control import _lint_debian_tests_control
from debputy.lsp.quickfixes import provide_standard_quickfixes_from_diagnostics
from debputy.lsp.spellchecking import disable_spellchecking
from debputy.lsp.text_edit import (
    get_well_formatted_edit,
    merge_sort_text_edits,
    apply_text_edits,
)
from debputy.util import _warn, _error, _info

LINTER_FORMATS = {
    "debian/changelog": _lint_debian_changelog,
    "debian/control": _lint_debian_control,
    "debian/copyright": _lint_debian_copyright,
    "debian/debputy.manifest": _lint_debian_debputy_manifest,
    "debian/rules": _lint_debian_rules_impl,
    "debian/tests/control": _lint_debian_tests_control,
}


def perform_linting(context: CommandContext) -> None:
    parsed_args = context.parsed_args
    if not parsed_args.spellcheck:
        disable_spellchecking()
    linter_exit_code = parsed_args.linter_exit_code
    lint_report = LintReport()
    fo = _output_styling(context.parsed_args, sys.stdout)
    for name_stem in LINTER_FORMATS:
        filename = f"./{name_stem}"
        if not os.path.isfile(filename):
            continue
        perform_linting_of_file(
            fo,
            filename,
            name_stem,
            context.parsed_args.auto_fix,
            lint_report,
        )
    if lint_report.diagnostics_without_severity:
        _warn(
            "Some diagnostics did not explicitly set severity. Please report the bug and include the output"
        )
    if lint_report.diagnostic_errors:
        _error(
            "Some sub-linters reported issues. Please report the bug and include the output"
        )

    if os.path.isfile("debian/debputy.manifest"):
        _info("Note: Due to a limitation in the linter, debian/debputy.manifest is")
        _info("only **partially** checked by this command at the time of writing.")
        _info("Please use `debputy check-manifest` to fully check the manifest.")

    if linter_exit_code:
        _exit_with_lint_code(lint_report)


def _exit_with_lint_code(lint_report: LintReport) -> NoReturn:
    diagnostics_count = lint_report.diagnostics_count
    if (
        diagnostics_count[DiagnosticSeverity.Error]
        or diagnostics_count[DiagnosticSeverity.Warning]
    ):
        sys.exit(2)
    sys.exit(0)


def perform_linting_of_file(
    fo: OutputStylingBase,
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
        _auto_fix_run(fo, filename, text, handler, lint_report)
    else:
        _diagnostics_run(fo, filename, text, handler, lint_report)


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
    fo: OutputStylingBase,
    filename: str,
    text: str,
    linter: LinterImpl,
    lint_report: LintReport,
) -> None:
    another_round = True
    unfixed_diagnostics = []
    remaining_rounds = 10
    fixed_count = False
    too_many_rounds = False
    lines = text.splitlines(keepends=True)
    current_issues = linter(filename, filename, lines, LINTER_POSITION_CODEC)
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

        for diagnostic in fixed_diagnostics:
            report_diagnostic(
                fo,
                filename,
                diagnostic,
                lines,
                True,
                True,
                lint_report,
            )
        current_issues = linter(filename, filename, lines, LINTER_POSITION_CODEC)

    if fixed_count:
        output_filename = f"{filename}.tmp"
        with open(output_filename, "wt", encoding="utf-8") as fd:
            fd.write(text)
        orig_mode = stat.S_IMODE(os.stat(filename).st_mode)
        os.chmod(output_filename, orig_mode)
        os.rename(output_filename, filename)
        lines = text.splitlines(keepends=True)
        remaining_issues = (
            linter(filename, filename, lines, LINTER_POSITION_CODEC) or []
        )
    else:
        remaining_issues = current_issues or []

    for diagnostic in remaining_issues:
        report_diagnostic(
            fo,
            filename,
            diagnostic,
            lines,
            False,
            False,
            lint_report,
        )

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
    fo: OutputStylingBase,
    filename: str,
    text: str,
    linter: LinterImpl,
    lint_report: LintReport,
) -> None:
    lines = text.splitlines(keepends=True)
    issues = linter(filename, filename, lines, LINTER_POSITION_CODEC) or []
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

        report_diagnostic(
            fo,
            filename,
            diagnostic,
            lines,
            has_auto_fixer,
            False,
            lint_report,
        )


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
