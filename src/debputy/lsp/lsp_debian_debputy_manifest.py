from typing import (
    Optional,
    List,
    Any,
    Tuple,
    Union,
    Iterable,
    Sequence,
    Literal,
    get_args,
    get_origin,
)

from lsprotocol.types import (
    Diagnostic,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    Position,
    Range,
    DiagnosticSeverity,
    HoverParams,
    Hover,
    MarkupKind,
    MarkupContent,
    TEXT_DOCUMENT_CODE_ACTION,
    CompletionParams,
    CompletionList,
    CompletionItem,
    DiagnosticRelatedInformation,
    Location,
)

from debputy.linting.lint_util import LintState
from debputy.lsp.quickfixes import propose_correct_text_quick_fix
from debputy.manifest_parser.base_types import DebputyDispatchableType
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.yaml.compat import (
    Node,
    CommentedMap,
    LineCol,
    CommentedSeq,
    CommentedBase,
    MarkedYAMLError,
    YAMLError,
)

from debputy.highlevel_manifest import MANIFEST_YAML
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_standard_handler,
    lsp_hover,
    lsp_completer,
)
from debputy.lsp.text_util import (
    LintCapablePositionCodec,
    detect_possible_typo,
)
from debputy.manifest_parser.declarative_parser import (
    AttributeDescription,
    ParserGenerator,
    DeclarativeNonMappingInputParser,
)
from debputy.manifest_parser.declarative_parser import DeclarativeMappingInputParser
from debputy.manifest_parser.parser_doc import (
    render_rule,
    render_attribute_doc,
    doc_args_for_parser_doc,
)
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.plugin.api.impl_types import (
    OPARSER_MANIFEST_ROOT,
    DeclarativeInputParser,
    DispatchingParserBase,
    DebputyPluginMetadata,
    ListWrappedDeclarativeInputParser,
    InPackageContextParser,
    DeclarativeValuelessKeywordInputParser,
)
from debputy.util import _info, _warn


try:
    from pygls.server import LanguageServer
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass


_LANGUAGE_IDS = [
    "debian/debputy.manifest",
    "debputy.manifest",
    # LSP's official language ID for YAML files
    "yaml",
]


lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)
lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)


def is_valid_file(path: str) -> bool:
    # For debian/debputy.manifest, the language ID is often set to makefile meaning we get random
    # "non-debian/debputy.manifest" YAML files here. Skip those.
    return path.endswith("debian/debputy.manifest")


def _word_range_at_position(
    lines: List[str],
    line_no: int,
    char_offset: int,
) -> Range:
    line = lines[line_no]
    line_len = len(line)
    start_idx = char_offset
    end_idx = char_offset
    while end_idx + 1 < line_len and not line[end_idx + 1].isspace():
        end_idx += 1

    while start_idx - 1 >= 0 and not line[start_idx - 1].isspace():
        start_idx -= 1

    return Range(
        Position(line_no, start_idx),
        Position(line_no, end_idx),
    )


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_debputy_manifest(
    lint_state: LintState,
) -> Optional[List[Diagnostic]]:
    lines = lint_state.lines
    position_codec = lint_state.position_codec
    path = lint_state.path
    if not is_valid_file(path):
        return None
    diagnostics = []
    try:
        content = MANIFEST_YAML.load("".join(lines))
    except MarkedYAMLError as e:
        if e.context_mark:
            line = e.context_mark.line
            column = e.context_mark.column + 1
        else:
            line = e.problem_mark.line
            column = e.problem_mark.column + 1
        error_range = position_codec.range_to_client_units(
            lines,
            _word_range_at_position(
                lines,
                line,
                column,
            ),
        )
        diagnostics.append(
            Diagnostic(
                error_range,
                f"YAML parse error: {e}",
                DiagnosticSeverity.Error,
            ),
        )
    except YAMLError as e:
        error_range = position_codec.range_to_client_units(
            lines,
            Range(
                Position(0, 0),
                Position(0, len(lines[0])),
            ),
        )
        diagnostics.append(
            Diagnostic(
                error_range,
                f"Unknown YAML parse error: {e} [{e!r}]",
                DiagnosticSeverity.Error,
            ),
        )
    else:
        feature_set = lint_state.plugin_feature_set
        pg = feature_set.manifest_parser_generator
        root_parser = pg.dispatchable_object_parsers[OPARSER_MANIFEST_ROOT]
        diagnostics.extend(
            _lint_content(
                lint_state,
                pg,
                root_parser,
                content,
            )
        )
    return diagnostics


