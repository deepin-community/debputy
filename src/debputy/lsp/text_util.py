from typing import List, Optional, Sequence, Union, Iterable

from lsprotocol.types import (
    TextEdit,
    Position,
    Range,
    WillSaveTextDocumentParams,
)

from debputy.linting.lint_util import LinterPositionCodec

try:
    from debian._deb822_repro.locatable import Position as TEPosition, Range as TERange
except ImportError:
    pass

try:
    from pygls.workspace import LanguageServer, TextDocument, PositionCodec

    LintCapablePositionCodec = Union[LinterPositionCodec, PositionCodec]
except ImportError:
    LintCapablePositionCodec = LinterPositionCodec


try:
    from Levenshtein import distance
except ImportError:

    def detect_possible_typo(
        provided_value: str,
        known_values: Iterable[str],
    ) -> Sequence[str]:
        return tuple()

else:

    def detect_possible_typo(
        provided_value: str,
        known_values: Iterable[str],
    ) -> Sequence[str]:
        k_len = len(provided_value)
        candidates = []
        for known_value in known_values:
            if abs(k_len - len(known_value)) > 2:
                continue
            d = distance(provided_value, known_value)
            if d > 2:
                continue
            candidates.append(known_value)
        return candidates


def normalize_dctrl_field_name(f: str) -> str:
    if not f or not f.startswith(("x", "X")):
        return f
    i = 0
    for i in range(1, len(f)):
        if f[i] == "-":
            i += 1
            break
        if f[i] not in ("b", "B", "s", "S", "c", "C"):
            return f
    assert i > 0
    return f[i:]


def on_save_trim_end_of_line_whitespace(
    ls: "LanguageServer",
    params: WillSaveTextDocumentParams,
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    return trim_end_of_line_whitespace(doc, doc.lines)


def trim_end_of_line_whitespace(
    doc: "TextDocument",
    lines: List[str],
) -> Optional[Sequence[TextEdit]]:
    edits = []
    for line_no, orig_line in enumerate(lines):
        orig_len = len(orig_line)
        if orig_line.endswith("\n"):
            orig_len -= 1
        stripped_len = len(orig_line.rstrip())
        if stripped_len == orig_len:
            continue

        edit_range = doc.position_codec.range_to_client_units(
            lines,
            Range(
                Position(
                    line_no,
                    stripped_len,
                ),
                Position(
                    line_no,
                    orig_len,
                ),
            ),
        )
        edits.append(
            TextEdit(
                edit_range,
                "",
            )
        )

    return edits


def te_position_to_lsp(te_position: "TEPosition") -> Position:
    return Position(
        te_position.line_position,
        te_position.cursor_position,
    )


def te_range_to_lsp(te_range: "TERange") -> Range:
    return Range(
        te_position_to_lsp(te_range.start_pos),
        te_position_to_lsp(te_range.end_pos),
    )
