import itertools
from typing import Optional, Iterable, Any, Tuple, Mapping, Sequence, FrozenSet

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.manifest_parser.declarative_parser import (
    DeclarativeMappingInputParser,
    DeclarativeNonMappingInputParser,
    AttributeDescription,
)
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
    DeclarativeInputParser,
    DispatchingObjectParser,
    ListWrappedDeclarativeInputParser,
    InPackageContextParser,
)
from debputy.plugin.api.spec import (
    ParserDocumentation,
    reference_documentation,
    undocumented_attr,
)
from debputy.util import assume_not_none


def _provide_placeholder_parser_doc(
    parser_doc: Optional[ParserDocumentation],
    attributes: Iterable[str],
) -> ParserDocumentation:
    if parser_doc is None:
        parser_doc = reference_documentation()
    changes = {}
    if parser_doc.attribute_doc is None:
        changes["attribute_doc"] = [undocumented_attr(attr) for attr in attributes]

    if changes:
        return parser_doc.replace(**changes)
    return parser_doc


def doc_args_for_parser_doc(
    rule_name: str,
    declarative_parser: DeclarativeInputParser[Any],
    plugin_metadata: DebputyPluginMetadata,
) -> Tuple[Mapping[str, str], ParserDocumentation]:
    attributes: Iterable[str]
    if isinstance(declarative_parser, DeclarativeMappingInputParser):
        attributes = declarative_parser.source_attributes.keys()
    else:
        attributes = []
    doc_args = {
        "RULE_NAME": rule_name,
        "MANIFEST_FORMAT_DOC": f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md",
        "PLUGIN_NAME": plugin_metadata.plugin_name,
    }
    parser_doc = _provide_placeholder_parser_doc(
        declarative_parser.inline_reference_documentation,
        attributes,
    )
    return doc_args, parser_doc


def render_attribute_doc(
    parser: Any,
    attributes: Mapping[str, "AttributeDescription"],
    required_attributes: FrozenSet[str],
    conditionally_required_attributes: FrozenSet[FrozenSet[str]],
    parser_doc: ParserDocumentation,
    doc_args: Mapping[str, str],
    *,
    rule_name: str = "<unset>",
    is_root_rule: bool = False,
    is_interactive: bool = False,
) -> Iterable[Tuple[FrozenSet[str], Sequence[str]]]:
    provided_attribute_docs = (
        parser_doc.attribute_doc if parser_doc.attribute_doc is not None else []
    )

    for attr_doc in assume_not_none(provided_attribute_docs):
        attr_description = attr_doc.description
        rendered_doc = []

        for parameter in sorted(attr_doc.attributes):
            parameter_details = attributes.get(parameter)
            if parameter_details is not None:
                source_name = parameter_details.source_attribute_name
                describe_type = parameter_details.type_validator.describe_type()
            else:
                assert isinstance(parser, DispatchingObjectParser)
                source_name = parameter
                subparser = parser.parser_for(source_name).parser
                if isinstance(subparser, InPackageContextParser):
                    if is_interactive:
                        describe_type = "PackageContext"
                    else:
                        rule_prefix = rule_name if not is_root_rule else ""
                        describe_type = f"PackageContext (chains to `{rule_prefix}::{subparser.manifest_attribute_path_template}`)"

                elif isinstance(subparser, DispatchingObjectParser):
                    if is_interactive:
                        describe_type = "Object"
                    else:
                        rule_prefix = rule_name if not is_root_rule else ""
                        describe_type = f"Object (see `{rule_prefix}::{subparser.manifest_attribute_path_template}`)"
                elif isinstance(subparser, DeclarativeMappingInputParser):
                    describe_type = "<Type definition not implemented yet>"  # TODO: Derive from subparser
                elif isinstance(subparser, DeclarativeNonMappingInputParser):
                    describe_type = (
                        subparser.alt_form_parser.type_validator.describe_type()
                    )
                else:
                    describe_type = f"<Unknown: Non-introspectable subparser - {subparser.__class__.__name__}>"

            if source_name in required_attributes:
                req_str = "required"
            elif any(source_name in s for s in conditionally_required_attributes):
                req_str = "conditional"
            else:
                req_str = "optional"
            rendered_doc.append(f"`{source_name}` ({req_str}): {describe_type}")

        if attr_description:
            rendered_doc.append("")
            rendered_doc.extend(
                line
                for line in attr_description.format(**doc_args).splitlines(
                    keepends=False
                )
            )
            rendered_doc.append("")
        yield attr_doc.attributes, rendered_doc


