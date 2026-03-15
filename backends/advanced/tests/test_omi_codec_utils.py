import pytest

from advanced_omi_backend.utils.omi_codec_utils import is_opus_header_stripped


@pytest.mark.unit
def test_defaults_to_header_stripped_when_metadata_missing():
    assert is_opus_header_stripped(None) is True
    assert is_opus_header_stripped({}) is True


@pytest.mark.unit
def test_respects_explicit_boolean_flag():
    assert is_opus_header_stripped({"opus_header_stripped": True}) is True
    assert is_opus_header_stripped({"opus_header_stripped": False}) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_handles_string_flags(value, expected):
    assert is_opus_header_stripped({"opus_header_stripped": value}) is expected
