import dataclasses
import os.path
import re
import textwrap
from itertools import chain
from typing import (
    Union,
    Sequence,
    Tuple,
    Optional,
    Mapping,
    List,
    Dict,
    Iterable,
)

from debputy.analysis.analysis_util import flatten_ppfs
from debputy.analysis.debian_dir import resolve_debhelper_config_files
from debputy.dh.dh_assistant import extract_dh_compat_level
from debputy.linting.lint_util import LintState
from debputy.lsp.apt_cache import PackageLookup
from debputy.lsp.debputy_ls import DebputyLanguageServer
from debputy.lsp.diagnostics import DiagnosticData
from debputy.lsp.lsp_debian_control_reference_data import (
    DctrlKnownField,
    BINARY_FIELDS,
    SOURCE_FIELDS,
    DctrlFileMetadata,
    package_name_to_section,
    all_package_relationship_fields,
    extract_first_value_and_position,
    all_source_relationship_fields,
)
from debputy.lsp.lsp_features import (
    lint_diagnostics,
    lsp_completer,
    lsp_hover,
    lsp_standard_handler,
    lsp_folding_ranges,
    lsp_semantic_tokens_full,
    lsp_will_save_wait_until,
    lsp_format_document,
    LanguageDispatch,
    lsp_text_doc_inlay_hints,
)
from debputy.lsp.lsp_generic_deb822 import (
    deb822_completer,
    deb822_hover,
    deb822_folding_ranges,
    deb822_semantic_tokens_full,
    deb822_token_iter,
    deb822_format_file,
)
from debputy.lsp.quickfixes import (
    propose_remove_line_quick_fix,
    range_compatible_with_remove_line_fix,
    propose_correct_text_quick_fix,
    propose_insert_text_on_line_after_diagnostic_quick_fix,
    propose_remove_range_quick_fix,
)
from debputy.lsp.spellchecking import default_spellchecker
from debputy.lsp.text_util import (
    normalize_dctrl_field_name,
    LintCapablePositionCodec,
    te_range_to_lsp,
    te_position_to_lsp,
)
from debputy.lsp.vendoring._deb822_repro import (
    Deb822FileElement,
    Deb822ParagraphElement,
)
from debputy.lsp.vendoring._deb822_repro.parsing import (
    Deb822KeyValuePairElement,
    LIST_SPACE_SEPARATED_INTERPRETATION,
)
from debputy.lsprotocol.types import (
    DiagnosticSeverity,
    Range,
    Diagnostic,
    Position,
    FoldingRange,
    FoldingRangeParams,
    CompletionItem,
    CompletionList,
    CompletionParams,
    DiagnosticRelatedInformation,
    Location,
    HoverParams,
    Hover,
    TEXT_DOCUMENT_CODE_ACTION,
    SemanticTokens,
    SemanticTokensParams,
    WillSaveTextDocumentParams,
    TextEdit,
    DocumentFormattingParams,
    InlayHintParams,
    InlayHint,
    InlayHintLabelPart,
)
from debputy.packager_provided_files import (
    PackagerProvidedFile,
    detect_all_packager_provided_files,
)
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.util import detect_possible_typo, PKGNAME_REGEX, _info

try:
    from debputy.lsp.vendoring._deb822_repro.locatable import (
        Position as TEPosition,
        Range as TERange,
        START_POSITION,
    )

    from pygls.workspace import TextDocument
except ImportError:
    pass


_LANGUAGE_IDS = [
    LanguageDispatch.from_language_id("debian/control"),
    # emacs's name
    LanguageDispatch.from_language_id("debian-control"),
    # vim's name
    LanguageDispatch.from_language_id("debcontrol"),
]


@dataclasses.dataclass(slots=True, frozen=True)
class SubstvarMetadata:
    name: str
    defined_by: str
    dh_sequence: Optional[str]
    source: Optional[str]
    description: str

    def render_metadata_fields(self) -> str:
        def_by = f"Defined by: {self.defined_by}"
        dh_seq = (
            f"DH Sequence: {self.dh_sequence}" if self.dh_sequence is not None else None
        )
        source = f"Source: {self.source}" if self.source is not None else None
        return "\n".join(filter(None, (def_by, dh_seq, source)))


def relationship_substvar_for_field(substvar: str) -> Optional[str]:
    relationship_fields = all_package_relationship_fields()
    try:
        col_idx = substvar.rindex(":")
    except ValueError:
        return None
    return relationship_fields.get(substvar[col_idx + 1 : -1].lower())


def _substvars_metadata(*args: SubstvarMetadata) -> Mapping[str, SubstvarMetadata]:
    r = {s.name: s for s in args}
    assert len(r) == len(args)
    return r


