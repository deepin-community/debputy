import itertools
import re
from typing import (
    Union,
    Sequence,
    Optional,
    Iterable,
    List,
    Mapping,
)

from debputy.filesystem_scan import VirtualPathBase
from debputy.linting.lint_util import LintState
from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsp.diagnostics import DiagnosticData
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_standard_handler,
    lsp_completer,
    lsp_semantic_tokens_full,
    SEMANTIC_TOKEN_TYPES_IDS,
    LanguageDispatch,
)
from debputy.lsp.quickfixes import (
    propose_remove_range_quick_fix,
    propose_correct_text_quick_fix,
)
from debputy.lsp.text_util import (
    SemanticTokensState,
)
from debputy.lsprotocol.types import (
    CompletionItem,
    Diagnostic,
    CompletionList,
    CompletionParams,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    SemanticTokensParams,
    SemanticTokens,
    SemanticTokenTypes,
    Position,
    Range,
    DiagnosticSeverity,
    CompletionItemKind,
    CompletionItemLabelDetails,
)

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
        START_POSITION,
    )

    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
except ImportError:
    pass


_LANGUAGE_IDS = [
    LanguageDispatch.from_language_id("debian/patches/series"),
    # quilt path name
    LanguageDispatch.from_language_id("patches/series"),
]


def _as_hook_targets(command_name: str) -> Iterable[str]:
    for prefix, suffix in itertools.product(
        ["override_", "execute_before_", "execute_after_"],
        ["", "-arch", "-indep"],
    ):
        yield f"{prefix}{command_name}{suffix}"


# lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)
lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)

_RE_LINE_COMMENT = re.compile(r"^\s*(#(?:.*\S)?)\s*$")
_RE_PATCH_LINE = re.compile(
    r"""
    ^  \s* (?P<patch_name> \S+ ) \s*
       (?: (?P<options> [^#\s]+ ) \s* )?
       (?: (?P<comment> \# (?:.*\S)? ) \s* )?
""",
    re.VERBOSE,
)
_RE_UNNECESSARY_LEADING_PREFIX = re.compile(r"(?:(?:[.]{1,2})?/+)+")
_RE_UNNECESSARY_SLASHES = re.compile("//+")


def is_valid_file(path: str) -> bool:
    return path.endswith("/patches/series")


def _all_patch_files(
    debian_patches: VirtualPathBase,
) -> Iterable[VirtualPathBase]:
    if not debian_patches.is_dir:
        return

    for patch_file in debian_patches.all_paths():
        if patch_file.is_dir or patch_file.path in (
            "debian/patches/series",
            "./debian/patches/series",
        ):
            continue

        if patch_file.name.endswith("~"):
            continue
        if patch_file.name.startswith((".#", "#")):
            continue
        parent = patch_file.parent_dir
        if (
            parent is not None
            and parent.path in ("debian/patches", "./debian/patches")
            and patch_file.name.endswith(".series")
        ):
            continue
        yield patch_file


