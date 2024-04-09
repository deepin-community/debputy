import re
from typing import (
    Union,
    Sequence,
    Tuple,
    Iterator,
    Optional,
    Iterable,
    Mapping,
    List,
)

from lsprotocol.types import (
    DiagnosticSeverity,
    Range,
    Diagnostic,
    Position,
    CompletionItem,
    CompletionList,
    CompletionParams,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    DiagnosticRelatedInformation,
    Location,
    HoverParams,
    Hover,
    TEXT_DOCUMENT_CODE_ACTION,
    SemanticTokens,
    SemanticTokensParams,
    FoldingRangeParams,
    FoldingRange,
)

from debputy.lsp.lsp_debian_control_reference_data import (
    _DEP5_HEADER_FIELDS,
    _DEP5_FILES_FIELDS,
    Deb822KnownField,
    _DEP5_LICENSE_FIELDS,
    Dep5FileMetadata,
)
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_completer,
    lsp_hover,
    lsp_standard_handler,
    lsp_folding_ranges,
    lsp_semantic_tokens_full,
)
from debputy.lsp.lsp_generic_deb822 import (
    deb822_completer,
    deb822_hover,
    deb822_folding_ranges,
    deb822_semantic_tokens_full,
)
from debputy.lsp.quickfixes import (
    propose_correct_text_quick_fix,
)
from debputy.lsp.spellchecking import default_spellchecker
from debputy.lsp.text_util import (
    normalize_dctrl_field_name,
    LintCapablePositionCodec,
    detect_possible_typo,
    te_range_to_lsp,
)
from debputy.lsp.vendoring._deb822_repro import (
    parse_deb822_file,
    Deb822FileElement,
    Deb822ParagraphElement,
)
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    LIST_SPACE_SEPARATED_INTERPRETATION,
)
from debputy.lsp.vendoring._deb822_repro.tokens import (
    Deb822Token,
)

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
        START_POSITION,
    )

    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


_CONTAINS_SPACE_OR_COLON = re.compile(r"[\s:]")
_LANGUAGE_IDS = [
    "debian/copyright",
    # emacs's name
    "debian-copyright",
    # vim's name
    "debcopyright",
]

_DEP5_FILE_METADATA = Dep5FileMetadata()

lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)
lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)


@lsp_hover(_LANGUAGE_IDS)
def _debian_copyright_hover(
    ls: "LanguageServer",
    params: HoverParams,
) -> Optional[Hover]:
    return deb822_hover(ls, params, _DEP5_FILE_METADATA)


