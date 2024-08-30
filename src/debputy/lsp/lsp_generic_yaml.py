from typing import Union, Any, Optional, List, Tuple

from debputy.manifest_parser.tagging_types import DebputyDispatchableType
from debputy.manifest_parser.declarative_parser import DeclarativeMappingInputParser
from debputy.manifest_parser.parser_doc import (
    render_rule,
    render_attribute_doc,
    doc_args_for_parser_doc,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
    DeclarativeInputParser,
    DispatchingParserBase,
)
from debputy.util import _info, _warn
from debputy.lsprotocol.types import MarkupContent, MarkupKind, Hover, Position, Range

try:
    from pygls.server import LanguageServer
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass


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


def as_hover_doc(
    ls: "DebputyLanguageServer",
    hover_doc_text: Optional[str],
) -> Optional[Hover]:
    if hover_doc_text is None:
        return None
    return Hover(
        contents=MarkupContent(
            kind=ls.hover_markup_format(MarkupKind.Markdown, MarkupKind.PlainText),
            value=hover_doc_text,
        ),
    )


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


def word_range_at_position(
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