def _listed_patches(
    lines: List[str],
) -> Iterable[str]:
    for line in lines:
        m = _RE_PATCH_LINE.match(line)
        if m is None:
            continue
        filename = m.group(1)
        if filename.startswith("#"):
            continue
        filename = _RE_UNNECESSARY_LEADING_PREFIX.sub("", filename, count=1)
        filename = _RE_UNNECESSARY_SLASHES.sub("/", filename)
        if not filename:
            continue
        yield filename


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_patches_series(lint_state: LintState) -> Optional[List[Diagnostic]]:
    if not is_valid_file(lint_state.path):
        return None

    source_root = lint_state.source_root
    if source_root is None:
        return None

    dpatches = source_root.lookup("debian/patches/")
    if dpatches is None or not dpatches.is_dir:
        return None

    position_codec = lint_state.position_codec
    diagnostics = []
    used_patches = set()
    all_patches = {pf.path for pf in _all_patch_files(dpatches)}

    for line_no, line in enumerate(lint_state.lines):
        m = _RE_PATCH_LINE.match(line)
        if not m:
            continue
        groups = m.groupdict()
        orig_filename = groups["patch_name"]
        filename = orig_filename
        patch_start_col, patch_end_col = m.span("patch_name")
        orig_filename_start_col = patch_start_col
        if filename.startswith("#"):
            continue
        if filename.startswith(("../", "./", "/")):
            sm = _RE_UNNECESSARY_LEADING_PREFIX.match(filename)
            assert sm is not None
            slash_start, slash_end = sm.span(0)
            orig_filename_start_col = slash_end
            prefix = filename[:orig_filename_start_col]
            filename = filename[orig_filename_start_col:]
            slash_range = position_codec.range_to_client_units(
                lint_state.lines,
                Range(
                    Position(
                        line_no,
                        patch_start_col + slash_start,
                    ),
                    Position(
                        line_no,
                        patch_start_col + slash_end,
                    ),
                ),
            )
            skip_use_check = False
            if ".." in prefix:
                diagnostic_title = f'Disallowed prefix "{prefix}"'
                severity = DiagnosticSeverity.Error
                skip_use_check = True
            else:
                diagnostic_title = f'Unnecessary prefix "{prefix}"'
                severity = DiagnosticSeverity.Warning
            diagnostics.append(
                Diagnostic(
                    slash_range,
                    diagnostic_title,
                    source="debputy",
                    severity=severity,
                    data=DiagnosticData(
                        quickfixes=[
                            propose_remove_range_quick_fix(
                                proposed_title=f'Remove prefix "{prefix}"'
                            )
                        ]
                    ),
                )
            )
            if skip_use_check:
                continue
        if "//" in filename:
            for usm in _RE_UNNECESSARY_SLASHES.finditer(filename):
                start_col, end_cold = usm.span()
                slash_range = position_codec.range_to_client_units(
                    lint_state.lines,
                    Range(
                        Position(
                            line_no,
                            orig_filename_start_col + start_col,
                        ),
                        Position(
                            line_no,
                            orig_filename_start_col + end_cold,
                        ),
                    ),
                )
                diagnostics.append(
                    Diagnostic(
                        slash_range,
                        "Unnecessary slashes",
                        source="debputy",
                        severity=DiagnosticSeverity.Warning,
                        data=DiagnosticData(
                            quickfixes=[propose_correct_text_quick_fix("/")]
                        ),
                    )
                )
            filename = _RE_UNNECESSARY_SLASHES.sub("/", filename)

        patch_name_range = position_codec.range_to_client_units(
            lint_state.lines,
            Range(
                Position(
                    line_no,
                    patch_start_col,
                ),
                Position(
                    line_no,
                    patch_end_col,
                ),
            ),
        )
        if not filename.lower().endswith((".diff", ".patch")):
            diagnostics.append(
                Diagnostic(
                    patch_name_range,
                    f'Patch not using ".patch" or ".diff" as extension: "{filename}"',
                    source="debputy",
                    severity=DiagnosticSeverity.Hint,
                    data=DiagnosticData(
                        quickfixes=[propose_correct_text_quick_fix(f"{filename}.patch")]
                    ),
                )
            )
        patch_path = f"{dpatches.path}/{filename}"
        if patch_path not in all_patches:
            diagnostics.append(
                Diagnostic(
                    patch_name_range,
                    f'Non-existing patch "{filename}"',
                    source="debputy",
                    severity=DiagnosticSeverity.Error,
                )
            )
        elif patch_path in used_patches:
            diagnostics.append(
                Diagnostic(
                    patch_name_range,
                    f'Duplicate patch: "{filename}"',
                    source="debputy",
                    severity=DiagnosticSeverity.Error,
                )
            )
        else:
            used_patches.add(patch_path)

    unused_patches = all_patches - used_patches
    for unused_patch in sorted(unused_patches):
        patch_name = unused_patch[len(dpatches.path) + 1 :]
        line_count = len(lint_state.lines)
        file_range = Range(
            Position(
                0,
                0,
            ),
            Position(
                line_count,
                len(lint_state.lines[-1]) if line_count else 0,
            ),
        )
        diagnostics.append(
            Diagnostic(
                file_range,
                f'Unused patch: "{patch_name}"',
                source="debputy",
                severity=DiagnosticSeverity.Warning,
            )
        )

    return diagnostics