_SUBSTVAR_RE = re.compile(r"[$][{][a-zA-Z0-9][a-zA-Z0-9-:]*[}]")
_SUBSTVARS_DOC = _substvars_metadata(
    SubstvarMetadata(
        "${}",
        "`dpkg-gencontrol`",
        "(default)",
        "<https://manpages.debian.org/deb-substvars.5>",
        textwrap.dedent(
            """\
            This is a substvar for a literal `$`. This form will never recurse
            into another substvar. As an example, `${}{binary:Version}` will result
            literal `${binary:Version}` (which will not be replaced).
        """
        ),
    ),
    SubstvarMetadata(
        "${binary:Version}",
        "`dpkg-gencontrol`",
        "(default)",
        "<https://manpages.debian.org/deb-substvars.5>",
        textwrap.dedent(
            """\
            The version of the current binary package including binNMU version.

            Often used with `Depends: dep (= ${binary:Version})` relations
            where:

             * The `dep` package is from the same source (listed in the same
               `debian/control` file)
             * The current package and `dep` are both `arch:any` (or both `arch:all`)
               packages.
    """
        ),
    ),
    SubstvarMetadata(
        "${source:Version}",
        "`dpkg-gencontrol`",
        "(default)",
        "<https://manpages.debian.org/deb-substvars.5>",
        textwrap.dedent(
            """\
            The version of the current source package excluding binNMU version.

            Often used with `Depends: dep (= ${source:Version})` relations
            where:

             * The `dep` package is from the same source (listed in the same
               `debian/control` file)
             * The `dep` is `arch:all`.
    """
        ),
    ),
    SubstvarMetadata(
        "${misc:Depends}",
        "`debhelper`",
        "(default)",
        "<https://manpages.debian.org/debhelper.7>",
        textwrap.dedent(
            """\
            Some debhelper commands may make the generated package need to depend on some other packages.
            For example, if you use `dh_installdebconf(1)`, your package will generally need to depend on
            debconf. Or if you use `dh_installxfonts(1)`, your package will generally need to depend on a
            particular version of xutils. Keeping track of these miscellaneous dependencies can be
            annoying since they are dependent on how debhelper does things, so debhelper offers a way to
            automate it.

            All commands of this type, besides documenting what dependencies may be needed on their man
            pages, will automatically generate a substvar called ${misc:Depends}. If you put that token
            into your `debian/control` file, it will be expanded to the dependencies debhelper figures
            you need.

            This is entirely independent of the standard `${shlibs:Depends}` generated by `dh_makeshlibs(1)`,
            and the `${perl:Depends}` generated by `dh_perl(1)`.
    """
        ),
    ),
    SubstvarMetadata(
        "${misc:Pre-Depends}",
        "`debhelper`",
        "(default)",
        None,
        textwrap.dedent(
            """\
            This is the moral equivalent to `${misc:Depends}` but for `Pre-Depends`.
    """
        ),
    ),
    SubstvarMetadata(
        "${perl:Depends}",
        "`dh_perl`",
        "(default)",
        "<https://manpages.debian.org/dh_perl.1>",
        textwrap.dedent(
            """\
            The dependency on perl as determined by `dh_perl`. Note this only covers the relationship
            with the Perl interpreter and not perl modules.

    """
        ),
    ),
    SubstvarMetadata(
        "${gir:Depends}",
        "`dh_girepository`",
        "gir",
        "<https://manpages.debian.org/dh_girepository.1>",
        textwrap.dedent(
            """\
            Dependencies related to GObject introspection data.
    """
        ),
    ),
    SubstvarMetadata(
        "${shlibs:Depends}",
        "`dpkg-shlibdeps` (often via `dh_shlibdeps`)",
        "(default)",
        "<https://manpages.debian.org/dpkg-shlibdeps.1>",
        textwrap.dedent(
            """\
            Dependencies related to ELF dependencies.
    """
        ),
    ),
    SubstvarMetadata(
        "${shlibs:Pre-Depends}",
        "`dpkg-shlibdeps` (often via `dh_shlibdeps`)",
        "(default)",
        "<https://manpages.debian.org/dpkg-shlibdeps.1>",
        textwrap.dedent(
            """\
            Dependencies related to ELF dependencies. The `Pre-Depends`
            version is often only seen in `Essential: yes` packages
            or packages that manually request the `Pre-Depends`
            relation via `dpkg-shlibdeps`.

            Note: This substvar only appears in `debhelper-compat (= 14)`, or
            with use of `debputy` (at an integration level, where `debputy`
            runs `dpkg-shlibdeps`), or when passing relevant options to
            `dpkg-shlibdeps`  (often via `dh_shlibdeps`) such as `-dPre-Depends`.
    """
        ),
    ),
)

_DCTRL_FILE_METADATA = DctrlFileMetadata()


lsp_standard_handler(_LANGUAGE_IDS, TEXT_DOCUMENT_CODE_ACTION)


@lsp_hover(_LANGUAGE_IDS)
def _debian_control_hover(
    ls: "DebputyLanguageServer",
    params: HoverParams,
) -> Optional[Hover]:
    return deb822_hover(ls, params, _DCTRL_FILE_METADATA, custom_handler=_custom_hover)


def _custom_hover_description(
    _ls: "DebputyLanguageServer",
    _known_field: DctrlKnownField,
    line: str,
    _word_at_position: str,
) -> Optional[Union[Hover, str]]:
    if line[0].isspace():
        return None
    try:
        col_idx = line.index(":")
    except ValueError:
        return None

    content = line[col_idx + 1 :].strip()

    # Synopsis
    return textwrap.dedent(
        f"""\
        # Package synopsis

        The synopsis functions as a phrase describing the package, not a
        complete sentence, so sentential punctuation is inappropriate: it
        does not need extra capital letters or a final period (full stop).
        It should also omit any initial indefinite or definite article
        - "a", "an", or "the". Thus for instance:

        ```
        Package: libeg0
        Description: exemplification support library
        ```

        Technically this is a noun phrase minus articles, as opposed to a
        verb phrase. A good heuristic is that it should be possible to
        substitute the package name and synopsis into this formula:

        ```
        # Generic
        The package provides {{a,an,the,some}} synopsis.

        # The current package for comparison
        The package provides {{a,an,the,some}} {content}.
        ```

        Other advice for writing synopsis:
         * Avoid using the package name. Any software would display the
           package name already and it generally does not help the user
           understand what they are looking at.
         * In many situations, the user will only see the package name
           and its synopsis. The synopsis must be able to stand alone.

        **Example renderings in various terminal UIs**:
        ```
        # apt search TERM
        package/stable,now 1.0-1 all:
           {content}

        # apt-get search TERM
        package - {content}
        ```

        ## Reference example

        An reference example for comparison: The Sphinx package
        (python3-sphinx/7.2.6-6) had the following synopsis:

        ```
        Description: documentation generator for Python projects
        ```

        In the test sentence, it would read as:

        ```
        The python3-sphinx package provides a documentation generator for Python projects.
        ```

        **Side-by-side comparison in the terminal UIs**:
        ```
        # apt search TERM
        python3-sphinx/stable,now 7.2.6-6 all:
           documentation generator for Python projects

        package/stable,now 1.0-1 all:
           {content}


        # apt-get search TERM
        package - {content}
        python3-sphinx - documentation generator for Python projects
        ```
    """
    )


