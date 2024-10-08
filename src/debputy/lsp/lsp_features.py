import collections
import dataclasses
import inspect
import sys
from typing import (
    Callable,
    TypeVar,
    Sequence,
    Union,
    Dict,
    List,
    Optional,
    AsyncIterator,
    Self,
    Generic,
)

from debputy.commands.debputy_cmd.context import CommandContext
from debputy.commands.debputy_cmd.output import _output_styling
from debputy.lsp.lsp_self_check import LSP_CHECKS
from debputy.lsprotocol.types import (
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    TEXT_DOCUMENT_CODE_ACTION,
    DidChangeTextDocumentParams,
    Diagnostic,
    DidOpenTextDocumentParams,
    SemanticTokensLegend,
    TEXT_DOCUMENT_FORMATTING,
    SemanticTokenTypes,
)

try:
    from pygls.server import LanguageServer
    from debputy.lsp.debputy_ls import DebputyLanguageServer
except ImportError:
    pass

from debputy.linting.lint_util import LinterImpl
from debputy.lsp.quickfixes import provide_standard_quickfixes_from_diagnostics
from debputy.lsp.text_util import on_save_trim_end_of_line_whitespace

C = TypeVar("C", bound=Callable)

SEMANTIC_TOKENS_LEGEND = SemanticTokensLegend(
    token_types=[
        SemanticTokenTypes.Keyword.value,
        SemanticTokenTypes.EnumMember.value,
        SemanticTokenTypes.Comment.value,
        SemanticTokenTypes.String.value,
    ],
    token_modifiers=[],
)
SEMANTIC_TOKEN_TYPES_IDS = {
    t: idx for idx, t in enumerate(SEMANTIC_TOKENS_LEGEND.token_types)
}

DiagnosticHandler = Callable[
    [
        "DebputyLanguageServer",
        Union["DidOpenTextDocumentParams", "DidChangeTextDocumentParams"],
    ],
    AsyncIterator[Optional[List[Diagnostic]]],
]

DIAGNOSTIC_HANDLERS: Dict[
    str,
    List["_DispatchRule[DiagnosticHandler]"],
] = {}
COMPLETER_HANDLERS = {}
HOVER_HANDLERS = {}
CODE_ACTION_HANDLERS = {}
FOLDING_RANGE_HANDLERS = {}
SEMANTIC_TOKENS_FULL_HANDLERS = {}
WILL_SAVE_WAIT_UNTIL_HANDLERS = {}
FORMAT_FILE_HANDLERS = {}
TEXT_DOC_INLAY_HANDLERS = {}
_ALIAS_OF = {}


@dataclasses.dataclass(slots=True, frozen=True)
class LanguageDispatch:
    language_id: Optional[str]
    filename_selector: Optional[Union[str, Callable[[str], bool]]] = None

    @classmethod
    def from_language_id(
        cls,
        language_id: str,
        filename_selector: Optional[Union[str, Callable[[str], bool]]] = None,
    ) -> Self:
        return cls(language_id, filename_selector=filename_selector)

    def filename_match(self, filename: str) -> bool:
        selector = self.filename_selector
        if selector is None:
            return True
        if isinstance(selector, str):
            return filename == selector
        return selector(filename)


@dataclasses.dataclass(slots=True, frozen=True)
class _DispatchRule(Generic[C]):
    language_dispatch: LanguageDispatch
    handler: C


_STANDARD_HANDLERS = {
    TEXT_DOCUMENT_FORMATTING: (
        FORMAT_FILE_HANDLERS,
        on_save_trim_end_of_line_whitespace,
    ),
    TEXT_DOCUMENT_CODE_ACTION: (
        CODE_ACTION_HANDLERS,
        lambda ls, params: provide_standard_quickfixes_from_diagnostics(params),
    ),
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL: (
        WILL_SAVE_WAIT_UNTIL_HANDLERS,
        on_save_trim_end_of_line_whitespace,
    ),
}


def lint_diagnostics(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[LinterImpl], LinterImpl]:

    def _wrapper(func: C) -> C:
        if not inspect.iscoroutinefunction(func):

            async def _lint_wrapper(
                ls: "DebputyLanguageServer",
                params: Union[
                    DidOpenTextDocumentParams,
                    DidChangeTextDocumentParams,
                ],
            ) -> Optional[List[Diagnostic]]:
                doc = ls.workspace.get_text_document(params.text_document.uri)
                lint_state = ls.lint_state(doc)
                yield func(lint_state)

        else:
            raise ValueError("Linters are all non-async at the moment")

        for file_format in file_formats:
            if file_format.language_id in DIAGNOSTIC_HANDLERS:
                raise AssertionError(
                    "There is already a diagnostics handler for " + file_format
                )
            handler_metadata = _DispatchRule(file_format, _lint_wrapper)
            handlers = DIAGNOSTIC_HANDLERS.get(file_format.language_id)
            if handlers is None:
                DIAGNOSTIC_HANDLERS[file_format.language_id] = [handler_metadata]
            else:
                handlers.append(handler_metadata)

        return func

    return _wrapper


