import dataclasses
import re
from itertools import chain
from typing import (
    Optional,
    Union,
    Sequence,
    Tuple,
    Any,
    Container,
    List,
    Iterable,
    Iterator,
    Callable,
    cast,
)

from debputy.lsprotocol.types import (
    CompletionParams,
    CompletionList,
    CompletionItem,
    Position,
    MarkupContent,
    Hover,
    MarkupKind,
    HoverParams,
    FoldingRangeParams,
    FoldingRange,
    FoldingRangeKind,
    SemanticTokensParams,
    SemanticTokens,
    TextEdit,
    MessageType,
    SemanticTokenTypes,
)

from debputy.linting.lint_util import LintState
from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsp.lsp_debian_control_reference_data import (
    Deb822FileMetadata,
    Deb822KnownField,
    StanzaMetadata,
    F,
    S,
)
from debputy.lsp.lsp_features import SEMANTIC_TOKEN_TYPES_IDS
from debputy.lsp.text_util import (
    te_position_to_lsp,
    trim_end_of_line_whitespace,
    SemanticTokensState,
)
from debputy.lsp.vendoring._deb822_repro.locatable import (
    START_POSITION,
    Range as TERange,
)
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    Deb822ParagraphElement,
    Deb822FileElement,
)
from debputy.lsp.vendoring._deb822_repro.tokens import tokenize_deb822_file, Deb822Token
from debputy.lsp.vendoring._deb822_repro.types import TokenOrElement
from debputy.util import _info, _warn

try:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


_CONTAINS_SPACE_OR_COLON = re.compile(r"[\s:]")


def in_range(
    te_range: TERange,
    cursor_position: Position,
    *,
    inclusive_end: bool = False,
) -> bool:
    cursor_line = cursor_position.line
    start_pos = te_range.start_pos
    end_pos = te_range.end_pos
    if cursor_line < start_pos.line_position or cursor_line > end_pos.line_position:
        return False

    if start_pos.line_position == end_pos.line_position:
        start_col = start_pos.cursor_position
        cursor_col = cursor_position.character
        end_col = end_pos.cursor_position
        if inclusive_end:
            return start_col <= cursor_col <= end_col
        return start_col <= cursor_col < end_col

    if cursor_line == end_pos.line_position:
        return cursor_position.character < end_pos.cursor_position

    return (
        cursor_line > start_pos.line_position
        or start_pos.cursor_position <= cursor_position.character
    )


def _field_at_position(
    stanza: Deb822ParagraphElement,
    stanza_metadata: S,
    stanza_range: TERange,
    position: Position,
) -> Tuple[Optional[Deb822KeyValuePairElement], Optional[F], str, bool]:
    te_range = TERange(stanza_range.start_pos, stanza_range.start_pos)
    for token_or_element in stanza.iter_parts():
        te_range = token_or_element.size().relative_to(te_range.end_pos)
        if not in_range(te_range, position):
            continue
        if isinstance(token_or_element, Deb822KeyValuePairElement):
            value_range = token_or_element.value_element.range_in_parent().relative_to(
                te_range.start_pos
            )
            known_field = stanza_metadata.get(token_or_element.field_name)
            in_value = in_range(value_range, position)
            interpreter = (
                known_field.field_value_class.interpreter()
                if known_field is not None
                else None
            )
            matched_value = ""
            if in_value and interpreter is not None:
                interpreted = token_or_element.interpret_as(interpreter)
                for value_ref in interpreted.iter_value_references():
                    value_token_range = (
                        value_ref.locatable.range_in_parent().relative_to(
                            value_range.start_pos
                        )
                    )
                    if in_range(value_token_range, position, inclusive_end=True):
                        matched_value = value_ref.value
                        break
            return token_or_element, known_field, matched_value, in_value
    return None, None, "", False


def _allow_stanza_continuation(
    token_or_element: TokenOrElement,
    is_completion: bool,
) -> bool:
    if not is_completion:
        return False
    if token_or_element.is_error or token_or_element.is_comment:
        return True
    return (
        token_or_element.is_whitespace
        and token_or_element.convert_to_text().count("\n") < 2
    )