def _render_package_lookup(
    package_lookup: PackageLookup,
    known_field: DctrlKnownField,
) -> str:
    name = package_lookup.name
    provider = package_lookup.package
    if package_lookup.package is None and len(package_lookup.provided_by) == 1:
        provider = package_lookup.provided_by[0]

    if provider:
        segments = [
            f"# {name} ({provider.version}, {provider.architecture}) ",
            "",
        ]

        if (
            _is_bd_field(known_field)
            and name.startswith("dh-sequence-")
            and len(name) > 12
        ):
            sequence = name[12:]
            segments.append(
                f"This build-dependency will activate the `dh` sequence called `{sequence}`."
            )
            segments.append("")

        elif (
            known_field.name == "Build-Depends"
            and name.startswith("debputy-plugin-")
            and len(name) > 15
        ):
            plugin_name = name[15:]
            segments.append(
                f"This build-dependency will activate the `debputy` plugin called `{plugin_name}`."
            )
            segments.append("")

        segments.extend(
            [
                f"Synopsis: {provider.synopsis}",
                f"Multi-Arch: {provider.multi_arch}",
                f"Section: {provider.section}",
            ]
        )
        if provider.upstream_homepage is not None:
            segments.append(f"Upstream homepage: {provider.upstream_homepage}")
        segments.append("")
        segments.append(
            "Data is from the system's APT cache, which may not match the target distribution."
        )
        return "\n".join(segments)

    segments = [
        f"# {name} [virtual]",
        "",
        "The package {name} is a virtual package provided by one of:",
    ]
    segments.extend(f" * {p.name}" for p in package_lookup.provided_by)
    segments.append("")
    segments.append(
        "Data is from the system's APT cache, which may not match the target distribution."
    )
    return "\n".join(segments)


def _disclaimer(is_empty: bool) -> str:
    if is_empty:
        return textwrap.dedent(
            """\
        The system's APT cache is empty, so it was not possible to verify that the
        package exist.
"""
        )
    return textwrap.dedent(
        """\
        The package is not known by the APT cache on this system, so there may be typo
        or the package may not be available in the version of your distribution.
"""
    )


def _render_package_by_name(
    name: str, known_field: DctrlKnownField, is_empty: bool
) -> Optional[str]:
    if _is_bd_field(known_field) and name.startswith("dh-sequence-") and len(name) > 12:
        sequence = name[12:]
        return (
            textwrap.dedent(
                f"""\
        # {name}

        This build-dependency will activate the `dh` sequence called `{sequence}`.

        """
            )
            + _disclaimer(is_empty)
        )
    if (
        known_field.name == "Build-Depends"
        and name.startswith("debputy-plugin-")
        and len(name) > 15
    ):
        plugin_name = name[15:]
        return (
            textwrap.dedent(
                f"""\
        # {name}

        This build-dependency will activate the `debputy` plugin called `{plugin_name}`.

        """
            )
            + _disclaimer(is_empty)
        )
    return (
        textwrap.dedent(
            f"""\
        # {name}

    """
        )
        + _disclaimer(is_empty)
    )


def _is_bd_field(known_field: DctrlKnownField) -> bool:
    return known_field.name in (
        "Build-Depends",
        "Build-Depends-Arch",
        "Build-Depends-Indep",
    )


def _custom_hover_relationship_field(
    ls: "DebputyLanguageServer",
    known_field: DctrlKnownField,
    _line: str,
    word_at_position: str,
) -> Optional[Union[Hover, str]]:
    apt_cache = ls.apt_cache
    state = apt_cache.state
    is_empty = False
    _info(f"Rel field: {known_field.name} - {word_at_position} - {state}")
    if "|" in word_at_position:
        return textwrap.dedent(
            f"""\
            Sorry, no hover docs for OR relations at the moment.

            The relation being matched: `{word_at_position}`

            The code is missing logic to determine which side of the OR the lookup is happening.
        """
        )
    match = next(iter(PKGNAME_REGEX.finditer(word_at_position)), None)
    if match is None:
        return
    package = match.group()
    if state == "empty-cache":
        state = "loaded"
        is_empty = True
    if state == "loaded":
        result = apt_cache.lookup(package)
        if result is None:
            return _render_package_by_name(
                package,
                known_field,
                is_empty=is_empty,
            )
        return _render_package_lookup(result, known_field)

    if state in (
        "not-loaded",
        "failed",
        "tooling-not-available",
    ):
        details = apt_cache.load_error if apt_cache.load_error else "N/A"
        return textwrap.dedent(
            f"""\
        Sorry, the APT cache data is not available due to an error or missing tool.

        Details: {details}
        """
        )

    if state == "empty-cache":
        return f"Cannot lookup {package}: APT cache data was empty"

    if state == "loading":
        return f"Cannot lookup {package}: APT cache data is still being indexed. Please try again in a moment."
    return None