@lsp_completer(_LANGUAGE_IDS)
def _debian_patches_series_completions(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    if not is_valid_file(doc.path):
        return None
    lint_state = ls.lint_state(doc)
    source_root = lint_state.source_root
    dpatches = source_root.lookup("debian/patches") if source_root is not None else None
    if dpatches is None:
        return None
    lines = doc.lines
    position = doc.position_codec.position_from_client_units(lines, params.position)
    line = lines[position.line]
    if line.startswith("#"):
        return None
    try:
        line.rindex(" #", 0, position.character)
        return None  # In an end of line comment
    except ValueError:
        pass
    already_used = set(_listed_patches(lines))
    # `debian/patches + "/"`
    dpatches_dir_len = len(dpatches.path) + 1
    all_patch_files_gen = (
        p.path[dpatches_dir_len:] for p in _all_patch_files(dpatches)
    )
    return [
        CompletionItem(
            p,
            kind=CompletionItemKind.File,
            insert_text=f"{p}\n",
            label_details=CompletionItemLabelDetails(
                description=f"debian/patches/{p}",
            ),
        )
        for p in all_patch_files_gen
        if p not in already_used
    ]


@lsp_semantic_tokens_full(_LANGUAGE_IDS)
def _debian_patches_semantic_tokens_full(
    ls: "DebputyLanguageServer",
    request: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    doc = ls.workspace.get_text_document(request.text_document.uri)
    if not is_valid_file(doc.path):
        return None
    lines = doc.lines
    position_codec = doc.position_codec

    tokens: List[int] = []
    string_token_code = SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.String.value]
    comment_token_code = SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.Comment.value]
    options_token_code = SEMANTIC_TOKEN_TYPES_IDS[SemanticTokenTypes.Keyword.value]
    sem_token_state = SemanticTokensState(
        ls,
        doc,
        lines,
        tokens,
    )

    for line_no, line in enumerate(lines):
        if line.isspace():
            continue
        m = _RE_LINE_COMMENT.match(line)
        if m:
            start_col, end_col = m.span(1)
            start_pos = position_codec.position_to_client_units(
                sem_token_state.lines,
                Position(
                    line_no,
                    start_col,
                ),
            )
            sem_token_state.emit_token(
                start_pos,
                position_codec.client_num_units(line[start_col:end_col]),
                comment_token_code,
            )
            continue
        m = _RE_PATCH_LINE.match(line)
        if not m:
            continue
        groups = m.groupdict()
        _emit_group(
            line_no,
            string_token_code,
            sem_token_state,
            "patch_name",
            groups,
            m,
        )
        _emit_group(
            line_no,
            options_token_code,
            sem_token_state,
            "options",
            groups,
            m,
        )
        _emit_group(
            line_no,
            comment_token_code,
            sem_token_state,
            "comment",
            groups,
            m,
        )

    return SemanticTokens(tokens)


def _emit_group(
    line_no: int,
    token_code: int,
    sem_token_state: SemanticTokensState,
    group_name: str,
    groups: Mapping[str, str],
    match: re.Match,
) -> None:
    value = groups.get(group_name)
    if not value:
        return None
    patch_start_col, patch_end_col = match.span(group_name)
    position_codec = sem_token_state.doc.position_codec
    patch_start_pos = position_codec.position_to_client_units(
        sem_token_state.lines,
        Position(
            line_no,
            patch_start_col,
        ),
    )
    sem_token_state.emit_token(
        patch_start_pos,
        position_codec.client_num_units(value),
        token_code,
    )
