from typing import Tuple, Union

try:
    from pygls.server import LanguageServer
    from lsprotocol.types import (
        TextDocumentItem,
        Position,
    )
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass


def _locate_cursor(text: str) -> Tuple[str, "Position"]:
    lines = text.splitlines(keepends=True)
    for line_no in range(len(lines)):
        line = lines[line_no]
        try:
            c = line.index("<CURSOR>")
        except ValueError:
            continue
        line = line.replace("<CURSOR>", "")
        lines[line_no] = line
        pos = Position(line_no, c)
        return "".join(lines), pos
    raise ValueError('Missing "<CURSOR>" marker')


def put_doc_with_cursor(
    ls: Union["LanguageServer", "DebputyLanguageServer"],
    uri: str,
    language_id: str,
    content: str,
) -> "Position":
    cleaned_content, cursor_pos = _locate_cursor(content)
    doc_version = 1
    existing = ls.workspace.text_documents.get(uri)
    if existing is not None:
        doc_version = existing.version + 1
    ls.workspace.put_text_document(
        TextDocumentItem(
            uri,
            language_id,
            doc_version,
            cleaned_content,
        )
    )
    return cursor_pos