_CUSTOM_FIELD_HOVER = {
    field: _custom_hover_relationship_field
    for field in chain(
        all_package_relationship_fields().values(),
        all_source_relationship_fields().values(),
    )
    if field != "Provides"
}

_CUSTOM_FIELD_HOVER["Description"] = _custom_hover_description


def _custom_hover(
    ls: "DebputyLanguageServer",
    server_position: Position,
    _current_field: Optional[str],
    word_at_position: str,
    known_field: Optional[DctrlKnownField],
    in_value: bool,
    _doc: "TextDocument",
    lines: List[str],
) -> Optional[Union[Hover, str]]:
    if not in_value:
        return None

    line_no = server_position.line
    line = lines[line_no]
    substvar_search_ref = server_position.character
    substvar = ""
    try:
        if line and line[substvar_search_ref] in ("$", "{"):
            substvar_search_ref += 2
        substvar_start = line.rindex("${", 0, substvar_search_ref)
        substvar_end = line.index("}", substvar_start)
        if server_position.character <= substvar_end:
            substvar = line[substvar_start : substvar_end + 1]
    except (ValueError, IndexError):
        pass

    if substvar == "${}" or _SUBSTVAR_RE.fullmatch(substvar):
        substvar_md = _SUBSTVARS_DOC.get(substvar)

        computed_doc = ""
        for_field = relationship_substvar_for_field(substvar)
        if for_field:
            # Leading empty line is intentional!
            computed_doc = textwrap.dedent(
                f"""
                This substvar is a relationship substvar for the field {for_field}.
                Relationship substvars are automatically added in the field they
                are named after in `debhelper-compat (= 14)` or later, or with
                `debputy` (any integration mode after 0.1.21).
            """
            )

        if substvar_md is None:
            doc = f"No documentation for {substvar}.\n"
            md_fields = ""
        else:
            doc = substvar_md.description
            md_fields = "\n" + substvar_md.render_metadata_fields()
        return f"# Substvar `{substvar}`\n\n{doc}{computed_doc}{md_fields}"

    if known_field is None:
        return None
    dispatch = _CUSTOM_FIELD_HOVER.get(known_field.name)
    if dispatch is None:
        return None
    return dispatch(ls, known_field, line, word_at_position)


@lsp_completer(_LANGUAGE_IDS)
def _debian_control_completions(
    ls: "DebputyLanguageServer",
    params: CompletionParams,
) -> Optional[Union[CompletionList, Sequence[CompletionItem]]]:
    return deb822_completer(ls, params, _DCTRL_FILE_METADATA)


@lsp_folding_ranges(_LANGUAGE_IDS)
def _debian_control_folding_ranges(
    ls: "DebputyLanguageServer",
    params: FoldingRangeParams,
) -> Optional[Sequence[FoldingRange]]:
    return deb822_folding_ranges(ls, params, _DCTRL_FILE_METADATA)