def render_rule(
    rule_name: str,
    declarative_parser: DeclarativeInputParser[Any],
    plugin_metadata: DebputyPluginMetadata,
    *,
    is_root_rule: bool = False,
) -> str:
    doc_args, parser_doc = doc_args_for_parser_doc(
        "the manifest root" if is_root_rule else rule_name,
        declarative_parser,
        plugin_metadata,
    )
    t = assume_not_none(parser_doc.title).format(**doc_args)
    r = [
        t,
        "=" * len(t),
        "",
        assume_not_none(parser_doc.description).format(**doc_args).rstrip(),
        "",
    ]

    alt_form_parser = getattr(declarative_parser, "alt_form_parser", None)
    is_list_wrapped = False
    unwrapped_parser = declarative_parser
    if isinstance(declarative_parser, ListWrappedDeclarativeInputParser):
        is_list_wrapped = True
        unwrapped_parser = declarative_parser.delegate

    if isinstance(
        unwrapped_parser, (DeclarativeMappingInputParser, DispatchingObjectParser)
    ):

        if isinstance(unwrapped_parser, DeclarativeMappingInputParser):
            attributes = unwrapped_parser.source_attributes
            required = unwrapped_parser.input_time_required_parameters
            conditionally_required = unwrapped_parser.at_least_one_of
            mutually_exclusive = unwrapped_parser.mutually_exclusive_attributes
        else:
            attributes = {}
            required = frozenset()
            conditionally_required = frozenset()
            mutually_exclusive = frozenset()
        if is_list_wrapped:
            r.append("List where each element has the following attributes:")
        else:
            r.append("Attributes:")

        rendered_attr_doc = render_attribute_doc(
            unwrapped_parser,
            attributes,
            required,
            conditionally_required,
            parser_doc,
            doc_args,
            is_root_rule=is_root_rule,
            rule_name=rule_name,
            is_interactive=False,
        )
        for _, rendered_doc in rendered_attr_doc:
            prefix = " - "
            for line in rendered_doc:
                if line:
                    r.append(f"{prefix}{line}")
                else:
                    r.append("")
                prefix = "   "

        if (
            bool(conditionally_required)
            or bool(mutually_exclusive)
            or any(pd.conflicting_attributes for pd in attributes.values())
        ):
            r.append("")
            if is_list_wrapped:
                r.append(
                    "This rule enforces the following restrictions on each element in the list:"
                )
            else:
                r.append("This rule enforces the following restrictions:")

            if conditionally_required or mutually_exclusive:
                all_groups = set(
                    itertools.chain(conditionally_required, mutually_exclusive)
                )
                for g in all_groups:
                    anames = "`, `".join(g)
                    is_mx = g in mutually_exclusive
                    is_cr = g in conditionally_required
                    if is_mx and is_cr:
                        r.append(f" - The rule must use exactly one of: `{anames}`")
                    elif is_cr:
                        r.append(f" - The rule must use at least one of: `{anames}`")
                    else:
                        assert is_mx
                        r.append(
                            f" - The following attributes are mutually exclusive: `{anames}`"
                        )

            if mutually_exclusive or any(
                pd.conflicting_attributes for pd in attributes.values()
            ):
                for parameter, parameter_details in sorted(attributes.items()):
                    source_name = parameter_details.source_attribute_name
                    conflicts = set(parameter_details.conflicting_attributes)
                    for mx in mutually_exclusive:
                        if parameter in mx and mx not in conditionally_required:
                            conflicts |= mx
                    if conflicts:
                        conflicts.discard(parameter)
                        cnames = "`, `".join(
                            attributes[a].source_attribute_name for a in conflicts
                        )
                        r.append(
                            f" - The attribute `{source_name}` cannot be used with any of: `{cnames}`"
                        )
        r.append("")
    if alt_form_parser is not None:
        # FIXME: Mapping[str, Any] ends here, which is ironic given the headline.
        r.append(
            f"Non-mapping format: {alt_form_parser.type_validator.describe_type()}"
        )
        alt_parser_desc = parser_doc.alt_parser_description
        if alt_parser_desc:
            r.extend(
                f"   {line}"
                for line in alt_parser_desc.format(**doc_args).splitlines(
                    keepends=False
                )
            )
        r.append("")

    if declarative_parser.reference_documentation_url is not None:
        r.append(
            f"Reference documentation: {declarative_parser.reference_documentation_url}"
        )
    else:
        r.append(
            "Reference documentation: No reference documentation link provided by the plugin"
        )

    return "\n".join(r)