def _unknown_key(
    key: str,
    expected_keys: Iterable[str],
    line: int,
    col: int,
    lines: List[str],
    position_codec: LintCapablePositionCodec,
    *,
    message_format: str = 'Unknown or unsupported key "{key}".',
) -> Tuple["Diagnostic", Optional[str]]:
    key_range = position_codec.range_to_client_units(
        lines,
        Range(
            Position(
                line,
                col,
            ),
            Position(
                line,
                col + len(key),
            ),
        ),
    )

    candidates = detect_possible_typo(key, expected_keys)
    extra = ""
    corrected_key = None
    if candidates:
        extra = f' It looks like a typo of "{candidates[0]}".'
        # TODO: We should be able to tell that `install-doc` and `install-docs` are the same.
        #  That would enable this to work in more cases.
        corrected_key = candidates[0] if len(candidates) == 1 else None

    diagnostic = Diagnostic(
        key_range,
        message_format.format(key=key) + extra,
        DiagnosticSeverity.Error,
        source="debputy",
        data=[propose_correct_text_quick_fix(n) for n in candidates],
    )
    return diagnostic, corrected_key


def _conflicting_key(
    uri: str,
    key_a: str,
    key_b: str,
    key_a_line: int,
    key_a_col: int,
    key_b_line: int,
    key_b_col: int,
    lines: List[str],
    position_codec: LintCapablePositionCodec,
) -> Iterable["Diagnostic"]:
    key_a_range = position_codec.range_to_client_units(
        lines,
        Range(
            Position(
                key_a_line,
                key_a_col,
            ),
            Position(
                key_a_line,
                key_a_col + len(key_a),
            ),
        ),
    )
    key_b_range = position_codec.range_to_client_units(
        lines,
        Range(
            Position(
                key_b_line,
                key_b_col,
            ),
            Position(
                key_b_line,
                key_b_col + len(key_b),
            ),
        ),
    )
    yield Diagnostic(
        key_a_range,
        f'The "{key_a}" cannot be used with "{key_b}".',
        DiagnosticSeverity.Error,
        source="debputy",
        related_information=[
            DiagnosticRelatedInformation(
                location=Location(
                    uri,
                    key_b_range,
                ),
                message=f'The attribute "{key_b}" is used here.',
            )
        ],
    )

    yield Diagnostic(
        key_b_range,
        f'The "{key_b}" cannot be used with "{key_a}".',
        DiagnosticSeverity.Error,
        source="debputy",
        related_information=[
            DiagnosticRelatedInformation(
                location=Location(
                    uri,
                    key_a_range,
                ),
                message=f'The attribute "{key_a}" is used here.',
            )
        ],
    )


def _lint_attr_value(
    lint_state: LintState,
    attr: AttributeDescription,
    pg: ParserGenerator,
    value: Any,
) -> Iterable["Diagnostic"]:
    attr_type = attr.attribute_type
    orig = get_origin(attr_type)
    valid_values: Sequence[Any] = tuple()
    if orig == Literal:
        valid_values = get_args(attr.attribute_type)
    elif orig == bool or attr.attribute_type == bool:
        valid_values = ("true", "false")
    elif isinstance(attr_type, type) and issubclass(attr_type, DebputyDispatchableType):
        parser = pg.dispatch_parser_table_for(attr_type)
        yield from _lint_content(
            lint_state,
            pg,
            parser,
            value,
        )
        return

    if value in valid_values:
        return
    # TODO: Emit diagnostic for broken values
    return


