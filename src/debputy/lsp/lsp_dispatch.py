import asyncio
from typing import (
    Dict,
    Sequence,
    Union,
    Optional,
    TypeVar,
    Mapping,
    List,
    TYPE_CHECKING,
)

from debputy import __version__
from debputy.lsp.lsp_features import (
    DIAGNOSTIC_HANDLERS,
    COMPLETER_HANDLERS,
    HOVER_HANDLERS,
    SEMANTIC_TOKENS_FULL_HANDLERS,
    CODE_ACTION_HANDLERS,
    SEMANTIC_TOKENS_LEGEND,
    WILL_SAVE_WAIT_UNTIL_HANDLERS,
    FORMAT_FILE_HANDLERS,
    _DispatchRule,
    C,
    TEXT_DOC_INLAY_HANDLERS,
)
from debputy.util import _info
from debputy.lsprotocol.types import (
    DidOpenTextDocumentParams,
    DidChangeTextDocumentParams,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_COMPLETION,
    TEXT_DOCUMENT_INLAY_HINT,
    CompletionList,
    CompletionItem,
    CompletionParams,
    InlayHintParams,
    InlayHint,
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
    TextEdit,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    WillSaveTextDocumentParams,
    TEXT_DOCUMENT_FORMATTING,
    INITIALIZE,
    InitializeParams,
)

_DOCUMENT_VERSION_TABLE: Dict[str, int] = {}


if TYPE_CHECKING:
    try:
        from pygls.server import LanguageServer
    except ImportError:
        pass

    from debputy.lsp.debputy_ls import DebputyLanguageServer

    DEBPUTY_LANGUAGE_SERVER = DebputyLanguageServer("debputy", f"v{__version__}")
else:
    try:
        from pygls.server import LanguageServer
        from debputy.lsp.debputy_ls import DebputyLanguageServer

        DEBPUTY_LANGUAGE_SERVER = DebputyLanguageServer("debputy", f"v{__version__}")
    except ImportError:

        class Mock:

            def feature(self, *args, **kwargs):
                return lambda x: x

        DEBPUTY_LANGUAGE_SERVER = Mock()


P = TypeVar("P")
R = TypeVar("R")
L = TypeVar("L", "LanguageServer", "DebputyLanguageServer")


def is_doc_at_version(uri: str, version: int) -> bool:
    dv = _DOCUMENT_VERSION_TABLE.get(uri)
    return dv == version


@DEBPUTY_LANGUAGE_SERVER.feature(INITIALIZE)
async def _on_initialize(
    ls: "DebputyLanguageServer",
    _: InitializeParams,
) -> None:
    await ls.on_initialize()


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_DID_OPEN)
async def _open_document(
    ls: "DebputyLanguageServer",
    params: DidChangeTextDocumentParams,
) -> None:
    await _open_or_changed_document(ls, params)


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_DID_CHANGE)
async def _changed_document(
    ls: "DebputyLanguageServer",
    params: DidChangeTextDocumentParams,
) -> None:
    await _open_or_changed_document(ls, params)


async def _open_or_changed_document(
    ls: "DebputyLanguageServer",
    params: Union[DidOpenTextDocumentParams, DidChangeTextDocumentParams],
) -> None:
    version = params.text_document.version
    doc_uri = params.text_document.uri
    doc = ls.workspace.get_text_document(doc_uri)

    _DOCUMENT_VERSION_TABLE[doc_uri] = version
    id_source, language_id, normalized_filename = ls.determine_language_id(doc)
    handler = _resolve_handler(DIAGNOSTIC_HANDLERS, language_id, normalized_filename)
    if handler is None:
        _info(
            f"Opened/Changed document: {doc.path} ({language_id}, {id_source},"
            f" normalized filename: {normalized_filename}) - no diagnostics handler"
        )
        return
    _info(
        f"Opened/Changed document: {doc.path} ({language_id}, {id_source}, normalized filename: {normalized_filename})"
        f" - running diagnostics for doc version {version}"
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
    ls: "DebputyLanguageServer",
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
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Hover]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        HOVER_HANDLERS,
        "Hover doc request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_INLAY_HINT)
def _doc_inlay_hint(
    ls: "DebputyLanguageServer",
    params: InlayHintParams,
) -> Optional[List[InlayHint]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        TEXT_DOC_INLAY_HANDLERS,
        "Inlay hint (doc) request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_CODE_ACTION)
def _code_actions(
    ls: "DebputyLanguageServer",
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
    ls: "DebputyLanguageServer",
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
    ls: "DebputyLanguageServer",
    params: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        SEMANTIC_TOKENS_FULL_HANDLERS,
        "Semantic tokens request",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)
def _will_save_wait_until(
    ls: "DebputyLanguageServer",
    params: WillSaveTextDocumentParams,
) -> Optional[Sequence[TextEdit]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        WILL_SAVE_WAIT_UNTIL_HANDLERS,
        "On-save formatting",
    )


@DEBPUTY_LANGUAGE_SERVER.feature(TEXT_DOCUMENT_FORMATTING)
def _format_document(
    ls: "DebputyLanguageServer",
    params: WillSaveTextDocumentParams,
) -> Optional[Sequence[TextEdit]]:
    return _dispatch_standard_handler(
        ls,
        params.text_document.uri,
        params,
        FORMAT_FILE_HANDLERS,
        "Full document formatting",
    )


def _dispatch_standard_handler(
    ls: "DebputyLanguageServer",
    doc_uri: str,
    params: P,
    handler_table: Mapping[str, List[_DispatchRule[C]]],
    request_type: str,
) -> Optional[R]:
    doc = ls.workspace.get_text_document(doc_uri)

    id_source, language_id, normalized_filename = ls.determine_language_id(doc)
    handler = _resolve_handler(handler_table, language_id, normalized_filename)
    if handler is None:
        _info(
            f"{request_type} for document: {doc.path} ({language_id}, {id_source},"
            f" normalized filename: {normalized_filename}) - no handler"
        )
        return None
    _info(
        f"{request_type} for document: {doc.path} ({language_id}, {id_source},"
        f" normalized filename: {normalized_filename}) - delegating to handler"
    )

    return handler(
        ls,
        params,
    )


def _resolve_handler(
    handler_table: Mapping[str, List[_DispatchRule[C]]],
    language_id: str,
    normalized_filename: str,
) -> Optional[C]:
    dispatch_rules = handler_table.get(language_id)
    if not dispatch_rules:
        return None
    for dispatch_rule in dispatch_rules:
        if dispatch_rule.language_dispatch.filename_match(normalized_filename):
            return dispatch_rule.handler
    return None
