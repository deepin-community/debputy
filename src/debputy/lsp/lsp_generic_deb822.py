import re
from typing import (
    Optional,
    Union,
    Sequence,
    Tuple,
    Set,
    Any,
    Container,
    List,
    Iterable,
    Iterator,
)

from lsprotocol.types import (
    CompletionParams,
    CompletionList,
    CompletionItem,
    Position,
    CompletionItemTag,
    MarkupContent,
    Hover,
    MarkupKind,
    HoverParams,
    FoldingRangeParams,
    FoldingRange,
    FoldingRangeKind,
    SemanticTokensParams,
    SemanticTokens,
)

from debputy.lsp.lsp_debian_control_reference_data import (
    Deb822FileMetadata,
    Deb822KnownField,
    StanzaMetadata,
    FieldValueClass,
)
from debputy.lsp.lsp_features import SEMANTIC_TOKEN_TYPES_IDS
from debputy.lsp.text_util import normalize_dctrl_field_name
from debputy.lsp.vendoring._deb822_repro import parse_deb822_file
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    LIST_SPACE_SEPARATED_INTERPRETATION,
)
from debputy.lsp.vendoring._deb822_repro.tokens import tokenize_deb822_file, Deb822Token
from debputy.util import _info

try:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


_CONTAINS_SPACE_OR_COLON = re.compile(r"[\s:]")


def _at_cursor(
    doc: "TextDocument",
    lines: List[str],
    client_position: Position,
) -> Tuple[Optional[str], str, bool, int, Set[str]]:
    paragraph_no = -1
    paragraph_started = False
    seen_fields = set()
    last_field_seen: Optional[str] = None
    current_field: Optional[str] = None
    server_position = doc.position_codec.position_from_client_units(
        lines,
        client_position,
    )
    position_line_no = server_position.line

    line_at_position = lines[position_line_no]
    line_start = ""
    if server_position.character:
        line_start = line_at_position[0 : server_position.character]

    for line_no, line in enumerate(lines):
        if not line or line.isspace():
            if line_no == position_line_no:
                current_field = last_field_seen
                continue
            last_field_seen = None
            if line_no > position_line_no:
                break
            paragraph_started = False
        elif line and line[0] == "#":
            continue
        elif line and not line[0].isspace() and ":" in line:
            if not paragraph_started:
                paragraph_started = True
                seen_fields = set()
                paragraph_no += 1
            key, _ = line.split(":", 1)
            key_lc = key.lower()
            last_field_seen = key_lc
            if line_no == position_line_no:
                current_field = key_lc
            seen_fields.add(key_lc)

    in_value = bool(_CONTAINS_SPACE_OR_COLON.search(line_start))
    current_word = doc.word_at_position(client_position)
    if current_field is not None:
        current_field = normalize_dctrl_field_name(current_field)
    return current_field, current_word, in_value, paragraph_no, seen_fields