def _lint_declarative_mapping_input_parser(
    lint_state: LintState,
    pg: ParserGenerator,
    parser: DeclarativeMappingInputParser,
    content: Any,
) -> Iterable["Diagnostic"]:
    if not isinstance(content, CommentedMap):
        return
    lc = content.lc
    for key, value in content.items():
        attr = parser.manifest_attributes.get(key)
        line, col = lc.key(key)
        if attr is None:
            diag, corrected_key = _unknown_key(
                key,
                parser.manifest_attributes,
                line,
                col,
                lint_state.lines,
                lint_state.position_codec,
            )
            yield diag
            if corrected_key:
                key = corrected_key
                attr = parser.manifest_attributes.get(corrected_key)
        if attr is None:
            continue

        yield from _lint_attr_value(
            lint_state,
            attr,
            pg,
            value,
        )

        for forbidden_key in attr.conflicting_attributes:
            if forbidden_key in content:
                con_line, con_col = lc.key(forbidden_key)
                yield from _conflicting_key(
                    lint_state.doc_uri,
                    key,
                    forbidden_key,
                    line,
                    col,
                    con_line,
                    con_col,
                    lint_state.lines,
                    lint_state.position_codec,
                )
    for mx in parser.mutually_exclusive_attributes:
        matches = content.keys() & mx
        if len(matches) < 2:
            continue
        key, *others = list(matches)
        line, col = lc.key(key)
        for other in others:
            con_line, con_col = lc.key(other)
            yield from _conflicting_key(
                lint_state.doc_uri,
                key,
                other,
                line,
                col,
                con_line,
                con_col,
                lint_state.lines,
                lint_state.position_codec,
            )


def _lint_content(
    lint_state: LintState,
    pg: ParserGenerator,
    parser: DeclarativeInputParser[Any],
    content: Any,
) -> Iterable["Diagnostic"]:
    if isinstance(parser, DispatchingParserBase):
        if not isinstance(content, CommentedMap):
            return
        lc = content.lc
        for key, value in content.items():
            is_known = parser.is_known_keyword(key)
            if not is_known:
                line, col = lc.key(key)
                diag, corrected_key = _unknown_key(
                    key,
                    parser.registered_keywords(),
                    line,
                    col,
                    lint_state.lines,
                    lint_state.position_codec,
                )
                yield diag
                if corrected_key is not None:
                    key = corrected_key
                    is_known = True

            if is_known:
                subparser = parser.parser_for(key)
                assert subparser is not None
                yield from _lint_content(
                    lint_state,
                    pg,
                    subparser.parser,
                    value,
                )
    elif isinstance(parser, ListWrappedDeclarativeInputParser):
        if not isinstance(content, CommentedSeq):
            return
        subparser = parser.delegate
        for value in content:
            yield from _lint_content(lint_state, pg, subparser, value)
    elif isinstance(parser, InPackageContextParser):
        if not isinstance(content, CommentedMap):
            return
        print(lint_state)
        known_packages = lint_state.binary_packages
        lc = content.lc
        for k, v in content.items():
            if "{{" not in k and known_packages is not None and k not in known_packages:
                line, col = lc.key(k)
                diag, _ = _unknown_key(
                    k,
                    known_packages,
                    line,
                    col,
                    lint_state.lines,
                    lint_state.position_codec,
                    message_format='Unknown package "{key}".',
                )
                yield diag
            yield from _lint_content(lint_state, pg, parser.delegate, v)
    elif isinstance(parser, DeclarativeMappingInputParser):
        yield from _lint_declarative_mapping_input_parser(
            lint_state,
            pg,
            parser,
            content,
        )


def is_at(position: Position, lc_pos: Tuple[int, int]) -> bool:
    return position.line == lc_pos[0] and position.character == lc_pos[1]


def is_before(position: Position, lc_pos: Tuple[int, int]) -> bool:
    line, column = lc_pos
    if position.line < line:
        return True
    if position.line == line and position.character < column:
        return True
    return False


def is_after(position: Position, lc_pos: Tuple[int, int]) -> bool:
    line, column = lc_pos
    if position.line > line:
        return True
    if position.line == line and position.character > column:
        return True
    return False


