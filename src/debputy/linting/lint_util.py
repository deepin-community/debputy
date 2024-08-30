import collections
import contextlib
import dataclasses
import datetime
import os
import time
import typing
from collections import defaultdict, Counter
from enum import IntEnum
from typing import (
    List,
    Optional,
    Callable,
    TYPE_CHECKING,
    Mapping,
    Sequence,
    cast,
)

from debputy.commands.debputy_cmd.output import OutputStylingBase
from debputy.dh.dh_assistant import (
    extract_dh_addons_from_control,
    DhSequencerData,
    parse_drules_for_addons,
)
from debputy.filesystem_scan import VirtualPathBase
from debputy.integration_detection import determine_debputy_integration_mode
from debputy.lsp.diagnostics import LintSeverity
from debputy.lsp.vendoring._deb822_repro import Deb822FileElement, parse_deb822_file
from debputy.lsprotocol.types import (
    Position,
    Range,
    Diagnostic,
    DiagnosticSeverity,
    TextEdit,
)
from debputy.packages import SourcePackage, BinaryPackage
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.spec import DebputyIntegrationMode
from debputy.util import _warn

if TYPE_CHECKING:
    from debputy.lsp.text_util import LintCapablePositionCodec
    from debputy.lsp.maint_prefs import (
        MaintainerPreferenceTable,
        EffectiveFormattingPreference,
    )


LinterImpl = Callable[["LintState"], Optional[List[Diagnostic]]]
FormatterImpl = Callable[["LintState"], Optional[Sequence[TextEdit]]]


@dataclasses.dataclass(slots=True)
class DebputyMetadata:
    debputy_integration_mode: Optional[DebputyIntegrationMode]

    @classmethod
    def from_data(
        cls,
        source_fields: Mapping[str, str],
        dh_sequencer_data: DhSequencerData,
    ) -> typing.Self:
        integration_mode = determine_debputy_integration_mode(
            source_fields,
            dh_sequencer_data.sequences,
        )
        return cls(integration_mode)


class LintState:

    @property
    def plugin_feature_set(self) -> PluginProvidedFeatureSet:
        raise NotImplementedError

    @property
    def doc_uri(self) -> str:
        raise NotImplementedError

    @property
    def source_root(self) -> Optional[VirtualPathBase]:
        raise NotImplementedError

    @property
    def debian_dir(self) -> Optional[VirtualPathBase]:
        raise NotImplementedError

    @property
    def path(self) -> str:
        raise NotImplementedError

    @property
    def content(self) -> str:
        raise NotImplementedError

    @property
    def lines(self) -> List[str]:
        raise NotImplementedError

    @property
    def position_codec(self) -> "LintCapablePositionCodec":
        raise NotImplementedError

    @property
    def parsed_deb822_file_content(self) -> Optional[Deb822FileElement]:
        raise NotImplementedError

    @property
    def source_package(self) -> Optional[SourcePackage]:
        raise NotImplementedError

    @property
    def binary_packages(self) -> Optional[Mapping[str, BinaryPackage]]:
        raise NotImplementedError

    @property
    def maint_preference_table(self) -> "MaintainerPreferenceTable":
        raise NotImplementedError

    @property
    def effective_preference(self) -> Optional["EffectiveFormattingPreference"]:
        raise NotImplementedError

    @property
    def debputy_metadata(self) -> DebputyMetadata:
        src_pkg = self.source_package
        src_fields = src_pkg.fields if src_pkg else {}
        return DebputyMetadata.from_data(
            src_fields,
            self.dh_sequencer_data,
        )

    @property
    def dh_sequencer_data(self) -> DhSequencerData:
        raise NotImplementedError


@dataclasses.dataclass(slots=True)
class LintStateImpl(LintState):
    plugin_feature_set: PluginProvidedFeatureSet = dataclasses.field(repr=False)
    maint_preference_table: "MaintainerPreferenceTable" = dataclasses.field(repr=False)
    source_root: Optional[VirtualPathBase]
    debian_dir: Optional[VirtualPathBase]
    path: str
    content: str
    lines: List[str]
    source_package: Optional[SourcePackage] = None
    binary_packages: Optional[Mapping[str, BinaryPackage]] = None
    effective_preference: Optional["EffectiveFormattingPreference"] = None
    _parsed_cache: Optional[Deb822FileElement] = None
    _dh_sequencer_cache: Optional[DhSequencerData] = None

    @property
    def doc_uri(self) -> str:
        path = self.path
        abs_path = os.path.join(os.path.curdir, path)
        return f"file://{abs_path}"

    @property
    def position_codec(self) -> "LintCapablePositionCodec":
        return LINTER_POSITION_CODEC

    @property
    def parsed_deb822_file_content(self) -> Optional[Deb822FileElement]:
        cache = self._parsed_cache
        if cache is None:
            cache = parse_deb822_file(
                self.lines,
                accept_files_with_error_tokens=True,
                accept_files_with_duplicated_fields=True,
            )
            self._parsed_cache = cache
        return cache

    @property
    def dh_sequencer_data(self) -> DhSequencerData:
        dh_sequencer_cache = self._dh_sequencer_cache
        if dh_sequencer_cache is None:
            debian_dir = self.debian_dir
            dh_sequences: typing.Set[str] = set()
            saw_dh = False
            src_pkg = self.source_package
            drules = debian_dir.get("rules") if debian_dir is not None else None
            if drules and drules.is_file:
                with drules.open() as fd:
                    saw_dh = parse_drules_for_addons(fd, dh_sequences)
            if src_pkg:
                extract_dh_addons_from_control(src_pkg.fields, dh_sequences)

            dh_sequencer_cache = DhSequencerData(
                frozenset(dh_sequences),
                saw_dh,
            )
            self._dh_sequencer_cache = dh_sequencer_cache
        return dh_sequencer_cache


