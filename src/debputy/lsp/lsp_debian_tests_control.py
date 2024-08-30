import re
from typing import (
    Union,
    Sequence,
    Tuple,
    Optional,
    Mapping,
    List,
    Dict,
)

from debputy.lsprotocol.types import (
    DiagnosticSeverity,
    Range,
    Diagnostic,
    Position,
    CompletionItem,
    CompletionList,
    CompletionParams,
    DiagnosticRelatedInformation,
    Location,
    HoverParams,
    Hover,
    TEXT_DOCUMENT_CODE_ACTION,
    SemanticTokens,
    SemanticTokensParams,
    FoldingRangeParams,
    FoldingRange,
    WillSaveTextDocumentParams,
    TextEdit,
    DocumentFormattingParams,
)

from debputy.linting.lint_util import LintState
from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsp.diagnostics import DiagnosticData
from debputy.lsp.lsp_debian_control_reference_data import (
    Deb822KnownField,
    DTestsCtrlFileMetadata,
    _DTESTSCTRL_FIELDS,
)
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_completer,
    lsp_hover,
    lsp_standard_handler,
    lsp_folding_ranges,
    lsp_semantic_tokens_full,
    lsp_will_save_wait_until,
    lsp_format_document,
    LanguageDispatch,
)
from debputy.lsp.lsp_generic_deb822 import (
    deb822_completer,
    deb822_hover,
    deb822_folding_ranges,
    deb822_semantic_tokens_full,
    deb822_token_iter,
    deb822_format_file,
)
from debputy.lsp.quickfixes import (
    propose_correct_text_quick_fix,
)
from debputy.lsp.spellchecking import default_spellchecker
from debputy.lsp.text_util import (
    normalize_dctrl_field_name,
    LintCapablePositionCodec,
    te_range_to_lsp,
)
from debputy.lsp.vendoring._deb822_repro import (
    Deb822FileElement,
    Deb822ParagraphElement,
)
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    LIST_SPACE_SEPARATED_INTERPRETATION,
)
from debputy.util import detect_possible_typo

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
    LanguageDispatch.from_language_id("debian/tests/control"),
    # emacs's name - expected in elpa-dpkg-dev-el (>> 37.11)
    LanguageDispatch.from_language_id("debian-autopkgtest-control-mode"),
    # Likely to be vim's name if it had support
    LanguageDispatch.from_language_id("debtestscontrol"),
]

_DTESTS_CTRL_FILE_METADATA = DTestsCtrlFileMetadata()

lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)


@lsp_hover(_LANGUAGE_IDS)
def debian_tests_control_hover(
    ls: "DebputyLanguageServer",
    params: HoverParams,
) -> Optional[Hover]:
    return deb822_hover(ls, params, _DTESTS_CTRL_FILE_METADATA)