def _at_cursor(
    deb822_file: Deb822FileElement,
    file_metadata: Deb822FileMetadata[S],
    doc: "TextDocument",
    lines: List[str],
    client_position: Position,
    is_completion: bool = False,
) -> Tuple[
    Position,
    Optional[str],
    str,
    bool,
    Optional[S],
    Optional[F],
    Iterable[Deb822ParagraphElement],
]:
    server_position = doc.position_codec.position_from_client_units(
        lines,
        client_position,
    )
    te_range = TERange(
        START_POSITION,
        START_POSITION,
    )
    paragraph_no = -1
    previous_stanza: Optional[Deb822ParagraphElement] = None
    next_stanza: Optional[Deb822ParagraphElement] = None
    current_word = doc.word_at_position(client_position)
    in_value: bool = False
    file_iter = iter(deb822_file.iter_parts())
    matched_token: Optional[TokenOrElement] = None
    matched_field: Optional[str] = None
    stanza_metadata: Optional[S] = None
    known_field: Optional[F] = None

    for token_or_element in file_iter:
        te_range = token_or_element.size().relative_to(te_range.end_pos)
        if isinstance(token_or_element, Deb822ParagraphElement):
            previous_stanza = token_or_element
            paragraph_no += 1
        elif not _allow_stanza_continuation(token_or_element, is_completion):
            previous_stanza = None
        if not in_range(te_range, server_position):
            continue
        matched_token = token_or_element
        if isinstance(token_or_element, Deb822ParagraphElement):
            stanza_metadata = file_metadata.guess_stanza_classification_by_idx(
                paragraph_no
            )
            kvpair, known_field, current_word, in_value = _field_at_position(
                token_or_element,
                stanza_metadata,
                te_range,
                server_position,
            )
            if kvpair is not None:
                matched_field = kvpair.field_name
        break

    if matched_token is not None and _allow_stanza_continuation(
        matched_token,
        is_completion,
    ):
        next_te = next(file_iter, None)
        if isinstance(next_te, Deb822ParagraphElement):
            next_stanza = next_te

    stanza_parts = (p for p in (previous_stanza, next_stanza) if p is not None)

    if stanza_metadata is None and is_completion:
        if paragraph_no < 0:
            paragraph_no = 0
        stanza_metadata = file_metadata.guess_stanza_classification_by_idx(paragraph_no)

    return (
        server_position,
        matched_field,
        current_word,
        in_value,
        stanza_metadata,
        known_field,
        stanza_parts,
    )