@lsp_text_doc_inlay_hints(_LANGUAGE_IDS)
def _doc_inlay_hint(
    ls: "DebputyLanguageServer",
    params: InlayHintParams,
) -> Optional[List[InlayHint]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lint_state = ls.lint_state(doc)
    deb822_file = lint_state.parsed_deb822_file_content
    if not deb822_file:
        return None
    inlay_hints = []
    stanzas = list(deb822_file)
    if len(stanzas) < 2:
        return None
    source_stanza = stanzas[0]
    source_stanza_pos = source_stanza.position_in_file()
    for stanza_no, stanza in enumerate(deb822_file):
        stanza_range = stanza.range_in_parent()
        if stanza_no < 1:
            continue
        pkg_kvpair = stanza.get_kvpair_element(("Package", 0), use_get=True)
        if pkg_kvpair is None:
            continue

        inlay_hint_pos_te = pkg_kvpair.range_in_parent().end_pos.relative_to(
            stanza_range.start_pos
        )
        inlay_hint_pos = doc.position_codec.position_to_client_units(
            lint_state.lines,
            te_position_to_lsp(inlay_hint_pos_te),
        )
        stanza_def = _DCTRL_FILE_METADATA.classify_stanza(stanza, stanza_no)
        for known_field in stanza_def.stanza_fields.values():
            if not known_field.inherits_from_source or known_field.name in stanza:
                continue

            inherited_value = source_stanza.get(known_field.name)
            if inherited_value is not None:
                kvpair = source_stanza.get_kvpair_element(known_field.name)
                value_range_te = kvpair.range_in_parent().relative_to(source_stanza_pos)
                value_range = doc.position_codec.range_to_client_units(
                    lint_state.lines,
                    te_range_to_lsp(value_range_te),
                )
                inlay_hints.append(
                    InlayHint(
                        inlay_hint_pos,
                        [
                            InlayHintLabelPart(
                                f"{known_field.name}: {inherited_value}\n",
                                tooltip="Inherited from Source stanza",
                                location=Location(
                                    params.text_document.uri,
                                    value_range,
                                ),
                            ),
                        ],
                    )
                )

    return inlay_hints


def _paragraph_representation_field(
    paragraph: Deb822ParagraphElement,
) -> Deb822KeyValuePairElement:
    return next(iter(paragraph.iter_parts_of_type(Deb822KeyValuePairElement)))


def _source_package_checks(
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    lint_state: LintState,
    diagnostics: List[Diagnostic],
) -> None:
    vcs_fields = {}
    for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
        name = normalize_dctrl_field_name(kvpair.field_name.lower())
        if (
            not name.startswith("vcs-")
            or name == "vcs-browser"
            or name not in SOURCE_FIELDS
        ):
            continue
        vcs_fields[name] = kvpair

    if len(vcs_fields) < 2:
        return
    for kvpair in vcs_fields.values():
        kvpair_range_server_units = te_range_to_lsp(
            kvpair.range_in_parent().relative_to(stanza_position)
        )
        diagnostics.append(
            Diagnostic(
                lint_state.position_codec.range_to_client_units(
                    lint_state.lines, kvpair_range_server_units
                ),
                f'Multiple Version Control fields defined ("{kvpair.field_name}")',
                severity=DiagnosticSeverity.Warning,
                source="debputy",
                data=DiagnosticData(
                    quickfixes=[
                        propose_remove_range_quick_fix(
                            proposed_title=f'Remove "{kvpair.field_name}"'
                        )
                    ]
                ),
            )
        )


def _binary_package_checks(
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    source_stanza: Deb822ParagraphElement,
    representation_field_range: Range,
    lint_state: LintState,
    diagnostics: List[Diagnostic],
) -> None:
    package_name = stanza.get("Package", "")
    source_section = source_stanza.get("Section")
    section_kvpair = stanza.get_kvpair_element(("Section", 0), use_get=True)
    section: Optional[str] = None
    if section_kvpair is not None:
        section, section_range = extract_first_value_and_position(
            section_kvpair,
            stanza_position,
            lint_state,
        )
    else:
        section_range = representation_field_range
    effective_section = section or source_section or "unknown"
    package_type = stanza.get("Package-Type", "")
    component_prefix = ""
    if "/" in effective_section:
        component_prefix, effective_section = effective_section.split("/", maxsplit=1)
        component_prefix += "/"

    if package_name.endswith("-udeb") or package_type == "udeb":
        if package_type != "udeb":
            package_type_kvpair = stanza.get_kvpair_element(
                "Package-Type", use_get=True
            )
            package_type_range = None
            if package_type_kvpair is not None:
                _, package_type_range = extract_first_value_and_position(
                    package_type_kvpair,
                    stanza_position,
                    lint_state,
                )
            if package_type_range is None:
                package_type_range = representation_field_range
            diagnostics.append(
                Diagnostic(
                    package_type_range,
                    'The Package-Type should be "udeb" given the package name',
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                )
            )
        guessed_section = "debian-installer"
        section_diagnostic_rationale = " since it is an udeb"
    else:
        guessed_section = package_name_to_section(package_name)
        section_diagnostic_rationale = " based on the package name"
    if guessed_section is not None and guessed_section != effective_section:
        if section is not None:
            quickfix_data = [
                propose_correct_text_quick_fix(f"{component_prefix}{guessed_section}")
            ]
        else:
            quickfix_data = [
                propose_insert_text_on_line_after_diagnostic_quick_fix(
                    f"Section: {component_prefix}{guessed_section}\n"
                )
            ]
        assert section_range is not None  # mypy hint
        diagnostics.append(
            Diagnostic(
                section_range,
                f'The Section should be "{component_prefix}{guessed_section}"{section_diagnostic_rationale}',
                severity=DiagnosticSeverity.Warning,
                source="debputy",
                data=DiagnosticData(quickfixes=quickfix_data),
            )
        )


def _diagnostics_for_paragraph(
    deb822_file: Deb822FileElement,
    stanza: Deb822ParagraphElement,
    stanza_position: "TEPosition",
    source_stanza: Deb822ParagraphElement,
    known_fields: Mapping[str, DctrlKnownField],
    other_known_fields: Mapping[str, DctrlKnownField],
    is_binary_paragraph: bool,
    doc_reference: str,
    lint_state: LintState,
    diagnostics: List[Diagnostic],
) -> None:
    representation_field = _paragraph_representation_field(stanza)
    representation_field_range = representation_field.range_in_parent().relative_to(
        stanza_position
    )
    representation_field_range = lint_state.position_codec.range_to_client_units(
        lint_state.lines,
        te_range_to_lsp(representation_field_range),
    )
    for known_field in known_fields.values():
        if known_field.name in stanza:
            continue

        diagnostics.extend(
            known_field.field_omitted_diagnostics(
                deb822_file,
                representation_field_range,
                stanza,
                stanza_position,
                source_stanza,
                lint_state,
            )
        )

    if is_binary_paragraph:
        _binary_package_checks(
            stanza,
            stanza_position,
            source_stanza,
            representation_field_range,
            lint_state,
            diagnostics,
        )
    else:
        _source_package_checks(
            stanza,
            stanza_position,
            lint_state,
            diagnostics,
        )

    seen_fields: Dict[str, Tuple[str, str, Range, List[Range]]] = {}

    for kvpair in stanza.iter_parts_of_type(Deb822KeyValuePairElement):
        field_name_token = kvpair.field_token
        field_name = field_name_token.text
        field_name_lc = field_name.lower()
        normalized_field_name_lc = normalize_dctrl_field_name(field_name_lc)
        known_field = known_fields.get(normalized_field_name_lc)
        field_value = stanza[field_name]
        kvpair_range_te = kvpair.range_in_parent().relative_to(stanza_position)
        field_range_te = kvpair.field_token.range_in_parent().relative_to(
            kvpair_range_te.start_pos
        )
        field_position_te = field_range_te.start_pos
        field_range_server_units = te_range_to_lsp(field_range_te)
        field_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            field_range_server_units,
        )
        field_name_typo_detected = False
        existing_field_range = seen_fields.get(normalized_field_name_lc)
        if existing_field_range is not None:
            existing_field_range[3].append(field_range)
        else:
            normalized_field_name = normalize_dctrl_field_name(field_name)
            seen_fields[field_name_lc] = (
                field_name,
                normalized_field_name,
                field_range,
                [],
            )

        if known_field is None:
            candidates = detect_possible_typo(normalized_field_name_lc, known_fields)
            if candidates:
                known_field = known_fields[candidates[0]]
                token_range_server_units = te_range_to_lsp(
                    TERange.from_position_and_size(
                        field_position_te, kvpair.field_token.size()
                    )
                )
                field_range = lint_state.position_codec.range_to_client_units(
                    lint_state.lines,
                    token_range_server_units,
                )
                field_name_typo_detected = True
                diagnostics.append(
                    Diagnostic(
                        field_range,
                        f'The "{field_name}" looks like a typo of "{known_field.name}".',
                        severity=DiagnosticSeverity.Warning,
                        source="debputy",
                        data=DiagnosticData(
                            quickfixes=[
                                propose_correct_text_quick_fix(known_fields[m].name)
                                for m in candidates
                            ]
                        ),
                    )
                )
        if known_field is None:
            known_else_where = other_known_fields.get(normalized_field_name_lc)
            if known_else_where is not None:
                intended_usage = "Source" if is_binary_paragraph else "Package"
                diagnostics.append(
                    Diagnostic(
                        field_range,
                        f'The {field_name} is defined for use in the "{intended_usage}" stanza.'
                        f" Please move it to the right place or remove it",
                        severity=DiagnosticSeverity.Error,
                        source="debputy",
                    )
                )
            continue

        if field_value.strip() == "":
            diagnostics.append(
                Diagnostic(
                    field_range,
                    f"The {field_name} has no value. Either provide a value or remove it.",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                )
            )
            continue
        diagnostics.extend(
            known_field.field_diagnostics(
                deb822_file,
                kvpair,
                stanza,
                stanza_position,
                kvpair_range_te,
                lint_state,
                field_name_typo_reported=field_name_typo_detected,
            )
        )
        if known_field.spellcheck_value:
            words = kvpair.interpret_as(LIST_SPACE_SEPARATED_INTERPRETATION)
            spell_checker = default_spellchecker()
            value_position = kvpair.value_element.position_in_parent().relative_to(
                field_position_te
            )
            for word_ref in words.iter_value_references():
                token = word_ref.value
                for word, pos, endpos in spell_checker.iter_words(token):
                    corrections = spell_checker.provide_corrections_for(word)
                    if not corrections:
                        continue
                    word_loc = word_ref.locatable
                    word_pos_te = word_loc.position_in_parent().relative_to(
                        value_position
                    )
                    if pos:
                        word_pos_te = TEPosition(0, pos).relative_to(word_pos_te)
                    word_range_te = TERange(
                        START_POSITION,
                        TEPosition(0, endpos - pos),
                    )
                    word_range_server_units = te_range_to_lsp(
                        TERange.from_position_and_size(word_pos_te, word_range_te)
                    )
                    word_range = lint_state.position_codec.range_to_client_units(
                        lint_state.lines,
                        word_range_server_units,
                    )
                    diagnostics.append(
                        Diagnostic(
                            word_range,
                            f'Spelling "{word}"',
                            severity=DiagnosticSeverity.Hint,
                            source="debputy",
                            data=DiagnosticData(
                                lint_severity="spelling",
                                quickfixes=[
                                    propose_correct_text_quick_fix(c)
                                    for c in corrections
                                ],
                            ),
                        )
                    )
        source_value = source_stanza.get(field_name)
        if known_field.warn_if_default and field_value == known_field.default_value:
            diagnostics.append(
                Diagnostic(
                    field_range,
                    f"The {field_name} is redundant as it is set to the default value and the field should only be"
                    " used in exceptional cases.",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                )
            )

        if known_field.inherits_from_source and field_value == source_value:
            if range_compatible_with_remove_line_fix(field_range):
                fix_data = propose_remove_line_quick_fix()
            else:
                fix_data = None
            diagnostics.append(
                Diagnostic(
                    field_range,
                    f"The field {field_name} duplicates the value from the Source stanza.",
                    severity=DiagnosticSeverity.Information,
                    source="debputy",
                    data=DiagnosticData(quickfixes=fix_data),
                )
            )
    for (
        field_name,
        normalized_field_name,
        field_range,
        duplicates,
    ) in seen_fields.values():
        if not duplicates:
            continue
        related_information = [
            DiagnosticRelatedInformation(
                location=Location(doc_reference, field_range),
                message=f"First definition of {field_name}",
            )
        ]
        related_information.extend(
            DiagnosticRelatedInformation(
                location=Location(doc_reference, r),
                message=f"Duplicate of {field_name}",
            )
            for r in duplicates
        )
        for dup_range in duplicates:
            diagnostics.append(
                Diagnostic(
                    dup_range,
                    f"The {normalized_field_name} field name was used multiple times in this stanza."
                    f" Please ensure the field is only used once per stanza. Note that {normalized_field_name} and"
                    f" X[BCS]-{normalized_field_name} are considered the same field.",
                    severity=DiagnosticSeverity.Error,
                    source="debputy",
                    related_information=related_information,
                )
            )


