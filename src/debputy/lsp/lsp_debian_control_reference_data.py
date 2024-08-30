import dataclasses
import functools
import itertools
import operator
import re
import sys
import textwrap
from abc import ABC
from enum import Enum, auto
from typing import (
    FrozenSet,
    Optional,
    cast,
    Mapping,
    Iterable,
    List,
    Generic,
    TypeVar,
    Union,
    Callable,
    Tuple,
    Any,
    Set,
    TYPE_CHECKING,
    Sequence,
)

from debian.debian_support import DpkgArchTable, Version

from debputy.lsprotocol.types import (
    DiagnosticSeverity,
    Diagnostic,
    DiagnosticTag,
    Range,
    TextEdit,
    Position,
    CompletionItem,
    MarkupContent,
    CompletionItemTag,
    MarkupKind,
    CompletionItemKind,
    CompletionItemLabelDetails,
)

from debputy.filesystem_scan import VirtualPathBase
from debputy.linting.lint_util import LintState
from debputy.lsp.diagnostics import DiagnosticData
from debputy.lsp.lsp_reference_keyword import (
    ALL_PUBLIC_NAMED_STYLES,
    Keyword,
    allowed_values,
    format_comp_item_synopsis_doc,
    UsageHint,
)
from debputy.lsp.quickfixes import (
    propose_correct_text_quick_fix,
    propose_remove_line_quick_fix,
    propose_remove_range_quick_fix,
)
from debputy.lsp.text_edit import apply_text_edits
from debputy.lsp.text_util import (
    normalize_dctrl_field_name,
    LintCapablePositionCodec,
    te_range_to_lsp,
    trim_end_of_line_whitespace,
)
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    LIST_SPACE_SEPARATED_INTERPRETATION,
    Deb822ParagraphElement,
    Deb822FileElement,
    Interpretation,
    LIST_COMMA_SEPARATED_INTERPRETATION,
    ListInterpretation,
    _parsed_value_render_factory,
    Deb822ParsedValueElement,
    LIST_UPLOADERS_INTERPRETATION,
    _parse_whitespace_list_value,
    parse_deb822_file,
    Deb822ParsedTokenList,
    Deb822ValueLineElement,
)
from debputy.lsp.vendoring._deb822_repro.tokens import (
    Deb822FieldNameToken,
    _value_line_tokenizer,
    Deb822ValueToken,
    Deb822Token,
    _RE_WHITESPACE_SEPARATED_WORD_LIST,
    Deb822SpaceSeparatorToken,
)
from debputy.lsp.vendoring._deb822_repro.types import FormatterCallback
from debputy.lsp.vendoring._deb822_repro.types import TE
from debputy.lsp.vendoring.wrap_and_sort import _sort_packages_key
from debputy.path_matcher import BasenameGlobMatch
from debputy.plugin.api import VirtualPath
from debputy.util import PKGNAME_REGEX, _info, detect_possible_typo

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
        START_POSITION,
    )
except ImportError:
    pass


if TYPE_CHECKING:
    from debputy.lsp.maint_prefs import EffectiveFormattingPreference


F = TypeVar("F", bound="Deb822KnownField")
S = TypeVar("S", bound="StanzaMetadata")


# FIXME: should go into python3-debian
_RE_COMMA = re.compile("([^,]*),([^,]*)")
_RE_SYNOPSIS_STARTS_WITH_ARTICLE = re.compile(r"^\s*(an?|the)(?:\s|$)", re.I)
_RE_SV = re.compile(r"(\d+[.]\d+[.]\d+)([.]\d+)?")
_RE_SYNOPSIS_IS_TEMPLATE = re.compile(
    r"^\s*(missing|<insert up to \d+ chars description>)$"
)
_RE_SYNOPSIS_IS_TOO_SHORT = re.compile(r"^\s*(\S+)$")
CURRENT_STANDARDS_VERSION = Version("4.7.0")


@_value_line_tokenizer
def comma_or_space_split_tokenizer(v):
    # type: (str) -> Iterable[Deb822Token]
    assert "\n" not in v
    for match in _RE_WHITESPACE_SEPARATED_WORD_LIST.finditer(v):
        space_before, word, space_after = match.groups()
        if space_before:
            yield Deb822SpaceSeparatorToken(sys.intern(space_before))
        if "," in word:
            for m in _RE_COMMA.finditer(word):
                word_before, word_after = m.groups()
                if word_before:
                    yield Deb822ValueToken(word_before)
                # ... not quite a whitespace, but it is too much pain to make it a non-whitespace token.
                yield Deb822SpaceSeparatorToken(",")
                if word_after:
                    yield Deb822ValueToken(word_after)
        else:
            yield Deb822ValueToken(word)
        if space_after:
            yield Deb822SpaceSeparatorToken(sys.intern(space_after))


# FIXME: should go into python3-debian
LIST_COMMA_OR_SPACE_SEPARATED_INTERPRETATION = ListInterpretation(
    comma_or_space_split_tokenizer,
    _parse_whitespace_list_value,
    Deb822ParsedValueElement,
    Deb822SpaceSeparatorToken,
    lambda: Deb822SpaceSeparatorToken(","),
    _parsed_value_render_factory,
)

CustomFieldCheck = Callable[
    [
        "F",
        Deb822FileElement,
        Deb822KeyValuePairElement,
        "TERange",
        "TERange",
        Deb822ParagraphElement,
        "TEPosition",
        LintState,
    ],
    Iterable[Diagnostic],
]


@functools.lru_cache
def all_package_relationship_fields() -> Mapping[str, str]:
    # TODO: Pull from `dpkg-dev` when possible fallback only to the static list.
    return {
        f.lower(): f
        for f in (
            "Pre-Depends",
            "Depends",
            "Recommends",
            "Suggests",
            "Enhances",
            "Conflicts",
            "Breaks",
            "Replaces",
            "Provides",
            "Built-Using",
            "Static-Built-Using",
        )
    }


@functools.lru_cache
def all_source_relationship_fields() -> Mapping[str, str]:
    # TODO: Pull from `dpkg-dev` when possible fallback only to the static list.
    return {
        f.lower(): f
        for f in (
            "Build-Depends",
            "Build-Depends-Arch",
            "Build-Depends-Indep",
            "Build-Conflicts",
            "Build-Conflicts-Arch",
            "Build-Conflicts-Indep",
        )
    }


ALL_SECTIONS_WITHOUT_COMPONENT = frozenset(
    [
        "admin",
        "cli-mono",
        "comm",
        "database",
        "debian-installer",
        "debug",
        "devel",
        "doc",
        "editors",
        "education",
        "electronics",
        "embedded",
        "fonts",
        "games",
        "gnome",
        "gnu-r",
        "gnustep",
        "golang",
        "graphics",
        "hamradio",
        "haskell",
        "httpd",
        "interpreters",
        "introspection",
        "java",
        "javascript",
        "kde",
        "kernel",
        "libdevel",
        "libs",
        "lisp",
        "localization",
        "mail",
        "math",
        "metapackages",
        "misc",
        "net",
        "news",
        "ocaml",
        "oldlibs",
        "otherosfs",
        "perl",
        "php",
        "python",
        "ruby",
        "rust",
        "science",
        "shells",
        "sound",
        "tasks",
        "tex",
        "text",
        "utils",
        "vcs",
        "video",
        "virtual",
        "web",
        "x11",
        "xfce",
        "zope",
    ]
)

ALL_COMPONENTS = frozenset(
    [
        "main",
        "restricted",  # Ubuntu
        "non-free",
        "non-free-firmware",
        "contrib",
    ]
)


def _fields(*fields: F) -> Mapping[str, F]:
    return {normalize_dctrl_field_name(f.name.lower()): f for f in fields}


ALL_SECTIONS = allowed_values(
    *[
        s if c is None else f"{c}/{s}"
        for c, s in itertools.product(
            itertools.chain(cast("Iterable[Optional[str]]", [None]), ALL_COMPONENTS),
            ALL_SECTIONS_WITHOUT_COMPONENT,
        )
    ]
)

ALL_PRIORITIES = allowed_values(
    Keyword(
        "required",
        usage_hint="rare",
        synopsis_doc="Package is Essential or an Essential package needs it (and is not a library)",
        hover_text=textwrap.dedent(
            """\
            The package is necessary for the proper functioning of the system (read: dpkg needs it).

            Applicable if dpkg *needs* this package to function and it is not a library.

            No two packages that both have a priority of *standard* or higher may conflict with
            each other.
        """
        ),
    ),
    Keyword(
        "important",
        usage_hint="rare",
        synopsis_doc="Bare minimum of commonly-expected and necessary tools",
        hover_text=textwrap.dedent(
            """\
            The *important* packages are a bare minimum of commonly-expected and necessary tools.

            Applicable if 99% of all users in the distribution needs this package and it is not a library.

            No two packages that both have a priority of *standard* or higher may conflict with
            each other.
        """
        ),
    ),
    Keyword(
        "standard",
        usage_hint="rare",
        synopsis_doc="If your distribution installer would install this by default (not for libraries)",
        hover_text=textwrap.dedent(
            """\
            These packages provide a reasonable small but not too limited character-mode system.  This is
            what will be installed by default (by the debian-installer) if the user does not select anything
            else.  This does not include many large applications.

            Applicable if your distribution installer will install this package by default on a new system
            and it is not a library.

            No two packages that both have a priority of *standard* or higher may conflict with
            each other.
        """
        ),
    ),
    Keyword(
        "optional",
        synopsis_doc="The default priority.",
        sort_text="aa-optional",
        hover_text="This is the default priority and used by the majority of all packages"
        " in the Debian archive",
    ),
    Keyword(
        "extra",
        is_obsolete=True,
        replaced_by="optional",
        sort_text="zz-extra",
        synopsis_doc="Obsolete alias of `optional`",
        hover_text="Obsolete alias of `optional`.",
    ),
)


def all_architectures_and_wildcards(
    arch2table, *, allow_negations: bool = False
) -> Iterable[Union[str, Keyword]]:
    wildcards = set()
    yield Keyword(
        "any",
        is_exclusive=True,
        synopsis_doc="Build once per machine architecture (native code, such as C/C++, interpreter to C bindings)",
        hover_text=textwrap.dedent(
            """\
            The package is an architecture dependent package and need to be compiled for each and every
            architecture it.

            The name `any` refers to the fact that this is an architecture *wildcard* matching
            *any machine architecture* supported by dpkg.
        """
        ),
    )
    yield Keyword(
        "all",
        is_exclusive=True,
        synopsis_doc="Independent of machine architecture (scripts, Java without JNI, data or documentation)",
        hover_text=textwrap.dedent(
            """\
            The package is an architecture independent package.  This is typically fitting for packages containing
            only scripts, data or documentation.

            This name `all` refers to the fact that the package can be used for *all* architectures at the same.
            Though note that it is still subject to the rules of the `Multi-Arch` field.
        """
        ),
    )
    for arch_name, quad_tuple in arch2table.items():
        yield arch_name
        if allow_negations:
            yield f"!{arch_name}"
        cpu_wc = "any-" + quad_tuple.cpu_name
        os_wc = quad_tuple.os_name + "-any"
        if cpu_wc not in wildcards:
            yield cpu_wc
            if allow_negations:
                yield f"!{cpu_wc}"
            wildcards.add(cpu_wc)
        if os_wc not in wildcards:
            yield os_wc
            if allow_negations:
                yield f"!{os_wc}"
            wildcards.add(os_wc)
        # Add the remaining wildcards


@functools.lru_cache
def dpkg_arch_and_wildcards(*, allow_negations=False) -> FrozenSet[Union[str, Keyword]]:
    dpkg_arch_table = DpkgArchTable.load_arch_table()
    return frozenset(
        all_architectures_and_wildcards(
            dpkg_arch_table._arch2table,
            allow_negations=allow_negations,
        )
    )


def extract_first_value_and_position(
    kvpair: Deb822KeyValuePairElement,
    stanza_pos: "TEPosition",
    lint_state: LintState,
    *,
    interpretation: Interpretation[
        Deb822ParsedTokenList[Any, Any]
    ] = LIST_SPACE_SEPARATED_INTERPRETATION,
) -> Tuple[Optional[str], Optional[Range]]:
    kvpair_pos = kvpair.position_in_parent().relative_to(stanza_pos)
    value_element_pos = kvpair.value_element.position_in_parent().relative_to(
        kvpair_pos
    )
    for value_ref in kvpair.interpret_as(interpretation).iter_value_references():
        v = value_ref.value
        section_value_loc = value_ref.locatable
        value_range_te = section_value_loc.range_in_parent().relative_to(
            value_element_pos
        )
        section_range_server_units = te_range_to_lsp(value_range_te)
        section_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            section_range_server_units,
        )
        return v, section_range
    return None, None


