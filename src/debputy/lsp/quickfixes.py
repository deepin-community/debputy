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
)

from lsprotocol.types import (
    CodeAction,
    Command,
    CodeActionParams,
    Diagnostic,
    CodeActionDisabledType,
    TextEdit,
    WorkspaceEdit,
    TextDocumentEdit,
    OptionalVersionedTextDocumentIdentifier,
    Range,
    Position,
    CodeActionKind,
)

from debputy.util import _warn

try:
    from debian._deb822_repro.locatable import Position as TEPosition, Range as TERange

    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


CodeActionName = Literal["correct-text", "remove-line"]


class CorrectTextCodeAction(TypedDict):
    code_action: Literal["correct-text"]
    correct_value: str


class RemoveLineCodeAction(TypedDict):
    code_action: Literal["remove-line"]


def propose_correct_text_quick_fix(correct_value: str) -> CorrectTextCodeAction:
    return {
        "code_action": "correct-text",
        "correct_value": correct_value,
    }


def propose_remove_line_quick_fix() -> RemoveLineCodeAction:
    return {
        "code_action": "remove-line",
    }


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
    edits = [
        TextEdit(
            diagnostic.range,
            corrected_value,
        ),
    ]
    yield CodeAction(
        title=f'Replace with "{corrected_value}"',
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: edits},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=edits,
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
def _correct_value_code_action(
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

    edits = [
        TextEdit(
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
        ),
    ]
    yield CodeAction(
        title="Remove the line",
        kind=CodeActionKind.QuickFix,
        diagnostics=[diagnostic],
        edit=WorkspaceEdit(
            changes={code_action_params.text_document.uri: edits},
            document_changes=[
                TextDocumentEdit(
                    text_document=OptionalVersionedTextDocumentIdentifier(
                        uri=code_action_params.text_document.uri,
                    ),
                    edits=edits,
                )
            ],
        ),
    )


def provide_standard_quickfixes_from_diagnostics(
    code_action_params: CodeActionParams,
) -> Optional[List[Union[Command, CodeAction]]]:
    actions = []
    for diagnostic in code_action_params.context.diagnostics:
        data = diagnostic.data
        if not isinstance(data, list):
            data = [data]
        for action_suggestion in data:
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