def _scan_for_syntax_errors_and_token_level_diagnostics(
    deb822_file: Deb822FileElement,
    position_codec: LintCapablePositionCodec,
    lines: List[str],
    diagnostics: List[Diagnostic],
) -> int:
    first_error = len(lines) + 1
    spell_checker = default_spellchecker()
    for (
        token,
        start_line,
        start_offset,
        end_line,
        end_offset,
    ) in deb822_token_iter(deb822_file.iter_tokens()):
        if token.is_error:
            first_error = min(first_error, start_line)
            start_pos = Position(
                start_line,
                start_offset,
            )
            end_pos = Position(
                end_line,
                end_offset,
            )
            token_range = position_codec.range_to_client_units(
                lines, Range(start_pos, end_pos)
            )
            diagnostics.append(
                Diagnostic(
                    token_range,
                    "Syntax error",
                    severity=DiagnosticSeverity.Error,
                    source="debputy (python-debian parser)",
                )
            )
        elif token.is_comment:
            for word, col_pos, end_col_pos in spell_checker.iter_words(token.text):
                corrections = spell_checker.provide_corrections_for(word)
                if not corrections:
                    continue
                start_pos = Position(
                    start_line,
                    col_pos,
                )
                end_pos = Position(
                    start_line,
                    end_col_pos,
                )
                word_range = position_codec.range_to_client_units(
                    lines, Range(start_pos, end_pos)
                )
                diagnostics.append(
                    Diagnostic(
                        word_range,
                        f'Spelling "{word}"',
                        severity=DiagnosticSeverity.Hint,
                        source="debputy",
                        data=DiagnosticData(
                            lint_severity="spelling",
                            quickfixes=[
                                propose_correct_text_quick_fix(c) for c in corrections
                            ],
                        ),
                    )
                )
    return first_error


