import pytest

from debputy.manifest_parser.base_types import SymbolicMode


@pytest.mark.parametrize(
    "base_mode,is_dir,symbolic_mode,expected",
    [
        (0o0644, False, "u+rwX,og=rX", 0o0644),
        (0o0000, False, "u+rwX,og=rX", 0o0644),
        (0o0400, True, "u+rwX,og=rX", 0o0755),
        (0o0000, True, "u+rwX,og=rX", 0o0755),
        (0o2400, False, "u+rwxs,og=rx", 0o04755),
        (0o7400, False, "u=rwX,og=rX", 0o0644),
        (0o0641, False, "u=rwX,og=rX", 0o0755),
        (0o4755, False, "a-x", 0o04644),
    ],
)
def test_generate_deb_filename(
    attribute_path, base_mode, is_dir, symbolic_mode, expected
):
    print(attribute_path.path)
    parsed_mode = SymbolicMode.parse_filesystem_mode(symbolic_mode, attribute_path)
    actual = parsed_mode.compute_mode(base_mode, is_dir)
    assert oct(actual)[2:] == oct(expected)[2:]
