import sys
from email.utils import parsedate_to_datetime
from typing import (
    Union,
    List,
    Dict,
    Iterator,
    Optional,
    Iterable,
)

from lsprotocol.types import (
    Diagnostic,
    DidOpenTextDocumentParams,
    DidChangeTextDocumentParams,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    TEXT_DOCUMENT_CODE_ACTION,
    DidCloseTextDocumentParams,
    Range,
    Position,
    DiagnosticSeverity,
)

from debputy.lsp.lsp_features import lsp_diagnostics, lsp_standard_handler
from debputy.lsp.quickfixes import (
    propose_correct_text_quick_fix,
)
from debputy.lsp.spellchecking import spellcheck_line
from debputy.lsp.text_util import (
    LintCapablePositionCodec,
)

try:
    from debian._deb822_repro.locatable import Position as TEPosition, Ranage as TERange

    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


# Same as Lintian
_MAXIMUM_WIDTH: int = 82
_LANGUAGE_IDS = [
    "debian/changelog",
    # emacs's name
    "debian-changelog",
    # vim's name
    "debchangelog",
]

_WEEKDAYS_BY_IDX = [
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
]
_KNOWN_WEEK_DAYS = frozenset(_WEEKDAYS_BY_IDX)

DOCUMENT_VERSION_TABLE: Dict[str, int] = {}


def _handle_close(
    ls: "LanguageServer",
    params: DidCloseTextDocumentParams,
) -> None:
    try:
        del DOCUMENT_VERSION_TABLE[params.text_document.uri]
    except KeyError:
        pass


def is_doc_at_version(uri: str, version: int) -> bool:
    dv = DOCUMENT_VERSION_TABLE.get(uri)
    return dv == version


lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)
lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)


@lsp_diagnostics(_LANGUAGE_IDS)
def _diagnostics_debian_changelog(
    ls: "LanguageServer",
    params: Union[DidOpenTextDocumentParams, DidChangeTextDocumentParams],
) -> Iterable[List[Diagnostic]]:
    doc_uri = params.text_document.uri
    doc = ls.workspace.get_text_document(doc_uri)
    lines = doc.lines
    max_words = 1_000
    delta_update_size = 10
    max_lines_between_update = 10
    scanner = _scan_debian_changelog_for_diagnostics(
        lines,
        doc.position_codec,
        delta_update_size,
        max_words,
        max_lines_between_update,
    )

    yield from scanner


def _check_footer_line(
    line: str,
    line_no: int,
    lines: List[str],
    position_codec: LintCapablePositionCodec,
) -> Iterator[Diagnostic]:
    try:
        end_email_idx = line.rindex(">  ")
    except ValueError:
        # Syntax error; flag later
        return
    line_len = len(line)
    start_date_idx = end_email_idx + 3
    # 3 characters for the day name (Mon), then a comma plus a space followed by the
    # actual date. The 6 characters limit is a gross under estimation of the real
    # size.
    if line_len < start_date_idx + 6:
        range_server_units = Range(
            Position(
                line_no,
                start_date_idx,
            ),
            Position(
                line_no,
                line_len,
            ),
        )
        yield Diagnostic(
            position_codec.range_to_client_units(lines, range_server_units),
            "Expected a date in RFC822 format (Tue, 12 Mar 2024 12:34:56 +0000)",
            severity=DiagnosticSeverity.Error,
            source="debputy",
        )
        return
    day_name_range_server_units = Range(
        Position(
            line_no,
            start_date_idx,
        ),
        Position(
            line_no,
            start_date_idx + 3,
        ),
    )
    day_name = line[start_date_idx : start_date_idx + 3]
    if day_name not in _KNOWN_WEEK_DAYS:
        yield Diagnostic(
            position_codec.range_to_client_units(lines, day_name_range_server_units),
            "Expected a three letter date here (Mon, Tue, ..., Sun).",
            severity=DiagnosticSeverity.Error,
            source="debputy",
        )
        return

    date_str = line[start_date_idx + 5 :]

    if line[start_date_idx + 3 : start_date_idx + 5] != ", ":
        sep = line[start_date_idx + 3 : start_date_idx + 5]
        range_server_units = Range(
            Position(
                line_no,
                start_date_idx + 3,
            ),
            Position(
                line_no,
                start_date_idx + 4,
            ),
        )
        yield Diagnostic(
            position_codec.range_to_client_units(lines, range_server_units),
            f'Improper formatting of date. Expected ", " here, not "{sep}"',
            severity=DiagnosticSeverity.Error,
            source="debputy",
        )
        return

    try:
        # FIXME: this parser is too forgiving (it ignores trailing garbage)
        date = parsedate_to_datetime(date_str)
    except ValueError as e:
        range_server_units = Range(
            Position(
                line_no,
                start_date_idx + 5,
            ),
            Position(
                line_no,
                line_len,
            ),
        )
        yield Diagnostic(
            position_codec.range_to_client_units(lines, range_server_units),
            f"Unable to the date as a valid RFC822 date: {e.args[0]}",
            severity=DiagnosticSeverity.Error,
            source="debputy",
        )
        return
    expected_week_day = _WEEKDAYS_BY_IDX[date.weekday()]
    if expected_week_day != day_name:
        yield Diagnostic(
            position_codec.range_to_client_units(lines, day_name_range_server_units),
            f"The date was a {expected_week_day}day.",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
            data=[propose_correct_text_quick_fix(expected_week_day)],
        )