def lsp_diagnostics(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:

    def _wrapper(func: C) -> C:

        if not inspect.iscoroutinefunction(func):

            async def _linter(*args, **kwargs) -> None:
                res = func(*args, **kwargs)
                if inspect.isgenerator(res):
                    for r in res:
                        yield r
                else:
                    yield res

        else:

            _linter = func

        _register_handler(file_formats, DIAGNOSTIC_HANDLERS, _linter)

        return func

    return _wrapper


def lsp_completer(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, COMPLETER_HANDLERS)


def lsp_hover(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, HOVER_HANDLERS)


def lsp_text_doc_inlay_hints(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, TEXT_DOC_INLAY_HANDLERS)


def lsp_folding_ranges(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, FOLDING_RANGE_HANDLERS)


def lsp_will_save_wait_until(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, WILL_SAVE_WAIT_UNTIL_HANDLERS)


def lsp_format_document(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, FORMAT_FILE_HANDLERS)


def lsp_semantic_tokens_full(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, SEMANTIC_TOKENS_FULL_HANDLERS)


def lsp_standard_handler(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]], topic: str
) -> None:
    res = _STANDARD_HANDLERS.get(topic)
    if res is None:
        raise ValueError(f"No standard handler for {topic}")

    table, handler = res

    _register_handler(file_formats, table, handler)


def _registering_wrapper(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]],
    handler_dict: Dict[str, C],
) -> Callable[[C], C]:
    def _wrapper(func: C) -> C:
        _register_handler(file_formats, handler_dict, func)
        return func

    return _wrapper


def _register_handler(
    file_formats: Union[LanguageDispatch, Sequence[LanguageDispatch]],
    handler_dict: Dict[str, List[_DispatchRule[C]]],
    handler: C,
) -> None:
    if isinstance(file_formats, LanguageDispatch):
        file_formats = [file_formats]
    else:
        if not file_formats:
            raise ValueError("At least one language ID (file format) must be provided")
        main = file_formats[0]
        for alias in file_formats[1:]:
            if alias not in _ALIAS_OF:
                _ALIAS_OF[alias.language_id] = main.language_id

    for file_format in file_formats:
        if file_format in handler_dict:
            raise AssertionError(f"There is already a handler for {file_format}")

        handler_metadata = _DispatchRule(file_format, handler)
        handlers = handler_dict.get(file_format.language_id)
        if handlers is None:
            handler_dict[file_format.language_id] = [handler_metadata]
        else:
            handlers.append(handler_metadata)


def ensure_lsp_features_are_loaded() -> None:
    # FIXME: This import is needed to force loading of the LSP files. But it only works
    #  for files with a linter (which currently happens to be all of them, but this is
    #  a bit fragile).
    from debputy.linting.lint_impl import LINTER_FORMATS

    assert LINTER_FORMATS


def describe_lsp_features(context: CommandContext) -> None:
    fo = _output_styling(context.parsed_args, sys.stdout)
    ensure_lsp_features_are_loaded()

    feature_list = [
        ("diagnostics (lint)", DIAGNOSTIC_HANDLERS),
        ("code actions/quickfixes", CODE_ACTION_HANDLERS),
        ("completion suggestions", COMPLETER_HANDLERS),
        ("hover docs", HOVER_HANDLERS),
        ("folding ranges", FOLDING_RANGE_HANDLERS),
        ("semantic tokens", SEMANTIC_TOKENS_FULL_HANDLERS),
        ("on-save handler", WILL_SAVE_WAIT_UNTIL_HANDLERS),
        ("inlay hint (doc)", TEXT_DOC_INLAY_HANDLERS),
        ("format file handler", FORMAT_FILE_HANDLERS),
    ]
    print("LSP language IDs and their features:")
    all_ids = sorted(set(lid for _, t in feature_list for lid in t))
    for lang_id in all_ids:
        if lang_id in _ALIAS_OF:
            continue
        features = [n for n, t in feature_list if lang_id in t]
        print(f" * {lang_id}:")
        for feature in features:
            print(f"   - {feature}")

    aliases = collections.defaultdict(list)
    for lang_id in all_ids:
        main_lang = _ALIAS_OF.get(lang_id)
        if main_lang is None:
            continue
        aliases[main_lang].append(lang_id)

    print()
    print("Aliases:")
    for main_id, aliases in aliases.items():
        print(f" * {main_id}: {', '.join(aliases)}")

    print()
    print("General features:")
    for self_check in LSP_CHECKS:
        is_ok = self_check.test()
        if is_ok:
            print(f" * {self_check.feature}: {fo.colored('enabled', fg='green')}")
        else:
            if self_check.is_mandatory:
                disabled = fo.colored(
                    "missing",
                    fg="red",
                    bg="black",
                    style="bold",
                )
            else:
                disabled = fo.colored(
                    "disabled",
                    fg="yellow",
                    bg="black",
                    style="bold",
                )

            if self_check.how_to_fix:
                print(f" * {self_check.feature}: {disabled}")
                print(f"   - {self_check.how_to_fix}")
            else:
                problem_suffix = f" ({self_check.problem})"
                print(f" * {self_check.feature}: {disabled}{problem_suffix}")
