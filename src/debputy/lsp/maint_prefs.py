import dataclasses
import functools
import os.path
import re
import textwrap
from typing import (
    Type,
    TypeVar,
    Generic,
    Optional,
    List,
    Union,
    Callable,
    Mapping,
    Self,
    Dict,
    Iterable,
    Any,
    Tuple,
)

from debputy.lsp.lsp_reference_keyword import ALL_PUBLIC_NAMED_STYLES
from debputy.lsp.vendoring._deb822_repro.types import FormatterCallback
from debputy.lsp.vendoring.wrap_and_sort import wrap_and_sort_formatter
from debputy.packages import SourcePackage
from debputy.util import _error
from debputy.yaml import MANIFEST_YAML
from debputy.yaml.compat import CommentedMap

PT = TypeVar("PT", bool, str, int)


BUILTIN_STYLES = os.path.join(os.path.dirname(__file__), "maint-preferences.yaml")

_NORMALISE_FIELD_CONTENT_KEY = ["deb822", "normalize-field-content"]
_UPLOADER_SPLIT_RE = re.compile(r"(?<=>)\s*,")

_WAS_OPTIONS = {
    "-a": ("deb822_always_wrap", True),
    "--always-wrap": ("deb822_always_wrap", True),
    "-s": ("deb822_short_indent", True),
    "--short-indent": ("deb822_short_indent", True),
    "-t": ("deb822_trailing_separator", True),
    "--trailing-separator": ("deb822_trailing_separator", True),
    # Noise option for us; we do not accept `--no-keep-first` though
    "-k": (None, True),
    "--keep-first": (None, True),
    "--no-keep-first": ("DISABLE_NORMALIZE_STANZA_ORDER", True),
    "-b": ("deb822_normalize_stanza_order", True),
    "--sort-binary-packages": ("deb822_normalize_stanza_order", True),
}

_WAS_DEFAULTS = {
    "deb822_always_wrap": False,
    "deb822_short_indent": False,
    "deb822_trailing_separator": False,
    "deb822_normalize_stanza_order": False,
    "deb822_normalize_field_content": True,
}


@dataclasses.dataclass(slots=True, frozen=True, kw_only=True)
class PreferenceOption(Generic[PT]):
    key: Union[str, List[str]]
    expected_type: Union[Type[PT], Callable[[Any], Optional[str]]]
    description: str
    default_value: Optional[Union[PT, Callable[[CommentedMap], Optional[PT]]]] = None

    @property
    def name(self) -> str:
        if isinstance(self.key, str):
            return self.key
        return ".".join(self.key)

    @property
    def attribute_name(self) -> str:
        return self.name.replace("-", "_").replace(".", "_")

    def extract_value(
        self,
        filename: str,
        key: str,
        data: CommentedMap,
    ) -> Optional[PT]:
        v = data.mlget(self.key, list_ok=True)
        if v is None:
            default_value = self.default_value
            if callable(default_value):
                return default_value(data)
            return default_value
        val_issue: Optional[str] = None
        expected_type = self.expected_type
        if not isinstance(expected_type, type) and callable(self.expected_type):
            val_issue = self.expected_type(v)
        elif not isinstance(v, self.expected_type):
            val_issue = f"It should have been a {self.expected_type} but it was not"

        if val_issue is None:
            return v
        raise ValueError(
            f'The value "{self.name}" for key {key} in file "{filename}" was incorrect: {val_issue}'
        )


def _is_packaging_team_default(m: CommentedMap) -> bool:
    v = m.get("canonical-name")
    if not isinstance(v, str):
        return False
    v = v.lower()
    return v.endswith((" maintainer", " maintainers", " team"))


def _false_when_formatting_content(m: CommentedMap) -> Optional[bool]:
    return m.mlget(_NORMALISE_FIELD_CONTENT_KEY, list_ok=True, default=False) is True


