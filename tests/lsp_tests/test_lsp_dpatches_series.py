import textwrap

from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsprotocol.types import (
    TextDocumentIdentifier,
    SemanticTokensParams,
)

try:
    from debputy.lsp.lsp_debian_patches_series import (
        _debian_patches_semantic_tokens_full,
        _debian_patches_series_completions,
    )

    from pygls.server import LanguageServer
except ImportError:
    pass
from lsp_tests.lsp_tutil import (
    put_doc_no_cursor,
    resolve_semantic_tokens,
    resolved_semantic_token,
)


def test_dpatches_series_semantic_tokens(ls: "DebputyLanguageServer") -> None:
    doc_uri = "file:///nowhere/debian/patches/series"
    put_doc_no_cursor(
        ls,
        doc_uri,
        "debian/patches/series",
        textwrap.dedent(
            """\
        # Some leading comment

        some.patch

        another-delta.diff # foo
"""
        ),
    )

    semantic_tokens = _debian_patches_semantic_tokens_full(
        ls,
        SemanticTokensParams(TextDocumentIdentifier(doc_uri)),
    )
    resolved_semantic_tokens = resolve_semantic_tokens(semantic_tokens)
    assert resolved_semantic_tokens is not None
    assert resolved_semantic_tokens == [
        resolved_semantic_token(0, 0, len("# Some leading comment"), "comment"),
        resolved_semantic_token(2, 0, len("some.patch"), "string"),
        resolved_semantic_token(4, 0, len("another-delta.diff"), "string"),
        resolved_semantic_token(
            4, len("another-delta.diff") + 1, len("# foo"), "comment"
        ),
    ]