class LintDiagnosticResultState(IntEnum):
    REPORTED = 1
    FIXABLE = 2
    FIXED = 3


@dataclasses.dataclass(slots=True, frozen=True)
class LintDiagnosticResult:
    diagnostic: Diagnostic
    result_state: LintDiagnosticResultState
    invalid_marker: Optional[RuntimeError]
    is_file_level_diagnostic: bool
    has_broken_range: bool
    missing_severity: bool
    discovered_in: str
    report_for_related_file: Optional[str]


class LintReport:

    def __init__(self) -> None:
        self.diagnostics_count: typing.Counter[DiagnosticSeverity] = Counter()
        self.diagnostics_by_file: Mapping[str, List[LintDiagnosticResult]] = (
            defaultdict(list)
        )
        self.number_of_invalid_diagnostics: int = 0
        self.number_of_broken_diagnostics: int = 0
        self.lint_state: Optional[LintState] = None
        self.start_timestamp = datetime.datetime.now()
        self.durations: typing.Dict[str, float] = collections.defaultdict(lambda: 0.0)
        self._timer = time.perf_counter()

    @contextlib.contextmanager
    def line_state(self, lint_state: LintState) -> typing.Iterable[None]:
        previous = self.lint_state
        if previous is not None:
            path = previous.path
            duration = time.perf_counter() - self._timer
            self.durations[path] += duration

        self.lint_state = lint_state

        try:
            self._timer = time.perf_counter()
            yield
        finally:
            now = time.perf_counter()
            duration = now - self._timer
            self.durations[lint_state.path] += duration
            self._timer = now
            self.lint_state = previous

    def report_diagnostic(
        self,
        diagnostic: Diagnostic,
        *,
        result_state: LintDiagnosticResultState = LintDiagnosticResultState.REPORTED,
        in_file: Optional[str] = None,
    ) -> None:
        lint_state = self.lint_state
        assert lint_state is not None
        if in_file is None:
            in_file = lint_state.path
        discovered_in_file = in_file
        severity = diagnostic.severity
        missing_severity = False
        error_marker: Optional[RuntimeError] = None
        if severity is None:
            self.number_of_invalid_diagnostics += 1
            severity = DiagnosticSeverity.Warning
            diagnostic.severity = severity
            missing_severity = True

        lines = lint_state.lines
        diag_range = diagnostic.range
        start_pos = diag_range.start
        end_pos = diag_range.start
        diag_data = diagnostic.data
        if isinstance(diag_data, dict):
            report_for_related_file = diag_data.get("report_for_related_file")
            if report_for_related_file is None or not isinstance(
                report_for_related_file, str
            ):
                report_for_related_file = None
            else:
                in_file = report_for_related_file
                # Force it to exist in self.durations, since subclasses can use .items() or "foo" in self.durations.
                if in_file not in self.durations:
                    self.durations[in_file] = 0
        else:
            report_for_related_file = None
        if report_for_related_file is not None:
            is_file_level_diagnostic = True
        else:
            is_file_level_diagnostic = _is_file_level_diagnostic(
                lines,
                start_pos.line,
                start_pos.character,
                end_pos.line,
                end_pos.character,
            )
        has_broken_range = not is_file_level_diagnostic and (
            end_pos.line > len(lines) or start_pos.line < 0
        )

        if has_broken_range or missing_severity:
            error_marker = RuntimeError("Registration Marker for invalid diagnostic")

        diagnostic_result = LintDiagnosticResult(
            diagnostic,
            result_state,
            error_marker,
            is_file_level_diagnostic,
            has_broken_range,
            missing_severity,
            report_for_related_file=report_for_related_file,
            discovered_in=discovered_in_file,
        )

        self.diagnostics_by_file[in_file].append(diagnostic_result)
        self.diagnostics_count[severity] += 1
        self.process_diagnostic(in_file, lint_state, diagnostic_result)

    def process_diagnostic(
        self,
        filename: str,
        lint_state: LintState,
        diagnostic_result: LintDiagnosticResult,
    ) -> None:
        # Subclass hook
        pass

    def finish_report(self) -> None:
        # Subclass hook
        pass


