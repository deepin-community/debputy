from typing import (
    Literal,
    TypedDict,
    Callable,
    Iterable,
    Union,
    TypeVar,
    Mapping,
    Dict,
    Optional,
    List,
    cast,
    NotRequired,
)

from debputy.lsprotocol.types import (
    CodeAction,
    Command,
    CodeActionParams,
    Diagnostic,
    TextEdit,
    WorkspaceEdit,
    TextDocumentEdit,
    OptionalVersionedTextDocumentIdentifier,
    Range,
    Position,
    CodeActionKind,
)

from debputy.lsp.diagnostics import DiagnosticData
from debputy.util import _warn

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
    )

    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


CodeActionName = Literal[
    "correct-text",
    "remove-line",
    "remove-range",
    "insert-text-on-line-after-diagnostic",
]


class CorrectTextCodeAction(TypedDict):
    code_action: Literal["correct-text"]
    correct_value: str


class InsertTextOnLineAfterDiagnosticCodeAction(TypedDict):
    code_action: Literal["insert-text-on-line-after-diagnostic"]
    text_to_insert: str


class RemoveLineCodeAction(TypedDict):
    code_action: Literal["remove-line"]


class RemoveRangeCodeAction(TypedDict):
    code_action: Literal["remove-range"]
    proposed_title: NotRequired[str]


def propose_correct_text_quick_fix(correct_value: str) -> CorrectTextCodeAction:
    return {
        "code_action": "correct-text",
        "correct_value": correct_value,
    }


def propose_insert_text_on_line_after_diagnostic_quick_fix(
    text_to_insert: str,
) -> InsertTextOnLineAfterDiagnosticCodeAction:
    return {
        "code_action": "insert-text-on-line-after-diagnostic",
        "text_to_insert": text_to_insert,
    }


def propose_remove_line_quick_fix() -> RemoveLineCodeAction:
    return {
        "code_action": "remove-line",
    }


def propose_remove_range_quick_fix(
    *, proposed_title: Optional[str]
) -> RemoveRangeCodeAction:
    r: RemoveRangeCodeAction = {
        "code_action": "remove-range",
    }
    if proposed_title:
        r["proposed_title"] = proposed_title
    return r


CODE_ACTION_HANDLERS: Dict[
    CodeActionName,
    Callable[
        [Mapping[str, str], CodeActionParams, Diagnostic],
        Iterable[Union[CodeAction, Command]],
    ],
] = {}
M = TypeVar("M", bound=Mapping[str, str])
Handler = Callable[
    [M, CodeActionParams, Diagnostic],
    Iterable[Union[CodeAction, Command]],
]


def _code_handler_for(action_name: CodeActionName) -> Callable[[Handler], Handler]:
    def _wrapper(func: Handler) -> Handler:
        assert action_name not in CODE_ACTION_HANDLERS
        CODE_ACTION_HANDLERS[action_name] = func
        return func

    return _wrapper


@_code_handler_for("correct-text")
def _correct_value_code_action(
    code_action_data: CorrectTextCodeAction,
    code_action_params: CodeActionParams,
    diagnostic: Diagnostic,
) -> Iterable[Union[CodeAction, Command]]:
    corrected_value = code_action_data["correct_value"]
    edit = TextEdit(
        diagnostic.range,
        corrected_value,
    )
    yield CodeAction(
        title=f'Replace with "{corrected_value}"',
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: [edit]},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=[edit],
                )
            ],
        ),
    )


@_code_handler_for("insert-text-on-line-after-diagnostic")
def _insert_text_on_line_after_diagnostic_code_action(
    code_action_data: InsertTextOnLineAfterDiagnosticCodeAction,
    code_action_params: CodeActionParams,
    diagnostic: Diagnostic,
) -> Iterable[Union[CodeAction, Command]]:
    corrected_value = code_action_data["text_to_insert"]
    line_no = diagnostic.range.end.line
    if diagnostic.range.end.character > 0:
        line_no += 1
    insert_range = Range(
        Position(
            line_no,
            0,
        ),
        Position(
            line_no,
            0,
        ),
    )
    edit = TextEdit(
        insert_range,
        corrected_value,
    )
    yield CodeAction(
        title=f'Insert "{corrected_value}"',
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: [edit]},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=[edit],
                )
            ],
        ),
    )


def range_compatible_with_remove_line_fix(range_: Range) -> bool:
    start = range_.start
    end = range_.end
    if start.line != end.line and (start.line + 1 != end.line or end.character > 0):
        return False
    return True


@_code_handler_for("remove-line")
def _remove_line_code_action(
    _code_action_data: RemoveLineCodeAction,
    code_action_params: CodeActionParams,
    diagnostic: Diagnostic,
) -> Iterable[Union[CodeAction, Command]]:
    start = code_action_params.range.start
    if range_compatible_with_remove_line_fix(code_action_params.range):
        _warn(
            "Bug: the quick was used for a diagnostic that spanned multiple lines and would corrupt the file."
        )
        return

    edit = TextEdit(
        Range(
            start=Position(
                line=start.line,
                character=0,
            ),
            end=Position(
                line=start.line + 1,
                character=0,
            ),
        ),
        "",
    )
    yield CodeAction(
        title="Remove the line",
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: [edit]},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=[edit],
                )
            ],
        ),
    )


@_code_handler_for("remove-range")
def _remove_range_code_action(
    code_action_data: RemoveRangeCodeAction,
    code_action_params: CodeActionParams,
    diagnostic: Diagnostic,
) -> Iterable[Union[CodeAction, Command]]:
    edit = TextEdit(
        diagnostic.range,
        "",
    )
    title = code_action_data.get("proposed_title", "Delete")
    yield CodeAction(
        title=title,
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: [edit]},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=[edit],
                )
            ],
        ),
    )


def provide_standard_quickfixes_from_diagnostics(
    code_action_params: CodeActionParams,
) -> Optional[List[Union[Command, CodeAction]]]:
    actions: List[Union[Command, CodeAction]] = []
    for diagnostic in code_action_params.context.diagnostics:
        if not isinstance(diagnostic.data, dict):
            continue
        data: DiagnosticData = cast("DiagnosticData", diagnostic.data)
        quickfixes = data.get("quickfixes")
        if quickfixes is None:
            continue
        for action_suggestion in quickfixes:
            if (
                action_suggestion
                and isinstance(action_suggestion, Mapping)
                and "code_action" in action_suggestion
            ):
                action_name: CodeActionName = action_suggestion["code_action"]
                handler = CODE_ACTION_HANDLERS.get(action_name)
                if handler is not None:
                    actions.extend(
                        handler(
                            cast("Mapping[str, str]", action_suggestion),
                            code_action_params,
                            diagnostic,
                        )
                    )
                else:
                    _warn(f"No codeAction handler for {action_name} !?")
    if not actions:
        return None
    return actions
