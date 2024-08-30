import dataclasses
from typing import Tuple, FrozenSet, Optional, List

from debputy.lsp.lsp_features import SEMANTIC_TOKENS_LEGEND
from debputy.util import grouper

from debputy.lsprotocol.types import (
    TextDocumentItem,
    Position,
    Range,
    SemanticTokens,
)

try:
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass


@dataclasses.dataclass(slots=True, frozen=True)
class ResolvedSemanticToken:
    range: "Range"
    token_name: str
    modifiers: FrozenSet[str] = frozenset()


def resolved_semantic_token(
    line_no: int,
    col_start: int,
    token_len: int,
    token_type: str,
    *,
    token_modifiers: FrozenSet[str] = frozenset(),
) -> ResolvedSemanticToken:
    return ResolvedSemanticToken(
        Range(
            Position(
                line_no,
                col_start,
            ),
            Position(
                line_no,
                col_start + token_len,
            ),
        ),
        token_type,
        token_modifiers,
    )


def _locate_cursor(text: str) -> Tuple[str, "Position"]:
    lines = text.splitlines(keepends=True)
    for line_no in range(len(lines)):
        line = lines[line_no]
        try:
            c = line.index("<CURSOR>")
        except ValueError:
            continue
        line = line.replace("<CURSOR>", "")
        lines[line_no] = line
        pos = Position(line_no, c)
        return "".join(lines), pos
    raise ValueError('Missing "<CURSOR>" marker')


def put_doc_with_cursor(
    ls: "DebputyLanguageServer",
    uri: str,
    language_id: str,
    content: str,
) -> "Position":
    cleaned_content, cursor_pos = _locate_cursor(content)
    put_doc_no_cursor(
        ls,
        uri,
        language_id,
        cleaned_content,
    )
    return cursor_pos


def put_doc_no_cursor(
    ls: "DebputyLanguageServer",
    uri: str,
    language_id: str,
    content: str,
) -> None:
    doc_version = 1
    existing = ls.workspace.text_documents.get(uri)
    if existing is not None:
        doc_version = existing.version + 1
    ls.workspace.put_text_document(
        TextDocumentItem(
            uri,
            language_id,
            doc_version,
            content,
        )
    )


def resolve_semantic_tokens(
    token_result: Optional["SemanticTokens"],
) -> Optional[List[ResolvedSemanticToken]]:
    if token_result is None:
        return None
    assert (len(token_result.data) % 5) == 0
    current_line = 0
    current_col = 0
    resolved_tokens = []
    token_types = SEMANTIC_TOKENS_LEGEND.token_types
    for token_data in grouper(token_result.data, 5, incomplete="strict"):
        line_delta, col_start_delta, token_len, token_code, modifier_codes = token_data
        if line_delta:
            current_col = 0
        current_line += line_delta
        current_col += col_start_delta
        assert (
            not modifier_codes
        ), "TODO: Modifiers not supported (no modifiers defined)"

        resolved_tokens.append(
            resolved_semantic_token(
                current_line,
                current_col,
                token_len,
                token_types[token_code],
            ),
        )

    return resolved_tokens
