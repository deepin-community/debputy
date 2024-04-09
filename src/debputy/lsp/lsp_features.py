import collections
import inspect
from typing import Callable, TypeVar, Sequence, Union, Dict, List, Optional

from lsprotocol.types import (
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    TEXT_DOCUMENT_CODE_ACTION,
    DidChangeTextDocumentParams,
    Diagnostic,
    DidOpenTextDocumentParams,
    SemanticTokensLegend,
)

from debputy.plugin.api.feature_set import PluginProvidedFeatureSet

try:
    from pygls.server import LanguageServer
except ImportError:
    pass

from debputy.linting.lint_util import LinterImpl
from debputy.lsp.quickfixes import provide_standard_quickfixes_from_diagnostics
from debputy.lsp.text_util import on_save_trim_end_of_line_whitespace

C = TypeVar("C", bound=Callable)

SEMANTIC_TOKENS_LEGEND = SemanticTokensLegend(
    token_types=["keyword", "enumMember"],
    token_modifiers=[],
)
SEMANTIC_TOKEN_TYPES_IDS = {
    t: idx for idx, t in enumerate(SEMANTIC_TOKENS_LEGEND.token_types)
}

LSP_PLUGIN_FEATURE_SET: Optional[PluginProvidedFeatureSet] = None
DIAGNOSTIC_HANDLERS = {}
COMPLETER_HANDLERS = {}
HOVER_HANDLERS = {}
CODE_ACTION_HANDLERS = {}
FOLDING_RANGE_HANDLERS = {}
SEMANTIC_TOKENS_FULL_HANDLERS = {}
WILL_SAVE_WAIT_UNTIL_HANDLERS = {}
_ALIAS_OF = {}

_STANDARD_HANDLERS = {
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
    file_formats: Union[str, Sequence[str]]
) -> Callable[[LinterImpl], LinterImpl]:

    def _wrapper(func: C) -> C:
        if not inspect.iscoroutinefunction(func):

            async def _lint_wrapper(
                ls: "LanguageServer",
                params: Union[
                    DidOpenTextDocumentParams,
                    DidChangeTextDocumentParams,
                ],
            ) -> Optional[List[Diagnostic]]:
                doc = ls.workspace.get_text_document(params.text_document.uri)
                yield func(
                    doc.uri,
                    doc.path,
                    doc.lines,
                    doc.position_codec,
                )

        else:
            raise ValueError("Linters are all non-async at the moment")

        for file_format in file_formats:
            if file_format in DIAGNOSTIC_HANDLERS:
                raise AssertionError(
                    "There is already a diagnostics handler for " + file_format
                )
            DIAGNOSTIC_HANDLERS[file_format] = _lint_wrapper

        return func

    return _wrapper


def lsp_diagnostics(file_formats: Union[str, Sequence[str]]) -> Callable[[C], C]:

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


def lsp_completer(file_formats: Union[str, Sequence[str]]) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, COMPLETER_HANDLERS)


def lsp_hover(file_formats: Union[str, Sequence[str]]) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, HOVER_HANDLERS)


def lsp_folding_ranges(file_formats: Union[str, Sequence[str]]) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, FOLDING_RANGE_HANDLERS)


def lsp_semantic_tokens_full(
    file_formats: Union[str, Sequence[str]]
) -> Callable[[C], C]:
    return _registering_wrapper(file_formats, SEMANTIC_TOKENS_FULL_HANDLERS)


def lsp_standard_handler(file_formats: Union[str, Sequence[str]], topic: str) -> None:
    res = _STANDARD_HANDLERS.get(topic)
    if res is None:
        raise ValueError(f"No standard handler for {topic}")

    table, handler = res

    _register_handler(file_formats, table, handler)


def _registering_wrapper(
    file_formats: Union[str, Sequence[str]], handler_dict: Dict[str, C]
) -> Callable[[C], C]:
    def _wrapper(func: C) -> C:
        _register_handler(file_formats, handler_dict, func)
        return func

    return _wrapper


def _register_handler(
    file_formats: Union[str, Sequence[str]],
    handler_dict: Dict[str, C],
    handler: C,
) -> None:
    if isinstance(file_formats, str):
        file_formats = [file_formats]
    else:
        if not file_formats:
            raise ValueError("At least one language ID (file format) must be provided")
        main = file_formats[0]
        for alias in file_formats[1:]:
            if alias not in _ALIAS_OF:
                _ALIAS_OF[alias] = main

    for file_format in file_formats:
        if file_format in handler_dict:
            raise AssertionError(f"There is already a handler for {file_format}")

        handler_dict[file_format] = handler


def lsp_set_plugin_features(feature_set: Optional[PluginProvidedFeatureSet]) -> None:
    global LSP_PLUGIN_FEATURE_SET
    LSP_PLUGIN_FEATURE_SET = feature_set


def lsp_get_plugin_features() -> PluginProvidedFeatureSet:
    global LSP_PLUGIN_FEATURE_SET
    features = LSP_PLUGIN_FEATURE_SET
    if features is None:
        raise RuntimeError(
            "Initialization error: The plugin feature set has not been initialized before it was needed."
        )
    return features


def ensure_lsp_features_are_loaded() -> None:
    # FIXME: This import is needed to force loading of the LSP files. But it only works
    #  for files with a linter (which currently happens to be all of them, but this is
    #  a bit fragile).
    from debputy.linting.lint_impl import LINTER_FORMATS

    assert LINTER_FORMATS


def describe_lsp_features() -> None:

    ensure_lsp_features_are_loaded()

    feature_list = [
        ("diagnostics (lint)", DIAGNOSTIC_HANDLERS),
        ("code actions/quickfixes", CODE_ACTION_HANDLERS),
        ("completion suggestions", COMPLETER_HANDLERS),
        ("hover docs", HOVER_HANDLERS),
        ("folding ranges", FOLDING_RANGE_HANDLERS),
        ("semantic tokens", SEMANTIC_TOKENS_FULL_HANDLERS),
        ("on-save handler", WILL_SAVE_WAIT_UNTIL_HANDLERS),
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
