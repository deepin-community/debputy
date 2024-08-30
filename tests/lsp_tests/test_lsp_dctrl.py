import textwrap
from typing import Optional

import pytest

from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsprotocol.types import (
    CompletionParams,
    TextDocumentIdentifier,
    HoverParams,
    MarkupContent,
    SemanticTokensParams,
)

try:
    from debputy.lsp.lsp_debian_control import (
        _debian_control_completions,
        _debian_control_hover,
        _debian_control_semantic_tokens_full,
    )

    from pygls.server import LanguageServer
except ImportError:
    pass
from lsp_tests.lsp_tutil import (
    put_doc_with_cursor,
    put_doc_no_cursor,
    resolve_semantic_tokens,
    resolved_semantic_token,
)


def test_dctrl_complete_field(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"

    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        Source: foo

        Package: foo
        <CURSOR>
"""
        ),
    )
    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    assert "Multi-Arch" in keywords
    assert "Architecture" in keywords
    # Already present or wrong section
    assert "Package" not in keywords
    assert "Source" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        Source: foo

        Package: foo
        <CURSOR>
        Architecture: any
"""
        ),
    )

    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    assert "Multi-Arch" in keywords
    # Should be considered present even though it is parsed as two stanzas with a space
    assert "Architecture" not in keywords
    # Already present or wrong section
    assert "Package" not in keywords
    assert "Source" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        Source: foo

        Package: foo
        Sec<CURSOR>
        Architecture: any
"""
        ),
    )

    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    # Included since we rely on client filtering (some clients let "RRR" match "R(ules-)R(equires-)R(oot), etc).
    assert "Multi-Arch" in keywords
    # Should be considered present even though it is parsed as two stanzas with an error
    assert "Architecture" not in keywords
    # Already present or wrong section
    assert "Package" not in keywords
    assert "Source" not in keywords


@pytest.mark.parametrize(
    "case,is_arch_all",
    [
        ("Architecture: any\n<CURSOR>", False),
        ("Architecture: any\nM-A<CURSOR>", False),
        ("<CURSOR>\nArchitecture: any", False),
        ("M-A<CURSOR>\nArchitecture: any", False),
        ("Architecture: all\n<CURSOR>", True),
        ("Architecture: all\nM-A<CURSOR>", True),
        ("<CURSOR>\nArchitecture: all", True),
        ("M-A<CURSOR>\nArchitecture: all", True),
        # Does not have architecture
        ("M-A<CURSOR>", None),
    ],
)
def test_dctrl_complete_field_context(
    ls: "DebputyLanguageServer",
    case: str,
    is_arch_all: Optional[bool],
) -> None:
    dctrl_uri = "file:///nowhere/debian/control"

    content = textwrap.dedent(
        """\
    Source: foo

    Package: foo
    {CASE}
"""
    ).format(CASE=case)
    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        content,
    )

    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    # Missing Architecture counts as "arch:all" by the completion logic
    if is_arch_all is False:
        assert "X-DH-Build-For-Type" in keywords
    else:
        assert "X-DH-Build-For-Type" not in keywords


def test_dctrl_complete_field_value_context(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"

    content = textwrap.dedent(
        """\
    Source: foo

    Package: foo
    Architecture: any
    Multi-Arch: <CURSOR>
"""
    )
    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        content,
    )

    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    assert keywords == {"no", "same", "foreign", "allowed"}

    content = textwrap.dedent(
        """\
    Source: foo

    Package: foo
    Architecture: all
    Multi-Arch: <CURSOR>