def _trace_cursor(
    content: Any,
    attribute_path: AttributePath,
    server_position: Position,
) -> Optional[Tuple[bool, AttributePath, Any, Any]]:
    matched_key: Optional[Union[str, int]] = None
    matched: Optional[Node] = None
    matched_was_key: bool = False

    if isinstance(content, CommentedMap):
        dict_lc: LineCol = content.lc
        for k, v in content.items():
            k_lc = dict_lc.key(k)
            if is_before(server_position, k_lc):
                break
            v_lc = dict_lc.value(k)
            if is_before(server_position, v_lc):
                # TODO: Handle ":" and "whitespace"
                matched = k
                matched_key = k
                matched_was_key = True
                break
            matched = v
            matched_key = k
    elif isinstance(content, CommentedSeq):
        list_lc: LineCol = content.lc
        for idx, value in enumerate(content):
            i_lc = list_lc.item(idx)
            if is_before(server_position, i_lc):
                break
            matched_key = idx
            matched = value

    if matched is not None:
        assert matched_key is not None
        sub_path = attribute_path[matched_key]
        if not matched_was_key and isinstance(matched, CommentedBase):
            return _trace_cursor(matched, sub_path, server_position)
        return matched_was_key, sub_path, matched, content
    return None


_COMPLETION_HINT_KEY = "___COMPLETE:"
_COMPLETION_HINT_VALUE = "___COMPLETE"


def resolve_keyword(
    current_parser: Union[DeclarativeInputParser[Any], DispatchingParserBase],
    current_plugin: DebputyPluginMetadata,
    segments: List[Union[str, int]],
    segment_idx: int,
    parser_generator: ParserGenerator,
    *,
    is_completion_attempt: bool = False,
) -> Optional[
    Tuple[
        Union[DeclarativeInputParser[Any], DispatchingParserBase],
        DebputyPluginMetadata,
        int,
    ]
]:
    if segment_idx >= len(segments):
        return current_parser, current_plugin, segment_idx
    current_segment = segments[segment_idx]
    if isinstance(current_parser, ListWrappedDeclarativeInputParser):
        if isinstance(current_segment, int):
            current_parser = current_parser.delegate
            segment_idx += 1
            if segment_idx >= len(segments):
                return current_parser, current_plugin, segment_idx
            current_segment = segments[segment_idx]

    if not isinstance(current_segment, str):
        return None

    if is_completion_attempt and current_segment.endswith(
        (_COMPLETION_HINT_KEY, _COMPLETION_HINT_VALUE)
    ):
        return current_parser, current_plugin, segment_idx

    if isinstance(current_parser, InPackageContextParser):
        return resolve_keyword(
            current_parser.delegate,
            current_plugin,
            segments,
            segment_idx + 1,
            parser_generator,
            is_completion_attempt=is_completion_attempt,
        )
    elif isinstance(current_parser, DispatchingParserBase):
        if not current_parser.is_known_keyword(current_segment):
            if is_completion_attempt:
                return current_parser, current_plugin, segment_idx
            return None
        subparser = current_parser.parser_for(current_segment)
        segment_idx += 1
        if segment_idx < len(segments):
            return resolve_keyword(
                subparser.parser,
                subparser.plugin_metadata,
                segments,
                segment_idx,
                parser_generator,
                is_completion_attempt=is_completion_attempt,
            )
        return subparser.parser, subparser.plugin_metadata, segment_idx
    elif isinstance(current_parser, DeclarativeMappingInputParser):
        attr = current_parser.manifest_attributes.get(current_segment)
        attr_type = attr.attribute_type if attr is not None else None
        if (
            attr_type is not None
            and isinstance(attr_type, type)
            and issubclass(attr_type, DebputyDispatchableType)
        ):
            subparser = parser_generator.dispatch_parser_table_for(attr_type)
            if subparser is not None and (
                is_completion_attempt or segment_idx + 1 < len(segments)
            ):
                return resolve_keyword(
                    subparser,
                    current_plugin,
                    segments,
                    segment_idx + 1,
                    parser_generator,
                    is_completion_attempt=is_completion_attempt,
                )
        return current_parser, current_plugin, segment_idx
    else:
        _info(f"Unknown parser: {current_parser.__class__}")
    return None


def _render_param_doc(
    rule_name: str,
    declarative_parser: DeclarativeMappingInputParser,
    plugin_metadata: DebputyPluginMetadata,
    attribute: str,
) -> Optional[str]:
    attr = declarative_parser.source_attributes.get(attribute)
    if attr is None:
        return None

    doc_args, parser_doc = doc_args_for_parser_doc(
        rule_name,
        declarative_parser,
        plugin_metadata,
    )
    rendered_docs = render_attribute_doc(
        declarative_parser,
        declarative_parser.source_attributes,
        declarative_parser.input_time_required_parameters,
        declarative_parser.at_least_one_of,
        parser_doc,
        doc_args,
        is_interactive=True,
        rule_name=rule_name,
    )

    for attributes, rendered_doc in rendered_docs:
        if attribute in attributes:
            full_doc = [
                f"# Attribute `{attribute}`",
                "",
            ]
            full_doc.extend(rendered_doc)

            return "\n".join(full_doc)
    return None