def _scan_debian_changelog_for_diagnostics(
    lines: List[str],
    position_codec: LintCapablePositionCodec,
    delta_update_size: int,
    max_words: int,
    max_lines_between_update: int,
    *,
    max_line_length: int = _MAXIMUM_WIDTH,
) -> Iterator[List[Diagnostic]]:
    diagnostics = []
    diagnostics_at_last_update = 0
    lines_since_last_update = 0
    for line_no, line in enumerate(lines):
        orig_line = line
        line = line.rstrip()
        if not line:
            continue
        if line.startswith(" --"):
            diagnostics.extend(_check_footer_line(line, line_no, lines, position_codec))
            continue
        if not line.startswith("  "):
            continue
        # minus 1 for newline
        orig_line_len = len(orig_line) - 1
        if orig_line_len > max_line_length:
            range_server_units = Range(
                Position(
                    line_no,
                    max_line_length,
                ),
                Position(
                    line_no,
                    orig_line_len,
                ),
            )
            diagnostics.append(
                Diagnostic(
                    position_codec.range_to_client_units(lines, range_server_units),
                    f"Line exceeds {max_line_length} characters",
                    severity=DiagnosticSeverity.Hint,
                    source="debputy",
                )
            )
        if len(line) > 3 and line[2] == "[" and line[-1] == "]":
            # Do not spell check [ X ] as X is usually a name
            continue
        lines_since_last_update += 1
        if max_words > 0:
            typos = list(spellcheck_line(lines, position_codec, line_no, line))
            new_diagnostics = len(typos)
            max_words -= new_diagnostics
            diagnostics.extend(typos)

        current_diagnostics_len = len(diagnostics)
        if (
            lines_since_last_update >= max_lines_between_update
            or current_diagnostics_len - diagnostics_at_last_update > delta_update_size
        ):
            diagnostics_at_last_update = current_diagnostics_len
            lines_since_last_update = 0

            yield diagnostics
    if not diagnostics or diagnostics_at_last_update != len(diagnostics):
        yield diagnostics


def _lint_debian_changelog(
    _doc_reference: str,
    _path: str,
    lines: List[str],
    position_codec: LintCapablePositionCodec,
) -> Optional[List[Diagnostic]]:
    limits = sys.maxsize
    scanner = _scan_debian_changelog_for_diagnostics(
        lines,
        position_codec,
        limits,
        limits,
        limits,
    )
    return next(iter(scanner), None)
