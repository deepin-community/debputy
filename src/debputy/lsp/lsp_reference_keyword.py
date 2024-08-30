import dataclasses
import textwrap
from typing import Optional, Union, Mapping, Sequence, Callable, Iterable, Literal

from debputy.lsp.vendoring._deb822_repro import Deb822ParagraphElement


UsageHint = Literal["rare",]


def format_comp_item_synopsis_doc(
    usage_hint: Optional[UsageHint], synopsis_doc: str, is_deprecated: bool
) -> str:
    if is_deprecated:
        return f"[OBSOLETE]: {synopsis_doc}"
    if usage_hint is not None:
        return f"[{usage_hint.upper()}]: {synopsis_doc}"
    return synopsis_doc


@dataclasses.dataclass(slots=True, frozen=True)
class Keyword:
    value: str
    synopsis_doc: Optional[str] = None
    hover_text: Optional[str] = None
    is_obsolete: bool = False
    replaced_by: Optional[str] = None
    is_exclusive: bool = False
    sort_text: Optional[str] = None
    usage_hint: Optional[UsageHint] = None
    can_complete_keyword_in_stanza: Optional[
        Callable[[Iterable[Deb822ParagraphElement]], bool]
    ] = None
    """For keywords in fields that allow multiple keywords, the `is_exclusive` can be
    used for keywords that cannot be used with other keywords. As an example, the `all`
    value in `Architecture` of `debian/control` cannot be used with any other architecture.
    """

    @property
    def is_deprecated(self) -> bool:
        return self.is_obsolete or self.replaced_by is not None

    def is_keyword_valid_completion_in_stanza(
        self,
        stanza_parts: Sequence[Deb822ParagraphElement],
    ) -> bool:
        return (
            self.can_complete_keyword_in_stanza is None
            or self.can_complete_keyword_in_stanza(stanza_parts)
        )


def allowed_values(*values: Union[str, Keyword]) -> Mapping[str, Keyword]:
    as_keywords = [k if isinstance(k, Keyword) else Keyword(k) for k in values]
    as_mapping = {k.value: k for k in as_keywords if k.value}
    # Simple bug check
    assert len(as_keywords) == len(as_mapping)
    return as_mapping


# This is the set of styles that `debputy` explicitly supports, which is more narrow than
# the ones in the config file.
ALL_PUBLIC_NAMED_STYLES = allowed_values(
    Keyword(
        "black",
        hover_text=textwrap.dedent(
            """\
            Uncompromising file formatting of Debian packaging files

            By using it, you  agree to cede control over minutiae of hand-formatting. In
            return, the formatter gives you speed, determinism, and freedom from style
            discussions about formatting.

            The `black` style is inspired by the `black` Python code formatter. Like with
            `black`, the style will evolve over time.
    """
        ),
    ),
)