MAINT_OPTIONS: List[PreferenceOption] = [
    PreferenceOption(
        key="canonical-name",
        expected_type=str,
        description=textwrap.dedent(
            """\
        Canonical spelling/case of the maintainer name.

        The `debputy` linter will emit a diagnostic if the name is not spelled exactly as provided here.
        Can be useful to ensure your name is updated after a change of name.
        """
        ),
    ),
    PreferenceOption(
        key="is-packaging-team",
        expected_type=bool,
        default_value=_is_packaging_team_default,
        description=textwrap.dedent(
            """\
    Whether this entry is for a packaging team

    This affects how styles are applied when multiple maintainers (`Maintainer` + `Uploaders`) are listed
    in `debian/control`. For package teams, the team preference prevails when the team is in the `Maintainer`
    field. For non-packaging teams, generally the rules do not apply as soon as there are co-maintainers.

    The default is derived from the canonical name. If said name ends with phrases like "Team" or "Maintainer"
    then the email is assumed to be for a team by default.
    """
        ),
    ),
    PreferenceOption(
        key="formatting",
        expected_type=lambda x: (
            None
            if isinstance(x, EffectiveFormattingPreference)
            else "It should have been a EffectiveFormattingPreference but it was not"
        ),
        default_value=None,
        description=textwrap.dedent(
            """\
    The formatting preference of the maintainer. Can either be a string for a named style or an inline
    style.
    """
        ),
    ),
]

FORMATTING_OPTIONS = [
    PreferenceOption(
        key=["deb822", "short-indent"],
        expected_type=bool,
        description=textwrap.dedent(
            """\
    Whether to use "short" indents for relationship fields (such as `Depends`).

    This roughly corresponds to `wrap-and-sort`'s `-s` option.

    **Example**:

    When `true`, the following:
    ```
    Depends: foo,
             bar
    ```

    would be reformatted as:

    ```
    Depends:
     foo,
     bar
    ```

    (Assuming `formatting.deb822.short-indent` is `false`)

    Note that defaults to `false` *if* (and only if) other formatting options will trigger reformat of
    the field and this option has not been set. Setting this option can trigger reformatting of fields
    that span multiple lines.

    Additionally, this only triggers when a field is being reformatted. Generally that requires
    another option such as `formatting.deb822.normalize-field-content` for that to happen.
    """
        ),
    ),
    PreferenceOption(
        key=["deb822", "always-wrap"],
        expected_type=bool,
        description=textwrap.dedent(
            """\
    Whether to always wrap fields (such as `Depends`).

    This roughly corresponds to `wrap-and-sort`'s `-a` option.

    **Example**:

    When `true`, the following:
    ```
    Depends: foo, bar
    ```

    would be reformatted as:

    ```
    Depends: foo,
             bar
    ```

    (Assuming `formatting.deb822.short-indent` is `false`)

    This option only applies to fields where formatting is a pure style preference. As an
    example, `Description` (`debian/control`) or `License` (`debian/copyright`) will not
    be affected by this option.

    Note: When `true`, this option overrules `formatting.deb822.max-line-length` when they interact. 
    Additionally, this only triggers when a field is being reformatted. Generally that requires
    another option such as `formatting.deb822.normalize-field-content` for that to happen.
    """
        ),
    ),
    PreferenceOption(
        key=["deb822", "trailing-separator"],
        expected_type=bool,
        default_value=False,
        description=textwrap.dedent(
            """\
    Whether to always end relationship fields (such as `Depends`) with a trailing separator.

    This roughly corresponds to `wrap-and-sort`'s `-t` option.

    **Example**:

    When `true`, the following:
    ```
    Depends: foo,
             bar
    ```

    would be reformatted as:

    ```
    Depends: foo,
             bar,
    ```

    Note: The trailing separator is only applied if the field is reformatted. This means this option
    generally requires another option to trigger reformatting (like
    `formatting.deb822.normalize-field-content`).
    """
        ),
    ),
    PreferenceOption(
        key=["deb822", "max-line-length"],
        expected_type=int,
        default_value=79,
        description=textwrap.dedent(
            """\
    How long a value line can be before it should be line wrapped.

    This roughly corresponds to `wrap-and-sort`'s `--max-line-length` option.

    This option only applies to fields where formatting is a pure style preference. As an
    example, `Description` (`debian/control`) or `License` (`debian/copyright`) will not
    be affected by this option.

    Note: When `formatting.deb822.always-wrap` is `true`, then this option will be overruled.
    Additionally, this only triggers when a field is being reformatted. Generally that requires
    another option such as `formatting.deb822.normalize-field-content` for that to happen.
    """
        ),
    ),
    PreferenceOption(
        key=_NORMALISE_FIELD_CONTENT_KEY,
        expected_type=bool,
        default_value=False,
        description=textwrap.dedent(
            """\
    Whether to normalize field content.

    This roughly corresponds to the subset of `wrap-and-sort` that normalizes field content
    like sorting and normalizing relations or sorting the architecture field.

    **Example**:

    When `true`, the following:
    ```
    Depends: foo,
             bar|baz
    ```

    would be reformatted as:

    ```
    Depends: bar | baz,
             foo,
    ```

    This causes affected fields to always be rewritten and therefore be sure that other options
    such as `formatting.deb822.short-indent` or `formatting.deb822.always-wrap` is set according
    to taste.

    Note: The field may be rewritten without this being set to `true`. As an example, the `always-wrap`
    option can trigger a field rewrite. However, in that case, the values (including any internal whitespace)
    are left as-is while the whitespace normalization between the values is still applied.
    """
        ),
    ),
    PreferenceOption(
        key=["deb822", "normalize-field-order"],
        expected_type=bool,
        default_value=False,
        description=textwrap.dedent(
            """\
    Whether to normalize field order in a stanza.

    There is no `wrap-and-sort` feature matching this.

    **Example**:

    When `true`, the following:
    ```
    Depends: bar
    Package: foo
    ```

    would be reformatted as:

    ```
    Depends: foo
    Package: bar
    ```

    The field order is not by field name but by a logic order defined in `debputy` based on existing
    conventions. The `deb822` format does not dictate any field order inside stanzas in general, so
    reordering of fields is generally safe.

    If a field of the first stanza is known to be a format discriminator such as the `Format' in
    `debian/copyright`, then it will be put first. Generally that matches existing convention plus
    it maximizes the odds that existing tools will correctly identify the file format.
    """
        ),
    ),
    PreferenceOption(
        key=["deb822", "normalize-stanza-order"],
        expected_type=bool,
        default_value=False,
        description=textwrap.dedent(
            """\
    Whether to normalize stanza order in a file.

    This roughly corresponds to `wrap-and-sort`'s `-kb` feature except this may apply to other deb822
    files.

    **Example**:

    When `true`, the following:
    ```
    Source: zzbar

    Package: zzbar

    Package: zzbar-util

    Package: libzzbar-dev

    Package: libzzbar2
    ```

    would be reformatted as:

    ```
    Source: zzbar

    Package: zzbar

    Package: libzzbar2

    Package: libzzbar-dev

    Package: zzbar-util
    ```

    Reordering will only performed when:
      1) There is a convention for a normalized order
      2) The normalization can be performed without changing semantics

    Note: This option only guards style/preference related re-ordering. It does not influence
    warnings about the order being semantic incorrect (which will still be emitted regardless
    of this setting).
    """
        ),
    ),
]


