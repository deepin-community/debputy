import textwrap

import pytest

from debputy.lsp.debputy_ls import DebputyLanguageServer
from lsp_tests.lsp_tutil import put_doc_with_cursor

from debputy.lsprotocol.types import (
    TextDocumentIdentifier,
    HoverParams,
    MarkupContent,
)

try:
    from pygls.server import LanguageServer
    from debputy.lsp.lsp_debian_debputy_manifest import debputy_manifest_hover

    HAS_PYGLS = True
except ImportError:
    HAS_PYGLS = False


def test_basic_debputy_hover_tlk(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        install<CURSOR>ations:
        - install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("Installations")


def test_basic_debputy_hover_install_docs_key(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - <CURSOR>install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("Install documentation (`install-docs`)")


def test_basic_debputy_hover_install_docs_sources(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - install-docs:
            sources<CURSOR>:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("# Attribute `sources`")


def test_basic_debputy_hover_install_docs_when(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
            when<CURSOR>:
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("# Attribute `when`")


def test_basic_debputy_hover_install_docs_str_cond(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
            when: cross-<CURSOR>compiling
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith(
        "Cross-Compiling condition `cross-compiling`"
    )


def test_basic_debputy_hover_install_docs_mapping_cond_key(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
            when:
             not<CURSOR>: cross-compiling
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("Negated condition `not` (mapping)")


@pytest.mark.xfail
def test_basic_debputy_hover_install_docs_mapping_cond_str_value(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        installations:
        - install-docs:
            sources:
            - GETTING-STARTED-WITH-dh-debputy.md
            - MANIFEST-FORMAT.md
            - MIGRATING-A-DH-PLUGIN.md
            when:
             not: cross<CURSOR>-compiling
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    # This should be showing `cross-compiling` docs, but we are showing `not` docs
    assert hover_doc.contents.value.startswith(
        "Cross-Compiling condition `cross-compiling`"
    )


def test_basic_debputy_hover_binary_version(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        packages:
            foo:
                binary-version<CURSOR>:
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith(
        "Custom binary version (`binary-version`)"
    )


def test_basic_debputy_hover_services(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        packages:
            foo:
                services<CURSOR>:
                - service: foo
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith(
        "Define how services in the package will be handled (`services`)"
    )


def test_basic_debputy_hover_services_service(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: '0.1'
        packages:
            foo:
                services:
                - servic<CURSOR>e: foo
"""
        ),
    )

    hover_doc = debputy_manifest_hover(
        ls,
        HoverParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("# Attribute `service`")