_LS2DEBPUTY_SEVERITY: Mapping[DiagnosticSeverity, LintSeverity] = {
    DiagnosticSeverity.Error: "error",
    DiagnosticSeverity.Warning: "warning",
    DiagnosticSeverity.Information: "informational",
    DiagnosticSeverity.Hint: "pedantic",
}


_TERM_SEVERITY2TAG = {
    DiagnosticSeverity.Error: lambda fo, lint_tag=None: fo.colored(
        lint_tag if lint_tag else "error",
        fg="red",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Warning: lambda fo, lint_tag=None: fo.colored(
        lint_tag if lint_tag else "warning",
        fg="yellow",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Information: lambda fo, lint_tag=None: fo.colored(
        lint_tag if lint_tag else "informational",
        fg="blue",
        bg="black",
        style="bold",
    ),
    DiagnosticSeverity.Hint: lambda fo, lint_tag=None: fo.colored(
        lint_tag if lint_tag else "pedantic",
        fg="green",
        bg="black",
        style="bold",
    ),
}


def debputy_severity(diagnostic: Diagnostic) -> LintSeverity:
    lint_tag: Optional[LintSeverity] = None
    if isinstance(diagnostic.data, dict):
        lint_tag = cast("LintSeverity", diagnostic.data.get("lint_severity"))

    if lint_tag is not None:
        return lint_tag
    severity = diagnostic.severity
    if severity is None:
        return "warning"
    return _LS2DEBPUTY_SEVERITY.get(severity, "warning")


class TermLintReport(LintReport):

    def __init__(self, fo: OutputStylingBase) -> None:
        super().__init__()
        self.fo = fo

    def finish_report(self) -> None:
        # Nothing to do for now
        pass

    def process_diagnostic(
        self,
        filename: str,
        lint_state: LintState,
        diagnostic_result: LintDiagnosticResult,
    ) -> None:
        diagnostic = diagnostic_result.diagnostic
        fo = self.fo
        severity = diagnostic.severity
        assert severity is not None
        if diagnostic_result.result_state != LintDiagnosticResultState.FIXED:
            tag_unresolved = _TERM_SEVERITY2TAG[severity]
            lint_tag: Optional[LintSeverity] = debputy_severity(diagnostic)
            tag = tag_unresolved(fo, lint_tag)
        else:
            tag = fo.colored(
                "auto-fixing",
                fg="magenta",
                bg="black",
                style="bold",
            )

        if diagnostic_result.is_file_level_diagnostic:
            start_line = 0
            start_position = 0
            end_line = 0
            end_position = 0
        else:
            start_line = diagnostic.range.start.line
            start_position = diagnostic.range.start.character
            end_line = diagnostic.range.end.line
            end_position = diagnostic.range.end.character

        has_fixit = ""
        lines = lint_state.lines
        line_no_width = len(str(len(lines)))

        if diagnostic_result.result_state == LintDiagnosticResultState.FIXABLE:
            has_fixit = " [Correctable via --auto-fix]"

        code = f"[{diagnostic.code}]: " if diagnostic.code else ""
        msg = f"{code}{diagnostic.message}"
        print(
            f"{tag}: File: {filename}:{start_line+1}:{start_position}:{end_line+1}:{end_position}: {msg}{has_fixit}",
        )
        if diagnostic_result.missing_severity:
            _warn(
                "  This warning did not have an explicit severity; Used Warning as a fallback!"
            )
        if diagnostic_result.result_state == LintDiagnosticResultState.FIXED:
            # If it is fixed, there is no reason to show additional context.
            return
        if diagnostic_result.is_file_level_diagnostic:
            print("    File-level diagnostic")
            return
        if diagnostic_result.has_broken_range:
            _warn(
                "Bug in the underlying linter: The line numbers of the warning does not fit in the file..."
            )
            return
        lines_to_print = _lines_to_print(diagnostic.range)
        for line_no in range(start_line, start_line + lines_to_print):
            line = _highlight_range(fo, lines[line_no], line_no, diagnostic.range)
            print(f"    {line_no+1:{line_no_width}}: {line}")


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


def _is_file_level_diagnostic(
    lines: List[str],
    start_line: int,
    start_position: int,
    end_line: int,
    end_position: int,
) -> bool:
    if start_line != 0 or start_position != 0:
        return False
    line_count = len(lines)
    if end_line + 1 == line_count and end_position == 0:
        return True
    return end_line == line_count and line_count and end_position == len(lines[-1])
