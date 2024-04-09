import dataclasses
from typing import List, Optional, Callable, Counter

from lsprotocol.types import Position, Range, Diagnostic, DiagnosticSeverity

from debputy.commands.debputy_cmd.output import OutputStylingBase
from debputy.util import _DEFAULT_LOGGER, _warn

LinterImpl = Callable[
    [str, str, List[str], "LintCapablePositionCodec"], Optional[List[Diagnostic]]
]


@dataclasses.dataclass(slots=True)
class LintReport:
    diagnostics_count: Counter[DiagnosticSeverity] = dataclasses.field(
        default_factory=Counter
    )
    diagnostics_without_severity: int = 0
    diagnostic_errors: int = 0
    fixed: int = 0
    fixable: int = 0


class LinterPositionCodec:

    def client_num_units(self, chars: str):
        return len(chars)

    def position_from_client_units(
        self, lines: List[str], position: Position
    ) -> Position:

        if len(lines) == 0:
            return Position(0, 0)
        if position.line >= len(lines):
            return Position(len(lines) - 1, self.client_num_units(lines[-1]))
        return position

    def position_to_client_units(
        self, _lines: List[str], position: Position
    ) -> Position:
        return position

    def range_from_client_units(self, _lines: List[str], range: Range) -> Range:
        return range

    def range_to_client_units(self, _lines: List[str], range: Range) -> Range:
        return range


LINTER_POSITION_CODEC = LinterPositionCodec()


_SEVERITY2TAG = {
    DiagnosticSeverity.Error: lambda fo: fo.colored(
        "error",
        fg="red",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Warning: lambda fo: fo.colored(
        "warning",
        fg="yellow",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Information: lambda fo: fo.colored(
        "informational",
        fg="blue",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Hint: lambda fo: fo.colored(
        "pedantic",
        fg="green",
        bg="black",
        style="bold",
    ),
}


def _lines_to_print(range_: Range) -> int:
    count = range_.end.line - range_.start.line
    if range_.end.character > 0:
        count += 1
    return count


def _highlight_range(
    fo: OutputStylingBase, line: str, line_no: int, range_: Range
) -> str:
    line_wo_nl = line.rstrip("\r\n")
    start_pos = 0
    prefix = ""
    suffix = ""
    if line_no == range_.start.line:
        start_pos = range_.start.character
        prefix = line_wo_nl[0:start_pos]
    if line_no == range_.end.line:
        end_pos = range_.end.character
        suffix = line_wo_nl[end_pos:]
    else:
        end_pos = len(line_wo_nl)

    marked_part = fo.colored(line_wo_nl[start_pos:end_pos], fg="red", style="bold")

    return prefix + marked_part + suffix


def report_diagnostic(
    fo: OutputStylingBase,
    filename: str,
    diagnostic: Diagnostic,
    lines: List[str],
    auto_fixable: bool,
    auto_fixed: bool,
    lint_report: LintReport,
) -> None:
    logger = _DEFAULT_LOGGER
    assert logger is not None
    severity = diagnostic.severity
    missing_severity = False
    if severity is None:
        severity = DiagnosticSeverity.Warning
        missing_severity = True
    if not auto_fixed:
        tag_unresolved = _SEVERITY2TAG.get(severity)
        if tag_unresolved is None:
            tag_unresolved = _SEVERITY2TAG[DiagnosticSeverity.Warning]
            lint_report.diagnostics_without_severity += 1
        else:
            lint_report.diagnostics_count[severity] += 1
        tag = tag_unresolved(fo)
    else:
        tag = fo.colored(
            "auto-fixing",
            fg="magenta",
            bg="black",
            style="bold",
        )
    start_line = diagnostic.range.start.line
    start_position = diagnostic.range.start.character
    end_line = diagnostic.range.end.line
    end_position = diagnostic.range.end.character
    has_fixit = ""
    line_no_width = len(str(len(lines)))
    if not auto_fixed and auto_fixable:
        has_fixit = " [Correctable via --auto-fix]"
        lint_report.fixable += 1
    print(
        f"{tag}: File: {filename}:{start_line+1}:{start_position}:{end_line+1}:{end_position}: {diagnostic.message}{has_fixit}",
    )
    if missing_severity:
        _warn(
            "  This warning did not have an explicit severity; Used Warning as a fallback!"
        )
    if auto_fixed:
        # If it is fixed, there is no reason to show additional context.
        lint_report.fixed += 1
        return
    lines_to_print = _lines_to_print(diagnostic.range)
    if diagnostic.range.end.line > len(lines) or diagnostic.range.start.line < 0:
        lint_report.diagnostic_errors += 1
        _warn(
            "Bug in the underlying linter: The line numbers of the warning does not fit in the file..."
        )
        return
    if lines_to_print == 1:
        line = _highlight_range(fo, lines[start_line], start_line, diagnostic.range)
        print(f"    {start_line+1:{line_no_width}}: {line}")
    else:
        for line_no in range(start_line, end_line):
            line = _highlight_range(fo, lines[line_no], line_no, diagnostic.range)
            print(f"    {line_no+1:{line_no_width}}: {line}")
