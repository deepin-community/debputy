import functools
import itertools
import os
import re
import subprocess
from typing import (
    Union,
    Sequence,
    Optional,
    Iterable,
    List,
    Iterator,
    Tuple,
    FrozenSet,
)

from debputy.dh.dh_assistant import (
    resolve_active_and_inactive_dh_commands,
    DhListCommands,
)
from debputy.linting.lint_util import LintState
from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsp.diagnostics import DiagnosticData
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_standard_handler,
    lsp_completer,
    LanguageDispatch,
)
from debputy.lsp.quickfixes import propose_correct_text_quick_fix
from debputy.lsp.spellchecking import spellcheck_line
from debputy.lsp.text_util import (
    LintCapablePositionCodec,
)
from debputy.lsprotocol.types import (
    CompletionItem,
    Diagnostic,
    Range,
    Position,
    DiagnosticSeverity,
    CompletionList,
    CompletionParams,
    TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL,
    TEXT_DOCUMENT_CODE_ACTION,
)
from debputy.util import detect_possible_typo

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


_CONTAINS_TAB_OR_COLON = re.compile(r"[\t:]")
_WORDS_RE = re.compile("([a-zA-Z0-9_-]+)")
_MAKE_ERROR_RE = re.compile(r"^[^:]+:(\d+):\s*(\S.+)")


_KNOWN_TARGETS = {
    "binary",
    "binary-arch",
    "binary-indep",
    "build",
    "build-arch",
    "build-indep",
    "clean",
}

_COMMAND_WORDS = frozenset(
    {
        "export",
        "ifeq",
        "ifneq",
        "ifdef",
        "ifndef",
        "endif",
        "else",
    }
)

_LANGUAGE_IDS = [
    LanguageDispatch.from_language_id("debian/rules"),
    # LSP's official language ID for Makefile
    LanguageDispatch.from_language_id("makefile", filename_selector="debian/rules"),
    # emacs's name (there is no debian-rules mode)
    LanguageDispatch.from_language_id(
        "makefile-gmake", filename_selector="debian/rules"
    ),
    # vim's name (there is no debrules)
    LanguageDispatch.from_language_id("make", filename_selector="debian/rules"),
]


def _as_hook_targets(command_name: str) -> Iterable[str]:
    for prefix, suffix in itertools.product(
        ["override_", "execute_before_", "execute_after_"],
        ["", "-arch", "-indep"],
    ):
        yield f"{prefix}{command_name}{suffix}"


lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)
lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_WILL_SAVE_WAIT_UNTIL)


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_rules(lint_state: LintState) -> Optional[List[Diagnostic]]:
    return _lint_debian_rules_impl(lint_state)


@functools.lru_cache
def _is_project_trusted(source_root: str) -> bool:
    return os.environ.get("DEBPUTY_TRUST_PROJECT", "0") == "1"


