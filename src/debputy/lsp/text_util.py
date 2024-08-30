from typing import List, Optional, Sequence, Union, Iterable, TYPE_CHECKING

from debputy.lsprotocol.types import (
    TextEdit,
    Position,
    Range,
    WillSaveTextDocumentParams,
    DocumentFormattingParams,
)

from debputy.linting.lint_util import LinterPositionCodec

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
    )
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass

try:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument, PositionCodec
except ImportError:
    pass

if TYPE_CHECKING:
    LintCapablePositionCodec = Union[LinterPositionCodec, PositionCodec]
else:
    LintCapablePositionCodec = LinterPositionCodec


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
    params: Union[WillSaveTextDocumentParams, DocumentFormattingParams],
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    return trim_end_of_line_whitespace(doc.position_codec, doc.lines)


def trim_end_of_line_whitespace(
    position_codec: "LintCapablePositionCodec",
    lines: List[str],
    *,
    line_range: Optional[Iterable[int]] = None,
    line_relative_line_no: int = 0,
) -> Optional[Sequence[TextEdit]]:
    edits = []
    if line_range is None:
        line_range = range(0, len(lines))
    for line_no in line_range:
        orig_line = lines[line_no]
        orig_len = len(orig_line)
        if orig_line.endswith("\n"):
            orig_len -= 1
        stripped_len = len(orig_line.rstrip())
        if stripped_len == orig_len:
            continue

        stripped_len_client_off = position_codec.client_num_units(
            orig_line[:stripped_len]
        )
        orig_len_client_off = position_codec.client_num_units(orig_line[:orig_len])
        edit_range = position_codec.range_to_client_units(
            lines,
            Range(
                Position(
                    line_no + line_relative_line_no,
                    stripped_len_client_off,
                ),
                Position(
                    line_no + line_relative_line_no,
                    orig_len_client_off,
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


class SemanticTokensState:
    __slots__ = ("ls", "doc", "lines", "tokens", "_previous_line", "_previous_col")

    def __init__(
        self,
        ls: "DebputyLanguageServer",
        doc: "TextDocument",
        lines: List[str],
        tokens: List[int],
    ) -> None:
        self.ls = ls
        self.doc = doc
        self.lines = lines
        self.tokens = tokens
        self._previous_line = 0
        self._previous_col = 0

    def emit_token(
        self,
        start_pos: Position,
        len_client_units: int,
        token_code: int,
        *,
        token_modifiers: int = 0,
    ) -> None:
        line_delta = start_pos.line - self._previous_line
        self._previous_line = start_pos.line
        previous_col = self._previous_col

        if line_delta:
            previous_col = 0

        column_delta = start_pos.character - previous_col
        self._previous_col = start_pos.character

        tokens = self.tokens
        tokens.append(line_delta)  # Line delta
        tokens.append(column_delta)  # Token column delta
        tokens.append(len_client_units)  # Token length
        tokens.append(token_code)
        tokens.append(token_modifiers)
