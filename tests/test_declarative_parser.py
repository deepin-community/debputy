from typing import (
    List,
    TypedDict,
    NotRequired,
    Annotated,
    Union,
    Mapping,
)

import pytest

from debputy.highlevel_manifest import PackageTransformationDefinition
from debputy.manifest_parser.tagging_types import (
    DebputyParsedContent,
    TypeMapping,
)
from debputy.manifest_parser.parse_hints import DebputyParseHint
from debputy.manifest_parser.declarative_parser import ParserGenerator
from debputy.manifest_parser.mapper_code import type_mapper_str2package
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.substitution import NULL_SUBSTITUTION
from tutil import faked_binary_package


class TFinalEntity(DebputyParsedContent):
    sources: List[str]
    install_as: NotRequired[str]
    into: NotRequired[List[BinaryPackage]]
    recursive: NotRequired[bool]


class TSourceEntity(TypedDict):
    sources: NotRequired[List[str]]
    source: NotRequired[Annotated[str, DebputyParseHint.target_attribute("sources")]]
    as_: NotRequired[
        Annotated[
            str,
            DebputyParseHint.target_attribute("install_as"),
            DebputyParseHint.conflicts_with_source_attributes("sources"),
        ]
    ]
    into: NotRequired[Union[BinaryPackage, List[BinaryPackage]]]
    recursive: NotRequired[bool]


TSourceEntityAltFormat = Union[TSourceEntity, List[str], str]


foo_package = faked_binary_package("foo")
context_packages = {
    foo_package.name: foo_package,
}
context_package_states = {
    p.name: PackageTransformationDefinition(
        p,
        NULL_SUBSTITUTION,
        False,
    )
    for p in context_packages.values()
}


class TestParserContextData(ParserContextData):
    @property
    def _package_states(self) -> Mapping[str, PackageTransformationDefinition]:
        return context_package_states

    @property
    def binary_packages(self) -> Mapping[str, BinaryPackage]:
        return context_packages


@pytest.fixture
def parser_context():
    return TestParserContextData()


@pytest.mark.parametrize(
    "source_payload,expected_data,expected_attribute_path,parse_content,source_content",
    [
        (
            {"sources": ["foo", "bar"]},
            {"sources": ["foo", "bar"]},
            {
                "sources": "sources",
            },
            TFinalEntity,
            None,
        ),
        (
            {"sources": ["foo", "bar"], "install-as": "as-value"},
            {"sources": ["foo", "bar"], "install_as": "as-value"},
            {"sources": "sources", "install_as": "install-as"},
            TFinalEntity,
            None,
        ),
        (
            {"sources": ["foo", "bar"], "install-as": "as-value", "into": ["foo"]},
            {
                "sources": ["foo", "bar"],
                "install_as": "as-value",
                "into": [foo_package],
            },
            {"sources": "sources", "install_as": "install-as", "into": "into"},
            TFinalEntity,
            None,
        ),
        (
            {"source": "foo", "as": "as-value", "into": ["foo"]},
            {
                "sources": ["foo"],
                "install_as": "as-value",
                "into": [foo_package],
            },
            {"sources": "source", "install_as": "as", "into": "into"},
            TFinalEntity,
            TSourceEntity,
        ),
        (
            {"source": "foo", "as": "as-value", "into": ["foo"]},
            {
                "sources": ["foo"],
                "install_as": "as-value",
                "into": [foo_package],
            },
            {"sources": "source", "install_as": "as", "into": "into"},
            TFinalEntity,
            TSourceEntityAltFormat,
        ),
        (
            ["foo", "bar"],
            {
                "sources": ["foo", "bar"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            TSourceEntityAltFormat,
        ),
        (
            "foo",
            {
                "sources": ["foo"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            TSourceEntityAltFormat,
        ),
        (
            "foo",
            {
                "sources": ["foo"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            str,
        ),
        (
            ["foo", "bar"],
            {
                "sources": ["foo", "bar"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            List[str],
        ),
        (
            "foo",
            {
                "sources": ["foo"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            Union[str, List[str]],
        ),
        (
            ["foo", "bar"],
            {
                "sources": ["foo", "bar"],
            },
            {"sources": "parse-root"},
            TFinalEntity,
            Union[str, List[str]],
        ),
        (
            {"source": "foo", "recursive": True},
            {
                "sources": ["foo"],
                "recursive": True,
            },
            {"sources": "source", "recursive": "recursive"},
            TFinalEntity,
            TSourceEntityAltFormat,
        ),
    ],
)
def test_declarative_parser_ok(
    attribute_path: AttributePath,
    parser_context: ParserContextData,
    source_payload,
    expected_data,
    expected_attribute_path,
    parse_content,
    source_content,
):
    pg = ParserGenerator()
    pg.register_mapped_type(TypeMapping(BinaryPackage, str, type_mapper_str2package))
    parser = pg.generate_parser(
        parse_content,
        source_content=source_content,
    )
    data_path = attribute_path["parse-root"]
    parsed_data = parser.parse_input(
        source_payload, data_path, parser_context=parser_context
    )
    assert expected_data == parsed_data
    attributes = {k: data_path[k].name for k in expected_attribute_path}
    assert attributes == expected_attribute_path