@lsp_completer(_LANGUAGE_IDS)
def _debian_copyright_completions(
    ls: "LanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    return deb822_completer(ls, params, _DEP5_FILE_METADATA)


@lsp_folding_ranges(_LANGUAGE_IDS)
def _debian_copyright_folding_ranges(
    ls: "LanguageServer",
    params: FoldingRangeParams,
) -> Optional[Sequence[FoldingRange]]:
    return deb822_folding_ranges(ls, params, _DEP5_FILE_METADATA)


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


def _paragraph_representation_field(
    paragraph: Deb822ParagraphElement,
) -> Deb822KeyValuePairElement:
    return next(iter(paragraph.iter_parts_of_type(Deb822KeyValuePairElement)))


def _diagnostics_for_paragraph(
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    known_fields: Mapping[str, Deb822KnownField],
    other_known_fields: Mapping[str, Deb822KnownField],
    is_files_or_license_paragraph: bool,
    doc_reference: str,
    position_codec: "LintCapablePositionCodec",
    lines: List[str],
    diagnostics: List[Diagnostic],
) -> None:
    representation_field = _paragraph_representation_field(stanza)
    representation_field_pos = representation_field.position_in_parent().relative_to(
        stanza_position
    )
    representation_field_range_server_units = te_range_to_lsp(
        TERange.from_position_and_size(
            representation_field_pos, representation_field.size()
        )
    )
    representation_field_range = position_codec.range_to_client_units(
        lines,
        representation_field_range_server_units,
    )
    for known_field in known_fields.values():
        missing_field_severity = known_field.missing_field_severity
        if missing_field_severity is None or known_field.name in stanza:
            continue

        diagnostics.append(
            Diagnostic(
                representation_field_range,
                f"Stanza is missing field {known_field.name}",
                severity=missing_field_severity,
                source="debputy",
            )
        )

    seen_fields = {}

    for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
        field_name_token = kvpair.field_token
        field_name = field_name_token.text
        field_name_lc = field_name.lower()
        normalized_field_name_lc = normalize_dctrl_field_name(field_name_lc)
        known_field = known_fields.get(normalized_field_name_lc)
        field_value = stanza[field_name]
        field_range_te = kvpair.range_in_parent().relative_to(stanza_position)
        field_position_te = field_range_te.start_pos
        field_range_server_units = te_range_to_lsp(field_range_te)
        field_range = position_codec.range_to_client_units(
            lines,
            field_range_server_units,
        )
        field_name_typo_detected = False
        existing_field_range = seen_fields.get(normalized_field_name_lc)
        if existing_field_range is not None:
            existing_field_range[3].append(field_range)
        else:
            normalized_field_name = normalize_dctrl_field_name(field_name)
            seen_fields[field_name_lc] = (
                field_name,
                normalized_field_name,
                field_range,
                [],
            )

        if known_field is None:
            candidates = detect_possible_typo(normalized_field_name_lc, known_fields)
            if candidates:
                known_field = known_fields[candidates[0]]
                token_range_server_units = te_range_to_lsp(
                    TERange.from_position_and_size(
                        field_position_te, kvpair.field_token.size()
                    )
                )
                field_range = position_codec.range_to_client_units(
                    lines,
                    token_range_server_units,
                )
                field_name_typo_detected = True
                diagnostics.append(
                    Diagnostic(
                        field_range,
                        f'The "{field_name}" looks like a typo of "{known_field.name}".',
                        severity=DiagnosticSeverity.Warning,
                        source="debputy",
                        data=[
                            propose_correct_text_quick_fix(known_fields[m].name)
                            for m in candidates
                        ],
                    )
                )
        if known_field is None:
            known_else_where = other_known_fields.get(normalized_field_name_lc)
            if known_else_where is not None:
                intended_usage = (
                    "Header" if is_files_or_license_paragraph else "Files/License"
                )
                diagnostics.append(
                    Diagnostic(
                        field_range,
                        f'The {field_name} is defined for use in the "{intended_usage}" stanza.'
                        f" Please move it to the right place or remove it",
                        severity=DiagnosticSeverity.Error,
                        source="debputy",
                    )
                )
            continue

        if field_value.strip() == "":
            diagnostics.append(
                Diagnostic(
                    field_range,
                    f"The {field_name} has no value. Either provide a value or remove it.",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                )
            )
            continue
        diagnostics.extend(
            known_field.field_diagnostics(
                kvpair,
                stanza,
                stanza_position,
                position_codec,
                lines,
                field_name_typo_reported=field_name_typo_detected,
            )
        )
        if known_field.spellcheck_value:
            words = kvpair.interpret_as(LIST_SPACE_SEPARATED_INTERPRETATION)
            spell_checker = default_spellchecker()
            value_position = kvpair.value_element.position_in_parent().relative_to(
                field_position_te
            )
            for word_ref in words.iter_value_references():
                token = word_ref.value
                for word, pos, endpos in spell_checker.iter_words(token):
                    corrections = spell_checker.provide_corrections_for(word)
                    if not corrections:
                        continue
                    word_loc = word_ref.locatable
                    word_pos_te = word_loc.position_in_parent().relative_to(
                        value_position
                    )
                    if pos:
                        word_pos_te = TEPosition(0, pos).relative_to(word_pos_te)
                    word_range = TERange(
                        START_POSITION,
                        TEPosition(0, endpos - pos),
                    )
                    word_range_server_units = te_range_to_lsp(
                        TERange.from_position_and_size(word_pos_te, word_range)
                    )
                    word_range = position_codec.range_to_client_units(
                        lines,
                        word_range_server_units,
                    )
                    diagnostics.append(
                        Diagnostic(
                            word_range,
                            f'Spelling "{word}"',
                            severity=DiagnosticSeverity.Hint,
                            source="debputy",
                            data=[
                                propose_correct_text_quick_fix(c) for c in corrections
                            ],
                        )
                    )
        if known_field.warn_if_default and field_value == known_field.default_value:
            diagnostics.append(
                Diagnostic(
                    field_range,
                    f"The {field_name} is redundant as it is set to the default value and the field should only be"
                    " used in exceptional cases.",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                )
            )
    for (
        field_name,
        normalized_field_name,
        field_range,
        duplicates,
    ) in seen_fields.values():
        if not duplicates:
            continue
        related_information = [
            DiagnosticRelatedInformation(
                location=Location(doc_reference, field_range),
                message=f"First definition of {field_name}",
            )
        ]
        related_information.extend(
            DiagnosticRelatedInformation(
                location=Location(doc_reference, r),
                message=f"Duplicate of {field_name}",
            )
            for r in duplicates
        )
        for dup_range in duplicates:
            diagnostics.append(
                Diagnostic(
                    dup_range,
                    f"The {normalized_field_name} field name was used multiple times in this stanza."
                    f" Please ensure the field is only used once per stanza. Note that {normalized_field_name} and"
                    f" X[BCS]-{normalized_field_name} are considered the same field.",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                    related_information=related_information,
                )
            )


def _scan_for_syntax_errors_and_token_level_diagnostics(
    deb822_file: Deb822FileElement,
    position_codec: LintCapablePositionCodec,
    lines: List[str],
    diagnostics: List[Diagnostic],
) -> int:
    first_error = len(lines) + 1
    spell_checker = default_spellchecker()
    for (
        token,
        start_line,
        start_offset,
        end_line,
        end_offset,
    ) in _deb822_token_iter(deb822_file.iter_tokens()):
        if token.is_error:
            first_error = min(first_error, start_line)
            start_pos = Position(
                start_line,
                start_offset,
            )
            end_pos = Position(
                end_line,
                end_offset,
            )
            token_range = position_codec.range_to_client_units(
                lines, Range(start_pos, end_pos)
            )
            diagnostics.append(
                Diagnostic(
                    token_range,
                    "Syntax error",
                    severity=DiagnosticSeverity.Error,
                    source="debputy (python-debian parser)",
                )
            )
        elif token.is_comment:
            for word, pos, end_pos in spell_checker.iter_words(token.text):
                corrections = spell_checker.provide_corrections_for(word)
                if not corrections:
                    continue
                start_pos = Position(
                    start_line,
                    pos,
                )
                end_pos = Position(
                    start_line,
                    end_pos,
                )
                word_range = position_codec.range_to_client_units(
                    lines, Range(start_pos, end_pos)
                )
                diagnostics.append(
                    Diagnostic(
                        word_range,
                        f'Spelling "{word}"',
                        severity=DiagnosticSeverity.Hint,
                        source="debputy",
                        data=[propose_correct_text_quick_fix(c) for c in corrections],
                    )
                )
    return first_error


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_copyright(
    doc_reference: str,
    _path: str,
    lines: List[str],
    position_codec: LintCapablePositionCodec,
) -> Optional[List[Diagnostic]]:
    diagnostics = []
    deb822_file = parse_deb822_file(
        lines,
        accept_files_with_duplicated_fields=True,
        accept_files_with_error_tokens=True,
    )

    first_error = _scan_for_syntax_errors_and_token_level_diagnostics(
        deb822_file,
        position_codec,
        lines,
        diagnostics,
    )

    paragraphs = list(deb822_file)
    is_dep5 = False

    for paragraph_no, paragraph in enumerate(paragraphs, start=1):
        paragraph_pos = paragraph.position_in_file()
        if paragraph_pos.line_position >= first_error:
            break
        is_files_or_license_paragraph = paragraph_no != 1
        if is_files_or_license_paragraph:
            known_fields = (
                _DEP5_FILES_FIELDS if "Files" in paragraph else _DEP5_LICENSE_FIELDS
            )
            other_known_fields = _DEP5_HEADER_FIELDS
        elif "Format" in paragraph:
            is_dep5 = True
            known_fields = _DEP5_HEADER_FIELDS
            other_known_fields = _DEP5_FILES_FIELDS
        else:
            break
        _diagnostics_for_paragraph(
            paragraph,
            paragraph_pos,
            known_fields,
            other_known_fields,
            is_files_or_license_paragraph,
            doc_reference,
            position_codec,
            lines,
            diagnostics,
        )
    if not is_dep5:
        return None
    return diagnostics


@lsp_semantic_tokens_full(_LANGUAGE_IDS)
def _semantic_tokens_full(
    ls: "LanguageServer",
    request: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    return deb822_semantic_tokens_full(
        ls,
        request,
        _DEP5_FILE_METADATA,
    )