@lint_diagnostics(_LANGUAGE_IDS)
def _lint_debian_control(
    lint_state: LintState,
) -> Optional[List[Diagnostic]]:
    lines = lint_state.lines
    position_codec = lint_state.position_codec
    doc_reference = lint_state.doc_uri
    diagnostics = []
    deb822_file = lint_state.parsed_deb822_file_content

    first_error = _scan_for_syntax_errors_and_token_level_diagnostics(
        deb822_file,
        position_codec,
        lines,
        diagnostics,
    )

    paragraphs = list(deb822_file)
    source_paragraph = paragraphs[0] if paragraphs else None
    binary_stanzas_w_pos = []

    for paragraph_no, paragraph in enumerate(paragraphs, start=1):
        paragraph_pos = paragraph.position_in_file()
        if paragraph_pos.line_position >= first_error:
            break
        is_binary_paragraph = paragraph_no != 1
        if is_binary_paragraph:
            known_fields = BINARY_FIELDS
            other_known_fields = SOURCE_FIELDS
            binary_stanzas_w_pos.append((paragraph, paragraph_pos))
        else:
            known_fields = SOURCE_FIELDS
            other_known_fields = BINARY_FIELDS
        _diagnostics_for_paragraph(
            deb822_file,
            paragraph,
            paragraph_pos,
            source_paragraph,
            known_fields,
            other_known_fields,
            is_binary_paragraph,
            doc_reference,
            lint_state,
            diagnostics,
        )

    _detect_misspelled_packaging_files(
        lint_state,
        binary_stanzas_w_pos,
        diagnostics,
    )

    return diagnostics


def _package_range_of_stanza(
    lint_state: LintState,
    binary_stanzas: List[Tuple[Deb822ParagraphElement, TEPosition]],
) -> Iterable[Tuple[str, Optional[str], Range]]:
    for stanza, stanza_position in binary_stanzas:
        kvpair = stanza.get_kvpair_element(("Package", 0), use_get=True)
        if kvpair is None:
            continue
        representation_field_range = kvpair.range_in_parent().relative_to(
            stanza_position
        )
        representation_field_range = lint_state.position_codec.range_to_client_units(
            lint_state.lines,
            te_range_to_lsp(representation_field_range),
        )
        yield stanza["Package"], stanza.get("Architecture"), representation_field_range