def deb822_completer(
    ls: "LanguageServer",
    params: CompletionParams,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lines = doc.lines

    current_field, _, in_value, paragraph_no, seen_fields = _at_cursor(
        doc,
        lines,
        params.position,
    )

    stanza_metadata = file_metadata.guess_stanza_classification_by_idx(paragraph_no)

    if in_value:
        _info(f"Completion for field value {current_field}")
        if current_field is None:
            return None
        known_field = stanza_metadata.get(current_field)
        if known_field is None:
            return None
        items = _complete_field_value(known_field)
    else:
        _info("Completing field name")
        items = _complete_field_name(
            stanza_metadata,
            seen_fields,
        )

    _info(f"Completion candidates: {items}")

    return items


def deb822_hover(
    ls: "LanguageServer",
    params: HoverParams,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[Hover]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lines = doc.lines
    current_field, word_at_position, in_value, paragraph_no, _ = _at_cursor(
        doc, lines, params.position
    )
    stanza_metadata = file_metadata.guess_stanza_classification_by_idx(paragraph_no)

    if current_field is None:
        _info("No hover information as we cannot determine which field it is for")
        return None
    known_field = stanza_metadata.get(current_field)

    if known_field is None:
        return None
    if in_value:
        if not known_field.known_values:
            return
        keyword = known_field.known_values.get(word_at_position)
        if keyword is None:
            return
        hover_text = keyword.hover_text
    else:
        hover_text = known_field.hover_text
        if hover_text is None:
            hover_text = f"The field {current_field} had no documentation."

    try:
        supported_formats = ls.client_capabilities.text_document.hover.content_format
    except AttributeError:
        supported_formats = []

    _info(f"Supported formats {supported_formats}")
    markup_kind = MarkupKind.Markdown
    if markup_kind not in supported_formats:
        markup_kind = MarkupKind.PlainText
    return Hover(
        contents=MarkupContent(
            kind=markup_kind,
            value=hover_text,
        )
    )


def _deb822_token_iter(
    tokens: Iterable[Deb822Token],
) -> Iterator[Tuple[Deb822Token, int, int, int, int, int]]:
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
    ls: "LanguageServer",
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
    ) in _deb822_token_iter(tokenize_deb822_file(doc.lines)):
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


def deb822_semantic_tokens_full(
    ls: "LanguageServer",
    request: SemanticTokensParams,
    file_metadata: Deb822FileMetadata[Any],
) -> Optional[SemanticTokens]:
    doc = ls.workspace.get_text_document(request.text_document.uri)
    lines = doc.lines
    deb822_file = parse_deb822_file(
        lines,
        accept_files_with_duplicated_fields=True,
        accept_files_with_error_tokens=True,
    )
    tokens = []
    previous_line = 0
    keyword_token_code = SEMANTIC_TOKEN_TYPES_IDS["keyword"]
    known_value_token_code = SEMANTIC_TOKEN_TYPES_IDS["enumMember"]
    no_modifiers = 0

    # TODO: Add comment support; slightly complicated by how we parse the file.

    for stanza_idx, stanza in enumerate(deb822_file):
        stanza_position = stanza.position_in_file()
        stanza_metadata = file_metadata.classify_stanza(stanza, stanza_idx=stanza_idx)
        for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
            kvpair_pos = kvpair.position_in_parent().relative_to(stanza_position)
            # These two happen to be the same; the indirection is to make it explicit that the two
            # positions for different tokens are the same.
            field_position_without_comments = kvpair_pos
            field_size = doc.position_codec.client_num_units(kvpair.field_name)
            current_line = field_position_without_comments.line_position
            line_delta = current_line - previous_line
            previous_line = current_line
            tokens.append(line_delta)  # Line delta
            tokens.append(0)  # Token column delta
            tokens.append(field_size)  # Token length
            tokens.append(keyword_token_code)
            tokens.append(no_modifiers)

            known_field: Optional[Deb822KnownField] = stanza_metadata.get(
                kvpair.field_name
            )
            if (
                known_field is None
                or not known_field.known_values
                or known_field.spellcheck_value
            ):
                continue

            if known_field.field_value_class not in (
                FieldValueClass.SINGLE_VALUE,
                FieldValueClass.SPACE_SEPARATED_LIST,
            ):
                continue
            value_element_pos = kvpair.value_element.position_in_parent().relative_to(
                kvpair_pos
            )

            last_token_start_column = 0

            for value_ref in kvpair.interpret_as(
                LIST_SPACE_SEPARATED_INTERPRETATION
            ).iter_value_references():
                if value_ref.value not in known_field.known_values:
                    continue
                value_loc = value_ref.locatable
                value_range_te = value_loc.range_in_parent().relative_to(
                    value_element_pos
                )
                start_line = value_range_te.start_pos.line_position
                line_delta = start_line - current_line
                current_line = start_line
                if line_delta:
                    last_token_start_column = 0

                value_start_column = value_range_te.start_pos.cursor_position
                column_delta = value_start_column - last_token_start_column
                last_token_start_column = value_start_column

                tokens.append(line_delta)  # Line delta
                tokens.append(column_delta)  # Token column delta
                tokens.append(field_size)  # Token length
                tokens.append(known_value_token_code)
                tokens.append(no_modifiers)

    if not tokens:
        return None
    return SemanticTokens(tokens)


def _should_complete_field_with_value(cand: Deb822KnownField) -> bool:
    return cand.known_values is not None and (
        len(cand.known_values) == 1
        or (
            len(cand.known_values) == 2
            and cand.warn_if_default
            and cand.default_value is not None
        )
    )


def _complete_field_name(
    fields: StanzaMetadata[Any],
    seen_fields: Container[str],
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    items = []
    for cand_key, cand in fields.items():
        if cand_key.lower() in seen_fields:
            continue
        name = cand.name
        complete_as = name + ": "
        if _should_complete_field_with_value(cand):
            value = next(iter(v for v in cand.known_values if v != cand.default_value))
            complete_as += value
        tags = []
        if cand.replaced_by or cand.deprecated_with_no_replacement:
            tags.append(CompletionItemTag.Deprecated)

        items.append(
            CompletionItem(
                name,
                insert_text=complete_as,
                tags=tags,
            )
        )
    return items


def _complete_field_value(
    field: Deb822KnownField,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    if field.known_values is None:
        return None
    return [CompletionItem(v) for v in field.known_values]
