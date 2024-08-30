from typing import Sequence, Union

import pytest

from debputy.util import escape_shell


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("foo bar", '"foo bar"'),
        ("a'b", r"""a\'b"""),
        ("foo=bar and baz", 'foo="bar and baz"'),
        ("--foo=bar and baz", '--foo="bar and baz"'),
        ("--foo with spaces=bar and baz", '"--foo with spaces=bar and baz"'),
    ],
)
def test_symlink_normalization(arg: Union[str, Sequence[str]], expected: str) -> None:
    actual = escape_shell(arg) if isinstance(arg, str) else escape_shell(*arg)
    assert actual == expected