DEBPUTY_PLUGIN_METADATA = plugin_metadata_for_debputys_own_plugin()


def _guess_rule_name(segments: List[Union[str, int]], idx: int) -> str:
    orig_idx = idx
    idx -= 1
    while idx >= 0:
        segment = segments[idx]
        if isinstance(segment, str):
            return segment
        idx -= 1
    _warn(f"Unable to derive rule name from {segments} [{orig_idx}]")
    return "<Bug: unknown rule name>"


def _escape(v: str) -> str:
    return '"' + v.replace("\n", "\\n") + '"'


def _insert_snippet(lines: List[str], server_position: Position) -> bool:
    _info(f"Complete at {server_position}")
    line_no = server_position.line
    line = lines[line_no]
    pos_rhs = line[server_position.character :]
    if pos_rhs and not pos_rhs.isspace():
        _info(f"No insertion: {_escape(line[server_position.character:])}")
        return False
    lhs_ws = line[: server_position.character]
    lhs = lhs_ws.strip()
    if lhs.endswith(":"):
        _info("Insertion of value (key seen)")
        new_line = line[: server_position.character] + _COMPLETION_HINT_VALUE
    elif lhs.startswith("-"):
        _info("Insertion of key or value (list item)")
        # Respect the provided indentation
        snippet = _COMPLETION_HINT_KEY if ":" not in lhs else _COMPLETION_HINT_VALUE
        new_line = line[: server_position.character] + snippet
    elif not lhs or (lhs_ws and not lhs_ws[0].isspace()):
        _info(f"Insertion of key or value: {_escape(line[server_position.character:])}")
        # Respect the provided indentation
        snippet = _COMPLETION_HINT_KEY if ":" not in lhs else _COMPLETION_HINT_VALUE
        new_line = line[: server_position.character] + snippet
    elif lhs.isalpha() and ":" not in lhs:
        _info(f"Expanding value to a key: {_escape(line[server_position.character:])}")
        # Respect the provided indentation
        new_line = line[: server_position.character] + _COMPLETION_HINT_KEY
    else:
        c = line[server_position.character]
        _info(f"Not touching line: {_escape(line)} -- {_escape(c)}")
        return False
    _info(f'Evaluating complete on synthetic line: "{new_line}"')
    lines[line_no] = new_line
    return True


