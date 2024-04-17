import textwrap

from lsp_tests.lsp_tutil import put_doc_with_cursor

try:
    from pygls.server import LanguageServer
    from lsprotocol.types import (
        InitializeParams,
        ClientCapabilities,
        GeneralClientCapabilities,
        PositionEncodingKind,
        TextDocumentItem,
        Position,
        CompletionParams,
        TextDocumentIdentifier,
        HoverParams,
        MarkupContent,
    )
    from debputy.lsp.lsp_debian_debputy_manifest import debputy_manifest_completer
    from debputy.lsp.debputy_ls import DebputyLanguageServer

    HAS_PYGLS = True
except ImportError:
    HAS_PYGLS = False


def test_basic_debputy_completer_empty(ls: "DebputyLanguageServer") -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "definitions:" in keywords
    assert "manifest-version:" in keywords
    assert "installations:" in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manif<CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "manifest-version:" in keywords
    assert "packages:" in keywords
    # We rely on client side filtering
    assert "installations:" in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "definitions:" in keywords
    assert "installations:" in keywords
    assert "packages:" in keywords
    # Already completed
    assert "manifest-version:" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            sources:
              - foo
              - bar
        <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "definitions:" in keywords
    assert "packages:" in keywords
    # Already completed
    assert "manifest-version:" not in keywords
    assert "installations:" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            sources:
              - foo
              - bar
        packa<CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "definitions:" in keywords
    assert "packages:" in keywords
    # Already completed
    assert "manifest-version:" not in keywords
    assert "installations:" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            sources:
              - foo
              - bar
        packages:
          foo:
            services:
            - service: foo
              service-scope: user
        <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "definitions:" in keywords
    # Already completed
    assert "manifest-version:" not in keywords
    assert "installations:" not in keywords
    assert "packages:" not in keywords


def test_basic_debputy_completer_manifest_variable_value(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "0.1" in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.<CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "0.1" in keywords


def test_basic_debputy_completer_install_rule_dispatch_key(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - <CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "install:" in keywords
    assert "install-doc:" in keywords
    assert "install-docs:" in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - i<CURSOR>
"""
        ),
    )

    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "install:" in keywords
    assert "install-doc:" in keywords
    assert "install-docs:" in keywords


def test_basic_debputy_completer_install_rule_install_keys(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install:
            <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "source:" in keywords
    assert "sources:" in keywords
    assert "as:" in keywords
    assert "dest-dir:" in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install:
            sources:
            - foo
            - bar
            <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "dest-dir:" in keywords
    # Already completed
    assert "sources:" not in keywords

    # Not possible (conflict)
    assert "source:" not in keywords
    assert "as:" not in keywords


def test_basic_debputy_completer_packages_foo(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "binary-version:" in keywords
    assert "services:" in keywords
    assert "transformations:" in keywords


def test_basic_debputy_completer_packages_foo_xfail(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"
    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            bin<CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "binary-version:" in keywords
    assert "services:" in keywords
    assert "transformations:" in keywords


def test_basic_debputy_completer_services_service_scope_values(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            services:
            - service: foo
              service-scope: <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert keywords == {"system", "user"}

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            services:
            - service: foo
              service-scope: s<CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert keywords == {"system", "user"}

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            services:
            - service: foo
              service-scope: system
              enable-on-install: <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert keywords == {"true", "false"}

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        packages:
          foo:
            services:
            - service: foo
              service-scope: system
              enable-on-install: tr<CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    # "false" is ok, because we rely on client side filtering
    assert keywords == {"true", "false"}


def test_basic_debputy_completer_manifest_conditions(
    ls: "DebputyLanguageServer",
) -> None:
    debputy_manifest_uri = "file:///nowhere/debian/debputy.manifest"

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            when: <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "cross-compiling" in keywords
    # Mapping-only forms are not applicable here
    assert "not" not in keywords
    assert "not:" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            when: c<CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "cross-compiling" in keywords
    # Mapping-only forms are not applicable here
    assert "not" not in keywords
    assert "not:" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            when:
              <CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "not:" in keywords
    # str-only forms are not applicable here
    assert "cross-compiling" not in keywords

    cursor_pos = put_doc_with_cursor(
        ls,
        debputy_manifest_uri,
        "debian/debputy.manifest",
        textwrap.dedent(
            """\
        manifest-version: 0.1
        installations:
        - install-docs:
            when:
              n<CURSOR>
"""
        ),
    )
    completions = debputy_manifest_completer(
        ls,
        CompletionParams(TextDocumentIdentifier(debputy_manifest_uri), cursor_pos),
    )
    assert isinstance(completions, list)
    keywords = {m.label for m in completions}
    assert "not:" in keywords
    # str-only forms are not applicable here
    assert "cross-compiling" not in keywords
