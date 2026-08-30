import json

import pytest
from pydantic import ValidationError

from models import (
    CreateHDVideoInput,
    DTS_INPUT_BUDGET_BYTES,
    MAX_PROMPTS,
    MAX_PROMPT_UTF8_BYTES,
    Orientation,
    ReferenceSpec,
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


def test_reference_spec_forbids_empty_prompt_and_extra_fields():
    with pytest.raises(ValidationError):
        ReferenceSpec(file="alice.png", prompt=" ")
    with pytest.raises(ValidationError):
        ReferenceSpec(file="alice.png", prompt="Anna", unknown="x")

    spec = ReferenceSpec(file="alice.png", prompt="Anna")
    assert spec.is_background is False


def test_create_input_rejects_reference_prompt_without_sibling_prompt():
    with pytest.raises(
        ValidationError,
        match="ref_speaker2_filename fourni sans ref_speaker2_prompt",
    ):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "ref_speaker1_prompt": "Anna"},
            strict=True,
        )
    with pytest.raises(
        ValidationError,
        match="ref_speaker1_filename fourni sans ref_speaker1_prompt",
    ):
        CreateHDVideoInput.model_validate(
            {**valid_payload(), "ref_speaker2_prompt": "Bob"},
            strict=True,
        )

    model = CreateHDVideoInput.model_validate(
        {
            **valid_payload(),
            "ref_speaker1_prompt": "Anna",
            "ref_speaker2_prompt": "Bob",
        },
        strict=True,
    )
    assert model.ref_speaker1_prompt == "Anna"


def test_create_input_rejects_incoherent_references():
    # Filename sans prompt en mode references.
    with pytest.raises(
        ValidationError,
        match="ref_speaker3_filename fourni sans ref_speaker3_prompt",
    ):
        CreateHDVideoInput.model_validate(
            {
                **valid_payload(),
                "ref_speaker1_prompt": "Anna",
                "ref_speaker2_prompt": "Bob",
                "ref_speaker3_filename": "carol",
            },
            strict=True,
        )

    # Prompt sans filename.
    with pytest.raises(
        ValidationError,
        match="background_prompt fourni sans background_filename",
    ):
        CreateHDVideoInput.model_validate(
            {
                **valid_payload(),
                "ref_speaker1_prompt": "Anna",
                "ref_speaker2_prompt": "Bob",
                "background_prompt": "Un studio",
            },
            strict=True,
        )


def test_create_input_legacy_mode_accepts_extra_reference_filenames():
    model = CreateHDVideoInput.model_validate(
        {
            **valid_payload(),
            "ref_speaker3_filename": "carol",
            "ref_speaker4_filename": "dave.png",
            "background_filename": "studio",
        },
        strict=True,
    )
    assert model.ref_speaker3_filename == "carol"
    assert model.background_filename == "studio"


def test_create_input_budget_simulation_matches_references_payload():
    overhead_prompt = json.dumps(
        {
            "videoid": "speaker-one.png",
            "prompt": "",
            "references": [
                {"file": "speaker-one.png", "prompt": "Anna", "is_background": False},
                {"file": "speaker-two.png", "prompt": "Bob", "is_background": False},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    overhead_size = len(overhead_prompt.encode("utf-8"))
    # Le prompt remplit le budget réel du message, références incluses, en
    # restant sous la limite par prompt du validateur de champ.
    prompt = "é" * ((MAX_PROMPT_UTF8_BYTES - overhead_size) // 2 + 1)

    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        CreateHDVideoInput.model_validate(
            {
                **valid_payload(),
                "videoid": "speaker-one.png",
                "prompts": [prompt],
                "ref_speaker1_prompt": "Anna",
                "ref_speaker2_prompt": "Bob",
            },
            strict=True,
        )

    # Le même prompt avec le payload legacy (pic1/pic2) passe : la simulation
    # doit bien refléter le schéma réellement émis.
    model = CreateHDVideoInput.model_validate(
        {**valid_payload(), "videoid": "speaker-one.png", "prompts": [prompt]},
        strict=True,
    )
    assert model.prompts == [prompt]


def test_create_input_budget_simulation_covers_all_five_references():
    references = [
        {"file": "speaker-one.png", "prompt": "Anna", "is_background": False},
        {"file": "speaker-two.png", "prompt": "Bob", "is_background": False},
        {"file": "speaker-three.png", "prompt": "Carol", "is_background": False},
        {"file": "speaker-four.png", "prompt": "Dave", "is_background": False},
        {"file": "studio.png", "prompt": "Un studio", "is_background": True},
    ]
    overhead_prompt = json.dumps(
        {"videoid": "speaker-one.png", "prompt": "", "references": references},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    overhead_size = len(overhead_prompt.encode("utf-8"))
    prompt = "é" * ((MAX_PROMPT_UTF8_BYTES - overhead_size) // 2 + 1)

    with pytest.raises(ValidationError, match="enveloppe Service Bus"):
        CreateHDVideoInput.model_validate(
            {
                **valid_payload(),
                "videoid": "speaker-one.png",
                "prompts": [prompt],
                "ref_speaker1_prompt": "Anna",
                "ref_speaker2_prompt": "Bob",
                "ref_speaker3_filename": "speaker-three",
                "ref_speaker3_prompt": "Carol",
                "ref_speaker4_filename": "speaker-four",
                "ref_speaker4_prompt": "Dave",
                "background_filename": "studio",
                "background_prompt": "Un studio",
            },
            strict=True,
        )


def test_create_input_rejects_empty_reference_prompt():
    with pytest.raises(ValidationError):
        CreateHDVideoInput.model_validate(
            {
                **valid_payload(),
                "ref_speaker1_prompt": "Anna",
                "ref_speaker2_prompt": "   ",
            },
            strict=True,
        )
