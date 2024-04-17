import textwrap

try:
    from lsprotocol.types import (
        CompletionParams,
        TextDocumentIdentifier,
        HoverParams,
        MarkupContent,
    )

    from debputy.lsp.lsp_debian_control import (
        _debian_control_completions,
        _debian_control_hover,
    )

    from pygls.server import LanguageServer
except ImportError:
    pass
from lsp_tests.lsp_tutil import put_doc_with_cursor


def test_dctrl_complete_field(ls: "LanguageServer") -> None:
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


def test_dctrl_hover_doc_field(ls: "LanguageServer") -> None:
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


def test_dctrl_hover_doc_synopsis(ls: "LanguageServer") -> None:
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


def test_dctrl_hover_doc_substvars(ls: "LanguageServer") -> None:
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
