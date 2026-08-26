import pytest
from pydantic import ValidationError

from models import (
    CreateHDVideoInput,
    DTS_INPUT_BUDGET_BYTES,
    MAX_PROMPTS,
    MAX_PROMPT_UTF8_BYTES,
    Orientation,
)


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


def test_create_input_rejects_multibyte_prompt_over_byte_budget():
    prompt = "😀" * (MAX_PROMPT_UTF8_BYTES // 4 + 1)

    with pytest.raises(ValidationError, match="octets UTF-8"):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "prompts": [prompt]},
            strict=True,
        )


def test_create_input_caps_fan_out():
    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "prompts": ["prompt"] * (MAX_PROMPTS + 1)},
            strict=True,
        )


def test_create_input_rejects_total_dts_payload_over_budget():
    prompt = "é" * ((220 * 1024) // 2)

    with pytest.raises(ValidationError, match="entrée DTS"):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "prompts": [prompt] * 5},
            strict=True,
        )


def test_create_input_accepts_multibyte_payload_below_dts_budget():
    model = CreateHDVideoInput.model_validate(
        {**valid_payload(), "prompts": ["café 😀"] * 10},
        strict=True,
    )

    assert len(model.model_dump_json().encode("utf-8")) < DTS_INPUT_BUDGET_BYTES
