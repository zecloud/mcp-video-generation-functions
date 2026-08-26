import pytest
from pydantic import ValidationError

from models import CreateHDVideoInput, Orientation


def valid_payload() -> dict:
    return {
        "videoid": "video-42",
        "ref_speaker1_filename": "speaker-one",
        "ref_speaker2_filename": "speaker-two",
        "prompts": ["Un plan rapproché"],
    }


def test_create_input_defaults_to_vertical_and_strips_strings():
    model = CreateHDVideoInput.model_validate(
        {
            **valid_payload(),
            "videoid": "  video-42  ",
            "prompts": ["  Un plan rapproché  "],
        },
        strict=True,
    )

    assert model.videoid == "video-42"
    assert model.prompts == ["Un plan rapproché"]
    assert model.orientation is Orientation.VERTICAL


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("videoid", " "),
        ("ref_speaker1_filename", ""),
        ("ref_speaker2_filename", "\t"),
        ("prompts", []),
        ("prompts", ["valide", " "]),
    ],
)
def test_create_input_rejects_empty_values(field, value):
    payload = valid_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(payload, strict=True)


def test_create_input_accepts_exact_orientation_values_in_strict_mode():
    model = CreateHDVideoInput.model_validate(
        {**valid_payload(), "orientation": "Horizontal"},
        strict=True,
    )

    assert model.orientation is Orientation.HORIZONTAL


def test_create_input_rejects_unknown_orientation():
    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "orientation": "square"},
            strict=True,
        )

