# Code extracted from devscripts/wrap-and-sort with typing added
import re
from typing import Tuple, Iterable, Literal, Union

from debputy.lsp.vendoring._deb822_repro.formatter import (
    one_value_per_line_formatter,
    FormatterContentToken,
)
from debputy.lsp.vendoring._deb822_repro.types import FormatterCallback

PACKAGE_SORT = re.compile("^[a-z0-9]")


def _sort_packages_key(package: str) -> Tuple[int, str]:
    # Sort dependencies starting with a "real" package name before ones starting
    # with a substvar
    return 0 if PACKAGE_SORT.search(package) else 1, package


def _emit_one_line_value(
    value_tokens: Iterable[FormatterContentToken],
    sep_token: FormatterContentToken,
    trailing_separator: bool,
) -> Iterable[Union[str, FormatterContentToken]]:
    first_token = True
    yield " "
    for token in value_tokens:
        if not first_token:
            yield sep_token
            if not sep_token.is_whitespace:
                yield " "
        first_token = False
        yield token
    if trailing_separator and not sep_token.is_whitespace:
        yield sep_token
    yield "\n"


def wrap_and_sort_formatter(
    indentation: Union[int, Literal["FIELD_NAME_LENGTH"]],
    trailing_separator: bool = True,
    immediate_empty_line: bool = False,
    max_line_length_one_liner: int = 0,
) -> FormatterCallback:
    """Provide a formatter that can handle indentation and trailing separators

    This is a custom wrap-and-sort formatter capable of supporting wrap-and-sort's
    needs. Where possible it delegates to python-debian's own formatter.

    :param indentation: Either the literal string "FIELD_NAME_LENGTH" or a positive
    integer, which determines the indentation fields.  If it is an integer,
    then a fixed indentation is used (notably the value 1 ensures the shortest
    possible indentation).  Otherwise, if it is "FIELD_NAME_LENGTH", then the
    indentation is set such that it aligns the values based on the field name.
    This parameter only affects values placed on the second line or later lines.
    :param trailing_separator: If True, then the last value will have a trailing
    separator token (e.g., ",") after it.
    :param immediate_empty_line: Whether the value should always start with an
    empty line.  If True, then the result becomes something like "Field:\n value".
    This parameter only applies to the values that will be formatted over more than
    one line.
    :param max_line_length_one_liner: If greater than zero, then this is the max length
    of the value if it is crammed into a "one-liner" value.  If the value(s) fit into
    one line, this parameter will overrule immediate_empty_line.

    """
    if indentation != "FIELD_NAME_LENGTH" and indentation < 1:
        raise ValueError('indentation must be at least 1 (or "FIELD_NAME_LENGTH")')

    # The python-debian library provides support for all cases except cramming
    # everything into a single line.  So we "only" have to implement the single-line
    # case(s) ourselves (which sadly takes plenty of code on its own)

    _chain_formatter = one_value_per_line_formatter(
        indentation,
        trailing_separator=trailing_separator,
        immediate_empty_line=immediate_empty_line,
    )

    if max_line_length_one_liner < 1:
        return _chain_formatter

    def _formatter(name, sep_token, formatter_tokens):
        # We should have unconditionally delegated to the python-debian formatter
        # if max_line_length_one_liner was set to "wrap_always"
        assert max_line_length_one_liner > 0
        all_tokens = list(formatter_tokens)
        values_and_comments = [x for x in all_tokens if x.is_comment or x.is_value]
        # There are special-cases where you could do a one-liner with comments, but
        # they are probably a lot more effort than it is worth investing.
        # - If you are here because you disagree, patches welcome. :)
        if all(x.is_value for x in values_and_comments):
            # We use " " (1 char) or ", " (2 chars) as separated depending on the field.
            # (at the time of writing, wrap-and-sort only uses this formatted for
            # dependency fields meaning this will be "2" - but now it is future proof).
            chars_between_values = 1 + (0 if sep_token.is_whitespace else 1)
            # Compute the total line length of the field as the sum of all values
            total_len = sum(len(x.text) for x in values_and_comments)
            # ... plus the separators
            total_len += (len(values_and_comments) - 1) * chars_between_values
            # plus the field name + the ": " after the field name
            total_len += len(name) + 2
            if total_len <= max_line_length_one_liner:
                yield from _emit_one_line_value(
                    values_and_comments, sep_token, trailing_separator
                )
                return
            # If it does not fit in one line, we fall through
        # Chain into the python-debian provided formatter, which will handle this
        # formatting for us.
        yield from _chain_formatter(name, sep_token, iter(all_tokens))

    return _formatter