@lsp_completer(_LANGUAGE_IDS)
def debian_tests_control_completions(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    return deb822_completer(ls, params, _DTESTS_CTRL_FILE_METADATA)


@lsp_folding_ranges(_LANGUAGE_IDS)
def debian_tests_control_folding_ranges(
    ls: "DebputyLanguageServer",
    params: FoldingRangeParams,
) -> Optional[Sequence[FoldingRange]]:
    return deb822_folding_ranges(ls, params, _DTESTS_CTRL_FILE_METADATA)


def _paragraph_representation_field(
    paragraph: Deb822ParagraphElement,
) -> Deb822KeyValuePairElement:
    return next(iter(paragraph.iter_parts_of_type(Deb822KeyValuePairElement)))


def _diagnostics_for_paragraph(
    deb822_file: Deb822FileElement,
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    known_fields: Mapping[str, Deb822KnownField],
    doc_reference: str,
    lint_state: LintState,
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
    representation_field_range = lint_state.position_codec.range_to_client_units(
        lint_state.lines,
        representation_field_range_server_units,
    )
    for known_field in known_fields.values():
        if known_field.name in stanza:
            continue

        diagnostics.extend(
            known_field.field_omitted_diagnostics(
                deb822_file,
                representation_field_range,
                stanza,
                stanza_position,
                None,
                lint_state,
            )
        )

    if "Tests" not in stanza and "Test-Command" not in stanza:
        diagnostics.append(
            Diagnostic(
                representation_field_range,
                'Stanza must have either a "Tests" or a "Test-Command" field',
                severity=DiagnosticSeverity.Error,
                source="debputy",
            )
        )
    if "Tests" in stanza and "Test-Command" in stanza:
        diagnostics.append(
            Diagnostic(
                representation_field_range,
                'Stanza cannot have both a "Tests" and a "Test-Command" field',
                severity=DiagnosticSeverity.Error,
                source="debputy",
            )
        )

    seen_fields: Dict[str, Tuple[str, str, Range, List[Range]]] = {}

    for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
        field_name_token = kvpair.field_token
        field_name = field_name_token.text
        field_name_lc = field_name.lower()
        normalized_field_name_lc = normalize_dctrl_field_name(field_name_lc)
        known_field = known_fields.get(normalized_field_name_lc)
        field_value = stanza[field_name]
        kvpair_range_te = kvpair.range_in_parent().relative_to(stanza_position)
        field_range_te = kvpair.field_token.range_in_parent().relative_to(
            kvpair_range_te.start_pos
        )
        field_position_te = field_range_te.start_pos
        field_range_server_units = te_range_to_lsp(field_range_te)
        field_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
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
                field_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    token_range_server_units,
                )
                field_name_typo_detected = True
                diagnostics.append(
                    Diagnostic(
                        field_range,
                        f'The "{field_name}" looks like a typo of "{known_field.name}".',
                        severity=DiagnosticSeverity.Warning,
                        source="debputy",
                        data=DiagnosticData(
                            quickfixes=[
                                propose_correct_text_quick_fix(known_fields[m].name)
                                for m in candidates
                            ]
                        ),
                    )
                )
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
        if known_field is None:
            continue
        diagnostics.extend(
            known_field.field_diagnostics(
                deb822_file,
                kvpair,
                stanza,
                stanza_position,
                kvpair_range_te,
                lint_state,
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
                    word_range = lint_state.position_codec.range_to_client_units(
                        lint_state.lines,
                        word_range_server_units,
                    )
                    diagnostics.append(
                        Diagnostic(
                            word_range,
                            f'Spelling "{word}"',
                            severity=DiagnosticSeverity.Hint,
                            source="debputy",
                            data=DiagnosticData(
                                lint_severity="spelling",
                                quickfixes=[
                                    propose_correct_text_quick_fix(c)
                                    for c in corrections
                                ],
                            ),
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
                    f" Please ensure the field is only used once per stanza.",
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
    ) in deb822_token_iter(deb822_file.iter_tokens()):
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
                        data=DiagnosticData(
                            lint_severity="spelling",
                            quickfixes=[
                                propose_correct_text_quick_fix(c) for c in corrections
                            ],
                        ),
                    )
                )
    return first_error


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_tests_control(
    lint_state: LintState,
) -> Optional[List[Diagnostic]]:
    lines = lint_state.lines
    position_codec = lint_state.position_codec
    doc_reference = lint_state.doc_uri
    diagnostics: List[Diagnostic] = []
    deb822_file = lint_state.parsed_deb822_file_content

    first_error = _scan_for_syntax_errors_and_token_level_diagnostics(
        deb822_file,
        position_codec,
        lines,
        diagnostics,
    )

    paragraphs = list(deb822_file)

    for paragraph_no, paragraph in enumerate(paragraphs, start=1):
        paragraph_pos = paragraph.position_in_file()
        if paragraph_pos.line_position >= first_error:
            break
        known_fields = _DTESTSCTRL_FIELDS
        _diagnostics_for_paragraph(
            deb822_file,
            paragraph,
            paragraph_pos,
            known_fields,
            doc_reference,
            lint_state,
            diagnostics,
        )
    return diagnostics


@lsp_will_save_wait_until(_LANGUAGE_IDS)
def _debian_tests_control_on_save_formatting(
    ls: "DebputyLanguageServer",
    params: WillSaveTextDocumentParams,
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lint_state = ls.lint_state(doc)
    return deb822_format_file(lint_state, _DTESTS_CTRL_FILE_METADATA)


def _reformat_debian_tests_control(
    lint_state: LintState,
) -> Optional[Sequence[TextEdit]]:
    return deb822_format_file(lint_state, _DTESTS_CTRL_FILE_METADATA)


@lsp_format_document(_LANGUAGE_IDS)
def _debian_tests_control_on_save_formatting(
    ls: "DebputyLanguageServer",
    params: DocumentFormattingParams,
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lint_state = ls.lint_state(doc)
    return deb822_format_file(lint_state, _DTESTS_CTRL_FILE_METADATA)


@lsp_semantic_tokens_full(_LANGUAGE_IDS)
def _debian_tests_control_semantic_tokens_full(
    ls: "DebputyLanguageServer",
    request: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    return deb822_semantic_tokens_full(
        ls,
        request,
        _DTESTS_CTRL_FILE_METADATA,
    )