def _sv_field_validation(
    _known_field: "F",
    _deb822_file: Deb822FileElement,
    kvpair: Deb822KeyValuePairElement,
    _kvpair_range: "TERange",
    _field_name_range_te: "TERange",
    _stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    sv_value, sv_value_range = extract_first_value_and_position(
        kvpair,
        stanza_position,
        lint_state,
    )
    m = _RE_SV.fullmatch(sv_value)
    if m is None:
        yield Diagnostic(
            sv_value_range,
            f'Not a valid version. Current version is "{CURRENT_STANDARDS_VERSION}"',
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )
        return

    sv_version = Version(sv_value)
    if sv_version < CURRENT_STANDARDS_VERSION:
        yield Diagnostic(
            sv_value_range,
            f"Latest Standards-Version is {CURRENT_STANDARDS_VERSION}",
            severity=DiagnosticSeverity.Information,
            source="debputy",
        )
        return
    extra = m.group(2)
    if extra:
        extra_len = lint_state.position_codec.client_num_units(extra)
        yield Diagnostic(
            Range(
                Position(
                    sv_value_range.end.line,
                    sv_value_range.end.character - extra_len,
                ),
                sv_value_range.end,
            ),
            "Unnecessary version segment. This part of the version is only used for editorial changes",
            severity=DiagnosticSeverity.Information,
            source="debputy",
            data=DiagnosticData(
                quickfixes=[
                    propose_remove_range_quick_fix(
                        proposed_title="Remove unnecessary version part"
                    )
                ]
            ),
        )


def _dctrl_ma_field_validation(
    _known_field: "F",
    _deb822_file: Deb822FileElement,
    _kvpair: Deb822KeyValuePairElement,
    _kvpair_range: "TERange",
    _field_name_range: "TERange",
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    ma_kvpair = stanza.get_kvpair_element(("Multi-Arch", 0), use_get=True)
    arch = stanza.get("Architecture", "any")
    if arch == "all" and ma_kvpair is not None:
        ma_value, ma_value_range = extract_first_value_and_position(
            ma_kvpair,
            stanza_position,
            lint_state,
        )
        if ma_value == "same":
            yield Diagnostic(
                ma_value_range,
                "Multi-Arch: same is not valid for Architecture: all packages. Maybe you want foreign?",
                severity=DiagnosticSeverity.Error,
                source="debputy",
            )


def _udeb_only_field_validation(
    known_field: "F",
    _deb822_file: Deb822FileElement,
    _kvpair: Deb822KeyValuePairElement,
    _kvpair_range: "TERange",
    field_name_range: "TERange",
    stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    package_type = stanza.get("Package-Type")
    if package_type != "udeb":
        field_range_server_units = te_range_to_lsp(field_name_range)
        field_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            field_range_server_units,
        )
        yield Diagnostic(
            field_range,
            f"The {known_field.name} field is only applicable to udeb packages (`Package-Type: udeb`)",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )


def _complete_only_in_arch_dep_pkgs(
    stanza_parts: Iterable[Deb822ParagraphElement],
) -> bool:
    for stanza in stanza_parts:
        arch = stanza.get("Architecture")
        if arch is None:
            continue
        archs = arch.split()
        return "all" not in archs
    return False


def _complete_only_for_udeb_pkgs(
    stanza_parts: Iterable[Deb822ParagraphElement],
) -> bool:
    for stanza in stanza_parts:
        for option in ("Package-Type", "XC-Package-Type"):
            pkg_type = stanza.get(option)
            if pkg_type is not None:
                return pkg_type == "udeb"
    return False


def _arch_not_all_only_field_validation(
    known_field: "F",
    _deb822_file: Deb822FileElement,
    _kvpair: Deb822KeyValuePairElement,
    _kvpair_range_te: "TERange",
    field_name_range_te: "TERange",
    stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    architecture = stanza.get("Architecture")
    if architecture == "all":
        field_range_server_units = te_range_to_lsp(field_name_range_te)
        field_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            field_range_server_units,
        )
        yield Diagnostic(
            field_range,
            f"The {known_field.name} field is not applicable to arch:all packages (`Architecture: all`)",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )


def _single_line_span_to_client_range(
    span: Tuple[int, int],
    relative_to: "TEPosition",
    lint_state: LintState,
) -> Range:
    range_server_units = Range(
        Position(
            relative_to.line_position,
            relative_to.cursor_position + span[0],
        ),
        Position(
            relative_to.line_position,
            relative_to.cursor_position + span[1],
        ),
    )
    return lint_state.position_codec.range_to_client_units(
        lint_state.lines,
        range_server_units,
    )


def _check_synopsis(
    synopsis_value_line: Deb822ValueLineElement,
    synopsis_range_te: "TERange",
    field_name_range_te: "TERange",
    package: Optional[str],
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    # This function would compute range would be wrong if there is a comment
    assert synopsis_value_line.comment_element is None
    synopsis_text_with_leading_space = synopsis_value_line.convert_to_text().rstrip()
    if not synopsis_text_with_leading_space:
        yield Diagnostic(
            lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                te_range_to_lsp(field_name_range_te),
            ),
            "Package synopsis is missing",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )
        return
    synopsis_text_trimmed = synopsis_text_with_leading_space.lstrip()
    synopsis_offset = len(synopsis_text_with_leading_space) - len(synopsis_text_trimmed)
    # synopsis_text_trimmed_lower = synopsis_text_trimmed.lower()
    starts_with_article = _RE_SYNOPSIS_STARTS_WITH_ARTICLE.search(
        synopsis_text_with_leading_space
    )
    # TODO: Handle ${...} expansion
    if starts_with_article:
        yield Diagnostic(
            _single_line_span_to_client_range(
                starts_with_article.span(1),
                synopsis_range_te.start_pos,
                lint_state,
            ),
            "Package synopsis starts with an article (a/an/the).",
            severity=DiagnosticSeverity.Warning,
            source="DevRef 6.2.2",
        )
    if len(synopsis_text_trimmed) >= 80:
        # Policy says `certainly under 80 characters.`, so exactly 80 characters is considered bad too.
        span = synopsis_offset + 79, len(synopsis_text_with_leading_space)
        yield Diagnostic(
            _single_line_span_to_client_range(
                span,
                synopsis_range_te.start_pos,
                lint_state,
            ),
            "Package synopsis is too long.",
            severity=DiagnosticSeverity.Warning,
            source="Policy 3.4.1",
        )
    if template_match := _RE_SYNOPSIS_IS_TEMPLATE.match(
        synopsis_text_with_leading_space
    ):
        yield Diagnostic(
            _single_line_span_to_client_range(
                template_match.span(1),
                synopsis_range_te.start_pos,
                lint_state,
            ),
            "Package synopsis is a placeholder",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )
    elif too_short_match := _RE_SYNOPSIS_IS_TOO_SHORT.match(
        synopsis_text_with_leading_space
    ):
        yield Diagnostic(
            _single_line_span_to_client_range(
                too_short_match.span(1),
                synopsis_range_te.start_pos,
                lint_state,
            ),
            "Package synopsis is too short",
            severity=DiagnosticSeverity.Warning,
            source="debputy",
        )


def dctrl_description_validator(
    _known_field: "F",
    _deb822_file: Deb822FileElement,
    kvpair: Deb822KeyValuePairElement,
    kvpair_range_te: "TERange",
    _field_name_range: "TERange",
    stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    value_lines = kvpair.value_element.value_lines
    if not value_lines:
        return
    package = stanza.get("Package")
    synopsis_value_line = value_lines[0]
    value_range_te = kvpair.value_element.range_in_parent().relative_to(
        kvpair_range_te.start_pos
    )
    if synopsis_value_line.continuation_line_token is None:
        field_name_range_te = kvpair.field_token.range_in_parent().relative_to(
            kvpair_range_te.start_pos
        )
        synopsis_range_te = synopsis_value_line.range_in_parent().relative_to(
            value_range_te.start_pos
        )
        yield from _check_synopsis(
            synopsis_value_line,
            synopsis_range_te,
            field_name_range_te,
            package,
            lint_state,
        )


def _each_value_match_regex_validation(
    regex: re.Pattern,
    *,
    diagnostic_severity: DiagnosticSeverity = DiagnosticSeverity.Error,
) -> CustomFieldCheck:

    def _validator(
        _known_field: "F",
        _deb822_file: Deb822FileElement,
        kvpair: Deb822KeyValuePairElement,
        kvpair_range_te: "TERange",
        _field_name_range_te: "TERange",
        _stanza: Deb822ParagraphElement,
        _stanza_position: "TEPosition",
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:

        value_element_pos = kvpair.value_element.position_in_parent().relative_to(
            kvpair_range_te.start_pos
        )
        for value_ref in kvpair.interpret_as(
            LIST_SPACE_SEPARATED_INTERPRETATION
        ).iter_value_references():
            v = value_ref.value
            m = regex.fullmatch(v)
            if m is not None:
                continue

            if "${" in v:
                # Ignore substvars
                continue

            section_value_loc = value_ref.locatable
            value_range_te = section_value_loc.range_in_parent().relative_to(
                value_element_pos
            )
            value_range_server_units = te_range_to_lsp(value_range_te)
            value_range = lint_state.position_codec.range_to_client_units(
                lint_state.lines, value_range_server_units
            )
            yield Diagnostic(
                value_range,
                f'The value "{v}" does not match the regex {regex.pattern}.',
                severity=diagnostic_severity,
                source="debputy",
            )

    return _validator


_DEP_OR_RELATION = re.compile(r"[|]")
_DEP_RELATION_CLAUSE = re.compile(
    r"""
    ^
    \s*
    (?P<name_arch_qual>[-+.a-zA-Z0-9${}:]{2,})
    \s*
    (?: [(] \s* (?P<operator>>>|>=|=|<=|<<) \s* (?P<version> [^)]+) \s* [)] \s* )?
    (?: \[ (?P<arch_restriction> [\s!\w\-]+) ] \s*)?
    (?: < (?P<build_profile_restriction> .+ ) > \s*)?
    ((?P<garbage>\S.*)\s*)?
    $
""",
    re.VERBOSE | re.MULTILINE,
)


def _span_to_te_range(
    text: str,
    start_pos: int,
    end_pos: int,
) -> TERange:
    prefix = text[0:start_pos]
    prefix_plus_text = text[0:end_pos]

    start_line = prefix.count("\n")
    if start_line:
        start_newline_offset = prefix.rindex("\n")
        # +1 to skip past the newline
        start_cursor_pos = start_pos - (start_newline_offset + 1)
    else:
        start_cursor_pos = start_pos

    end_line = prefix_plus_text.count("\n")
    if end_line == start_line:
        end_cursor_pos = start_cursor_pos + (end_pos - start_pos)
    else:
        end_newline_offset = prefix_plus_text.rindex("\n")
        end_cursor_pos = end_pos - (end_newline_offset + 1)

    return TERange(
        TEPosition(
            start_line,
            start_cursor_pos,
        ),
        TEPosition(
            end_line,
            end_cursor_pos,
        ),
    )


def _split_w_spans(
    v: str,
    sep: str,
    *,
    offset: int = 0,
) -> Sequence[Tuple[str, int, int]]:
    separator_size = len(sep)
    parts = v.split(sep)
    for part in parts:
        size = len(part)
        end_offset = offset + size
        yield part, offset, end_offset
        offset = end_offset + separator_size


_COLLAPSE_WHITESPACE = re.compile(r"\s+")


def _cleanup_rel(rel: str) -> str:
    return _COLLAPSE_WHITESPACE.sub(" ", rel.strip())


def _text_to_te_position(text: str) -> "TEPosition":
    newlines = text.count("\n")
    if not newlines:
        return TEPosition(
            newlines,
            len(text),
        )
    last_newline_offset = text.rindex("\n")
    line_offset = len(text) - (last_newline_offset + 1)
    return TEPosition(
        newlines,
        line_offset,
    )


def _dctrl_validate_dep(
    known_field: "F",
    _deb822_file: Deb822FileElement,
    kvpair: Deb822KeyValuePairElement,
    kvpair_range_te: "TERange",
    _field_name_range: "TERange",
    _stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    value_element_pos = kvpair.value_element.position_in_parent().relative_to(
        kvpair_range_te.start_pos
    )
    raw_value_with_comments = kvpair.value_element.convert_to_text()
    raw_value_masked_comments = "".join(
        (line if not line.startswith("#") else (" " * (len(line) - 1)) + "\n")
        for line in raw_value_with_comments.splitlines(keepends=True)
    )
    if isinstance(known_field, DctrlRelationshipKnownField):
        version_operators = known_field.allowed_version_operators
        supports_or_relation = known_field.supports_or_relation
    else:
        version_operators = frozenset()
        supports_or_relation = True

    for rel, rel_offset, rel_end_offset in _split_w_spans(
        raw_value_masked_comments, ","
    ):
        seen_relation = False
        for or_rel, offset, end_offset in _split_w_spans(rel, "|", offset=rel_offset):
            if or_rel.isspace():
                continue
            if seen_relation and not supports_or_relation:
                separator_range_te = TERange(
                    _text_to_te_position(raw_value_masked_comments[: offset - 1]),
                    _text_to_te_position(raw_value_masked_comments[:offset]),
                ).relative_to(value_element_pos)
                separator_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    te_range_to_lsp(separator_range_te),
                )
                yield Diagnostic(
                    lint_state.position_codec.range_to_client_units(
                        lint_state.lines,
                        separator_range,
                    ),
                    f'The field {known_field.name} does not support "|" (OR) in relations.',
                    DiagnosticSeverity.Error,
                    source="debputy",
                )
            seen_relation = True
            m = _DEP_RELATION_CLAUSE.fullmatch(or_rel)

            if m is not None:
                garbage = m.group("garbage")
                version_operator = m.group("operator")
                if (
                    version_operators
                    and version_operator is not None
                    and version_operator not in version_operators
                ):
                    operator_span = m.span("operator")
                    v_start_offset = offset + operator_span[0]
                    v_end_offset = offset + operator_span[1]
                    version_problem_range_te = TERange(
                        _text_to_te_position(
                            raw_value_masked_comments[:v_start_offset]
                        ),
                        _text_to_te_position(raw_value_masked_comments[:v_end_offset]),
                    ).relative_to(value_element_pos)

                    version_problem_range = (
                        lint_state.position_codec.range_to_client_units(
                            lint_state.lines,
                            te_range_to_lsp(version_problem_range_te),
                        )
                    )
                    sorted_version_operators = sorted(version_operators)
                    yield Diagnostic(
                        lint_state.position_codec.range_to_client_units(
                            lint_state.lines,
                            version_problem_range,
                        ),
                        f'The version operator "{version_operator}" is not allowed in {known_field.name}',
                        DiagnosticSeverity.Error,
                        source="debputy",
                        data=DiagnosticData(
                            quickfixes=[
                                propose_correct_text_quick_fix(n)
                                for n in sorted_version_operators
                            ]
                        ),
                    )
            else:
                garbage = None

            if m is not None and not garbage:
                continue
            if m is not None:
                garbage_span = m.span("garbage")
                garbage_start, garbage_end = garbage_span
                error_start_offset = offset + garbage_start
                error_end_offset = offset + garbage_end
                garbage_part = raw_value_masked_comments[
                    error_start_offset:error_end_offset
                ]
            else:
                garbage_part = None
                error_start_offset = offset
                error_end_offset = end_offset

            problem_range_te = TERange(
                _text_to_te_position(raw_value_masked_comments[:error_start_offset]),
                _text_to_te_position(raw_value_masked_comments[:error_end_offset]),
            ).relative_to(value_element_pos)

            problem_range = lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                te_range_to_lsp(problem_range_te),
            )
            if garbage_part is not None:
                if _DEP_RELATION_CLAUSE.fullmatch(garbage_part) is not None:
                    msg = (
                        "Trailing data after a relationship that might be a second relationship."
                        " Is a separator missing before this part?"
                    )
                else:
                    msg = "Parse error of the relationship. Either a syntax error or a missing separator somewhere."
                yield Diagnostic(
                    lint_state.position_codec.range_to_client_units(
                        lint_state.lines,
                        problem_range,
                    ),
                    msg,
                    DiagnosticSeverity.Error,
                    source="debputy",
                )
            else:
                dep = _cleanup_rel(
                    raw_value_masked_comments[error_start_offset:error_end_offset]
                )
                yield Diagnostic(
                    lint_state.position_codec.range_to_client_units(
                        lint_state.lines,
                        problem_range,
                    ),
                    f'Could not parse "{dep}" as a dependency relation.',
                    DiagnosticSeverity.Error,
                    source="debputy",
                )


def _rrr_build_driver_mismatch(
    _known_field: "F",
    _deb822_file: Deb822FileElement,
    _kvpair: Deb822KeyValuePairElement,
    kvpair_range_te: "TERange",
    _field_name_range: "TERange",
    stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    dr = stanza.get("Build-Driver", "debian-rules")
    if dr != "debian-rules":
        yield Diagnostic(
            lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                te_range_to_lsp(kvpair_range_te),
            ),
            f"The Rules-Requires-Root field is irrelevant for the `Build-Driver` `{dr}`.",
            DiagnosticSeverity.Warning,
            source="debputy",
            data=DiagnosticData(
                quickfixes=[
                    propose_remove_range_quick_fix(
                        proposed_title="Remove Rules-Requires-Root"
                    )
                ]
            ),
        )


class Dep5Matcher(BasenameGlobMatch):
    def __init__(self, basename_glob: str) -> None:
        super().__init__(
            basename_glob,
            only_when_in_directory=None,
            path_type=None,
            recursive_match=False,
        )


def _match_dep5_segment(
    current_dir: VirtualPathBase, basename_glob: str
) -> Iterable[VirtualPathBase]:
    if "*" in basename_glob or "?" in basename_glob:
        return Dep5Matcher(basename_glob).finditer(current_dir)
    else:
        res = current_dir.get(basename_glob)
        if res is None:
            return tuple()
        return (res,)


_RE_SLASHES = re.compile(r"//+")


def _dep5_unnecessary_symbols(
    value: str,
    value_range: TERange,
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    slash_check_index = 0
    if value.startswith(("./", "/")):
        prefix_len = 1 if value[0] == "/" else 2
        if value[prefix_len - 1 : prefix_len + 2].startswith("//"):
            _, slashes_end = _RE_SLASHES.search(value).span()
            prefix_len = slashes_end

        slash_check_index = prefix_len
        prefix_range = te_range_to_lsp(
            TERange(
                value_range.start_pos,
                TEPosition(
                    value_range.start_pos.line_position,
                    value_range.start_pos.cursor_position + prefix_len,
                ),
            )
        )
        yield Diagnostic(
            lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                prefix_range,
            ),
            f'Unnecessary prefix "{value[0:prefix_len]}"',
            DiagnosticSeverity.Warning,
            source="debputy",
            data=DiagnosticData(
                quickfixes=[
                    propose_remove_range_quick_fix(
                        proposed_title=f'Delete "{value[0:prefix_len]}"'
                    )
                ]
            ),
        )

    for m in _RE_SLASHES.finditer(value, slash_check_index):
        m_start, m_end = m.span(0)

        prefix_range = te_range_to_lsp(
            TERange(
                TEPosition(
                    value_range.start_pos.line_position,
                    value_range.start_pos.cursor_position + m_start,
                ),
                TEPosition(
                    value_range.start_pos.line_position,
                    value_range.start_pos.cursor_position + m_end,
                ),
            )
        )
        yield Diagnostic(
            lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                prefix_range,
            ),
            'Simplify to a single "/"',
            DiagnosticSeverity.Warning,
            source="debputy",
            data=DiagnosticData(quickfixes=[propose_correct_text_quick_fix("/")]),
        )


def _dep5_files_check(
    known_field: "F",
    _deb822_file: Deb822FileElement,
    kvpair: Deb822KeyValuePairElement,
    kvpair_range_te: "TERange",
    _field_name_range: "TERange",
    _stanza: Deb822ParagraphElement,
    _stanza_position: "TEPosition",
    lint_state: LintState,
) -> Iterable[Diagnostic]:
    interpreter = known_field.field_value_class.interpreter()
    assert interpreter is not None
    full_value_range = kvpair.value_element.range_in_parent().relative_to(
        kvpair_range_te.start_pos
    )
    values_with_ranges = []
    for value_ref in kvpair.interpret_as(interpreter).iter_value_references():
        value_range = value_ref.locatable.range_in_parent().relative_to(
            full_value_range.start_pos
        )
        value = value_ref.value
        values_with_ranges.append((value_ref.value, value_range))
        yield from _dep5_unnecessary_symbols(value, value_range, lint_state)

    source_root = lint_state.source_root
    if source_root is None:
        return
    i = 0
    limit = len(values_with_ranges)
    while i < limit:
        value, value_range = values_with_ranges[i]
        i += 1


def _combined_custom_field_check(*checks: CustomFieldCheck) -> CustomFieldCheck:
    def _validator(
        known_field: "F",
        deb822_file: Deb822FileElement,
        kvpair: Deb822KeyValuePairElement,
        kvpair_range_te: "TERange",
        field_name_range_te: "TERange",
        stanza: Deb822ParagraphElement,
        stanza_position: "TEPosition",
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        for check in checks:
            yield from check(
                known_field,
                deb822_file,
                kvpair,
                kvpair_range_te,
                field_name_range_te,
                stanza,
                stanza_position,
                lint_state,
            )

    return _validator


@dataclasses.dataclass(slots=True, frozen=True)
class PackageNameSectionRule:
    section: str
    check: Callable[[str], bool]


def _package_name_section_rule(
    section: str,
    check: Union[Callable[[str], bool], re.Pattern],
    *,
    confirm_re: Optional[re.Pattern] = None,
) -> PackageNameSectionRule:
    if confirm_re is not None:
        assert callable(check)

        def _impl(v: str) -> bool:
            return check(v) and confirm_re.search(v)

    elif isinstance(check, re.Pattern):

        def _impl(v: str) -> bool:
            return check.search(v) is not None

    else:
        _impl = check

    return PackageNameSectionRule(section, _impl)


# rules: order is important (first match wins in case of a conflict)
_PKGNAME_VS_SECTION_RULES = [
    _package_name_section_rule("debian-installer", lambda n: n.endswith("-udeb")),
    _package_name_section_rule("doc", lambda n: n.endswith(("-doc", "-docs"))),
    _package_name_section_rule("debug", lambda n: n.endswith(("-dbg", "-dbgsym"))),
    _package_name_section_rule(
        "httpd",
        lambda n: n.startswith(("lighttpd-mod", "libapache2-mod-", "libnginx-mod-")),
    ),
    _package_name_section_rule("gnustep", lambda n: n.startswith("gnustep-")),
    _package_name_section_rule(
        "gnustep",
        lambda n: n.endswith(
            (
                ".framework",
                ".framework-common",
                ".tool",
                ".tool-common",
                ".app",
                ".app-common",
            )
        ),
    ),
    _package_name_section_rule("embedded", lambda n: n.startswith("moblin-")),
    _package_name_section_rule("javascript", lambda n: n.startswith("node-")),
    _package_name_section_rule(
        "zope",
        lambda n: n.startswith(("python-zope", "python3-zope", "zope")),
    ),
    _package_name_section_rule(
        "python",
        lambda n: n.startswith(("python-", "python3-")),
    ),
    _package_name_section_rule(
        "gnu-r",
        lambda n: n.startswith(("r-cran-", "r-bioc-", "r-other-")),
    ),
    _package_name_section_rule("editors", lambda n: n.startswith("elpa-")),
    _package_name_section_rule("lisp", lambda n: n.startswith("cl-")),
    _package_name_section_rule(
        "lisp",
        lambda n: "-elisp-" in n or n.endswith("-elisp"),
    ),
    _package_name_section_rule(
        "lisp",
        lambda n: n.startswith("lib") and n.endswith("-guile"),
    ),
    _package_name_section_rule("lisp", lambda n: n.startswith("guile-")),
    _package_name_section_rule("golang", lambda n: n.startswith("golang-")),
    _package_name_section_rule(
        "perl",
        lambda n: n.startswith("lib") and n.endswith("-perl"),
    ),
    _package_name_section_rule(
        "cli-mono",
        lambda n: n.startswith("lib") and n.endswith(("-cil", "-cil-dev")),
    ),
    _package_name_section_rule(
        "java",
        lambda n: n.startswith("lib") and n.endswith(("-java", "-gcj", "-jni")),
    ),
    _package_name_section_rule(
        "php",
        lambda n: n.startswith(("libphp", "php")),
        confirm_re=re.compile(r"^(?:lib)?php(?:\d(?:\.\d)?)?-"),
    ),
    _package_name_section_rule(
        "php", lambda n: n.startswith("lib-") and n.endswith("-php")
    ),
    _package_name_section_rule(
        "haskell",
        lambda n: n.startswith(("haskell-", "libhugs-", "libghc-", "libghc6-")),
    ),
    _package_name_section_rule(
        "ruby",
        lambda n: "-ruby" in n,
        confirm_re=re.compile(r"^lib.*-ruby(?:1\.\d)?$"),
    ),
    _package_name_section_rule("ruby", lambda n: n.startswith("ruby-")),
    _package_name_section_rule(
        "rust",
        lambda n: n.startswith("librust-") and n.endswith("-dev"),
    ),
    _package_name_section_rule("rust", lambda n: n.startswith("rust-")),
    _package_name_section_rule(
        "ocaml",
        lambda n: n.startswith("lib-") and n.endswith(("-ocaml-dev", "-camlp4-dev")),
    ),
    _package_name_section_rule("javascript", lambda n: n.startswith("libjs-")),
    _package_name_section_rule(
        "interpreters",
        lambda n: n.startswith("lib-") and n.endswith(("-tcl", "-lua", "-gst")),
    ),
    _package_name_section_rule(
        "introspection",
        lambda n: n.startswith("gir-"),
        confirm_re=re.compile(r"^gir\d+\.\d+-.*-\d+\.\d+$"),
    ),
    _package_name_section_rule(
        "fonts",
        lambda n: n.startswith(("xfonts-", "fonts-", "ttf-")),
    ),
    _package_name_section_rule("admin", lambda n: n.startswith(("libnss-", "libpam-"))),
    _package_name_section_rule(
        "localization",
        lambda n: n.startswith(
            (
                "aspell-",
                "hunspell-",
                "myspell-",
                "mythes-",
                "dict-freedict-",
                "gcompris-sound-",
            )
        ),
    ),
    _package_name_section_rule(
        "localization",
        lambda n: n.startswith("hyphen-"),
        confirm_re=re.compile(r"^hyphen-[a-z]{2}(?:-[a-z]{2})?$"),
    ),
    _package_name_section_rule(
        "localization",
        lambda n: "-l10n-" in n or n.endswith("-l10n"),
    ),
    _package_name_section_rule("kernel", lambda n: n.endswith(("-dkms", "-firmware"))),
    _package_name_section_rule(
        "libdevel",
        lambda n: n.startswith("lib") and n.endswith(("-dev", "-headers")),
    ),
    _package_name_section_rule(
        "libs",
        lambda n: n.startswith("lib"),
        confirm_re=re.compile(r"^lib.*\d[ad]?$"),
    ),
]


# Fiddling with the package name can cause a lot of changes (diagnostic scans), so we have an upper bound
# on the cache. The number is currently just taken out of a hat.
@functools.lru_cache(64)
def package_name_to_section(name: str) -> Optional[str]:
    for rule in _PKGNAME_VS_SECTION_RULES:
        if rule.check(name):
            return rule.section
    return None


class FieldValueClass(Enum):
    SINGLE_VALUE = auto(), LIST_SPACE_SEPARATED_INTERPRETATION
    SPACE_SEPARATED_LIST = auto(), LIST_SPACE_SEPARATED_INTERPRETATION
    BUILD_PROFILES_LIST = auto(), None  # TODO
    COMMA_SEPARATED_LIST = auto(), LIST_COMMA_SEPARATED_INTERPRETATION
    COMMA_SEPARATED_EMAIL_LIST = auto(), LIST_UPLOADERS_INTERPRETATION
    COMMA_OR_SPACE_SEPARATED_LIST = auto(), LIST_COMMA_OR_SPACE_SEPARATED_INTERPRETATION
    FREE_TEXT_FIELD = auto(), None
    DEP5_FILE_LIST = auto(), LIST_SPACE_SEPARATED_INTERPRETATION

    def interpreter(self) -> Optional[Interpretation[Deb822ParsedTokenList[Any, Any]]]:
        return self.value[1]


def _unknown_value_check(
    field_name: str,
    value: str,
    known_values: Mapping[str, Keyword],
    unknown_value_severity: Optional[DiagnosticSeverity],
) -> Tuple[
    Optional[Keyword], Optional[str], Optional[DiagnosticSeverity], Optional[Any]
]:
    known_value = known_values.get(value)
    message = None
    severity = unknown_value_severity
    fix_data = None
    if known_value is None:
        candidates = detect_possible_typo(
            value,
            known_values,
        )
        if len(known_values) < 5:
            values = ", ".join(sorted(known_values))
            hint_text = f" Known values for this field: {values}"
        else:
            hint_text = ""
        fix_data = None
        severity = unknown_value_severity
        fix_text = hint_text
        if candidates:
            match = candidates[0]
            if len(candidates) == 1:
                known_value = known_values[match]
            fix_text = (
                f' It is possible that the value is a typo of "{match}".{fix_text}'
            )
            fix_data = [propose_correct_text_quick_fix(m) for m in candidates]
        elif severity is None:
            return None, None, None, None
        if severity is None:
            severity = DiagnosticSeverity.Warning
            # It always has leading whitespace
            message = fix_text.strip()
        else:
            message = f'The value "{value}" is not supported in {field_name}.{fix_text}'
    return known_value, message, severity, fix_data


def _dep5_escape_path(path: str) -> str:
    return path.replace(" ", "?")


def _noop_escape_path(path: str) -> str:
    return path


def _should_ignore_dir(
    path: VirtualPath,
    *,
    supports_dir_match: bool = False,
    match_non_persistent_paths: bool = False,
) -> bool:
    if not supports_dir_match and not any(path.iterdir):
        return True
    cachedir_tag = path.get("CACHEDIR.TAG")
    if (
        not match_non_persistent_paths
        and cachedir_tag is not None
        and cachedir_tag.is_file
    ):
        # https://bford.info/cachedir/
        with cachedir_tag.open(byte_io=True, buffering=64) as fd:
            start = fd.read(43)
        if start == b"Signature: 8a477f597d28d172789f06886806bc55":
            return True
    return False


@dataclasses.dataclass(slots=True, frozen=True)
class Deb822KnownField:
    name: str
    field_value_class: FieldValueClass
    warn_if_default: bool = True
    replaced_by: Optional[str] = None
    deprecated_with_no_replacement: bool = False
    missing_field_severity: Optional[DiagnosticSeverity] = None
    default_value: Optional[str] = None
    known_values: Optional[Mapping[str, Keyword]] = None
    unknown_value_diagnostic_severity: Optional[DiagnosticSeverity] = (
        DiagnosticSeverity.Error
    )
    # One-line description for space-constrained docs (such as completion docs)
    synopsis_doc: Optional[str] = None
    usage_hint: Optional[UsageHint] = None
    hover_text: Optional[str] = None
    spellcheck_value: bool = False
    is_stanza_name: bool = False
    is_single_value_field: bool = True
    custom_field_check: Optional[CustomFieldCheck] = None
    can_complete_field_in_stanza: Optional[
        Callable[[Iterable[Deb822ParagraphElement]], bool]
    ] = None

    def _can_complete_field_in_stanza(
        self,
        stanza_parts: Sequence[Deb822ParagraphElement],
    ) -> bool:
        return (
            self.can_complete_field_in_stanza is None
            or self.can_complete_field_in_stanza(stanza_parts)
        )

    def complete_field(
        self,
        lint_state: LintState,
        stanza_parts: Sequence[Deb822ParagraphElement],
        markdown_kind: MarkupKind,
    ) -> Optional[CompletionItem]:
        if not self._can_complete_field_in_stanza(stanza_parts):
            return None
        name = self.name
        complete_as = name + ": "
        options = self.value_options_for_completer(
            lint_state,
            stanza_parts,
            "",
            markdown_kind,
            is_completion_for_field=True,
        )
        if options is not None and len(options) == 1:
            value = options[0].insert_text
            if value is not None:
                complete_as += value
        tags = []
        is_deprecated = False
        if self.replaced_by or self.deprecated_with_no_replacement:
            is_deprecated = True
            tags.append(CompletionItemTag.Deprecated)

        doc = self.hover_text
        if doc:
            doc = MarkupContent(
                value=doc,
                kind=markdown_kind,
            )
        else:
            doc = None

        return CompletionItem(
            name,
            insert_text=complete_as,
            deprecated=is_deprecated,
            tags=tags,
            detail=format_comp_item_synopsis_doc(
                self.usage_hint,
                self.synopsis_doc,
                is_deprecated,
            ),
            documentation=doc,
        )

    def _complete_files(
        self,
        base_dir: Optional[VirtualPathBase],
        value_being_completed: str,
        *,
        is_dep5_file_list: bool = False,
        supports_dir_match: bool = False,
        supports_spaces_in_filename: bool = False,
        match_non_persistent_paths: bool = False,
    ) -> Optional[Sequence[CompletionItem]]:
        _info(f"_complete_files: {base_dir.fs_path} - {value_being_completed!r}")
        if base_dir is None or not base_dir.is_dir:
            return None

        if is_dep5_file_list:
            supports_spaces_in_filename = True
            supports_dir_match = False
            match_non_persistent_paths = False

        if value_being_completed == "":
            current_dir = base_dir
            unmatched_parts: Sequence[str] = ()
        else:
            current_dir, unmatched_parts = base_dir.attempt_lookup(
                value_being_completed
            )

        if len(unmatched_parts) > 1:
            # Unknown directory part / glob, and we currently do not deal with that.
            return None
        if len(unmatched_parts) == 1 and unmatched_parts[0] == "*":
            # Avoid convincing the client to remove the star (seen with emacs)
            return None
        items = []

        path_escaper = _dep5_escape_path if is_dep5_file_list else _noop_escape_path

        for child in current_dir.iterdir:
            if child.is_symlink and is_dep5_file_list:
                continue
            if not supports_spaces_in_filename and (
                " " in child.name or "\t" in child.name
            ):
                continue
            sort_text = (
                f"z-{child.name}" if child.name.startswith(".") else f"a-{child.name}"
            )
            if child.is_dir:
                if _should_ignore_dir(
                    child,
                    supports_dir_match=supports_dir_match,
                    match_non_persistent_paths=match_non_persistent_paths,
                ):
                    continue
                items.append(
                    CompletionItem(
                        f"{child.path}/",
                        label_details=CompletionItemLabelDetails(
                            description=child.path,
                        ),
                        insert_text=path_escaper(f"{child.path}/"),
                        filter_text=f"{child.path}/",
                        sort_text=sort_text,
                        kind=CompletionItemKind.Folder,
                    )
                )
            else:
                items.append(
                    CompletionItem(
                        child.path,
                        label_details=CompletionItemLabelDetails(
                            description=child.path,
                        ),
                        insert_text=path_escaper(child.path),
                        filter_text=child.path,
                        sort_text=sort_text,
                        kind=CompletionItemKind.File,
                    )
                )
        return items

    def value_options_for_completer(
        self,
        lint_state: LintState,
        stanza_parts: Sequence[Deb822ParagraphElement],
        value_being_completed: str,
        markdown_kind: MarkupKind,
        *,
        is_completion_for_field: bool = False,
    ) -> Optional[Sequence[CompletionItem]]:
        known_values = self.known_values
        if self.field_value_class == FieldValueClass.DEP5_FILE_LIST:
            if is_completion_for_field:
                return None
            return self._complete_files(
                lint_state.source_root,
                value_being_completed,
                is_dep5_file_list=True,
            )

        if known_values is None:
            return None
        if is_completion_for_field and (
            len(known_values) == 1
            or (
                len(known_values) == 2
                and self.warn_if_default
                and self.default_value is not None
            )
        ):
            value = next(
                iter(v for v in self.known_values if v != self.default_value),
                None,
            )
            if value is None:
                return None
            return [CompletionItem(value, insert_text=value)]
        return [
            CompletionItem(
                keyword.value,
                insert_text=keyword.value,
                sort_text=keyword.sort_text,
                detail=format_comp_item_synopsis_doc(
                    keyword.usage_hint,
                    keyword.synopsis_doc,
                    keyword.is_deprecated,
                ),
                deprecated=keyword.is_deprecated,
                tags=[CompletionItemTag.Deprecated] if keyword.is_deprecated else None,
                documentation=(
                    MarkupContent(value=keyword.hover_text, kind=markdown_kind)
                    if keyword.hover_text
                    else None
                ),
            )
            for keyword in known_values.values()
            if keyword.is_keyword_valid_completion_in_stanza(stanza_parts)
        ]

    def field_omitted_diagnostics(
        self,
        deb822_file: Deb822FileElement,
        representation_field_range: Range,
        stanza: Deb822ParagraphElement,
        stanza_position: "TEPosition",
        header_stanza: Optional[Deb822FileElement],
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        missing_field_severity = self.missing_field_severity
        if missing_field_severity is None:
            return

        yield Diagnostic(
            representation_field_range,
            f"Stanza is missing field {self.name}",
            severity=missing_field_severity,
            source="debputy",
        )

    def field_diagnostics(
        self,
        deb822_file: Deb822FileElement,
        kvpair: Deb822KeyValuePairElement,
        stanza: Deb822ParagraphElement,
        stanza_position: "TEPosition",
        kvpair_range_te: "TERange",
        lint_state: LintState,
        *,
        field_name_typo_reported: bool = False,
    ) -> Iterable[Diagnostic]:
        field_name_token = kvpair.field_token
        field_name_range_te = kvpair.field_token.range_in_parent().relative_to(
            kvpair_range_te.start_pos
        )
        yield from self._diagnostics_for_field_name(
            field_name_token,
            field_name_range_te,
            field_name_typo_reported,
            lint_state,
        )
        if self.custom_field_check is not None:
            yield from self.custom_field_check(
                self,
                deb822_file,
                kvpair,
                kvpair_range_te,
                field_name_range_te,
                stanza,
                stanza_position,
                lint_state,
            )
        yield from self._dep5_file_list_diagnostics(
            kvpair, kvpair_range_te.start_pos, lint_state
        )
        if not self.spellcheck_value:
            yield from self._known_value_diagnostics(
                kvpair,
                kvpair_range_te.start_pos,
                lint_state,
            )

    def _diagnostics_for_field_name(
        self,
        token: Deb822FieldNameToken,
        token_range: "TERange",
        typo_detected: bool,
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        field_name = token.text
        # Defeat the case-insensitivity from python-debian
        field_name_cased = str(field_name)
        token_range_server_units = te_range_to_lsp(token_range)
        token_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            token_range_server_units,
        )
        if self.deprecated_with_no_replacement:
            yield Diagnostic(
                token_range,
                f"{field_name_cased} is deprecated and no longer used",
                severity=DiagnosticSeverity.Warning,
                source="debputy",
                tags=[DiagnosticTag.Deprecated],
                data=DiagnosticData(quickfixes=[propose_remove_line_quick_fix()]),
            )
        elif self.replaced_by is not None:
            yield Diagnostic(
                token_range,
                f"{field_name_cased} is a deprecated name for {self.replaced_by}",
                severity=DiagnosticSeverity.Warning,
                source="debputy",
                tags=[DiagnosticTag.Deprecated],
                data=DiagnosticData(
                    quickfixes=[propose_correct_text_quick_fix(self.replaced_by)],
                ),
            )

        if not typo_detected and field_name_cased != self.name:
            yield Diagnostic(
                token_range,
                f"Non-canonical spelling of {self.name}",
                severity=DiagnosticSeverity.Information,
                source="debputy",
                data=DiagnosticData(
                    quickfixes=[propose_correct_text_quick_fix(self.name)]
                ),
            )

    def _dep5_file_list_diagnostics(
        self,
        kvpair: Deb822KeyValuePairElement,
        kvpair_position: "TEPosition",
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        source_root = lint_state.source_root
        if (
            self.field_value_class != FieldValueClass.DEP5_FILE_LIST
            or source_root is None
        ):
            return
        interpreter = self.field_value_class.interpreter()
        values = kvpair.interpret_as(interpreter)
        value_off = kvpair.value_element.position_in_parent().relative_to(
            kvpair_position
        )

        assert interpreter is not None

        for token in values.iter_parts():
            if token.is_whitespace:
                continue
            text = token.convert_to_text()
            if "?" in text or "*" in text:
                # TODO: We should validate these as well
                continue
            matched_path, missing_part = source_root.attempt_lookup(text)
            # It is common practice to delete "dirty" files during clean. This causes files listed
            # in `debian/copyright` to go missing and as a consequence, we do not validate whether
            # they are present (that would require us to check the `.orig.tar`, which we could but
            # do not have the infrastructure for).
            if not missing_part and matched_path.is_dir:
                path_range_te = token.range_in_parent().relative_to(value_off)
                path_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    te_range_to_lsp(path_range_te),
                )
                yield Diagnostic(
                    path_range,
                    "Directories cannot be a match. Use `dir/*` to match everything in it",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        quickfixes=[
                            propose_correct_text_quick_fix(f"{matched_path.path}/*")
                        ]
                    ),
                )

    def _known_value_diagnostics(
        self,
        kvpair: Deb822KeyValuePairElement,
        kvpair_position: "TEPosition",
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        unknown_value_severity = self.unknown_value_diagnostic_severity
        interpreter = self.field_value_class.interpreter()
        if interpreter is None:
            return
        values = kvpair.interpret_as(interpreter)
        value_off = kvpair.value_element.position_in_parent().relative_to(
            kvpair_position
        )

        last_token_non_ws_sep_token: Optional[TE] = None
        for token in values.iter_parts():
            if token.is_whitespace:
                continue
            if not token.is_separator:
                last_token_non_ws_sep_token = None
                continue
            if last_token_non_ws_sep_token is not None:
                sep_range_te = token.range_in_parent().relative_to(value_off)
                value_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    te_range_to_lsp(sep_range_te),
                )
                yield Diagnostic(
                    value_range,
                    "Duplicate separator",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                )
            last_token_non_ws_sep_token = token

        allowed_values = self.known_values
        if not allowed_values:
            return

        first_value = None
        first_exclusive_value_ref = None
        first_exclusive_value = None
        has_emitted_for_exclusive = False

        for value_ref in values.iter_value_references():
            value = value_ref.value
            if (
                first_value is not None
                and self.field_value_class == FieldValueClass.SINGLE_VALUE
            ):
                value_loc = value_ref.locatable
                range_position_te = value_loc.range_in_parent().relative_to(value_off)
                value_range_in_server_units = te_range_to_lsp(range_position_te)
                value_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    value_range_in_server_units,
                )
                yield Diagnostic(
                    value_range,
                    f"The field {self.name} can only have exactly one value.",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                )
                # TODO: Add quickfix if the value is also invalid
                continue

            if first_exclusive_value_ref is not None and not has_emitted_for_exclusive:
                assert first_exclusive_value is not None
                value_loc = first_exclusive_value_ref.locatable
                value_range_te = value_loc.range_in_parent().relative_to(value_off)
                value_range_in_server_units = te_range_to_lsp(value_range_te)
                value_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    value_range_in_server_units,
                )
                yield Diagnostic(
                    value_range,
                    f'The value "{first_exclusive_value}" cannot be used with other values.',
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                )

            known_value, unknown_value_message, unknown_severity, typo_fix_data = (
                _unknown_value_check(
                    self.name,
                    value,
                    self.known_values,
                    unknown_value_severity,
                )
            )

            issues = []

            if known_value and known_value.is_exclusive:
                first_exclusive_value = known_value.value  # In case of typos.
                first_exclusive_value_ref = value_ref
                if first_value is not None:
                    has_emitted_for_exclusive = True
                    issues.append(
                        {
                            "message": f'The value "{known_value.value}" cannot be used with other values.',
                            "severity": DiagnosticSeverity.Error,
                            "source": "debputy",
                        }
                    )

            if first_value is None:
                first_value = value

            if unknown_value_message is not None:
                assert unknown_severity is not None
                issues.append(
                    {
                        "message": unknown_value_message,
                        "severity": unknown_severity,
                        "source": "debputy",
                        "data": DiagnosticData(quickfixes=typo_fix_data),
                    }
                )

            if known_value is not None and known_value.is_obsolete:
                replacement = known_value.replaced_by
                if replacement is not None:
                    obsolete_value_message = (
                        f'The value "{value}" has been replaced by {replacement}'
                    )
                    obsolete_severity = DiagnosticSeverity.Warning
                    obsolete_fix_data = [propose_correct_text_quick_fix(replacement)]
                else:
                    obsolete_value_message = (
                        f'The value "{value}" is obsolete without a single replacement'
                    )
                    obsolete_severity = DiagnosticSeverity.Warning
                    obsolete_fix_data = None
                issues.append(
                    {
                        "message": obsolete_value_message,
                        "severity": obsolete_severity,
                        "source": "debputy",
                        "data": DiagnosticData(quickfixes=obsolete_fix_data),
                    }
                )

            if not issues:
                continue

            value_loc = value_ref.locatable
            value_range_te = value_loc.range_in_parent().relative_to(value_off)
            value_range_in_server_units = te_range_to_lsp(value_range_te)
            value_range = lint_state.position_codec.range_to_client_units(
                lint_state.lines,
                value_range_in_server_units,
            )
            yield from (Diagnostic(value_range, **issue_data) for issue_data in issues)

    def reformat_field(
        self,
        effective_preference: "EffectiveFormattingPreference",
        stanza_range: TERange,
        kvpair: Deb822KeyValuePairElement,
        formatter: FormatterCallback,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        kvpair_range = kvpair.range_in_parent().relative_to(stanza_range.start_pos)
        return trim_end_of_line_whitespace(
            position_codec,
            lines,
            line_range=range(
                kvpair_range.start_pos.line_position,
                kvpair_range.end_pos.line_position,
            ),
        )


@dataclasses.dataclass(slots=True, frozen=True)
class DctrlLikeKnownField(Deb822KnownField):

    def reformat_field(
        self,
        effective_preference: "EffectiveFormattingPreference",
        stanza_range: TERange,
        kvpair: Deb822KeyValuePairElement,
        formatter: FormatterCallback,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        interpretation = self.field_value_class.interpreter()
        if (
            not effective_preference.deb822_normalize_field_content
            or interpretation is None
        ):
            yield from super(DctrlLikeKnownField, self).reformat_field(
                effective_preference,
                stanza_range,
                kvpair,
                formatter,
                position_codec,
                lines,
            )
            return
        if not self.reformattable_field:
            yield from super(DctrlLikeKnownField, self).reformat_field(
                effective_preference,
                stanza_range,
                kvpair,
                formatter,
                position_codec,
                lines,
            )
            return
        seen: Set[str] = set()
        old_kvpair_range = kvpair.range_in_parent()
        sort = self.is_sortable_field

        # Avoid the context manager as we do not want to perform the change (it would contaminate future ranges)
        field_content = kvpair.interpret_as(interpretation)
        old_value = field_content.convert_to_text(with_field_name=True)
        for package_ref in field_content.iter_value_references():
            value = package_ref.value
            if self.is_relationship_field:
                new_value = " | ".join(x.strip() for x in value.split("|"))
            else:
                new_value = value
            if not sort or new_value not in seen:
                if new_value != value:
                    package_ref.value = new_value
                seen.add(new_value)
            else:
                package_ref.remove()
            if sort:
                field_content.sort(key=_sort_packages_key)
            field_content.value_formatter(formatter)
            field_content.reformat_when_finished()

        new_value = field_content.convert_to_text(with_field_name=True)
        if new_value != old_value:
            range_server_units = te_range_to_lsp(
                old_kvpair_range.relative_to(stanza_range.start_pos)
            )
            yield TextEdit(
                position_codec.range_to_client_units(lines, range_server_units),
                new_value,
            )

    @property
    def reformattable_field(self) -> bool:
        return self.is_relationship_field or self.is_sortable_field

    @property
    def is_relationship_field(self) -> bool:
        return False

    @property
    def is_sortable_field(self) -> bool:
        return self.is_relationship_field


@dataclasses.dataclass(slots=True, frozen=True)
class DTestsCtrlKnownField(DctrlLikeKnownField):
    @property
    def is_relationship_field(self) -> bool:
        return self.name == "Depends"

    @property
    def is_sortable_field(self) -> bool:
        return self.is_relationship_field or self.name in (
            "Features",
            "Restrictions",
            "Tests",
        )


@dataclasses.dataclass(slots=True, frozen=True)
class DctrlKnownField(DctrlLikeKnownField):
    inherits_from_source: bool = False

    def field_omitted_diagnostics(
        self,
        deb822_file: Deb822FileElement,
        representation_field_range: Range,
        stanza: Deb822ParagraphElement,
        stanza_position: "TEPosition",
        header_stanza: Optional[Deb822FileElement],
        lint_state: LintState,
    ) -> Iterable[Diagnostic]:
        missing_field_severity = self.missing_field_severity
        if missing_field_severity is None:
            return

        if (
            self.inherits_from_source
            and header_stanza is not None
            and self.name in header_stanza
        ):
            return

        if self.name == "Standards-Version":
            stanzas = list(deb822_file)[1:]
            if all(s.get("Package-Type") == "udeb" for s in stanzas):
                return

        yield Diagnostic(
            representation_field_range,
            f"Stanza is missing field {self.name}",
            severity=missing_field_severity,
            source="debputy",
        )

    def reformat_field(
        self,
        effective_preference: "EffectiveFormattingPreference",
        stanza_range: TERange,
        kvpair: Deb822KeyValuePairElement,
        formatter: FormatterCallback,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        if (
            self.name == "Architecture"
            and effective_preference.deb822_normalize_field_content
        ):
            interpretation = self.field_value_class.interpreter()
            assert interpretation is not None
            archs = list(kvpair.interpret_as(interpretation))
            # Sort, with wildcard entries (such as linux-any) first:
            archs = sorted(archs, key=lambda x: ("any" not in x, x))
            new_value = f"{kvpair.field_name}: {' '.join(archs)}\n"
            if new_value != kvpair.convert_to_text():
                kvpair_range = te_range_to_lsp(
                    kvpair.range_in_parent().relative_to(stanza_range.start_pos)
                )
                return [
                    TextEdit(
                        position_codec.range_to_client_units(lines, kvpair_range),
                        new_value,
                    )
                ]
            return tuple()

        return super(DctrlKnownField, self).reformat_field(
            effective_preference,
            stanza_range,
            kvpair,
            formatter,
            position_codec,
            lines,
        )

    @property
    def is_relationship_field(self) -> bool:
        name_lc = self.name.lower()
        return (
            name_lc in all_package_relationship_fields()
            or name_lc in all_source_relationship_fields()
        )

    @property
    def reformattable_field(self) -> bool:
        return self.is_relationship_field or self.name == "Uploaders"


@dataclasses.dataclass(slots=True, frozen=True)
class DctrlRelationshipKnownField(DctrlKnownField):
    allowed_version_operators: FrozenSet[str] = frozenset()
    supports_or_relation: bool = True

    @property
    def is_relationship_field(self) -> bool:
        return True


SOURCE_FIELDS = _fields(
    DctrlKnownField(
        "Source",
        FieldValueClass.SINGLE_VALUE,
        custom_field_check=_each_value_match_regex_validation(PKGNAME_REGEX),
        missing_field_severity=DiagnosticSeverity.Error,
        is_stanza_name=True,
        synopsis_doc="Name of source package",
        hover_text=textwrap.dedent(
            """\
            Declares the name of the source package.

            Note this must match the name in the first entry of `debian/changelog` file.
            """
        ),
    ),
    DctrlKnownField(
        "Standards-Version",
        FieldValueClass.SINGLE_VALUE,
        # Conditionally mandatory (special-cased by field_omitted_diagnostics)
        missing_field_severity=DiagnosticSeverity.Error,
        custom_field_check=_sv_field_validation,
        synopsis_doc="Debian Policy version this package complies with",
        hover_text=textwrap.dedent(
            """\
                  Declares the last semantic version of the Debian Policy this package as last checked against.

                  **Example**:
                  ```
                  Standards-Version: 4.5.2
                  ```

                  Note that the last version part of the full Policy version (the **.X** in 4.5.2**.X**) is
                  typically omitted as it is used solely for editorial changes to the policy (e.g. typo fixes).
            """
        ),
    ),
    DctrlKnownField(
        "Section",
        FieldValueClass.SINGLE_VALUE,
        known_values=ALL_SECTIONS,
        unknown_value_diagnostic_severity=DiagnosticSeverity.Warning,
        synopsis_doc="Default section",
        hover_text=textwrap.dedent(
            """\
                Define the default section for packages in this source package.

                **Example**:
                ```
                Section: devel
                ```

                Please see <https://packages.debian.org/unstable> for more details about the sections.
            """
        ),
    ),
    DctrlKnownField(
        "Priority",
        FieldValueClass.SINGLE_VALUE,
        default_value="optional",
        warn_if_default=False,
        known_values=ALL_PRIORITIES,
        synopsis_doc="Default priority",
        hover_text=textwrap.dedent(
            """\
                    Define the default priority for packages in this source package.

                    The priority field describes how important the package is for the functionality of the system.

                    **Example**:
                    ```
                    Priority: optional
                    ```

                    Unless you know you need a different value, you should choose **optional** for your packages.
                """
        ),
    ),
    DctrlKnownField(
        "Maintainer",
        FieldValueClass.SINGLE_VALUE,
        missing_field_severity=DiagnosticSeverity.Error,
        synopsis_doc="Name and email of maintainer / maintenance team",
        hover_text=textwrap.dedent(
            """\
                  The maintainer of the package.

                  **Example**:
                  ```
                  Maintainer: Jane Contributor <jane@janes.email-provider.org>
                  ```

                  Note: If a person is listed in the Maintainer field, they should *not* be listed in Uploaders field.
            """
        ),
    ),
    DctrlKnownField(
        "Uploaders",
        FieldValueClass.COMMA_SEPARATED_EMAIL_LIST,
        synopsis_doc="Names and emails of co-maintainers",
        hover_text=textwrap.dedent(
            """\
                  Comma separated list of uploaders associated with the package.

                  **Example**:
                  ```
                  Uploaders:
                   John Doe <john@doe.org>,
                   Lisbeth Worker <lis@worker.org>,
                  ```

                  Formally uploaders are considered co-maintainers for the package with the party listed in the
                  **Maintainer** field being the primary maintainer. In practice, each maintainer or maintenance
                  team can have their own ruleset about the difference between the **Maintainer** and the
                  **Uploaders**. As an example, the Python packaging team has a different rule set for how to
                  react to a package depending on whether the packaging team is the **Maintainer** or in the
                  **Uploaders** field.

                  Note: If a person is listed in the Maintainer field, they should *not* be listed in Uploaders field.
            """
        ),
    ),
    DctrlKnownField(
        "Build-Driver",
        FieldValueClass.SINGLE_VALUE,
        default_value="debian-rules",
        known_values=allowed_values(
            Keyword(
                "debian-rules",
                synopsis_doc="Build via `debian/rules`",
                hover_text=textwrap.dedent(
                    """\
                    Use the `debian/rules` interface for building packages.

                    This is the historical default and the interface that Debian Packages have used for
                    decades to build debs.
            """
                ),
            ),
            Keyword(
                "debputy",
                synopsis_doc="Build with `debputy`",
                hover_text=textwrap.dedent(
                    """\
                    Use the `debputy` interface for building the package.

                    This is provides the "full" integration mode with `debputy` where all parts of the
                    package build is handled by `debputy`.

                    This *may* make any `debhelper` build-dependency redundant depending on which build
                    system is used. Some build systems (such as `autoconf` still use `debhelper` based tools).
            """
                ),
            ),
        ),
        synopsis_doc="Which implementation dpkg should use for the build",
        hover_text=textwrap.dedent(
            """\
                The name of the build driver that dpkg (`dpkg-buildpackage`) will use for assembling the
                package.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Browser",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="URL for browsers to interact with packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the Version control system repo used for the packaging. The URL should be usable with a
                browser *without* requiring any login.

                This should be used together with one of the other **Vcs-** fields.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Git",
        FieldValueClass.SPACE_SEPARATED_LIST,
        synopsis_doc="URL and options for cloning the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable with `git clone`
                *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.

                Note it is possible to specify a branch via the `-b` option.

                ```
                Vcs-Git: https://salsa.debian.org/some/packaging-repo -b debian/unstable
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Svn",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable with `svn checkout`
                *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Arch",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable for getting a copy of the
                sources *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Cvs",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable for getting a copy of the
                sources *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Darcs",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable for getting a copy of the
                sources *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Hg",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable for getting a copy of the
                sources *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
            """
        ),
    ),
    DctrlKnownField(
        "Vcs-Mtn",
        FieldValueClass.SPACE_SEPARATED_LIST,  # TODO: Might be a single value
        synopsis_doc="URL for checking out the packaging VCS",
        hover_text=textwrap.dedent(
            """\
                URL to the git repo used for the packaging. The URL should be usable for getting a copy of the
                sources *without* requiring any login.

                This should be used together with the **Vcs-Browser** field provided there is a web UI for the repo.
            """
        ),
    ),
    DctrlKnownField(
        "DM-Upload-Allowed",
        FieldValueClass.SINGLE_VALUE,
        deprecated_with_no_replacement=True,
        default_value="no",
        known_values=allowed_values("yes", "no"),
        synopsis_doc="Old ACL mechanism for Debian Maintainers",
        hover_text=textwrap.dedent(
            """\
                Obsolete field

                It was used to enabling Debian Maintainers to upload the package without requiring a Debian Developer
                to sign the package. This mechanism has been replaced by a new authorization mechanism.

                Please see <https://lists.debian.org/debian-devel-announce/2012/09/msg00008.html> for details about the
                replacement.
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Build-Depends",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Dependencies requires for clean and full build actions",
        hover_text=textwrap.dedent(
            """\
                   All minimum build-dependencies for this source package. Needed for any target including **clean**.
                   """
        ),
    ),
    DctrlKnownField(
        "Build-Depends-Arch",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Dependencies requires for arch:any action (build-arch/binary-arch)",
        hover_text=textwrap.dedent(
            """\
                Build-dependencies required for building the architecture dependent binary packages of this source
                package.

                These build-dependencies must be satisfied when executing the **build-arch** and **binary-arch**
                targets either directly or indirectly in addition to those listed in **Build-Depends**.

                Note that these dependencies are *not* available during **clean**.
       """
        ),
    ),
    DctrlKnownField(
        "Build-Depends-Indep",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Dependencies requires for arch:all action (build-indep/binary-indep)",
        hover_text=textwrap.dedent(
            """\
                Build-dependencies required for building the architecture independent binary packages of this source
                package.

                These build-dependencies must be satisfied when executing the **build-indep** and **binary-indep**
                targets either directly or indirectly in addition to those listed in **Build-Depends**.

                Note that these dependencies are *not* available during **clean**.
       """
        ),
    ),
    DctrlRelationshipKnownField(
        "Build-Conflicts",
        FieldValueClass.COMMA_SEPARATED_LIST,
        supports_or_relation=False,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Package versions that will break the build or the clean target (use sparingly)",
        hover_text=textwrap.dedent(
            """\
                Packages that must **not** be installed during **any** part of the build, including the **clean**
                target **clean**.

                Where possible, it is often better to configure the build so that it does not react to the package
                being present in the first place. Usually this is a question of using a `--without-foo` or
                `--disable-foo` or such to the build configuration.
       """
        ),
    ),
    DctrlRelationshipKnownField(
        "Build-Conflicts-Arch",
        FieldValueClass.COMMA_SEPARATED_LIST,
        supports_or_relation=False,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Package versions that will break an arch:any build (use sparingly)",
        hover_text=textwrap.dedent(
            """\
                Packages that must **not** be installed during the **build-arch** or **binary-arch** targets.
                This also applies when these targets are run implicitly such as via the **binary** target.

                Where possible, it is often better to configure the build so that it does not react to the package
                being present in the first place. Usually this is a question of using a `--without-foo` or
                `--disable-foo` or such to the build configuration.
       """
        ),
    ),
    DctrlRelationshipKnownField(
        "Build-Conflicts-Indep",
        FieldValueClass.COMMA_SEPARATED_LIST,
        supports_or_relation=False,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Package versions that will break an arch:all build (use sparingly)",
        hover_text=textwrap.dedent(
            """\
                Packages that must **not** be installed during the **build-indep** or **binary-indep** targets.
                This also applies when these targets are run implicitly such as via the **binary** target.

                Where possible, it is often better to configure the build so that it does not react to the package
                being present in the first place. Usually this is a question of using a `--without-foo` or
                `--disable-foo` or such to the build configuration.
       """
        ),
    ),
    DctrlKnownField(
        "Testsuite",
        FieldValueClass.SPACE_SEPARATED_LIST,
        synopsis_doc="Announce **autodep8** tests",
        hover_text=textwrap.dedent(
            """\
                Declares that this package provides or should run install time tests via `autopkgtest`.

                This field can be used to request an automatically generated autopkgtests via the **autodep8** package.
                Please refer to the documentation of the **autodep8** package for which values you can put into
                this field and what kind of testsuite the keywords will provide.

                Declaring this field in `debian/control` is only necessary when you want additional tests beyond
                those in `debian/tests/control` as **dpkg** automatically records the package provided ones from
                `debian/tests/control`.
            """
        ),
    ),
    DctrlKnownField(
        "Homepage",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="Upstream homepage",
        hover_text=textwrap.dedent(
            """\
                Link to the upstream homepage for this source package.

                **Example**:
                ```
                Homepage: https://www.janes-tools.org/frob-cleaner
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Rules-Requires-Root",
        FieldValueClass.SPACE_SEPARATED_LIST,
        unknown_value_diagnostic_severity=None,
        custom_field_check=_rrr_build_driver_mismatch,
        known_values=allowed_values(
            Keyword(
                "no",
                is_exclusive=True,
                hover_text=textwrap.dedent(
                    """\
                The build process will not require root or fakeroot during any step.  This enables
                dpkg-buildpackage, debhelper or/and `debputy` to perform several optimizations during the build.

                This is the default with dpkg-build-api at version 1 or later.
        """
                ),
            ),
            Keyword(
                "binary-targets",
                is_exclusive=True,
                hover_text=textwrap.dedent(
                    """\
                    The build process assumes that dpkg-buildpackage will run the relevant binary
                    target with root or fakeroot. This was the historical default behaviour.

                    This is the default with dpkg-build-api at version 0.
        """
                ),
            ),
            Keyword(
                "debputy/deb-assembly",
                hover_text=textwrap.dedent(
                    """\
                    When using `debputy`, `debputy` is expected to use root or fakeroot when assembling
                    a .deb or .udeb, where it is required to use `dpkg-deb`.

                    Note: The `debputy` can always use `no` instead by falling back to an internal
                    assembly method instead for .deb or .udebs that would need root or fakeroot with
                    `dpkg-deb`.
        """
                ),
            ),
        ),
        synopsis_doc="Declare (fake)root requirements for the package",
        hover_text=textwrap.dedent(
            """\
                Declare if and when the package build assumes it is run as root or fakeroot.

                Most packages do not need to run as root or fakeroot and the legacy behaviour comes with a
                performance cost. This field can be used to explicitly declare that the legacy behaviour is
                unnecessary.

                **Example**:
                ```
                Rules-Requires-Root: no
                ```

                Setting this field to `no` *can* cause the package to stop building if it requires root.
                Depending on the situation, it might require some trivial or some complicated changes to fix that.
                If it breaks and you cannot figure out how to fix it, then reset the field to `binary-targets`
                and move on until you have time to fix it.

                The default value for this field depends on the `dpkg-build-api` version. If the package
                ` Build-Depends` on `dpkg-build-api (>= 1)` or later, the default is `no`. Otherwise,
                the default is `binary-target`

                This field is only relevant when when the `Build-Driver` is `debian-rules` (which it is by
                default).

                Note it is **not** possible to require running the package as "true root".
            """
        ),
    ),
    DctrlKnownField(
        "Bugs",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="Custom bugtracker URL (for third-party packages)",
        hover_text=textwrap.dedent(
            """\
            Provide a custom bug tracker URL

            This field is *not* used by packages uploaded to Debian or most derivatives as the distro tooling
            has a default bugtracker built-in. It is primarily useful for third-party provided packages such
            that bug reporting tooling can redirect the user to their bug tracker.
            """
        ),
    ),
    DctrlKnownField(
        "Origin",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="Custom origin (for third-party packages)",
        hover_text=textwrap.dedent(
            """\
            Declare the origin of the package.

            This field is *not* used by packages uploaded to Debian or most derivatives as the origin would
            be the distribution. It is primarily useful for third-party provided packages as some tools will
            detect this field.
            """
        ),
    ),
    DctrlKnownField(
        "X-Python-Version",
        FieldValueClass.COMMA_SEPARATED_LIST,
        replaced_by="X-Python3-Version",
        synopsis_doc="Supported Python2 versions (`dh-python` specific)",
        hover_text=textwrap.dedent(
            """\
            Obsolete field for declaring the supported Python2 versions

            Since Python2 is no longer supported, this field is now redundant. For Python3, the field is
            called **X-Python3-Version**.
            """
        ),
    ),
    DctrlKnownField(
        "X-Python3-Version",
        FieldValueClass.COMMA_SEPARATED_LIST,
        synopsis_doc="Supported Python3 versions (`dh-python` specific)",
        hover_text=textwrap.dedent(
            # Too lazy to provide a better description
            """\
            For declaring the supported Python3 versions

            This is used by the tools from `dh-python` package. Please see the documentation of that package
            for when and how to use it.
            """
        ),
    ),
    DctrlKnownField(
        "XS-Autobuild",
        FieldValueClass.SINGLE_VALUE,
        known_values=allowed_values("yes"),
        synopsis_doc="Whether this non-free is auto-buildable on buildds",
        # TODO: Needs logic for reading source section: can_complete_field_in_stanza=_complete_only_for_non_free_pkgs,
        hover_text=textwrap.dedent(
            """\
            Used for non-free packages to denote that they may be auto-build on the Debian build infrastructure

            Note that adding this field **must** be combined with following the instructions at
            <https://www.debian.org/doc/manuals/developers-reference/pkgs.html#non-free-buildd>
            """
        ),
    ),
    DctrlKnownField(
        "X-Style",
        FieldValueClass.SINGLE_VALUE,
        known_values=ALL_PUBLIC_NAMED_STYLES,
        unknown_value_diagnostic_severity=DiagnosticSeverity.Warning,
        synopsis_doc="Choose a formatting style",
        hover_text=textwrap.dedent(
            """\
        This field is read by `debputy` to determine how it should format the files in the package.

        In its absence, `debputy` will attempt to determine the formatting style by looking at
        the maintainers and built-in style preferences.

        This value influences commands such as `debputy reformat` and `debputy lsp server`. When this
        field is present, it will overrule any built-in style detection that `debputy` would otherwise
        have applied.

        Note that unknown styles will cause the styling to be disabled (and trigger a `debputy lint`
        warning).
        """
        ),
    ),
    DctrlKnownField(
        "XS-Ruby-Versions",
        FieldValueClass.FREE_TEXT_FIELD,
        deprecated_with_no_replacement=True,
        synopsis_doc="Obsolete",
        hover_text=textwrap.dedent(
            """\
        Obsolete according to https://bugs.debian.org/1075762
        """
        ),
    ),
    DctrlKnownField(
        "Description",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        synopsis_doc="Common base description for all packages via substvar",
        hover_text=textwrap.dedent(
            """\
            This field contains a human-readable description of the package. However, it is not used directly.

            Binary packages can reference parts of it via the `${source:Synopsis}` and the
            `${source:Extended-Description}` substvars. Without any of these substvars, the `Description` field
            of the `Source` stanza remains unused.

            The first line immediately after the field is called the *Synopsis* and is a short "noun-phrase"
            intended to provide a one-line summary of a package. The lines after the **Synopsis** is known
            as the **Extended Description** and is intended as a longer summary of a package.

            **Example**:
            ```
            Description: documentation generator for Python projects
              Sphinx is a tool for producing documentation for Python projects, using
              reStructuredText as markup language.
              .
              Sphinx features:
               * HTML, CHM, LaTeX output,
               * Cross-referencing source code,
               * Automatic indices,
               * Code highlighting, using Pygments,
               * Extensibility. Existing extensions:
                 - automatic testing of code snippets,
                 - including docstrings from Python modules.
              .
              Build-depend on sphinx if your package uses /usr/bin/sphinx-*
              executables. Build-depend on python3-sphinx if your package uses
              the Python API (for instance by calling python3 -m sphinx).
            ```

            The **Synopsis** is usually displayed in cases where there is limited space such as when reviewing
            the search results from `apt search foo`.  It is often a good idea to imagine that the **Synopsis**
            part is inserted into a sentence like "The package provides {{Synopsis-goes-here}}". The
            **Extended Description** is a standalone description that should describe what the package does and
            how it relates to the rest of the system (in terms of, for example, which subsystem it is which part of).
            Please see <https://www.debian.org/doc/debian-policy/ch-controlfields.html#description> for more details
            about the description field and suggestions for how to write it.
            """
        ),
    ),
)


BINARY_FIELDS = _fields(
    DctrlKnownField(
        "Package",
        FieldValueClass.SINGLE_VALUE,
        custom_field_check=_each_value_match_regex_validation(PKGNAME_REGEX),
        is_stanza_name=True,
        missing_field_severity=DiagnosticSeverity.Error,
        synopsis_doc="Declares the name of a binary package",
        hover_text="Declares the name of a binary package",
    ),
    DctrlKnownField(
        "Package-Type",
        FieldValueClass.SINGLE_VALUE,
        default_value="deb",
        known_values=allowed_values(
            Keyword("deb", hover_text="The package will be built as a regular deb."),
            Keyword(
                "udeb",
                hover_text="The package will be built as a micro-deb (also known as a udeb).  These are solely used by the debian-installer.",
            ),
        ),
        synopsis_doc="Non-standard package type (such as `udeb`)",
        hover_text=textwrap.dedent(
            """\
                **Special-purpose only**. *This field is a special purpose field and is rarely needed.*
                *You are recommended to omit unless you know you need it or someone told you to use it.*

                Determines the type of package.  This field can be used to declare that a given package is a different
                type of package than usual.  The primary case where this is known to be useful is for building
                micro-debs ("udeb") to be consumed by the debian-installer.
            """
        ),
    ),
    DctrlKnownField(
        "Architecture",
        FieldValueClass.SPACE_SEPARATED_LIST,
        missing_field_severity=DiagnosticSeverity.Error,
        unknown_value_diagnostic_severity=None,
        # FIXME: Specialize validation for architecture ("!foo" is not a "typo" and should have a better warning)
        known_values=allowed_values(*dpkg_arch_and_wildcards()),
        synopsis_doc="Architecture of the package",
        hover_text=textwrap.dedent(
            """\
                Determines which architectures this package can be compiled for or if it is an architecture-independent
                package.  The value is a space-separated list of dpkg architecture names or wildcards.

                **Example**:
                ```
                Package: architecture-specific-package
                Architecture: any
                # ...


                Package: data-only-package
                Architecture: all
                Multi-Arch: foreign
                # ...


                Package: linux-only-package
                Architecture: linux-any
                # ...
                ```

                When in doubt, stick to the values **all** (for scripts, data or documentation, etc.) or **any**
                (for anything that can be compiled).  For official Debian packages, it is often easier to attempt the
                compilation for unsupported architectures than to maintain the list of machine architectures that work.
            """
        ),
    ),
    DctrlKnownField(
        "Essential",
        FieldValueClass.SINGLE_VALUE,
        default_value="no",
        known_values=allowed_values(
            Keyword(
                "yes",
                hover_text="The package is essential and uninstalling it will completely and utterly break the"
                " system beyond repair.",
            ),
            Keyword(
                "no",
                hover_text=textwrap.dedent(
                    """\
                The package is a regular package.  This is the default and recommended.

                Note that declaring a package to be "Essential: no" is the same as not having the field except omitting
                the field wastes fewer bytes on everyone's hard disk.
            """
                ),
            ),
        ),
        synopsis_doc="Whether the package is essential (Policy term)",
        hover_text=textwrap.dedent(
            """\
                **Special-purpose only**. *This field is a special purpose field and is rarely needed.*
                *You are recommended to omit unless you know you need it or someone told you to use it.*

                Whether the package should be considered Essential as defined by Debian Policy.

                Essential packages are subject to several distinct but very important rules:

                 * Essential packages are considered essential for the system to work.  The packaging system
                   (APT and dpkg) will refuse to uninstall it without some very insisting force options and warnings.

                 * Other packages are not required to declare explicit dependencies on essential packages as a
                   side-effect of the above except as to ensure that the given essential package is upgraded
                   to a given minimum version.

                 * Once installed, essential packages must function at all times no matter where dpkg is in its
                   installation or upgrade process. During bootstrapping or installation, this requirement is
                   relaxed.
            """
        ),
    ),
    DctrlKnownField(
        "XB-Important",
        FieldValueClass.SINGLE_VALUE,
        replaced_by="Protected",
        default_value="no",
        synopsis_doc="**Deprecated**: Use Protected instead",
        known_values=allowed_values(
            Keyword(
                "yes",
                hover_text="The package is protected and attempts to uninstall it will cause strong warnings to the"
                " user that they might be breaking the system.",
            ),
            Keyword(
                "no",
                hover_text=textwrap.dedent(
                    """\
                    The package is a regular package.  This is the default and recommended.

                    Note that declaring a package to be `XB-Important: no` is the same as not having the field
                    except omitting the field wastes fewer bytes on everyone's hard-disk.
            """
                ),
            ),
        ),
        hover_text=textwrap.dedent(
            """\
            This is the prototype field that lead to `Protected`, which should be used instead.

            It makes `apt` (but not `dpkg`) require extra confirmation before removing the package.
        """
        ),
    ),
    DctrlKnownField(
        "Protected",
        FieldValueClass.SINGLE_VALUE,
        default_value="no",
        known_values=allowed_values(
            Keyword(
                "yes",
                hover_text="The package is protected and attempts to uninstall it will cause strong warnings to the"
                " user that they might be breaking the system.",
            ),
            Keyword(
                "no",
                hover_text=textwrap.dedent(
                    """\
                    The package is a regular package.  This is the default and recommended.

                    Note that declaring a package to be `Protected: no` is the same as not having the field
                    except omitting the field wastes fewer bytes on everyone's hard-disk.
            """
                ),
            ),
        ),
        synopsis_doc="Mark as protected (uninstall protection)",
        hover_text=textwrap.dedent(
            """\
            Declare this package as a potential system critical package. When set to `yes`, both `apt`
            and `dpkg` will assume that removing the package *may* break the system. As a consequence,
            they will require extra confirmation (or "force" options) before removing the package.

            This field basically provides a "uninstall" protection similar to that of `Essential` packages
            without the other benefits and requirements that comes with `Essential` packages. This option
            is generally applicable to packages like bootloaders, kernels, and other packages that might
            be necessary for booting the system.
        """
        ),
    ),
    DctrlKnownField(
        "Pre-Depends",
        FieldValueClass.COMMA_SEPARATED_LIST,
        synopsis_doc="Very strong dependencies; prefer Depends when applicable",
        hover_text=textwrap.dedent(
            """\
              **Advanced field**. *This field covers an advanced topic.  If you are new to packaging, you are*
              *probably not looking for this field (except to set a **${misc:Pre-Depends}** relation.  Incorrect use*
              *of this field can cause issues - among other causing issues during upgrades that users cannot work*
              *around without passing `--force-*` options to dpkg.*

              This field is like *Depends*, except that is also forces dpkg to complete installation of the packages
              named before even starting the installation of the package which declares the pre-dependency.

              **Example**:
              ```
              Pre-Depends: ${misc:Pre-Depends}
              ```

              Note this is a very strong dependency and not all packages support being a pre-dependency because it
              puts additional requirements on the package being depended on. Use of **${misc:Pre-Depends}** is
              pre-approved and recommended. Essential packages are known to support being in **Pre-Depends**.
              However, careless use of **Pre-Depends** for essential packages can still cause dependency resolvers
              problems.
            """
        ),
    ),
    DctrlKnownField(
        "Depends",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Dependencies required to install and use this package",
        hover_text=textwrap.dedent(
            """\
              Lists the packages that must be installed, before this package is installed.

              **Example**:
              ```
              Package: foo
              Architecture: any
              Depends: ${misc:Depends},
                       ${shlibs:Depends},
                       libfoo1 (= ${binary:Version}),
                       foo-data (= ${source:Version}),
              ```

              This field declares an absolute dependency. Before installing the package, **dpkg** will require
              all dependencies to be in state `configured` first. Though, if there is a circular dependency between
              two or more packages, **dpkg** will break that circle at an arbitrary point where necessary based on
              built-in heuristics.

              This field should be used if the depended-on package is required for the depending package to provide a
              *significant amount of functionality* or when it is used in the **postinst** or **prerm** maintainer
              scripts.
            """
        ),
    ),
    DctrlKnownField(
        "Recommends",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Optional dependencies **most** people should have",
        hover_text=textwrap.dedent(
            """\
                Lists the packages that *should* be installed when this package is installed in all but
                *unusual installations*.

                **Example**:
                ```
                Recommends: foo-optional
                ```

                By default, APT will attempt to install recommends unless they cannot be installed or the user
                has configured APT skip recommends. Notably, during automated package builds for the Debian
                archive, **Recommends** are **not** installed.

                As implied, the package must have some core functionality that works **without** the
                **Recommends** being satisfied as they are not guaranteed to be there.  If the package cannot
                provide any functionality without a given package, that package should be in **Depends**.
            """
        ),
    ),
    DctrlKnownField(
        "Suggests",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Optional dependencies that some people might want",
        hover_text=textwrap.dedent(
            """\
                Lists the packages that may make this package more useful but not installing them is perfectly
                reasonable as well. Suggests can also be useful for add-ons that only make sense in particular
                corner cases like supporting a non-standard file format.

                **Example**:
                ```
                Suggests: bar
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Enhances",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="Packages enhanced by installing this package",
        hover_text=textwrap.dedent(
            """\
                This field is similar to Suggests but works in the opposite direction.  It is used to declare that
                this package can enhance the functionality of another package.

                **Example**:
                ```
                Package: foo
                Provides: debputy-plugin-foo
                Enhances: debputy
                ```
            """
        ),
    ),
    DctrlRelationshipKnownField(
        "Provides",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        supports_or_relation=False,
        allowed_version_operators=frozenset(["="]),
        synopsis_doc="Additional packages/versions this package dependency-wise satisfy",
        hover_text=textwrap.dedent(
            """\
                  Declare this package also provide one or more other packages.  This means that this package can
                  substitute for the provided package in some relations.

                  **Example**:
                  ```
                  Package: foo
                  ...

                  Package: foo-plus
                  Provides: foo (= ${source:Upstream-Version})
                  ```

                  If the provides relation is versioned, it must use a "strictly equals" version.  If it does not
                  declare a version, then it *cannot* be used to satisfy a dependency with a version restriction.
                  Consider the following example:

                  **Archive scenario**:  (This is *not* a `debian/control` file, despite the resemblance)
                  ```
                  Package foo
                  Depends: bar (>= 1.0)

                  Package: bar
                  Version: 0.9

                  Package: bar-plus
                  Provides: bar (= 1.0)

                  Package: bar-clone
                  Provides: bar
                  ```

                  In this archive scenario, the `bar-plus` package will satisfy the dependency of `foo` as the
                  only one. The `bar` package fails because the version is only *0.9* and `bar-clone` because
                  the provides is unversioned, but the dependency clause is versioned.
            """
        ),
    ),
    DctrlRelationshipKnownField(
        "Conflicts",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        supports_or_relation=False,
        synopsis_doc="Packages that this package is not co-installable with",
        hover_text=textwrap.dedent(
            """\
                  **Warning**: *You may be looking for Breaks instead of Conflicts*.

                  This package cannot be installed together with the packages listed in the Conflicts field.  This
                  is a *bigger hammer* than **Breaks** and is used sparingly.  Notably, if you want to do a versioned
                  **Conflicts** then you *almost certainly* want **Breaks** instead.

                  **Example**:
                  ```
                  Conflicts: bar
                  ```

                  Please see https://wiki.debian.org/PackageTransition for when to uses **Breaks**, **Conflicts**,
                  or/and **Replaces**.

                  Note if a package conflicts with itself (indirectly or via **Provides**), then it is using a
                  special rule for **Conflicts**.  See section
                  7.6.2 "[Replacing whole packages, forcing their removal]" in the Debian Policy Manual for the
                  details.

                  [Replacing whole packages, forcing their removal]: https://www.debian.org/doc/debian-policy/ch-relationships.html#replacing-whole-packages-forcing-their-removal
            """
        ),
    ),
    DctrlRelationshipKnownField(
        "Breaks",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        supports_or_relation=False,
        synopsis_doc="Package/versions that does not work with this package",
        hover_text=textwrap.dedent(
            """\
      This package cannot be installed together with the packages listed in the `Breaks` field.

      This is often use to declare versioned issues such as "This package does not work with foo if
      it is version 1.0 or less". In comparison, `Conflicts` is generally used to declare that
      "This package does not work at all as long as foo is installed".

      **Example**:
      ```
      Breaks: bar (<= 1.0~)
      ````

      Please see https://wiki.debian.org/PackageTransition for when to uses **Breaks**, **Conflicts**, or/and
      **Replaces**.

      Note the use of *~* in version numbers in the example is used to ensure this works correctly in
      case of a backports (in the Debian archive), where the package is rebuilt with the "~bpo" suffix in its
      version.
            """
        ),
    ),
    DctrlRelationshipKnownField(
        "Replaces",
        FieldValueClass.COMMA_SEPARATED_LIST,
        custom_field_check=_dctrl_validate_dep,
        synopsis_doc="This package replaces content from these packages/versions",
        hover_text=textwrap.dedent(
            """\
                  This package either replaces another package or overwrites files that used to be provided by
                  another package.

                  **Attention**: The `Replaces` field should **always** used with either `Breaks` or `Conflicts` field.

                  **Example**:
                  ```
                  Package: foo
                  ...

                  # The foo package was split to move data files into foo-data in version 1.2-3
                  Package: foo-data
                  Replaces: foo (<< 1.2-3~)
                  Breaks: foo (<< 1.2-3~)
                  ```

                  Please see https://wiki.debian.org/PackageTransition for when to uses **Breaks**, **Conflicts**,
                  or/and **Replaces**.

                  Note the use of *~* in version numbers in the example is used to ensure this works correctly in
                  case of a backports (in the Debian archive), where the package is rebuilt with the "~bpo" suffix in its
                  version.
            """
        ),
    ),
    DctrlKnownField(
        "Build-Profiles",
        FieldValueClass.BUILD_PROFILES_LIST,
        synopsis_doc="Conditionally build this package",
        hover_text=textwrap.dedent(
            """\
      **Advanced field**. *This field covers an advanced topic. If you are new to packaging, you are*
      *advised to leave it at its default until you have a working basic package or lots of time to understand*
      *this topic.*

      Declare that the package will only be built when the given build-profiles are satisfied.

      This field is primarily used in combination with build profiles inside the build dependency related fields
      to reduce the number of build dependencies required during bootstrapping of a new architecture.

      **Example**:
      ```
      Package: foo
      ...

      Package: foo-udeb
      Package-Type: udeb
      # Skip building foo-udeb when the build profile "noudeb" is set (e.g., via dpkg-buildpackage -Pnoudeb)
      Build-Profiles: <!noudeb>
      ```

      Note that there is an official list of "common" build profiles with predefined purposes along with rules
      for how and when the can be used. This list can be found at
      <https://wiki.debian.org/BuildProfileSpec#Registered_profile_names>.
            """
        ),
    ),
    DctrlKnownField(
        "Section",
        FieldValueClass.SINGLE_VALUE,
        missing_field_severity=DiagnosticSeverity.Error,
        inherits_from_source=True,
        known_values=ALL_SECTIONS,
        unknown_value_diagnostic_severity=DiagnosticSeverity.Warning,
        synopsis_doc="Which section this package should be in",
        hover_text=textwrap.dedent(
            """\
                Define the section for this package.

                **Example**:
                ```
                Section: devel
                ```

                Please see <https://packages.debian.org/unstable> for more details about the sections.
            """
        ),
    ),
    DctrlKnownField(
        "Priority",
        FieldValueClass.SINGLE_VALUE,
        default_value="optional",
        warn_if_default=False,
        missing_field_severity=DiagnosticSeverity.Error,
        inherits_from_source=True,
        known_values=ALL_PRIORITIES,
        synopsis_doc="The package's priority (Policy term)",
        hover_text=textwrap.dedent(
            """\
                    Define the priority this package.

                    The priority field describes how important the package is for the functionality of the system.

                    **Example**:
                    ```
                    Priority: optional
                    ```

                    Unless you know you need a different value, you should choose **optional** for your packages.
                """
        ),
    ),
    DctrlKnownField(
        "Multi-Arch",
        FieldValueClass.SINGLE_VALUE,
        # Explicit "no" tends to be used as "someone reviewed this and concluded no", so we do
        # not warn about it being explicitly "no".
        warn_if_default=False,
        default_value="no",
        custom_field_check=_dctrl_ma_field_validation,
        known_values=allowed_values(
            Keyword(
                "no",
                # Show `no` after `foreign` and `same`. Often, you want `same` or `foreign`.
                sort_text="zz-no",
                synopsis_doc='No Multi-Arch support (often used for "reviewed and no support possible/needed")',
                hover_text=textwrap.dedent(
                    """\
                    The default. The package can be installed for at most one architecture at the time.  It can
                    *only* satisfy relations for the same architecture as itself. Note that `Architecture: all`
                    packages are considered as a part of the system's "primary" architecture (see output of
                    `dpkg --print-architecture`).

                    Note: Despite the "no", the package *can* be installed for a foreign architecture (as an example,
                    you can install a 32-bit version of a package on a 64-bit system).  However, packages depending
                    on it must also be installed for the foreign architecture.
                """
                ),
            ),
            Keyword(
                "foreign",
                synopsis_doc="Can satisfy dependencies for other architectures (common for data and *some* scripts)",
                hover_text=textwrap.dedent(
                    """\
                    The package can be installed for at most one architecture at the time.  However, it can
                    satisfy relations for packages regardless of their architecture.  This is often useful for packages
                    solely providing data or binaries that have "Multi-Arch neutral interfaces".

                    Sadly, describing a "Multi-Arch neutral interface" is hard and often only done by Multi-Arch
                    experts on a case-by-case basis.  Some programs and scripts have "Multi-Arch dependent interfaces"
                    and are not safe to declare as `Multi-Arch: foreign`.

                    The name "foreign" refers to the fact that the package can satisfy relations for native
                    *and foreign* architectures at the same time.
                """
                ),
            ),
            Keyword(
                "same",
                can_complete_keyword_in_stanza=_complete_only_in_arch_dep_pkgs,
                synopsis_doc="Co-installable with itself for different architectures (common for native libraries)",
                hover_text=textwrap.dedent(
                    """\
                    The same version of the package can be co-installed for multiple architecture. However,
                    for this to work, the package *must* ship all files in architecture unique paths (usually
                    beneath `/usr/lib/<DEB_HOST_MULTIARCH>`) or have bit-for-bit identical content
                    in files that are in non-architecture unique paths (such as files beneath `/usr/share/doc`).

                    The name `same` refers to the fact that the package can satisfy relations only for the `same`
                    architecture as itself.  However, in this case, it is co-installable with itself as noted above.
                    Note: This value **cannot** be used with `Architecture: all`.
                """
                ),
            ),
            Keyword(
                "allowed",
                # Never show `allowed` first, it is the absolute least likely candidate.
                sort_text="zzzz-allowed",
                usage_hint="rare",
                synopsis_doc="Consumer decides whether it is `same` and `foreign`",
                hover_text=textwrap.dedent(
                    """\
                  **Advanced and very rare value**. This value is exceedingly rare to the point that less
                  than 0.40% of all packages in Debian used in it (May 2024). It is even rarer for for
                  `Architecture: all` packages, where the number an order of magnitude smaller. Unless
                  a Multi-Arch expert or the Multi-Arch hinter suggested that you use this value, for
                  this package, then probably it is not the right choice.

                  The value means that the package is *not* co-installable with itself but can satisfy Multi-Arch
                  `foreign` and Multi-Arch `same` relations at the same time.  This is useful for implementations of
                  scripting languages (such as Perl or Python).  Here the interpreter contextually need to
                  satisfy some relations as `Multi-Arch: foreign` and others as `Multi-Arch: same`.

                  Typically, native extensions or plugins will need a `Multi-Arch: same`-relation as they only
                  work with the interpreter compiled for the same machine architecture as themselves whereas
                  scripts are usually less picky and can rely on the `Multi-Arch: foreign` relation.  Packages
                  wanting to rely on the "Multi-Arch: foreign" interface must explicitly declare this adding a
                  `:any` suffix to the package name in the dependency relation (e.g. `Depends: python3:any`).
                  However, the `:any"`suffix cannot be used unconditionally and should not be used unless you
                  know you need it.
            """
                ),
            ),
        ),
        synopsis_doc="**Advanced field**: How this package interacts with multi arch",
        hover_text=textwrap.dedent(
            """\
      **Advanced field**. *This field covers an advanced topic. If you are new to packaging, you are*
      *advised to leave it at its default until you have a working basic package or lots of time to understand*
      *this topic.*

      This field is used to declare the Multi-Arch interface of the package.

      The `Multi-Arch` field is used to inform the installation system (APT and dpkg) about how it should handle
      dependency relations involving this package and foreign architectures. This is useful for multiple purposes
      such as cross-building without emulation and installing 32-bit packages on a 64-bit system. The latter is
      often done to use legacy apps or old games that was never ported to 64-bit machines.

      **Example**:
      ```
      Multi-Arch: foreign
      ```

      The rules for `Multi-Arch` can be quite complicated, but in many cases the following simple rules of thumb
      gets you a long way:

       * If the [Multi-Arch hinter] comes with a hint, then it almost certainly correct. You are recommended
         to check the hint for further details (some changes can be complicated to do).  Note that the
         Multi-Arch hinter is only run for official Debian packages and may not be applicable to your case.

       * If you have an `Architecture: all` data-only package, then it often want to be `Multi-Arch: foreign`

       * If you have an architecture dependent package, where everything is installed in
         `/usr/lib/${DEB_HOST_MULTIARCH}` (plus a bit of standard documentation in `/usr/share/doc`), then
         you *probably* want `Multi-Arch: same`. Note that when using `debputy` as the build helper, `debputy`
         will automatically detect the most common variants of this case and sets the field for you when
          relevant.

       * If none of the above applies, then omit the field unless you know what you are doing or you are
         receiving advice from a Multi-Arch expert.


      There are 4 possible values for the Multi-Arch field, though not all values are applicable to all packages:


        * `no` - The default. The package can be installed for at most one architecture at the time.  It can
          *only* satisfy relations for the same architecture as itself. Note that `Architecture: all` packages
          are considered as a part of the system's "primary" architecture (see output of `dpkg --print-architecture`).

          Use of an explicit `no` over omitting the field is commonly done to signal that someone took the
          effort to understand the situation and concluded `no` was the right answer.

          Note: Despite the `no`, the package *can* be installed for a foreign architecture (e.g. you can
          install a 32-bit version of a package on a 64-bit system).  However, packages depending on it must also
          be installed for the foreign architecture.


        * `foreign` - The package can be installed for at most one architecture at the time.  However, it can
          satisfy relations for packages regardless of their architecture.  This is often useful for packages
          solely providing data or binaries that have "Multi-Arch neutral interfaces". Sadly, describing
          a "Multi-Arch neutral interface" is hard and often only done by Multi-Arch experts on a case-by-case
          basis. Among other, scripts despite being the same on all architectures can still have a "non-neutral"
          "Multi-Arch" interface if their output is architecture dependent or if their dependencies force them
          out of the `foreign` role. The dependency issue usually happens when depending indirectly on an
          `Multi-Arch: allowed` package.

          Some programs have "Multi-Arch dependent interfaces" and are not safe to declare as
          `Multi-Arch: foreign`. The name `foreign` refers to the fact that the package can satisfy relations
          for native *and foreign* architectures at the same time.


        * `same` - The same version of the package can be co-installed for multiple architecture. However,
          for this to work, the package **must** ship all files in architecture unique paths (usually
          beneath `/usr/lib/${DEB_HOST_MULTIARCH}`) **or** have bit-for-bit identical content in files
          that are in non-architecture unique paths (e.g. `/usr/share/doc`). Note that these packages
          typically do not contain configuration files or **dpkg** `conffile`s.

          The name `same` refers to the fact that the package can satisfy relations only for the "same"
          architecture as itself.  However, in this case, it is co-installable with itself as noted above.

          Note: This value **cannot** be used with `Architecture: all`.


        * `allowed` - **Advanced value**. This value is for a complex use-case that most people do not
          need. Consider it only if none of the other values seem to do the trick.

          The package is **NOT** co-installable with itself but can satisfy Multi-Arch foreign and Multi-Arch same
          relations at the same. This is useful for implementations of scripting languages (e.g. Perl or Python).
          Here the interpreter contextually need to satisfy some relations as `Multi-Arch: foreign` and others as
          `Multi-Arch: same` (or `Multi-Arch: no`).

          Typically, native extensions or plugins will need a `Multi-Arch: same`-relation as they only work with
          the interpreter compiled for the same machine architecture as themselves whereas scripts are usually
          less picky and can rely on the `Multi-Arch: foreign` relation.  Packages wanting to rely on the
          `Multi-Arch: foreign` interface must explicitly declare this adding a `:any` suffix to the package name
          in the dependency relation (such as `Depends: python3:any`).  However, the `:any` suffix cannot be used
          unconditionally and should not be used unless you know you need it.

          Note that depending indirectly on a `Multi-Arch: allowed` package can require a `Architecture: all` +
          `Multi-Arch: foreign` package to be converted to a `Architecture: any` package. This case is named
          the "Multi-Arch interpreter problem", since it is commonly seen with script interpreters. However,
          despite the name, it can happen to any kind of package. The bug [Debian#984701] is an example of
          this happen in practice.

      [Multi-Arch hinter]: https://wiki.debian.org/MultiArch/Hints
      [Debian#984701]: https://bugs.debian.org/984701
            """
        ),
    ),
    DctrlKnownField(
        "XB-Installer-Menu-Item",
        FieldValueClass.SINGLE_VALUE,
        can_complete_field_in_stanza=_complete_only_for_udeb_pkgs,
        custom_field_check=_combined_custom_field_check(
            _udeb_only_field_validation,
            _each_value_match_regex_validation(re.compile(r"^[1-9]\d{3,4}$")),
        ),
        synopsis_doc="(udeb-only) Package's order in the d-i menu",
        hover_text=textwrap.dedent(
            """\
            This field is only relevant for `udeb` packages (debian-installer).

            The field is used to declare where in the installer menu this package's menu item should
            be placed (assuming it has any menu item). For packages targeting the Debian archive,
            any new package should have its menu item number aligned with the debian-installer team
            before upload.

            A menu item is 4-5 digits (In the range `1000 <= X <= 99999`). In rare cases, the menu
            item can be architecture dependent. For architecture dependent menu item values, use a
            custom substvar.

            See <https://d-i.debian.org/doc/internals/apa.html> for the full list of menu item ranges
            and for how to request a number.
        """
        ),
    ),
    DctrlKnownField(
        "X-DH-Build-For-Type",
        FieldValueClass.SINGLE_VALUE,
        custom_field_check=_arch_not_all_only_field_validation,
        can_complete_field_in_stanza=_complete_only_in_arch_dep_pkgs,
        default_value="host",
        known_values=allowed_values(
            Keyword(
                "host",
                hover_text="The package should be compiled for `DEB_HOST_TARGET` (the default).",
            ),
            Keyword(
                "target",
                hover_text="The package should be compiled for `DEB_TARGET_ARCH`.",
            ),
        ),
        synopsis_doc="(Special purpose) For cross-compiling cross-compilers",
        hover_text=textwrap.dedent(
            """\
                  **Special-purpose only**. *This field is a special purpose field and is rarely needed.*
                  *You are recommended to omit unless you know you need it or someone told you to use it.*

                  This field is used when building a cross-compiling C-compiler (or similar cases), some packages need
                  to be build for target (DEB_**TARGET**_ARCH) rather than the host (DEB_**HOST**_ARCH) architecture.

                  **Example**:
                  ```
                  Package: gcc
                  Architecture: any
                  # ...

                  Package: libgcc-s1
                  Architecture: any
                  # When building a cross-compiling gcc, then this library needs to be built for the target architecture
                  # as binaries compiled by gcc will link with this library.
                  X-DH-Build-For-Type: target
                  # ...
                  ```

                  If you are in doubt, then you probably do **not** need this field.
                """
        ),
    ),
    DctrlKnownField(
        "X-Time64-Compat",
        FieldValueClass.SINGLE_VALUE,
        can_complete_field_in_stanza=_complete_only_in_arch_dep_pkgs,
        custom_field_check=_combined_custom_field_check(
            _each_value_match_regex_validation(PKGNAME_REGEX),
            _arch_not_all_only_field_validation,
        ),
        synopsis_doc="(Special purpose) Compat name for time64_t transition",
        hover_text=textwrap.dedent(
            """\
                Special purpose field related to the 64-bit time transition.

                It is used to inform packaging helpers what the original (non-transitioned) package name
                was when the auto-detection is inadequate. The non-transitioned package name is then
                conditionally provided in the `${t64:Provides}` substitution variable.

                The field only works for architecture dependent packages.
                """
        ),
    ),
    DctrlKnownField(
        "Homepage",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="(Special purpose) Upstream homepage URL for this binary package",
        hover_text=textwrap.dedent(
            """\
                Link to the upstream homepage for this binary package.

                This field is rarely used in Package stanzas as most binary packages should have the
                same homepage as the source package. Though, in the exceptional case where a particular
                binary package should have a more specific homepage than the source package, you can
                use this field to override the source package field.
                ```
            """
        ),
    ),
    DctrlKnownField(
        "Description",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        # It will build just fine. But no one will know what it is for, so it probably won't be installed
        missing_field_severity=DiagnosticSeverity.Warning,
        synopsis_doc="Package synopsis and description",
        custom_field_check=dctrl_description_validator,
        hover_text=textwrap.dedent(
            """\
            A human-readable description of the package. This field consists of two related but distinct parts.

            The first line immediately after the field is called the *Synopsis* and is a short "noun-phrase"
            intended to provide a one-line summary of the package. The lines after the **Synopsis** is known
            as the **Extended Description** and is intended as a longer summary of the package.

            **Example**:
            ```
            Description: documentation generator for Python projects
              Sphinx is a tool for producing documentation for Python projects, using
              reStructuredText as markup language.
              .
              Sphinx features:
               * HTML, CHM, LaTeX output,
               * Cross-referencing source code,
               * Automatic indices,
               * Code highlighting, using Pygments,
               * Extensibility. Existing extensions:
                 - automatic testing of code snippets,
                 - including docstrings from Python modules.
              .
              Build-depend on sphinx if your package uses /usr/bin/sphinx-*
              executables. Build-depend on python3-sphinx if your package uses
              the Python API (for instance by calling python3 -m sphinx).
            ```

            The **Synopsis** is usually displayed in cases where there is limited space such as when reviewing
            the search results from `apt search foo`.  It is often a good idea to imagine that the **Synopsis**
            part is inserted into a sentence like "The package provides {{Synopsis-goes-here}}". The
            **Extended Description** is a standalone description that should describe what the package does and
            how it relates to the rest of the system (in terms of, for example, which subsystem it is which part of).
            Please see <https://www.debian.org/doc/debian-policy/ch-controlfields.html#description> for more details
            about the description field and suggestions for how to write it.

            Note: The synopsis part has its own hover doc that is specialized at aiding with writing and checking
            the synopsis.
            """
        ),
    ),
    DctrlKnownField(
        "XB-Cnf-Visible-Pkgname",
        FieldValueClass.SINGLE_VALUE,
        custom_field_check=_each_value_match_regex_validation(PKGNAME_REGEX),
        synopsis_doc="(Special purpose) Hint for `command-not-found`",
        hover_text=textwrap.dedent(
            """\
            **Special-case field**: *This field is only useful in very special circumstances.*
            *Consider whether you truly need it before adding this field.*

            This field is used by `command-not-found` and can be used to override which package
            `command-not-found` should propose the user to install.

            Normally, when `command-not-found` detects a missing command, it will suggest the
            user to install the package name listed in the `Package` field. In most cases, this
            is what you want. However, in certain special-cases, the binary is provided by a
            minimal package for technical reasons (like `python3-minimal`) and the user should
            really install a package that provides more features (such as `python3` to follow
            the example).

            **Example**:
            ```
            Package: python3-minimal
            XB-Cnf-Visible-Pkgname: python3
            ```

            Related bug: <https://bugs.launchpad.net/ubuntu/+source/python-defaults/+bug/1867157>
            """
        ),
    ),
    DctrlKnownField(
        "X-DhRuby-Root",
        FieldValueClass.SINGLE_VALUE,
        synopsis_doc="For multi-binary layout with `dh_ruby`",
        hover_text=textwrap.dedent(
            """\
            Used by `dh_ruby` to request "multi-binary" layout and where the root for the given
            package is.

            Please refer to the documentation of `dh_ruby` for more details.

            <https://manpages.debian.org/dh_ruby>
            """
        ),
    ),
    DctrlKnownField(
        "XB-Ruby-Versions",
        FieldValueClass.FREE_TEXT_FIELD,
        deprecated_with_no_replacement=True,
        synopsis_doc="Obsolete",
        hover_text=textwrap.dedent(
            """\
        Obsolete according to https://bugs.debian.org/1075762
        """
        ),
    ),
)
_DEP5_HEADER_FIELDS = _fields(
    Deb822KnownField(
        "Format",
        FieldValueClass.SINGLE_VALUE,
        is_stanza_name=True,
        missing_field_severity=DiagnosticSeverity.Error,
        hover_text=textwrap.dedent(
            """\
            URI of the format specification. The field that should be used for the current version of this
            document is:

            **Example**:
            ```
            Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
            ```

            The original version of this specification used the non-https version of this URL as its URI, namely:

            ```
            Format: http://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
            ```

            Both versions are valid and refer to the same specification, and parsers should interpret both as
            referencing the same format. The https URI is preferred.

            The value must be on a single line (that is, on same line as the field).
        """
        ),
    ),
    Deb822KnownField(
        "Upstream-Name",
        FieldValueClass.FREE_TEXT_FIELD,
        hover_text=textwrap.dedent(
            """\
            The name upstream uses for the software

            The value must be on a single line (that is, on same line as the field).
        """
        ),
    ),
    Deb822KnownField(
        "Upstream-Contact",
        FieldValueClass.FREE_TEXT_FIELD,
        hover_text=textwrap.dedent(
            """\
            The preferred address(es) to reach the upstream project. May be free-form text, but by convention will
            usually be written as a list of RFC5322 addresses or URIs.

            The value should be written as a line-based list (one value per line).
        """
        ),
    ),
    Deb822KnownField(
        "Source",
        FieldValueClass.FREE_TEXT_FIELD,
        hover_text=textwrap.dedent(
            """\
            An explanation of where the upstream source came from. Typically this would be a URL, but it might be
            a free-form explanation. The [Debian Policy section 12.5] requires this information unless there are
            no upstream sources, which is mainly the case for native Debian packages. If the upstream source has
            been modified to remove non-free parts, that should be explained in this field.

            The value should be written as "Formatted text" without no synopsis (when it is a free-form explanation).
            The "Formatted text" is similar to the extended description (the `Description` from `debian/control`).

            [Debian Policy section 12.5]: https://www.debian.org/doc/debian-policy/ch-docs#s-copyrightfile
        """
        ),
    ),
    Deb822KnownField(
        "Disclaimer",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        hover_text=textwrap.dedent(
            """\
            For `non-free`, `non-free-firmware` or `contrib` packages, this field is used to that they are not part
            of Debian and to explain why (see [Debian Policy section 12.5])

            The value should be written as "Formatted text" without no synopsis. The "Formatted text" is similar
            to the extended description (the `Description` from `debian/control`).

            [Debian Policy section 12.5]: https://www.debian.org/doc/debian-policy/ch-docs#s-copyrightfile
        """
        ),
    ),
    Deb822KnownField(
        "Comment",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        hover_text=textwrap.dedent(
            """\
            Comment field to optionally provide additional information. For example, it might quote an e-mail from
            upstream justifying why the combined license is acceptable to the `main` archive, or an explanation of
            how this version of the package has been forked from a version known to be [DFSG]-free, even though the
            current upstream version is not.

            Note if the `Comment` is only applicable to a set of files or a particular license out of many,
            the `Comment` field should probably be moved to the relevant `Files`-stanza or `License`-stanza instead.

            The value should be written as "Formatted text" without no synopsis. The "Formatted text" is similar
            to the extended description (the `Description` from `debian/control`).

            [DFSG]: https://www.debian.org/social_contract#guidelines
        """
        ),
    ),
    Deb822KnownField(
        "License",
        FieldValueClass.FREE_TEXT_FIELD,
        # Do not tempt people to change legal text because the spellchecker wants to do a typo fix.
        spellcheck_value=False,
        hover_text=textwrap.dedent(
            """\
            Provide license information for the package as a whole, which may be different or simplified form
            a combination of all the per-file license information.

            Using `License` in the `Header`-stanza is useful when it records a notable difference or simplification
            of the other `License` fields in this files. However, it serves no purpose to provide the field for the
            sole purpose of aggregating the other `License` fields.

            The first line (the same line as as the field name) should use an abbreviated license name or
            expression. The following lines can be used for the full license text. Though, to avoid repetition,
            the license text would generally be in its own `License`-stanza after the `Header`-stanza.
        """
        ),
    ),
    Deb822KnownField(
        "Copyright",
        FieldValueClass.FREE_TEXT_FIELD,
        # Mostly going to be names with very little free-text; high risk of false positives with low value
        spellcheck_value=False,
        hover_text=textwrap.dedent(
            """\
            One or more free-form copyright statements that applies to the package as a whole.

            Using `Copyright` in the `Header`-stanza is useful when it records a notable difference or simplification
            of the other `Copyright` fields in this files. However, it serves no purpose to provide the field for the
            sole purpose of aggregating the other `Copyright` fields.

            Any formatting is permitted. Simple cases often end up effectively being one copyright holder per
            line; see the examples below for some ideas for how to structure the field to make it easier to read.

            If a work has no copyright holder (i.e., it is in the public domain), that information should be recorded
            here.

            The Copyright field collects all relevant copyright notices for the files of this stanza. Not all
            copyright notices may apply to every individual file, and years of publication for one copyright
            holder may be gathered together. For example, if file A has:

            ```
            Copyright 2008 John Smith
            Copyright 2009 Angela Watts
            ```

            and file B has:

            ```
            Copyright 2010 Angela Watts
            ```

            a single stanza may still be used for both files. The Copyright field for that stanza might be written
            as:

            ```
            Files: A B
            Copyright:
              Copyright 2008 John Smith
              Copyright 2009, 2010 Angela Watts
            License: ...
            ```

            The `Copyright` field may contain the original copyright statement copied exactly (including the word
            "Copyright"), or it may shorten the text or merge it with other copyright statements as described above,
            as long as it does not sacrifice information.

            Formally, the value should be written as "Formatted text" without no synopsis. Though, it often
            ends up resembling a line-based list. The "Formatted text" is similar to the extended description
            (the `Description` from `debian/control`).
        """
        ),
    ),
    Deb822KnownField(
        "Files-Excluded",
        FieldValueClass.FREE_TEXT_FIELD,
        hover_text=textwrap.dedent(
            """\
        Remove the listed files from the tarball when repacking (commonly via uscan). This can be useful when the
        listed files are non-free but not necessary for the Debian package. In this case, the upstream version of
        the package should generally end with `~dfsg` or `+dfsg` (to mark the content changed due to the
        Debian Free Software Guidelines). The exclusion can also be useful to remove large files or directories
        that are not used by Debian or pre-built binaries. In this case, `~ds` or `+ds` should be added to the
        version instead of `~dfsg` or `+dfsg` for "Debian Source" to mark it as altered by Debian. If both reasons
        are used, the `~dfsg` or `+dfsg` version is used as that is the more important reason for the repacking.

        Example:
        ```
        Files-Excluded: exclude-this
          exclude-dir
          */exclude-dir
          .*
          */js/jquery.js
        ```

        The `Files-Included` field can be used to "re-include" files matched by `Files-Excluded`.

        It is also possible to exclude files in specific "upstream components" for source packages with multiple
        upstream tarballs. This is done by adding a field called `Files-Excluded-<component>`. The `<component>`
        part should then match the component name exactly (case sensitive).

        Defined by: mk-origtargz (usually used via uscan)
        """
        ),
    ),
    Deb822KnownField(
        "Files-Included",
        FieldValueClass.FREE_TEXT_FIELD,
        hover_text=textwrap.dedent(
            """\
        Re-include files that were marked for exclusion by `Files-Excluded`. This can be useful for "exclude
        everything except X" style semantics where `Files-Excluded` has a very broad pattern and
        `Files-Included` then marks a few exceptions.

        It is also possible to re-include files in specific "upstream components" for source packages with multiple
        upstream tarballs. This is done by adding a field called `Files-Include-<component>` which is then used
        in tandem with `Files-Exclude-<component>`. The `<component>` part should then match the component name
        exactly (case sensitive).


        Defined by: mk-origtargz (usually used via uscan)
        """
        ),
    ),
)
_DEP5_FILES_FIELDS = _fields(
    Deb822KnownField(
        "Files",
        FieldValueClass.DEP5_FILE_LIST,
        is_stanza_name=True,
        custom_field_check=_dep5_files_check,
        missing_field_severity=DiagnosticSeverity.Error,
        hover_text=textwrap.dedent(
            """\
            Whitespace separated list of patterns indicating files covered by the license and copyright specified in
            this stanza.

            Filename patterns in the `Files` field are specified using a simplified shell glob syntax. Patterns are
            separated by whitespace.

              * Only the wildcards `*` and `?` apply; the former matches any number of characters (including none),
                the latter a single character. Both match slashes (`/`) and leading dots, unlike shell globs. The
                pattern `*.in` therefore matches any file whose name ends in `.in` anywhere in the source tree,
                not just at the top level.

              * Patterns match pathnames that start at the root of the source tree. Thus, `Makefile.in` matches only
                the file at the root of the tree, but `*/Makefile.in` matches at any depth.

              * The backslash (`\\`) is used to remove the magic from the next character; see below.

            Escape sequences:
             * `\\*` matches a single literal asterisk (`*`)
             * `\\?` matches a single literal question mark (`?`)
             * `\\\\` matches a single literal backslash (`\\`)

            Any other character following a backslash is an error.

            This is the same pattern syntax as [fnmatch(3)] without the FNM_PATHNAME flag, or the argument to the
            `-path` test of the GNU find command, except that `[]` wildcards are not recognized.

            Multiple Files stanzas are allowed. The last stanza that matches a particular file applies to it.
            More general stanzas should therefore be given first, followed by more specific overrides. Accordingly,
            `Files: *` must be the first `Files`-stanza when used.

            Exclusions are only supported by adding `Files` stanzas to override the previous match:

            ```
            Files: *
            Copyright: ...
            License: ...
              ... license that applies by default ...

            Files: data/*
            Copyright: ...
            License: ...
              ... license that applies to all paths in data/* ...

            Files: data/file-with-special-license
            Copyright: ...
            License: ...
              ... license that applies to this particular file ...
            ```

            This syntax does not distinguish file names from directory names; a trailing slash in a pattern will never
            match any actual path. A whole directory tree may be selected with a pattern like `foo/*`.

            The space character, used to separate patterns, cannot be escaped with a backslash. A path like `foo bar`
            may be selected with a pattern like `foo?bar`.

            [fnmatch(3)]: https://manpages.debian.org/fnmatch.3
        """
        ),
    ),
    Deb822KnownField(
        "Copyright",
        FieldValueClass.FREE_TEXT_FIELD,
        # Mostly going to be names with very little free-text; high risk of false positives with low value
        spellcheck_value=False,
        missing_field_severity=DiagnosticSeverity.Error,
        hover_text=textwrap.dedent(
            """\
            One or more free-form copyright statements that applies to the files matched by this `Files`-stanza.
            Any formatting is permitted. Simple cases often end up effectively being one copyright holder per
            line; see the examples below for some ideas for how to structure the field to make it easier to read.

            If a work has no copyright holder (i.e., it is in the public domain), that information should be recorded
            here.

            The Copyright field collects all relevant copyright notices for the files of this stanza. Not all
            copyright notices may apply to every individual file, and years of publication for one copyright
            holder may be gathered together. For example, if file A has:

            ```
            Copyright 2008 John Smith
            Copyright 2009 Angela Watts
            ```

            and file B has:

            ```
            Copyright 2010 Angela Watts
            ```

            a single stanza may still be used for both files. The Copyright field for that stanza might be written
            as:

            ```
            Files: A B
            Copyright:
              Copyright 2008 John Smith
              Copyright 2009, 2010 Angela Watts
            License: ...
            ```

            The `Copyright` field may contain the original copyright statement copied exactly (including the word
            "Copyright"), or it may shorten the text or merge it with other copyright statements as described above,
            as long as it does not sacrifice information.

            Formally, the value should be written as "Formatted text" without no synopsis. Though, it often
            ends up resembling a line-based list. The "Formatted text" is similar to the extended description
            (the `Description` from `debian/control`).
        """
        ),
    ),
    Deb822KnownField(
        "License",
        FieldValueClass.FREE_TEXT_FIELD,
        missing_field_severity=DiagnosticSeverity.Error,
        # Do not tempt people to change legal text because the spellchecker wants to do a typo fix.
        spellcheck_value=False,
        hover_text=textwrap.dedent(
            """\
            Provide license information for the files matched by this `Files`-stanza.

            The first line is either an abbreviated name for the license or an expression giving
            alternatives.

            When there are additional lines, they are expected to give the fill license terms for
            the files matched or a pointer to `/usr/share/common-licences`. Otherwise, each license
            referenced in the first line must have a separate stand-alone `License`-stanza describing
            the license terms.

            **Extended example**:
            ```
            Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/

            Files: *
            Copyright: 2013, Someone
            License: GPL-2+

            Files: tests/*
            Copyright: 2013, Someone
            # In-line license
            License: MIT
              ... full license text of the MIT license here ...

            Files: tests/complex_text.py
            Copyright: 2013, Someone
            License: GPL-2+

            # Referenced license
            License: GPL-2+
             The code is licensed under GNU General Public License version 2 or, at your option, any
             later version.
             .
             On Debian systems the full text of the GNU General Public License version 2
             can be found in the `/usr/share/common-licenses/GPL-2' file.
            ```

            The first line (the same line as as the field name) should use the abbreviated license name that
            other stanzas use as reference.

        """
        ),
    ),
    Deb822KnownField(
        "Comment",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        hover_text=textwrap.dedent(
            """\
            Comment field to optionally provide additional information. For example, it might quote an e-mail from
            upstream justifying why the license is acceptable to the `main` archive, or an explanation of how this
            version of the package has been forked from a version known to be [DFSG]-free, even though the current
            upstream version is not.

            The value should be written as "Formatted text" without no synopsis. The "Formatted text" is similar
            to the extended description (the `Description` from `debian/control`).

            [DFSG]: https://www.debian.org/social_contract#guidelines
        """
        ),
    ),
)
_DEP5_LICENSE_FIELDS = _fields(
    Deb822KnownField(
        "License",
        FieldValueClass.FREE_TEXT_FIELD,
        is_stanza_name=True,
        # Do not tempt people to change legal text because the spellchecker wants to do a typo fix.
        spellcheck_value=False,
        missing_field_severity=DiagnosticSeverity.Error,
        hover_text=textwrap.dedent(
            """\
            Provide the license text for a given license shortname referenced from either the `Header`-stanza
            or a `Files` stanza.

            **Extended example**:
            ```
            Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/

            Files: *
            Copyright: 2013, Someone
            License: GPL-2+

            Files: tests/*
            Copyright: 2013, Someone
            # In-line license
            License: MIT
              ... full license text of the MIT license here ...

            Files: tests/complex_text.py
            Copyright: 2013, Someone
            License: GPL-2+

            # Referenced license
            License: GPL-2+
             The code is licensed under GNU General Public License version 2 or, at your option, any
             later version.
             .
             On Debian systems the full text of the GNU General Public License version 2
             can be found in the `/usr/share/common-licenses/GPL-2' file.
            ```

            The first line (the same line as as the field name) should use the abbreviated license name that
            other stanzas use as reference. In the `License`-stanza, this field must always contain the full
            license text in the following lines or a reference to a license in `/usr/share/common-licenses`.

            By convention, stand-alone `License`-stanza are usually placed in the bottom of the file.
        """
        ),
    ),
    Deb822KnownField(
        "Comment",
        FieldValueClass.FREE_TEXT_FIELD,
        spellcheck_value=True,
        hover_text=textwrap.dedent(
            """\
            Comment field to optionally provide additional information. For example, it might quote an e-mail from
            upstream justifying why the license is acceptable to the `main` archive, or an explanation of how this
            version of the package has been forked from a version known to be [DFSG]-free, even though the current
            upstream version is not.

            The value should be written as "Formatted text" without no synopsis. The "Formatted text" is similar
            to the extended description (the `Description` from `debian/control`).

            [DFSG]: https://www.debian.org/social_contract#guidelines
        """
        ),
    ),
)

_DTESTSCTRL_FIELDS = _fields(
    DTestsCtrlKnownField(
        "Architecture",
        FieldValueClass.SPACE_SEPARATED_LIST,
        unknown_value_diagnostic_severity=None,
        # FIXME: Specialize validation for architecture ("!fou" to "foo" would be bad)
        known_values=allowed_values(*dpkg_arch_and_wildcards(allow_negations=True)),
        synopsis_doc="Only run these tests on specific architectures",
        hover_text=textwrap.dedent(
            """\
            When package tests are only supported on a limited set of
            architectures, or are known to not work on a particular (set of)
            architecture(s), this field can be used to define the supported
            architectures. The autopkgtest will be skipped when the
            architecture of the testbed doesn't match the content of this
            field. The format is the same as in (Build-)Depends, with the
            understanding that `all` is not allowed, and `any` means that
            the test will be run on every architecture, which is the default
            when not specifying this field at all.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Classes",
        FieldValueClass.FREE_TEXT_FIELD,
        synopsis_doc="Hardware related tagging",
        hover_text=textwrap.dedent(
            """\
            Most package tests should work in a minimal environment and are
            usually not hardware specific. However, some packages like the
            kernel, X.org, or graphics drivers should be tested on particular
            hardware, and also run on a set of different platforms rather than
            just a single virtual testbeds.

            This field can specify a list of abstract class names such as
            "desktop" or "graphics-driver". Consumers of autopkgtest can then
            map these class names to particular machines/platforms/policies.
            Unknown class names should be ignored.

            This is purely an informational field for autopkgtest itself and
            will be ignored.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Depends",
        FieldValueClass.COMMA_SEPARATED_LIST,
        default_value="@",
        synopsis_doc="Dependencies for running the tests",
        hover_text=textwrap.dedent(
            """\
            Declares that the specified packages must be installed for the test
            to go ahead. This supports all features of dpkg dependencies, including
            the architecture qualifiers (see
            <https://www.debian.org/doc/debian-policy/ch-relationships.html>),
            plus the following extensions:

            `@` stands for the package(s) generated by the source package
            containing the tests; each dependency (strictly, or-clause, which
            may contain `|`s but not commas) containing `@` is replicated
            once for each such binary package, with the binary package name
            substituted for each `@` (but normally `@` should occur only
            once and without a version restriction).

            `@builddeps@` will be replaced by the package's
            `Build-Depends:`, `Build-Depends-Indep:`, `Build-Depends-Arch:`, and
            `build-essential`. This is useful if you have many build
            dependencies which are only necessary for running the test suite and
            you don't want to replicate them in the test `Depends:`. However,
            please use this sparingly, as this can easily lead to missing binary
            package dependencies being overlooked if they get pulled in via
            build dependencies.

            `@recommends@` stands for all the packages listed in the
            `Recommends:` fields of all the binary packages mentioned in the
            `debian/control` file. Please note that variables are stripped,
            so if some required test dependencies aren't explicitly mentioned,
            they may not be installed.

            If no Depends field is present, `Depends: @` is assumed. Note that
            the source tree's Build-Dependencies are *not* necessarily
            installed, and if you specify any Depends, no binary packages from
            the source are installed unless explicitly requested.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Features",
        FieldValueClass.COMMA_OR_SPACE_SEPARATED_LIST,
        hover_text=textwrap.dedent(
            """\
            Declares some additional capabilities or good properties of the
            tests defined in this stanza. Any unknown features declared will be
            completely ignored. See below for the defined features.

            Features are separated by commas and/or whitespace.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Restrictions",
        FieldValueClass.COMMA_OR_SPACE_SEPARATED_LIST,
        unknown_value_diagnostic_severity=DiagnosticSeverity.Warning,
        known_values=allowed_values(
            Keyword(
                "allow-stderr",
                hover_text=textwrap.dedent(
                    """\
                    Output to stderr is not considered a failure. This is useful for
                    tests which write e. g. lots of logging to stderr.
                """
                ),
            ),
            Keyword(
                "breaks-testbed",
                hover_text=textwrap.dedent(
                    """\
                    The test, when run, is liable to break the testbed system. This
                    includes causing data loss, causing services that the machine is
                    running to malfunction, or permanently disabling services; it does
                    not include causing services on the machine to temporarily fail.

                    When this restriction is present the test will usually be skipped
                    unless the testbed's virtualisation arrangements are sufficiently
                    powerful, or alternatively if the user explicitly requests.
                """
                ),
            ),
            Keyword(
                "build-needed",
                hover_text=textwrap.dedent(
                    """\
                    The tests need to be run from a built source tree. The test runner
                    will build the source tree (honouring the source package's build
                    dependencies), before running the tests. However, the tests are
                    *not* entitled to assume that the source package's build
                    dependencies will be installed when the test is run.

                    Please use this considerately, as for large builds it unnecessarily
                    builds the entire project when you only need a tiny subset (like the
                    `tests/` subdirectory). It is often possible to run `make -C tests`
                    instead, or copy the test code to `$AUTOPKGTEST_TMP` and build it
                    there with some custom commands. This cuts down the load on the
                    Continuous Integration servers and also makes tests more robust as
                    it prevents accidentally running them against the built source tree
                    instead of the installed packages.
                """
                ),
            ),
            Keyword(
                "flaky",
                hover_text=textwrap.dedent(
                    """\
                    The test is expected to fail intermittently, and is not suitable for
                    gating continuous integration. This indicates a bug in either the
                    package under test, a dependency or the test itself, but such bugs
                    can be difficult to fix, and it is often difficult to know when the
                    bug has been fixed without running the test for a while. If a
                    `flaky` test succeeds, it will be treated like any other
                    successful test, but if it fails it will be treated as though it
                    had been skipped.
                """
                ),
            ),
            Keyword(
                "hint-testsuite-triggers",
                hover_text=textwrap.dedent(
                    """\
                    This test exists purely as a hint to suggest when rerunning the
                    tests is likely to be useful.  Specifically, it exists to
                    influence the way dpkg-source generates the Testsuite-Triggers
                    .dsc header from test metadata: the Depends for this test are
                    to be added to Testsuite-Triggers.  (Just as they are for any other
                    test.)

                    The test with the hint-testsuite-triggers restriction should not
                    actually be run.

                    The packages listed as Depends for this test are usually indirect
                    dependencies, updates to which are considered to pose a risk of
                    regressions in other tests defined in this package.

                    There is currently no way to specify this hint on a per-test
                    basis; but in any case the debian.org machinery is not able to
                    think about triggering individual tests.
                """
                ),
            ),
            Keyword(
                "isolation-container",
                hover_text=textwrap.dedent(
                    """\
                    The test wants to start services or open network TCP ports. This
                    commonly fails in a simple chroot/schroot, so tests need to be run
                    in their own container (e. g. autopkgtest-virt-lxc) or their own
                    machine/VM (e. g. autopkgtest-virt-qemu or autopkgtest-virt-null).
                    When running the test in a virtualization server which does not
                    provide this (like autopkgtest-schroot) it will be skipped.

                    Tests may assume that this restriction implies that process 1 in the
                    container's process namespace is a system service manager (init system)
                    such as systemd or sysvinit + sysv-rc, and therefore system services
                    are available via the `service(8)`, `invoke-rc.d(8)` and
                    `update-rc.d(8))` interfaces.

                    Tests must not assume that a specific init system is in use: a
                    dependency such as `systemd-sysv` or `sysvinit-core` does not work
                    in practice, because switching the init system often cannot be done
                    automatically. Tests that require a specific init system should use the
                    `skippable` restriction, and skip the test if the required init system
                    was not detected.

                    Many implementations of the `isolation-container` restriction will
                    also provide `systemd-logind(8)` or a compatible interface, but this
                    is not guaranteed. Tests requiring a login session registered with
                    logind should declare a dependency on `default-logind | logind`
                    or on a more specific implementation of `logind`, and should use the
                    `skippable` restriction to exit gracefully if its functionality is
                    not available at runtime.

                """
                ),
            ),
            Keyword(
                "isolation-machine",
                hover_text=textwrap.dedent(
                    """\
                    The test wants to interact with the kernel, reboot the machine, or
                    other things which fail in a simple schroot and even a container.
                    Those tests need to be run in their own machine/VM (e. g.
                    autopkgtest-virt-qemu or autopkgtest-virt-null). When running the
                    test in a virtualization server which does not provide this it will
                    be skipped.

                    This restriction also provides the same facilities as
                    `isolation-container`.
                """
                ),
            ),
            Keyword(
                "needs-internet",
                hover_text=textwrap.dedent(
                    """\
                    The test needs unrestricted internet access, e.g. to download test data
                    that's not shipped as a package, or to test a protocol implementation
                    against a test server. Please also see the note about Network access later
                    in this document.
                """
                ),
            ),
            Keyword(
                "needs-reboot",
                hover_text=textwrap.dedent(
                    """\
                    The test wants to reboot the machine using
                    `/tmp/autopkgtest-reboot`.
                """
                ),
            ),
            Keyword(
                "needs-recommends",
                is_obsolete=True,
                hover_text=textwrap.dedent(
                    """\
                        Please use `@recommends@` in your test `Depends:` instead.
                """
                ),
            ),
            Keyword(
                "needs-root",
                hover_text=textwrap.dedent(
                    """\
                    The test script must be run as root.

                    While running tests with this restriction, some test runners will
                    set the `AUTOPKGTEST_NORMAL_USER` environment variable to the name
                    of an ordinary user account. If so, the test script may drop
                    privileges from root to that user, for example via the `runuser`
                    command. Test scripts must not assume that this environment variable
                    will always be set.

                    For tests that declare both the `needs-root` and `isolation-machine`
                    restrictions, the test may assume that it has "global root" with full
                    control over the kernel that is running the test, and not just root
                    in a container (more formally, it has uid 0 and full capabilities in
                    the initial user namespace as defined in `user_namespaces(7)`).
                    For example, it can expect that mounting block devices will succeed.

                    For tests that declare the `needs-root` restriction but not the
                    `isolation-machine` restriction, the test will be run as uid 0 in
                    a user namespace with a reasonable range of system and user uids
                    available, but will not necessarily have full control over the kernel,
                    and in particular it is not guaranteed to have elevated capabilities
                    in the initial user namespace as defined by `user_namespaces(7)`.
                    For example, it might be run in a namespace where uid 0 is mapped to
                    an ordinary uid in the initial user namespace, or it might run in a
                    Docker-style container where global uid 0 is used but its ability to
                    carry out operations that affect the whole system is restricted by
                    capabilities and system call filtering.  Tests requiring particular
                    privileges should use the `skippable` restriction to check for
                    required functionality at runtime.
                """
                ),
            ),
            Keyword(
                "needs-sudo",
                hover_text=textwrap.dedent(
                    """\
                    The test script needs to be run as a non-root user who is a member of
                    the `sudo` group, and has the ability to elevate privileges to root
                    on-demand.

                    This is useful for testing user components which should not normally
                    be run as root, in test scenarios that require configuring a system
                    service to support the test. For example, gvfs has a test-case which
                    uses sudo for privileged configuration of a Samba server, so that
                    the unprivileged gvfs service under test can communicate with that server.

                    While running a test with this restriction, `sudo(8)` will be
                    installed and configured to allow members of the `sudo` group to run
                    any command without password authentication.

                    Because the test user is a member of the `sudo` group, they will
                    also gain the ability to take any other privileged actions that are
                    controlled by membership in that group. In particular, several packages
                    install `polkit(8)` policies allowing members of group `sudo` to
                    take administrative actions with or without authentication.

                    If the test requires access to additional privileged actions, it may
                    use its access to `sudo(8)` to install additional configuration
                    files, for example configuring `polkit(8)` or `doas.conf(5)`
                    to allow running `pkexec(1)` or `doas(1)` without authentication.

                    Commands run via `sudo(8)` or another privilege-elevation tool could
                    be run with either "global root" or root in a container, depending
                    on the presence or absence of the `isolation-machine` restriction,
                    in the same way described for `needs-root`.
                """
                ),
            ),
            Keyword(
                "rw-build-tree",
                hover_text=textwrap.dedent(
                    """\
                    The test(s) needs write access to the built source tree (so it may
                    need to be copied first). Even with this restriction, the test is
                    not allowed to make any change to the built source tree which (i)
                    isn't cleaned up by `debian/rules clean`, (ii) affects the future
                    results of any test, or (iii) affects binary packages produced by
                    the build tree in the future.
                """
                ),
            ),
            Keyword(
                "skip-not-installable",
                hover_text=textwrap.dedent(
                    """\
                    This restrictions may cause a test to miss a regression due to
                    installability issues, so use with caution. If one only wants to
                    skip certain architectures, use the `Architecture` field for
                    that.

                    This test might have test dependencies that can't be fulfilled in
                    all suites or in derivatives. Therefore, when apt-get installs the
                    test dependencies, it will fail. Don't treat this as a test
                    failure, but instead treat it as if the test was skipped.
                """
                ),
            ),
            Keyword(
                "skippable",
                hover_text=textwrap.dedent(
                    """\
                    The test might need to be skipped for reasons that cannot be
                    described by an existing restriction such as isolation-machine or
                    breaks-testbed, but must instead be detected at runtime. If the
                    test exits with status 77 (a convention borrowed from Automake), it
                    will be treated as though it had been skipped. If it exits with any
                    other status, its success or failure will be derived from the exit
                    status and stderr as usual. Test authors must be careful to ensure
                    that `skippable` tests never exit with status 77 for reasons that
                    should be treated as a failure.
                """
                ),
            ),
            Keyword(
                "superficial",
                hover_text=textwrap.dedent(
                    """\
                    The test does not provide significant test coverage, so if it
                    passes, that does not necessarily mean that the package under test
                    is actually functional. If a `superficial` test fails, it will be
                    treated like any other failing test, but if it succeeds, this is
                    only a weak indication of success. Continuous integration systems
                    should treat a package where all non-superficial tests are skipped as
                    equivalent to a package where all tests are skipped.

                    For example, a C library might have a superficial test that simply
                    compiles, links and executes a "hello world" program against the
                    library under test but does not attempt to make use of the library's
                    functionality, while a Python or Perl library might have a
                    superficial test that runs `import foo` or `require Foo;` but
                    does not attempt to use the library beyond that.
                """
                ),
            ),
        ),
        synopsis_doc="Test restrictions and requirements",
        hover_text=textwrap.dedent(
            """\
            Declares some restrictions or problems with the tests defined in
            this stanza. Depending on the test environment capabilities, user
            requests, and so on, restrictions can cause tests to be skipped or
            can cause the test to be run in a different manner. Tests which
            declare unknown restrictions will be skipped. See below for the
            defined restrictions.

            Restrictions are separated by commas and/or whitespace.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Tests",
        FieldValueClass.COMMA_OR_SPACE_SEPARATED_LIST,
        synopsis_doc="List of test scripts to run",
        hover_text=textwrap.dedent(
            """\
            This field names the tests which are defined by this stanza, and map
            to executables/scripts in the test directory. All of the other
            fields in the same stanza apply to all of the named tests. Either
            this field or `Test-Command:` must be present.

            Test names are separated by comma and/or whitespace and should
            contain only characters which are legal in package names. It is
            permitted, but not encouraged, to use upper-case characters as well.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Test-Command",
        FieldValueClass.FREE_TEXT_FIELD,
        synopsis_doc="Single test command",
        hover_text=textwrap.dedent(
            """\
            If your test only contains a shell command or two, or you want to
            reuse an existing upstream test executable and just need to wrap it
            with some command like `dbus-launch` or `env`, you can use this
            field to specify the shell command directly. It will be run under
            `bash -e`. This is mutually exclusive with the `Tests:` field.

            This is also useful for running the same script under different
            interpreters and/or with different dependencies, such as
            `Test-Command: python debian/tests/mytest.py` and
            `Test-Command: python3 debian/tests/mytest.py`.
        """
        ),
    ),
    DTestsCtrlKnownField(
        "Test-Directory",
        FieldValueClass.FREE_TEXT_FIELD,  # TODO: Single path
        default_value="debian/tests",
        synopsis_doc="The directory containing the tests listed in from `Tests`",
        hover_text=textwrap.dedent(
            """\
            Replaces the path segment `debian/tests` in the filenames of the
            test programs with `path`. I. e., the tests are run by executing
            `built/source/tree/path/testname`. `path` must be a relative
            path and is interpreted starting from the root of the built source
            tree.

            This allows tests to live outside the `debian/` metadata area, so that
            they can more palatably be shared with non-Debian distributions.
        """
        ),
    ),
)


@dataclasses.dataclass(slots=True, frozen=True)
class StanzaMetadata(Mapping[str, F], Generic[F], ABC):
    stanza_type_name: str
    stanza_fields: Mapping[str, F]

    def stanza_diagnostics(
        self,
        stanza: Deb822ParagraphElement,
        stanza_position_in_file: "TEPosition",
    ) -> Iterable[Diagnostic]:
        raise NotImplementedError

    def __getitem__(self, key: str) -> F:
        key_lc = key.lower()
        key_norm = normalize_dctrl_field_name(key_lc)
        return self.stanza_fields[key_norm]

    def __len__(self) -> int:
        return len(self.stanza_fields)

    def __iter__(self):
        return iter(self.stanza_fields.keys())

    def reformat_stanza(
        self,
        effective_preference: "EffectiveFormattingPreference",
        stanza: Deb822ParagraphElement,
        stanza_range: TERange,
        formatter: FormatterCallback,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        for known_field in self.stanza_fields.values():
            kvpair = stanza.get_kvpair_element((known_field.name, 0), use_get=True)
            if kvpair is None:
                continue
            yield from known_field.reformat_field(
                effective_preference,
                stanza_range,
                kvpair,
                formatter,
                position_codec,
                lines,
            )


@dataclasses.dataclass(slots=True, frozen=True)
class Dep5StanzaMetadata(StanzaMetadata[Deb822KnownField]):
    def stanza_diagnostics(
        self,
        stanza: Deb822ParagraphElement,
        stanza_position_in_file: "TEPosition",
    ) -> Iterable[Diagnostic]:
        pass


@dataclasses.dataclass(slots=True, frozen=True)
class DctrlStanzaMetadata(StanzaMetadata[DctrlKnownField]):

    def stanza_diagnostics(
        self,
        stanza: Deb822ParagraphElement,
        stanza_position_in_file: "TEPosition",
    ) -> Iterable[Diagnostic]:
        pass


@dataclasses.dataclass(slots=True, frozen=True)
class DTestsCtrlStanzaMetadata(StanzaMetadata[DTestsCtrlKnownField]):

    def stanza_diagnostics(
        self,
        stanza: Deb822ParagraphElement,
        stanza_position_in_file: "TEPosition",
    ) -> Iterable[Diagnostic]:
        pass


class Deb822FileMetadata(Generic[S]):
    def classify_stanza(self, stanza: Deb822ParagraphElement, stanza_idx: int) -> S:
        return self.guess_stanza_classification_by_idx(stanza_idx)

    def guess_stanza_classification_by_idx(self, stanza_idx: int) -> S:
        raise NotImplementedError

    def stanza_types(self) -> Iterable[S]:
        raise NotImplementedError

    def __getitem__(self, item: str) -> S:
        raise NotImplementedError

    def file_diagnostics(
        self,
        file: Deb822FileElement,
    ) -> Iterable[Diagnostic]:
        raise NotImplementedError

    def get(self, item: str) -> Optional[S]:
        try:
            return self[item]
        except KeyError:
            return None

    def reformat(
        self,
        effective_preference: "EffectiveFormattingPreference",
        deb822_file: Deb822FileElement,
        formatter: FormatterCallback,
        _content: str,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        stanza_idx = -1
        for token_or_element in deb822_file.iter_parts():
            if isinstance(token_or_element, Deb822ParagraphElement):
                stanza_range = token_or_element.range_in_parent()
                stanza_idx += 1
                stanza_metadata = self.classify_stanza(token_or_element, stanza_idx)
                yield from stanza_metadata.reformat_stanza(
                    effective_preference,
                    token_or_element,
                    stanza_range,
                    formatter,
                    position_codec,
                    lines,
                )
            else:
                token_range = token_or_element.range_in_parent()
                yield from trim_end_of_line_whitespace(
                    position_codec,
                    lines,
                    line_range=range(
                        token_range.start_pos.line_position,
                        token_range.end_pos.line_position,
                    ),
                )


_DCTRL_SOURCE_STANZA = DctrlStanzaMetadata(
    "Source",
    SOURCE_FIELDS,
)
_DCTRL_PACKAGE_STANZA = DctrlStanzaMetadata("Package", BINARY_FIELDS)

_DEP5_HEADER_STANZA = Dep5StanzaMetadata(
    "Header",
    _DEP5_HEADER_FIELDS,
)
_DEP5_FILES_STANZA = Dep5StanzaMetadata(
    "Files",
    _DEP5_FILES_FIELDS,
)
_DEP5_LICENSE_STANZA = Dep5StanzaMetadata(
    "License",
    _DEP5_LICENSE_FIELDS,
)

_DTESTSCTRL_STANZA = DTestsCtrlStanzaMetadata("Tests", _DTESTSCTRL_FIELDS)


class Dep5FileMetadata(Deb822FileMetadata[Dep5StanzaMetadata]):
    def classify_stanza(
        self, stanza: Deb822ParagraphElement, stanza_idx: int
    ) -> Dep5StanzaMetadata:
        if stanza_idx == 0:
            return _DEP5_HEADER_STANZA
        if stanza_idx > 0:
            if "Files" in stanza:
                return _DEP5_FILES_STANZA
            return _DEP5_LICENSE_STANZA
        raise ValueError("The stanza_idx must be 0 or greater")

    def guess_stanza_classification_by_idx(self, stanza_idx: int) -> Dep5StanzaMetadata:
        if stanza_idx == 0:
            return _DEP5_HEADER_STANZA
        if stanza_idx > 0:
            return _DEP5_FILES_STANZA
        raise ValueError("The stanza_idx must be 0 or greater")

    def stanza_types(self) -> Iterable[Dep5StanzaMetadata]:
        yield _DEP5_HEADER_STANZA
        yield _DEP5_FILES_STANZA
        yield _DEP5_LICENSE_STANZA

    def __getitem__(self, item: str) -> Dep5StanzaMetadata:
        if item == "Header":
            return _DEP5_FILES_STANZA
        if item == "Files":
            return _DEP5_FILES_STANZA
        if item == "License":
            return _DEP5_LICENSE_STANZA
        raise KeyError(item)


class DctrlFileMetadata(Deb822FileMetadata[DctrlStanzaMetadata]):
    def guess_stanza_classification_by_idx(
        self, stanza_idx: int
    ) -> DctrlStanzaMetadata:
        if stanza_idx == 0:
            return _DCTRL_SOURCE_STANZA
        if stanza_idx > 0:
            return _DCTRL_PACKAGE_STANZA
        raise ValueError("The stanza_idx must be 0 or greater")

    def stanza_types(self) -> Iterable[DctrlStanzaMetadata]:
        yield _DCTRL_SOURCE_STANZA
        yield _DCTRL_PACKAGE_STANZA

    def __getitem__(self, item: str) -> DctrlStanzaMetadata:
        if item == "Source":
            return _DCTRL_SOURCE_STANZA
        if item == "Package":
            return _DCTRL_PACKAGE_STANZA
        raise KeyError(item)

    def reformat(
        self,
        effective_preference: "EffectiveFormattingPreference",
        deb822_file: Deb822FileElement,
        formatter: FormatterCallback,
        content: str,
        position_codec: LintCapablePositionCodec,
        lines: List[str],
    ) -> Iterable[TextEdit]:
        edits = list(
            super().reformat(
                effective_preference,
                deb822_file,
                formatter,
                content,
                position_codec,
                lines,
            )
        )

        if (
            not effective_preference.deb822_normalize_stanza_order
            or deb822_file.find_first_error_element() is not None
        ):
            return edits
        names = []
        for idx, stanza in enumerate(deb822_file):
            if idx < 2:
                continue
            name = stanza.get("Package")
            if name is None:
                return edits
            names.append(name)

        reordered = sorted(names)
        if names == reordered:
            return edits

        if edits:
            content = apply_text_edits(content, lines, edits)
            lines = content.splitlines(keepends=True)
            deb822_file = parse_deb822_file(
                lines,
                accept_files_with_duplicated_fields=True,
                accept_files_with_error_tokens=True,
            )

        stanzas = list(deb822_file)
        reordered_stanza = stanzas[:2] + sorted(
            stanzas[2:], key=operator.itemgetter("Package")
        )
        bits = []
        stanza_idx = 0
        for token_or_element in deb822_file.iter_parts():
            if isinstance(token_or_element, Deb822ParagraphElement):
                bits.append(reordered_stanza[stanza_idx].dump())
                stanza_idx += 1
            else:
                bits.append(token_or_element.convert_to_text())

        new_content = "".join(bits)

        return [
            TextEdit(
                Range(
                    Position(0, 0),
                    Position(len(lines) + 1, 0),
                ),
                new_content,
            )
        ]


class DTestsCtrlFileMetadata(Deb822FileMetadata[DctrlStanzaMetadata]):
    def guess_stanza_classification_by_idx(self, stanza_idx: int) -> S:
        if stanza_idx >= 0:
            return _DTESTSCTRL_STANZA
        raise ValueError("The stanza_idx must be 0 or greater")

    def stanza_types(self) -> Iterable[S]:
        yield _DTESTSCTRL_STANZA

    def __getitem__(self, item: str) -> S:
        if item == "Tests":
            return _DTESTSCTRL_STANZA
        raise KeyError(item)