def _run_make_dryrun(
    source_root: str,
    lines: List[str],
) -> Optional[Diagnostic]:
    if not _is_project_trusted(source_root):
        return None
    try:
        make_res = subprocess.run(
            ["make", "--dry-run", "-f", "-", "debhelper-fail-me"],
            input="".join(lines).encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=source_root,
            timeout=1,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    else:
        if make_res.returncode != 0:
            make_output = make_res.stderr.decode("utf-8")
            m = _MAKE_ERROR_RE.match(make_output)
            if m:
                # We want it zero-based and make reports it one-based
                line_of_error = int(m.group(1)) - 1
                msg = m.group(2).strip()
                error_range = Range(
                    Position(
                        line_of_error,
                        0,
                    ),
                    Position(
                        line_of_error + 1,
                        0,
                    ),
                )
                # No conversion needed; it is pure line numbers
                return Diagnostic(
                    error_range,
                    f"make error: {msg}",
                    severity=DiagnosticSeverity.Error,
                    source="debputy (make)",
                )
    return None


def iter_make_lines(
    lines: List[str],
    position_codec: LintCapablePositionCodec,
    diagnostics: List[Diagnostic],
) -> Iterator[Tuple[int, str]]:
    skip_next_line = False
    is_extended_comment = False
    for line_no, line in enumerate(lines):
        skip_this = skip_next_line
        skip_next_line = False
        if line.rstrip().endswith("\\"):
            skip_next_line = True

        if skip_this:
            if is_extended_comment:
                diagnostics.extend(
                    spellcheck_line(lines, position_codec, line_no, line)
                )
            continue

        if line.startswith("#"):
            diagnostics.extend(spellcheck_line(lines, position_codec, line_no, line))
            is_extended_comment = skip_next_line
            continue
        is_extended_comment = False

        if line.startswith("\t") or line.isspace():
            continue

        is_extended_comment = False
        # We are not really dealing with extension lines at the moment (other than for spellchecking),
        # since nothing needs it
        yield line_no, line


def _forbidden_hook_targets(dh_commands: DhListCommands) -> FrozenSet[str]:
    if not dh_commands.disabled_commands:
        return frozenset()
    return frozenset(
        itertools.chain.from_iterable(
            _as_hook_targets(c) for c in dh_commands.disabled_commands
        )
    )


def _lint_debian_rules_impl(
    lint_state: LintState,
) -> Optional[List[Diagnostic]]:
    lines = lint_state.lines
    position_codec = lint_state.position_codec
    path = lint_state.path
    source_root = os.path.dirname(os.path.dirname(path))
    if source_root == "":
        source_root = "."
    diagnostics: List[Diagnostic] = []

    make_error = _run_make_dryrun(source_root, lines)
    if make_error is not None:
        diagnostics.append(make_error)
    dh_sequencer_data = lint_state.dh_sequencer_data
    dh_sequences = dh_sequencer_data.sequences
    dh_commands = resolve_active_and_inactive_dh_commands(
        dh_sequences,
        source_root=source_root,
    )
    if dh_commands.active_commands:
        all_hook_targets = {
            ht for c in dh_commands.active_commands for ht in _as_hook_targets(c)
        }
        all_hook_targets.update(_KNOWN_TARGETS)
        source = "debputy (dh_assistant)"
    else:
        all_hook_targets = _KNOWN_TARGETS
        source = "debputy"

    missing_targets = {}
    forbidden_hook_targets = _forbidden_hook_targets(dh_commands)
    all_allowed_hook_targets = all_hook_targets - forbidden_hook_targets

    for line_no, line in iter_make_lines(lines, position_codec, diagnostics):
        try:
            colon_idx = line.index(":")
            if len(line) > colon_idx + 1 and line[colon_idx + 1] == "=":
                continue
        except ValueError:
            continue
        target_substring = line[0:colon_idx]
        if "=" in target_substring or "$(for" in target_substring:
            continue
        for i, m in enumerate(_WORDS_RE.finditer(target_substring)):
            target = m.group(1)
            if i == 0 and (target in _COMMAND_WORDS or target.startswith("(")):
                break
            if "%" in target or "$" in target:
                continue
            if target in forbidden_hook_targets:
                pos, endpos = m.span(1)
                r_server_units = Range(
                    Position(
                        line_no,
                        pos,
                    ),
                    Position(
                        line_no,
                        endpos,
                    ),
                )
                r = position_codec.range_to_client_units(lines, r_server_units)
                diagnostics.append(
                    Diagnostic(
                        r,
                        f"The hook target {target} will not be run due to the choice of sequences.",
                        severity=DiagnosticSeverity.Error,
                        source=source,
                    )
                )
                continue

            if target in all_allowed_hook_targets or target in missing_targets:
                continue
            pos, endpos = m.span(1)
            hook_location = line_no, pos, endpos
            missing_targets[target] = hook_location

    for target, (line_no, pos, endpos) in missing_targets.items():
        candidates = detect_possible_typo(target, all_allowed_hook_targets)
        if not candidates and not target.startswith(
            ("override_", "execute_before_", "execute_after_")
        ):
            continue

        r_server_units = Range(
            Position(
                line_no,
                pos,
            ),
            Position(
                line_no,
                endpos,
            ),
        )
        r = position_codec.range_to_client_units(lines, r_server_units)
        if candidates:
            msg = f"Target {target} looks like a typo of a known target"
        else:
            msg = f"Unknown rules dh hook target {target}"
        if candidates:
            fixes = [propose_correct_text_quick_fix(c) for c in candidates]
        else:
            fixes = []
        diagnostics.append(
            Diagnostic(
                r,
                msg,
                severity=DiagnosticSeverity.Warning,
                data=DiagnosticData(quickfixes=fixes),
                source=source,
            )
        )
    return diagnostics


@lsp_completer(_LANGUAGE_IDS)
def _debian_rules_completions(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lines = doc.lines
    server_position = doc.position_codec.position_from_client_units(
        lines, params.position
    )

    line = lines[server_position.line]
    line_start = line[0 : server_position.character]

    if _CONTAINS_TAB_OR_COLON.search(line_start):
        return None

    source_root = os.path.dirname(os.path.dirname(doc.path))
    dh_sequencer_data = ls.lint_state(doc).dh_sequencer_data
    dh_sequences = dh_sequencer_data.sequences
    dh_commands = resolve_active_and_inactive_dh_commands(
        dh_sequences,
        source_root=source_root,
    )
    if not dh_commands.active_commands:
        return None
    items = [
        CompletionItem(ht)
        for c in dh_commands.active_commands
        for ht in _as_hook_targets(c)
    ]

    return items