@dataclasses.dataclass(slots=True, frozen=True)
class EffectiveFormattingPreference:
    deb822_short_indent: Optional[bool] = None
    deb822_always_wrap: Optional[bool] = None
    deb822_trailing_separator: bool = False
    deb822_normalize_field_content: bool = False
    deb822_normalize_field_order: bool = False
    deb822_normalize_stanza_order: bool = False
    deb822_max_line_length: int = 79

    @classmethod
    def from_file(
        cls,
        filename: str,
        key: str,
        styles: CommentedMap,
    ) -> Self:
        attr = {}

        for option in FORMATTING_OPTIONS:
            if not hasattr(cls, option.attribute_name):
                continue
            value = option.extract_value(filename, key, styles)
            attr[option.attribute_name] = value
        return cls(**attr)  # type: ignore

    @classmethod
    def aligned_preference(
        cls,
        a: Optional["EffectiveFormattingPreference"],
        b: Optional["EffectiveFormattingPreference"],
    ) -> Optional["EffectiveFormattingPreference"]:
        if a is None or b is None:
            return None

        for option in MAINT_OPTIONS:
            attr_name = option.attribute_name
            if not hasattr(EffectiveFormattingPreference, attr_name):
                continue
            a_value = getattr(a, attr_name)
            b_value = getattr(b, attr_name)
            if a_value != b_value:
                return None
        return a

    def deb822_formatter(self) -> FormatterCallback:
        line_length = self.deb822_max_line_length
        return wrap_and_sort_formatter(
            1 if self.deb822_short_indent else "FIELD_NAME_LENGTH",
            trailing_separator=self.deb822_trailing_separator,
            immediate_empty_line=self.deb822_short_indent or False,
            max_line_length_one_liner=(0 if self.deb822_always_wrap else line_length),
        )

    def replace(self, /, **changes: Any) -> Self:
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(slots=True, frozen=True)
class MaintainerPreference:
    canonical_name: Optional[str] = None
    is_packaging_team: bool = False
    formatting: Optional[EffectiveFormattingPreference] = None

    @classmethod
    def from_file(
        cls,
        filename: str,
        key: str,
        styles: CommentedMap,
    ) -> Self:
        attr = {}

        for option in MAINT_OPTIONS:
            if not hasattr(cls, option.attribute_name):
                continue
            value = option.extract_value(filename, key, styles)
            attr[option.attribute_name] = value
        return cls(**attr)  # type: ignore