"""
    )
    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        content,
    )

    matches = _debian_control_completions(
        ls,
        CompletionParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert isinstance(matches, list)
    keywords = {m.label for m in matches}
    assert keywords == {"no", "foreign", "allowed"}


def test_dctrl_hover_doc_field(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"
    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        Source: foo

        Package: foo
        Arch<CURSOR>itecture: any
"""
        ),
    )

    hover_doc = _debian_control_hover(
        ls,
        HoverParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert "Determines which architecture" in hover_doc.contents.value


def test_dctrl_hover_doc_synopsis(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"
    cursor_pos = put_doc_with_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        Source: foo

        Package: foo
        Architecture: any
        Description: super charged<CURSOR> tool with batteries included
"""
        ),
    )

    hover_doc = _debian_control_hover(
        ls,
        HoverParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
    )
    assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
    assert hover_doc.contents.value.startswith("# Package synopsis")
    assert "super charged tool with batteries included" in hover_doc.contents.value


def test_dctrl_hover_doc_substvars(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"
    matching_cases = [
        "bar (= <CURSOR>${binary:Version})",
        "bar (= $<CURSOR>{binary:Version})",
        "bar (= ${binary:Version<CURSOR>})",
    ]
    for variant in matching_cases:
        cursor_pos = put_doc_with_cursor(
            ls,
            dctrl_uri,
            "debian/control",
            textwrap.dedent(
                f"""\
            Source: foo

            Package: foo
            Architecture: any
            Depends: bar (= {variant})
            Description: super charged tool with batteries included
    """
            ),
        )

        hover_doc = _debian_control_hover(
            ls,
            HoverParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
        )
        assert hover_doc is not None and isinstance(hover_doc.contents, MarkupContent)
        assert hover_doc.contents.value.startswith("# Substvar `${binary:Version}`")

    non_matching_cases = [
        "bar (=<CURSOR> ${binary:Version})",
        "bar (= ${binary:Version}<CURSOR>)",
    ]
    for variant in non_matching_cases:
        cursor_pos = put_doc_with_cursor(
            ls,
            dctrl_uri,
            "debian/control",
            textwrap.dedent(
                f"""\
            Source: foo

            Package: foo
            Architecture: any
            Depends: bar (= {variant})
            Description: super charged tool with batteries included
    """
            ),
        )

        hover_doc = _debian_control_hover(
            ls,
            HoverParams(TextDocumentIdentifier(dctrl_uri), cursor_pos),
        )
        provided_doc = ""
        if hover_doc is not None and isinstance(hover_doc.contents, MarkupContent):
            provided_doc = hover_doc.contents.value
        assert not provided_doc.startswith("# Substvar `${binary:Version}`")


def test_dctrl_semantic_tokens(ls: "DebputyLanguageServer") -> None:
    dctrl_uri = "file:///nowhere/debian/control"
    put_doc_no_cursor(
        ls,
        dctrl_uri,
        "debian/control",
        textwrap.dedent(
            """\
        # Some leading comment

        Source: foo

        # Comment between stanzas

        Package: foo
        # Comment before Architecture
        Architecture: any
        Depends:
        # Comment about bar
             bar (>= 1.0),
             baz [linux-any] <!pkg.foo.bootstrap>
        Description: super charged tool with batteries included
        Unknown-Field: Some value
        # Comment in that field
          that we do not know about.
"""
        ),
    )

    semantic_tokens = _debian_control_semantic_tokens_full(
        ls,
        SemanticTokensParams(TextDocumentIdentifier(dctrl_uri)),
    )
    resolved_semantic_tokens = resolve_semantic_tokens(semantic_tokens)
    assert resolved_semantic_tokens is not None
    assert resolved_semantic_tokens == [
        resolved_semantic_token(0, 0, len("# Some leading comment"), "comment"),
        resolved_semantic_token(2, 0, len("Source"), "keyword"),
        resolved_semantic_token(4, 0, len("# Comment between stanzas"), "comment"),
        resolved_semantic_token(6, 0, len("Package"), "keyword"),
        resolved_semantic_token(7, 0, len("# Comment before Architecture"), "comment"),
        resolved_semantic_token(8, 0, len("Architecture"), "keyword"),
        resolved_semantic_token(8, len("Architecture: "), len("any"), "enumMember"),
        resolved_semantic_token(9, 0, len("Depends"), "keyword"),
        resolved_semantic_token(10, 0, len("# Comment about bar"), "comment"),
        resolved_semantic_token(13, 0, len("Description"), "keyword"),
        resolved_semantic_token(14, 0, len("Unknown-Field"), "keyword"),
        # TODO: resolved_semantic_token(15, 0, len("# Comment in that field"), "comment"),
    ]
