import asyncio
import os.path
from typing import (
    Dict,
    Sequence,
    Union,
    Optional,
    TypeVar,
    Callable,
    Mapping,
    List,
    Tuple,
)

from lsprotocol.types import (
    DidOpenTextDocumentParams,
    DidChangeTextDocumentParams,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_COMPLETION,
    CompletionList,
    CompletionItem,
    CompletionParams,
    TEXT_DOCUMENT_HOVER,
    TEXT_DOCUMENT_FOLDING_RANGE,
    FoldingRange,
    FoldingRangeParams,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    SemanticTokensParams,
    SemanticTokens,
    Hover,
    TEXT_DOCUMENT_CODE_ACTION,
    Command,
    CodeAction,
    CodeActionParams,
    SemanticTokensRegistrationOptions,
)

from debputy import __version__
from debputy.lsp.lsp_features import (
    DIAGNOSTIC_HANDLERS,
    COMPLETER_HANDLERS,
    HOVER_HANDLERS,
    SEMANTIC_TOKENS_FULL_HANDLERS,
    CODE_ACTION_HANDLERS,
    SEMANTIC_TOKENS_LEGEND,
)
from debputy.util import _info

_DOCUMENT_VERSION_TABLE: Dict[str, int] = {}

try:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument

    DEBPUTY_LANGUAGE_SERVER = LanguageServer("debputy", f"v{__version__}")
except ImportError:

    class Mock:

        def feature(self, *args, **kwargs):
            return lambda x: x

    DEBPUTY_LANGUAGE_SERVER = Mock()


P = TypeVar("P")
R = TypeVar("R")


def is_doc_at_version(uri: str, version: int) -> bool:
    dv = _DOCUMENT_VERSION_TABLE.get(uri)
    return dv == version


def determine_language_id(doc: "TextDocument") -> Tuple[str, str]:
    lang_id = doc.language_id
    if lang_id and not lang_id.isspace():
        return "declared", lang_id
    path = doc.path
    try:
        last_idx = path.rindex("debian/")
    except ValueError:
        return "filename", os.path.basename(path)
    guess_language_id = path[last_idx:]
    return "filename", guess_language_id


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_DID_OPEN)
@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_DID_CHANGE)
async def _open_or_changed_document(
    ls: "LanguageServer",
    params: Union[DidOpenTextDocumentParams, DidChangeTextDocumentParams],
) -> None:
    version = params.text_document.version
    doc_uri = params.text_document.uri
    doc = ls.workspace.get_text_document(doc_uri)

    _DOCUMENT_VERSION_TABLE[doc_uri] = version
    id_source, language_id = determine_language_id(doc)
    handler = DIAGNOSTIC_HANDLERS.get(language_id)
    if handler is None:
        _info(
            f"Opened/Changed document: {doc.path} ({language_id}, {id_source}) - no diagnostics handler"
        )
        return
    _info(
        f"Opened/Changed document: {doc.path} ({language_id}, {id_source}) - running diagnostics for doc version {version}"
    )
    last_publish_count = -1

    diagnostics_scanner = handler(ls, params)
    async for diagnostics in diagnostics_scanner:
        await asyncio.sleep(0)
        if not is_doc_at_version(doc_uri, version):
            # This basically happens with very edit, so lets not notify the client
            # for that.
            _info(
                f"Cancel (obsolete) diagnostics for doc version {version}: document version changed"
            )
            break
        if diagnostics is None or last_publish_count != len(diagnostics):
            last_publish_count = len(diagnostics) if diagnostics is not None else 0
            ls.publish_diagnostics(
                doc.uri,
                diagnostics,
            )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_COMPLETION)
def _completions(
    ls: "LanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        COMPLETER_HANDLERS,
        "Complete request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_HOVER)
def _hover(
    ls: "LanguageServer",
    params: CompletionParams,
) -> Optional[Hover]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        HOVER_HANDLERS,
        "Hover doc request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_CODE_ACTION)
def _code_actions(
    ls: "LanguageServer",
    params: CodeActionParams,
) -> Optional[List[Union[Command, CodeAction]]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        CODE_ACTION_HANDLERS,
        "Code action request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_FOLDING_RANGE)
def _folding_ranges(
    ls: "LanguageServer",
    params: FoldingRangeParams,
) -> Optional[Sequence[FoldingRange]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        HOVER_HANDLERS,
        "Folding range request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    SemanticTokensRegistrationOptions(
        SEMANTIC_TOKENS_LEGEND,
        full=True,
    ),
)
def _semantic_tokens_full(
    ls: "LanguageServer",
    params: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        SEMANTIC_TOKENS_FULL_HANDLERS,
        "Semantic tokens request",
    )


def _dispatch_standard_handler(
    ls: "LanguageServer",
    doc_uri: str,
    params: P,
    handler_table: Mapping[str, Callable[["LanguageServer", P], R]],
    request_type: str,
) -> R:
    doc = ls.workspace.get_text_document(doc_uri)

    id_source, language_id = determine_language_id(doc)
    handler = handler_table.get(language_id)
    if handler is None:
        _info(
            f"{request_type} for document: {doc.path} ({language_id}, {id_source}) - no handler"
        )
        return
    _info(
        f"{request_type} for document: {doc.path} ({language_id}, {id_source}) - delegating to handler"
    )

    return handler(
        ls,
        params,
    )