class MaintainerPreferenceTable:

    def __init__(
        self,
        named_styles: Mapping[str, EffectiveFormattingPreference],
        maintainer_preferences: Mapping[str, MaintainerPreference],
    ) -> None:
        self._named_styles = named_styles
        self._maintainer_preferences = maintainer_preferences

    @classmethod
    def load_preferences(cls) -> Self:
        named_styles: Dict[str, EffectiveFormattingPreference] = {}
        maintainer_preferences: Dict[str, MaintainerPreference] = {}
        with open(BUILTIN_STYLES) as fd:
            parse_file(named_styles, maintainer_preferences, BUILTIN_STYLES, fd)

        missing_keys = set(named_styles.keys()).difference(
            ALL_PUBLIC_NAMED_STYLES.keys()
        )
        if missing_keys:
            missing_styles = ", ".join(sorted(missing_keys))
            _error(
                f"The following named styles are public API but not present in the config file: {missing_styles}"
            )

        # TODO: Support fetching styles online to pull them in faster than waiting for a stable release.
        return cls(named_styles, maintainer_preferences)

    @property
    def named_styles(self) -> Mapping[str, EffectiveFormattingPreference]:
        return self._named_styles

    @property
    def maintainer_preferences(self) -> Mapping[str, MaintainerPreference]:
        return self._maintainer_preferences


def parse_file(
    named_styles: Dict[str, EffectiveFormattingPreference],
    maintainer_preferences: Dict[str, MaintainerPreference],
    filename: str,
    fd,
) -> None:
    content = MANIFEST_YAML.load(fd)
    if not isinstance(content, CommentedMap):
        raise ValueError(
            f'The file "{filename}" should be a YAML file with a single mapping at the root'
        )
    try:
        maintainer_rules = content["maintainer-rules"]
        if not isinstance(maintainer_rules, CommentedMap):
            raise KeyError("maintainer-rules") from None
    except KeyError:
        raise ValueError(
            f'The file "{filename}" should have a "maintainer-rules" key which must be a mapping.'
        )
    named_styles_raw = content.get("formatting")
    if named_styles_raw is None or not isinstance(named_styles_raw, CommentedMap):
        named_styles_raw = {}

    for style_name, content in named_styles_raw.items():
        style = EffectiveFormattingPreference.from_file(
            filename,
            style_name,
            content,
        )
        named_styles[style_name] = style

    for maintainer_email, maintainer_pref in maintainer_rules.items():
        if not isinstance(maintainer_pref, CommentedMap):
            line_no = maintainer_rules.lc.key(maintainer_email).line
            raise ValueError(
                f'The value for maintainer "{maintainer_email}" should have been a mapping,'
                f' but it is not. The problem entry is at line {line_no} in "{filename}"'
            )
        formatting = maintainer_pref.get("formatting")
        if isinstance(formatting, str):
            try:
                style = named_styles[formatting]
            except KeyError:
                line_no = maintainer_rules.lc.key(maintainer_email).line
                raise ValueError(
                    f'The maintainer "{maintainer_email}" requested the named style "{formatting}",'
                    f' but said style was not defined {filename}. The problem entry is at line {line_no} in "{filename}"'
                ) from None
            maintainer_pref["formatting"] = style
        elif formatting is not None:
            maintainer_pref["formatting"] = EffectiveFormattingPreference.from_file(
                filename,
                "formatting",
                formatting,
            )
        mp = MaintainerPreference.from_file(
            filename,
            maintainer_email,
            maintainer_pref,
        )

        maintainer_preferences[maintainer_email] = mp


@functools.lru_cache(64)
def extract_maint_email(maint: str) -> str:
    if not maint.endswith(">"):
        return ""

    try:
        idx = maint.index("<")
    except ValueError:
        return ""
    return maint[idx + 1 : -1]