def _packaging_files(
    lint_state: LintState,
) -> Iterable[PackagerProvidedFile]:
    source_root = lint_state.source_root
    debian_dir = lint_state.debian_dir
    binary_packages = lint_state.binary_packages
    if (
        source_root is None
        or not source_root.has_fs_path
        or debian_dir is None
        or binary_packages is None
    ):
        return

    dh_sequencer_data = lint_state.dh_sequencer_data
    dh_sequences = dh_sequencer_data.sequences
    is_debputy_package = (
        "debputy" in dh_sequences
        or "zz-debputy" in dh_sequences
        or "zz_debputy" in dh_sequences
        or "zz-debputy-rrr" in dh_sequences
    )
    feature_set = lint_state.plugin_feature_set
    known_packaging_files = feature_set.known_packaging_files
    static_packaging_files = {
        kpf.detection_value: kpf
        for kpf in known_packaging_files.values()
        if kpf.detection_method == "path"
    }
    ignored_path = set(static_packaging_files)

    if is_debputy_package:
        all_debputy_ppfs = list(
            flatten_ppfs(
                detect_all_packager_provided_files(
                    feature_set.packager_provided_files,
                    debian_dir,
                    binary_packages,
                    allow_fuzzy_matches=True,
                    detect_typos=True,
                    ignore_paths=ignored_path,
                )
            )
        )
        for ppf in all_debputy_ppfs:
            if ppf.path.path in ignored_path:
                continue
            ignored_path.add(ppf.path.path)
            yield ppf

    # FIXME: This should read the editor data, but dh_assistant does not support that.
    dh_compat_level, _ = extract_dh_compat_level(cwd=source_root.fs_path)
    if dh_compat_level is not None:
        debputy_plugin_metadata = plugin_metadata_for_debputys_own_plugin()
        dh_pkgfile_docs = {
            kpf.detection_value: kpf
            for kpf in known_packaging_files.values()
            if kpf.detection_method == "dh.pkgfile"
        }
        (
            all_dh_ppfs,
            _,
            _,
        ) = resolve_debhelper_config_files(
            debian_dir,
            binary_packages,
            debputy_plugin_metadata,
            dh_pkgfile_docs,
            dh_sequences,
            dh_compat_level,
            saw_dh=dh_sequencer_data.uses_dh_sequencer,
            ignore_paths=ignored_path,
        )
        for ppf in all_dh_ppfs:
            if ppf.path.path in ignored_path:
                continue
            ignored_path.add(ppf.path.path)
            yield ppf


def _detect_misspelled_packaging_files(
    lint_state: LintState,
    binary_stanzas_w_pos: List[Tuple[Deb822ParagraphElement, TEPosition]],
    diagnostics: List[Diagnostic],
) -> None:
    stanza_ranges = {
        p: (a, r)
        for p, a, r in _package_range_of_stanza(lint_state, binary_stanzas_w_pos)
    }
    for ppf in _packaging_files(lint_state):
        binary_package = ppf.package_name
        explicit_package = ppf.uses_explicit_package_name
        name_segment = ppf.name_segment is not None
        stem = ppf.definition.stem
        if binary_package is None or stem is None:
            continue
        res = stanza_ranges.get(binary_package)
        if res is None:
            continue
        declared_arch, diag_range = res
        if diag_range is None:
            continue
        path = ppf.path.path
        likely_typo_of = ppf.expected_path
        arch_restriction = ppf.architecture_restriction
        if likely_typo_of is not None:
            # Handles arch_restriction == 'all' at the same time due to how
            # the `likely-typo-of` is created
            diagnostics.append(
                Diagnostic(
                    diag_range,
                    f'The file "{path}" is likely a typo of "{likely_typo_of}"',
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        report_for_related_file=path,
                    ),
                )
            )
            continue
        if declared_arch == "all" and arch_restriction is not None:
            diagnostics.append(
                Diagnostic(
                    diag_range,
                    f'The file "{path}" has an architecture restriction but is for an `arch:all` package, so'
                    f" the restriction does not make sense.",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        report_for_related_file=path,
                    ),
                )
            )
        elif arch_restriction == "all":
            diagnostics.append(
                Diagnostic(
                    diag_range,
                    f'The file "{path}" has an architecture restriction of `all` rather than a real architecture',
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        report_for_related_file=path,
                    ),
                )
            )

        if not ppf.definition.has_active_command:
            diagnostics.append(
                Diagnostic(
                    diag_range,
                    f"The file {path} is related to a command that is not active in the dh sequence"
                    " with the current addons",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        report_for_related_file=path,
                    ),
                )
            )
            continue

        if not explicit_package and name_segment is not None:
            basename = os.path.basename(path)
            if basename == ppf.definition.stem:
                continue
            alt_name = f"{binary_package}.{stem}"
            if arch_restriction is not None:
                alt_name = f"{alt_name}.{arch_restriction}"
            if ppf.definition.allow_name_segment:
                or_alt_name = f' (or maybe "debian/{binary_package}.{basename}")'
            else:
                or_alt_name = ""
            diagnostics.append(
                Diagnostic(
                    diag_range,
                    f'Possible typo in "{path}". Consider renaming the file to "debian/{alt_name}"'
                    f"{or_alt_name} if it is intended for {binary_package}",
                    severity=DiagnosticSeverity.Warning,
                    source="debputy",
                    data=DiagnosticData(
                        report_for_related_file=path,
                    ),
                )
            )


@lsp_will_save_wait_until(_LANGUAGE_IDS)
def _debian_control_on_save_formatting(
    ls: "DebputyLanguageServer",
    params: WillSaveTextDocumentParams,
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lint_state = ls.lint_state(doc)
    return _reformat_debian_control(lint_state)


def _reformat_debian_control(
    lint_state: LintState,
) -> Optional[Sequence[TextEdit]]:
    return deb822_format_file(lint_state, _DCTRL_FILE_METADATA)


@lsp_format_document(_LANGUAGE_IDS)
def _debian_control_on_save_formatting(
    ls: "DebputyLanguageServer",
    params: DocumentFormattingParams,
) -> Optional[Sequence[TextEdit]]:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    lint_state = ls.lint_state(doc)
    return deb822_format_file(lint_state, _DCTRL_FILE_METADATA)


@lsp_semantic_tokens_full(_LANGUAGE_IDS)
def _debian_control_semantic_tokens_full(
    ls: "DebputyLanguageServer",
    request: SemanticTokensParams,
) -> Optional[SemanticTokens]:
    return deb822_semantic_tokens_full(
        ls,
        request,
        _DCTRL_FILE_METADATA,
    )