def deb822_completer(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lines = doc.lines
    lint_state = ls.lint_state(doc)
    deb822_file = lint_state.parsed_deb822_file_content
    if deb822_file is None:
        _warn("The deb822 result missing failed!?")
        ls.show_message_log(
            "Internal error; could not get deb822 content!?", MessageType.Warning
        )
        return None

    (
        _a,
        current_field,
        word_at_position,
        in_value,
        stanza_metadata,
        known_field,
        matched_stanzas,
    ) = _at_cursor(
        deb822_file,
        file_metadata,
        doc,
        lines,
        params.position,
        is_completion=True,
    )

    items: Optional[Sequence[CompletionItem]]
    markdown_kind = ls.completion_item_document_markup(
        MarkupKind.Markdown, MarkupKind.PlainText
    )
    if in_value:
        _info(f"Completion for field value {current_field} -- {word_at_position}")
        if known_field is None:
            return None
        value_being_completed = word_at_position
        items = known_field.value_options_for_completer(
            lint_state,
            list(matched_stanzas),
            value_being_completed,
            markdown_kind,
        )
    else:
        _info("Completing field name")
        assert stanza_metadata is not None
        items = _complete_field_name(
            lint_state,
            stanza_metadata,
            matched_stanzas,
            markdown_kind,
        )

    _info(
        f"Completion candidates: {[i.label for i in items] if items is not None else 'None'}"
    )

    return items


def deb822_hover(
    ls: "DebputyLanguageServer",
    params: HoverParams,
    file_metadata: Deb822FileMetadata[S],
    *,
    custom_handler: Optional[
        Callable[
            [
                "DebputyLanguageServer",
                Position,
                Optional[str],
                str,
                Optional[F],
                bool,
                "TextDocument",
                List[str],
            ],
            Optional[Hover],
        ]
    ] = None,
) -> Optional[Hover]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lines = doc.lines
    deb822_file = ls.lint_state(doc).parsed_deb822_file_content
    if deb822_file is None:
        _warn("The deb822 result missing failed!?")
        ls.show_message_log(
            "Internal error; could not get deb822 content!?", MessageType.Warning
        )
        return None

    (
        server_pos,
        current_field,
        word_at_position,
        in_value,
        _,
        known_field,
        _,
    ) = _at_cursor(
        deb822_file,
        file_metadata,
        doc,
        lines,
        params.position,
    )
    hover_text = None
    if custom_handler is not None:
        res = custom_handler(
            ls,
            server_pos,
            current_field,
            word_at_position,
            known_field,
            in_value,
            doc,
            lines,
        )
        if isinstance(res, Hover):
            return res
        hover_text = res

    if hover_text is None:
        if current_field is None:
            _info("No hover information as we cannot determine which field it is for")
            return None

        if known_field is None:
            return None
        if in_value:
            if not known_field.known_values:
                return None
            keyword = known_field.known_values.get(word_at_position)
            if keyword is None:
                return None
            hover_text = keyword.hover_text
            if hover_text is not None:
                header = "`{VALUE}` (Field: {FIELD_NAME})".format(
                    VALUE=keyword.value,
                    FIELD_NAME=known_field.name,
                )
                hover_text = f"# {header})\n\n{hover_text}"
        else:
            hover_text = known_field.hover_text
            if hover_text is None:
                hover_text = (
                    f"No documentation is available for the field {current_field}."
                )
            hover_text = f"# {known_field.name}\n\n{hover_text}"

    if hover_text is None:
        return None
    return Hover(
        contents=MarkupContent(
            kind=ls.hover_markup_format(MarkupKind.Markdown, MarkupKind.PlainText),
            value=hover_text,
        )
    )


def deb822_token_iter(
    tokens: Iterable[Deb822Token],
) -> Iterator[Tuple[Deb822Token, int, int, int, int]]:
    line_no = 0
    line_offset = 0

    for token in tokens:
        start_line = line_no
        start_line_offset = line_offset

        newlines = token.text.count("\n")
        line_no += newlines
        text_len = len(token.text)
        if newlines:
            if token.text.endswith("\n"):
                line_offset = 0
            else:
                # -2, one to remove the "\n" and one to get 0-offset
                line_offset = text_len - token.text.rindex("\n") - 2
        else:
            line_offset += text_len

        yield token, start_line, start_line_offset, line_no, line_offset


def deb822_folding_ranges(
    ls: "DebputyLanguageServer",
    params: FoldingRangeParams,
    # Unused for now: might be relevant for supporting folding for some fields
    _file_metadata: Deb822FileMetadata[Any],
) -> Optional[Sequence[FoldingRange]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    comment_start = -1
    folding_ranges = []
    for (
        token,
        start_line,
        start_offset,
        end_line,
        end_offset,
    ) in deb822_token_iter(tokenize_deb822_file(doc.lines)):
        if token.is_comment:
            if comment_start < 0:
                comment_start = start_line
        elif comment_start > -1:
            comment_start = -1
            folding_range = FoldingRange(
                comment_start,
                end_line,
                kind=FoldingRangeKind.Comment,
            )

            folding_ranges.append(folding_range)

    return folding_ranges


class Deb822SemanticTokensState(SemanticTokensState):

    __slots__ = (
        "file_metadata",
        "keyword_token_code",
        "known_value_token_code",
        "comment_token_code",
    )

    def __init__(
        self,
        ls: "DebputyLanguageServer",
        doc: "TextDocument",
        lines: List[str],
        tokens: List[int],
        file_metadata: Deb822FileMetadata[Any],
        keyword_token_code: int,
        known_value_token_code: int,
        comment_token_code: int,
    ) -> None:
        super().__init__(ls, doc, lines, tokens)
        self.file_metadata = file_metadata
        self.keyword_token_code = keyword_token_code
        self.known_value_token_code = known_value_token_code
        self.comment_token_code = comment_token_code


def _deb822_paragraph_semantic_tokens_full(
    sem_token_state: Deb822SemanticTokensState,
    stanza: Deb822ParagraphElement,
    stanza_idx: int,
) -> None:
    doc = sem_token_state.doc
    keyword_token_code = sem_token_state.keyword_token_code
    known_value_token_code = sem_token_state.known_value_token_code
    comment_token_code = sem_token_state.comment_token_code

    stanza_position = stanza.position_in_file()
    stanza_metadata = sem_token_state.file_metadata.classify_stanza(
        stanza,
        stanza_idx=stanza_idx,
    )
    for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
        kvpair_position = kvpair.position_in_parent().relative_to(stanza_position)
        field_start = kvpair.field_token.position_in_parent().relative_to(
            kvpair_position
        )
        comment = kvpair.comment_element
        if comment:
            comment_start_line = field_start.line_position - len(comment)
            for comment_line_no, comment_token in enumerate(
                comment.iter_parts(),
                start=comment_start_line,
            ):
                assert comment_token.is_comment
                assert isinstance(comment_token, Deb822Token)
                sem_token_state.emit_token(
                    Position(comment_line_no, 0),
                    len(comment_token.text.rstrip()),
                    comment_token_code,
                )
        field_size = doc.position_codec.client_num_units(kvpair.field_name)

        sem_token_state.emit_token(
            te_position_to_lsp(field_start),
            field_size,
            keyword_token_code,
        )

        known_field: Optional[Deb822KnownField] = stanza_metadata.get(kvpair.field_name)
        if known_field is not None:
            if known_field.spellcheck_value:
                continue
            known_values: Container[str] = known_field.known_values or frozenset()
            interpretation = known_field.field_value_class.interpreter()
        else:
            known_values = frozenset()
            interpretation = None

        value_element_pos = kvpair.value_element.position_in_parent().relative_to(
            kvpair_position
        )
        if interpretation is None:
            # TODO: Emit tokens for value comments of unknown fields.
            continue
        else:
            parts = kvpair.interpret_as(interpretation).iter_parts()
        for te in parts:
            if te.is_whitespace:
                continue
            if te.is_separator:
                continue
            value_range_in_parent_te = te.range_in_parent()
            value_range_te = value_range_in_parent_te.relative_to(value_element_pos)
            value = te.convert_to_text()
            if te.is_comment:
                token_type = comment_token_code
                value = value.rstrip()
            elif value in known_values:
                token_type = known_value_token_code
            else:
                continue
            value_len = doc.position_codec.client_num_units(value)

            sem_token_state.emit_token(
                te_position_to_lsp(value_range_te.start_pos),
                value_len,
                token_type,
            )


def deb822_format_file(
    lint_state: LintState,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[Sequence[TextEdit]]:
    effective_preference = lint_state.effective_preference
    if effective_preference is None:
        return trim_end_of_line_whitespace(lint_state.position_codec, lint_state.lines)
    formatter = effective_preference.deb822_formatter()
    lines = lint_state.lines
    deb822_file = lint_state.parsed_deb822_file_content
    if deb822_file is None:
        _warn("The deb822 result missing failed!?")
        return None

    return list(
        file_metadata.reformat(
            effective_preference,
            deb822_file,
            formatter,
            lint_state.content,
            lint_state.position_codec,
            lines,
        )
    )


def deb822_semantic_tokens_full(
    ls: "DebputyLanguageServer",
    request: SemanticTokensParams,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[SemanticTokens]:
    doc = ls.workspace.get_text_document(request.text_document.uri)
    position_codec = doc.position_codec
    lines = doc.lines
    deb822_file = ls.lint_state(doc).parsed_deb822_file_content
    if deb822_file is None:
        _warn("The deb822 result missing failed!?")
        ls.show_message_log(
            "Internal error; could not get deb822 content!?", MessageType.Warning
        )
        return None

    tokens: List[int] = []
    comment_token_code = SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.Comment.value]
    sem_token_state = Deb822SemanticTokensState(
        ls,
        doc,
        lines,
        tokens,
        file_metadata,
        SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.Keyword],
        SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.EnumMember],
        comment_token_code,
    )

    stanza_idx = 0

    for part in deb822_file.iter_parts():
        if part.is_comment:
            pos = part.position_in_file()
            sem_token_state.emit_token(
                te_position_to_lsp(pos),
                # Avoid trailing newline
                position_codec.client_num_units(part.convert_to_text().rstrip()),
                comment_token_code,
            )
        elif isinstance(part, Deb822ParagraphElement):
            _deb822_paragraph_semantic_tokens_full(
                sem_token_state,
                part,
                stanza_idx,
            )
            stanza_idx += 1
    if not tokens:
        return None
    return SemanticTokens(tokens)


def _complete_field_name(
    lint_state: LintState,
    fields: StanzaMetadata[Any],
    matched_stanzas: Iterable[Deb822ParagraphElement],
    markdown_kind: MarkupKind,
) -> Sequence[CompletionItem]:
    items = []
    matched_stanzas = list(matched_stanzas)
    # TODO: Normalize fields according to file rules (X[BCS]- should be stripped in some files)
    seen_fields = set(
        f.lower()
        for f in chain.from_iterable(
            # The typing from python3-debian is not entirely optimal here. The iter always return a
            # `str`, but the provided type is `ParagraphKey` (because `__getitem__` supports those)
            # and that is not exclusively a `str`.
            #
            # So, this cast for now
            cast("Iterable[str]", s)
            for s in matched_stanzas
        )
    )
    for cand_key, cand in fields.items():
        if cand_key.lower() in seen_fields:
            continue
        item = cand.complete_field(lint_state, matched_stanzas, markdown_kind)
        if item is not None:
            items.append(item)
    return items