def determine_effective_preference(
    maint_preference_table: MaintainerPreferenceTable,
    source_package: Optional[SourcePackage],
    salsa_ci: Optional[CommentedMap],
) -> Tuple[Optional[EffectiveFormattingPreference], Optional[str], Optional[str]]:
    style = source_package.fields.get("X-Style") if source_package is not None else None
    if style is not None:
        if style not in ALL_PUBLIC_NAMED_STYLES:
            return None, None, "X-Style contained an unknown/unsupported style"
        return maint_preference_table.named_styles.get(style), "debputy reformat", None

    if salsa_ci:
        disable_wrap_and_sort = salsa_ci.mlget(
            ["variables", "SALSA_CI_DISABLE_WRAP_AND_SORT"],
            list_ok=True,
            default=True,
        )

        if isinstance(disable_wrap_and_sort, str):
            disable_wrap_and_sort = disable_wrap_and_sort in ("yes", "1", "true")
        elif not isinstance(disable_wrap_and_sort, (int, bool)):
            disable_wrap_and_sort = True
        else:
            disable_wrap_and_sort = (
                disable_wrap_and_sort is True or disable_wrap_and_sort == 1
            )
        if not disable_wrap_and_sort:
            wrap_and_sort_options = salsa_ci.mlget(
                ["variables", "SALSA_CI_WRAP_AND_SORT_ARGS"],
                list_ok=True,
                default=None,
            )
            if wrap_and_sort_options is None:
                wrap_and_sort_options = ""
            elif not isinstance(wrap_and_sort_options, str):
                return (
                    None,
                    None,
                    "The salsa-ci had a non-string option for wrap-and-sort",
                )
            detected_style = parse_salsa_ci_wrap_and_sort_args(wrap_and_sort_options)
            tool_w_args = f"wrap-and-sort {wrap_and_sort_options}".strip()
            if detected_style is None:
                msg = "One or more of the wrap-and-sort options in the salsa-ci file was not supported"
            else:
                msg = None
            return detected_style, tool_w_args, msg
    if source_package is None:
        return None, None, None

    maint = source_package.fields.get("Maintainer")
    if maint is None:
        return None, None, None
    maint_email = extract_maint_email(maint)
    maint_pref = maint_preference_table.maintainer_preferences.get(maint_email)
    # Special-case "@packages.debian.org" when missing, since they are likely to be "ad-hoc"
    # teams that will not be registered. In that case, we fall back to looking at the uploader
    # preferences as-if the maintainer had not been listed at all.
    if maint_pref is None and not maint_email.endswith("@packages.debian.org"):
        return None, None, None
    if maint_pref is not None and maint_pref.is_packaging_team:
        # When the maintainer is registered as a packaging team, then we assume the packaging
        # team's style applies unconditionally.
        effective = maint_pref.formatting
        tool_w_args = _guess_tool_from_style(maint_preference_table, effective)
        return effective, tool_w_args, None
    uploaders = source_package.fields.get("Uploaders")
    if uploaders is None:
        detected_style = maint_pref.formatting if maint_pref is not None else None
        tool_w_args = _guess_tool_from_style(maint_preference_table, detected_style)
        return detected_style, tool_w_args, None
    all_styles: List[Optional[EffectiveFormattingPreference]] = []
    if maint_pref is not None:
        all_styles.append(maint_pref.formatting)
    for uploader in _UPLOADER_SPLIT_RE.split(uploaders):
        uploader_email = extract_maint_email(uploader)
        uploader_pref = maint_preference_table.maintainer_preferences.get(
            uploader_email
        )
        all_styles.append(uploader_pref.formatting if uploader_pref else None)

    if not all_styles:
        return None, None, None
    r = functools.reduce(EffectiveFormattingPreference.aligned_preference, all_styles)
    assert not isinstance(r, MaintainerPreference)
    tool_w_args = _guess_tool_from_style(maint_preference_table, r)
    return r, tool_w_args, None


def _guess_tool_from_style(
    maint_preference_table: MaintainerPreferenceTable,
    pref: Optional[EffectiveFormattingPreference],
) -> Optional[str]:
    if pref is None:
        return None
    if maint_preference_table.named_styles["black"] == pref:
        return "debputy reformat"
    return None


def _split_options(args: Iterable[str]) -> Iterable[str]:
    for arg in args:
        if arg.startswith("--"):
            yield arg
            continue
        if not arg.startswith("-") or len(arg) < 2:
            yield arg
            continue
        for sarg in arg[1:]:
            yield f"-{sarg}"


@functools.lru_cache
def parse_salsa_ci_wrap_and_sort_args(
    args: str,
) -> Optional[EffectiveFormattingPreference]:
    options = dict(_WAS_DEFAULTS)
    for arg in _split_options(args.split()):
        v = _WAS_OPTIONS.get(arg)
        if v is None:
            return None
        varname, value = v
        if varname is None:
            continue
        options[varname] = value
    if "DISABLE_NORMALIZE_STANZA_ORDER" in options:
        del options["DISABLE_NORMALIZE_STANZA_ORDER"]
        options["deb822_normalize_stanza_order"] = False

    return EffectiveFormattingPreference(**options)  # type: ignore