@lsp_completer(_LANGUAGE_IDS)
def debputy_manifest_completer(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    if not is_valid_file(doc.path):
        return None
    lines = doc.lines
    server_position = doc.position_codec.position_from_client_units(
        lines, params.position
    )
    attribute_root_path = AttributePath.root_path()
    added_key = _insert_snippet(lines, server_position)
    attempts = 1 if added_key else 2
    content = None

    while attempts > 0:
        attempts -= 1
        try:
            content = MANIFEST_YAML.load("".join(lines))
            break
        except MarkedYAMLError as e:
            context_line = (
                e.context_mark.line if e.context_mark else e.problem_mark.line
            )
            if (
                e.problem_mark.line != server_position.line
                and context_line != server_position.line
            ):
                l_data = (
                    lines[e.problem_mark.line].rstrip()
                    if e.problem_mark.line < len(lines)
                    else "N/A (OOB)"
                )

                _info(f"Parse error on line: {e.problem_mark.line}: {l_data}")
                return None

            if attempts > 0:
                # Try to make it a key and see if that fixes the problem
                new_line = lines[server_position.line].rstrip() + _COMPLETION_HINT_KEY
                lines[server_position.line] = new_line
        except YAMLError:
            break
    if content is None:
        context = lines[server_position.line].replace("\n", "\\n")
        _info(f"Completion failed: parse error: Line in question: {context}")
        return None
    m = _trace_cursor(content, attribute_root_path, server_position)

    if m is None:
        _info("No match")
        return None
    matched_key, attr_path, matched, parent = m
    _info(f"Matched path: {matched} (path: {attr_path.path}) [{matched_key=}]")
    feature_set = ls.plugin_feature_set
    root_parser = feature_set.manifest_parser_generator.dispatchable_object_parsers[
        OPARSER_MANIFEST_ROOT
    ]
    segments = list(attr_path.path_segments())
    km = resolve_keyword(
        root_parser,
        DEBPUTY_PLUGIN_METADATA,
        segments,
        0,
        feature_set.manifest_parser_generator,
        is_completion_attempt=True,
    )
    if km is None:
        return None
    parser, _, at_depth_idx = km
    _info(f"Match leaf parser {at_depth_idx} -- {parser.__class__}")
    items = []
    if at_depth_idx + 1 >= len(segments):
        if isinstance(parser, DispatchingParserBase):
            if matched_key:
                items = [
                    CompletionItem(f"{k}:")
                    for k in parser.registered_keywords()
                    if k not in parent
                    and not isinstance(
                        parser.parser_for(k).parser,
                        DeclarativeValuelessKeywordInputParser,
                    )
                ]
            else:
                items = [
                    CompletionItem(k)
                    for k in parser.registered_keywords()
                    if k not in parent
                    and isinstance(
                        parser.parser_for(k).parser,
                        DeclarativeValuelessKeywordInputParser,
                    )
                ]
        elif isinstance(parser, InPackageContextParser):
            binary_packages = ls.lint_state(doc).binary_packages
            if binary_packages is not None:
                items = [
                    CompletionItem(f"{p}:") for p in binary_packages if p not in parent
                ]
        elif isinstance(parser, DeclarativeMappingInputParser):
            if matched_key:
                _info("Match attributes")
                locked = set(parent)
                for mx in parser.mutually_exclusive_attributes:
                    if not mx.isdisjoint(parent.keys()):
                        locked.update(mx)
                for attr_name, attr in parser.manifest_attributes.items():
                    if not attr.conflicting_attributes.isdisjoint(parent.keys()):
                        locked.add(attr_name)
                        break
                items = [
                    CompletionItem(f"{k}:")
                    for k in parser.manifest_attributes
                    if k not in locked
                ]
            else:
                # Value
                key = segments[at_depth_idx] if len(segments) > at_depth_idx else None
                attr = parser.manifest_attributes.get(key)
                if attr is not None:
                    _info(f"Expand value / key: {key} -- {attr.attribute_type}")
                    items = _completion_from_attr(
                        attr,
                        feature_set.manifest_parser_generator,
                        matched,
                    )
                else:
                    _info(
                        f"Expand value / key: {key} -- !! {list(parser.manifest_attributes)}"
                    )
        elif isinstance(parser, DeclarativeNonMappingInputParser):
            attr = parser.alt_form_parser
            items = _completion_from_attr(
                attr,
                feature_set.manifest_parser_generator,
                matched,
            )
    return items


def _completion_from_attr(
    attr: AttributeDescription,
    pg: ParserGenerator,
    matched: Any,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    orig = get_origin(attr.attribute_type)
    valid_values: Sequence[Any] = tuple()
    if orig == Literal:
        valid_values = get_args(attr.attribute_type)
    elif orig == bool or attr.attribute_type == bool:
        valid_values = ("true", "false")
    elif isinstance(orig, type) and issubclass(orig, DebputyDispatchableType):
        parser = pg.dispatch_parser_table_for(orig)
        _info(f"M: {parser}")

    if matched in valid_values:
        _info(f"Already filled: {matched} is one of {valid_values}")
        return None
    if valid_values:
        return [CompletionItem(x) for x in valid_values]
    return None


@lsp_hover(_LANGUAGE_IDS)
def debputy_manifest_hover(
    ls: "DebputyLanguageServer",
    params: HoverParams,
) -> Optional[Hover]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    if not is_valid_file(doc.path):
        return None
    lines = doc.lines
    position_codec = doc.position_codec
    attribute_root_path = AttributePath.root_path()
    server_position = position_codec.position_from_client_units(lines, params.position)

    try:
        content = MANIFEST_YAML.load("".join(lines))
    except YAMLError:
        return None
    m = _trace_cursor(content, attribute_root_path, server_position)
    if m is None:
        _info("No match")
        return None
    matched_key, attr_path, matched, _ = m
    _info(f"Matched path: {matched} (path: {attr_path.path}) [{matched_key=}]")

    feature_set = ls.plugin_feature_set
    parser_generator = feature_set.manifest_parser_generator
    root_parser = parser_generator.dispatchable_object_parsers[OPARSER_MANIFEST_ROOT]
    segments = list(attr_path.path_segments())
    km = resolve_keyword(
        root_parser,
        DEBPUTY_PLUGIN_METADATA,
        segments,
        0,
        parser_generator,
    )
    if km is None:
        _info("No keyword match")
        return
    parser, plugin_metadata, at_depth_idx = km
    _info(f"Match leaf parser {at_depth_idx}/{len(segments)} -- {parser.__class__}")
    hover_doc_text = resolve_hover_text(
        feature_set,
        parser,
        plugin_metadata,
        segments,
        at_depth_idx,
        matched,
        matched_key,
    )
    return _hover_doc(ls, hover_doc_text)


def resolve_hover_text_for_value(
    feature_set: PluginProvidedFeatureSet,
    parser: DeclarativeMappingInputParser,
    plugin_metadata: DebputyPluginMetadata,
    segment: Union[str, int],
    matched: Any,
) -> Optional[str]:

    hover_doc_text: Optional[str] = None
    attr = parser.manifest_attributes.get(segment)
    attr_type = attr.attribute_type if attr is not None else None
    if attr_type is None:
        _info(f"Matched value for {segment} -- No attr or type")
        return None
    if isinstance(attr_type, type) and issubclass(attr_type, DebputyDispatchableType):
        parser_generator = feature_set.manifest_parser_generator
        parser = parser_generator.dispatch_parser_table_for(attr_type)
        if parser is None or not isinstance(matched, str):
            _info(
                f"Unknown parser for {segment} or matched is not a str -- {attr_type} {type(matched)=}"
            )
            return None
        subparser = parser.parser_for(matched)
        if subparser is None:
            _info(f"Unknown parser for {matched} (subparser)")
            return None
        hover_doc_text = render_rule(
            matched,
            subparser.parser,
            plugin_metadata,
        )
    else:
        _info(f"Unknown value: {matched} -- {segment}")
    return hover_doc_text


def resolve_hover_text(
    feature_set: PluginProvidedFeatureSet,
    parser: Optional[Union[DeclarativeInputParser[Any], DispatchingParserBase]],
    plugin_metadata: DebputyPluginMetadata,
    segments: List[Union[str, int]],
    at_depth_idx: int,
    matched: Any,
    matched_key: bool,
) -> Optional[str]:
    hover_doc_text: Optional[str] = None
    if at_depth_idx == len(segments):
        segment = segments[at_depth_idx - 1]
        _info(f"Matched {segment} at ==, {matched_key=} ")
        hover_doc_text = render_rule(
            segment,
            parser,
            plugin_metadata,
            is_root_rule=False,
        )
    elif at_depth_idx + 1 == len(segments) and isinstance(
        parser, DeclarativeMappingInputParser
    ):
        segment = segments[at_depth_idx]
        _info(f"Matched {segment} at -1, {matched_key=} ")
        if isinstance(segment, str):
            if not matched_key:
                hover_doc_text = resolve_hover_text_for_value(
                    feature_set,
                    parser,
                    plugin_metadata,
                    segment,
                    matched,
                )
            if matched_key or hover_doc_text is None:
                rule_name = _guess_rule_name(segments, at_depth_idx)
                hover_doc_text = _render_param_doc(
                    rule_name,
                    parser,
                    plugin_metadata,
                    segment,
                )
    else:
        _info(f"No doc: {at_depth_idx=} {len(segments)=}")

    return hover_doc_text


def _hover_doc(ls: "LanguageServer", hover_doc_text: Optional[str]) -> Optional[Hover]:
    if hover_doc_text is None:
        return None
    try:
        supported_formats = ls.client_capabilities.text_document.hover.content_format
    except AttributeError:
        supported_formats = []
    markup_kind = MarkupKind.Markdown
    if markup_kind not in supported_formats:
        markup_kind = MarkupKind.PlainText
    return Hover(
        contents=MarkupContent(
            kind=markup_kind,
            value=hover_doc_text,
        ),
    )
