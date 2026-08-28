from __future__ import annotations

from enum import Enum
import json
from typing import Annotated, Any, List, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


KIBIBYTE = 1024
DTS_INPUT_LIMIT_BYTES = 1024 * KIBIBYTE
DTS_INPUT_SAFETY_MARGIN_BYTES = 64 * KIBIBYTE
DTS_INPUT_BUDGET_BYTES = DTS_INPUT_LIMIT_BYTES - DTS_INPUT_SAFETY_MARGIN_BYTES
SERVICE_BUS_MESSAGE_LIMIT_BYTES = 256 * KIBIBYTE
SERVICE_BUS_SAFETY_MARGIN_BYTES = 4 * KIBIBYTE
SERVICE_BUS_BODY_BUDGET_BYTES = (
    SERVICE_BUS_MESSAGE_LIMIT_BYTES - SERVICE_BUS_SAFETY_MARGIN_BYTES
)
SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES = 16 * KIBIBYTE
MAX_PROMPT_UTF8_BYTES = (
    SERVICE_BUS_BODY_BUDGET_BYTES
    - SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES
)
MAX_PROMPTS = 64


NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class Orientation(str, Enum):
    VERTICAL = "Vertical"
    HORIZONTAL = "Horizontal"


class CreateHDVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString = Field(
        description="Identifiant du dossier de travail vidéo existant."
    )
    ref_speaker1_filename: NonEmptyString = Field(
        description=(
            "Nom du fichier de référence du premier intervenant. "
            "L'extension .png est ajoutée si elle est absente."
        )
    )
    ref_speaker2_filename: NonEmptyString = Field(
        description=(
            "Nom du fichier de référence du second intervenant. "
            "L'extension .png est ajoutée si elle est absente."
        )
    )
    prompts: List[NonEmptyString] = Field(
        min_length=1,
        max_length=MAX_PROMPTS,
        description="Liste non vide des prompts, avec une génération parallèle par prompt.",
    )
    orientation: Orientation = Field(
        default=Orientation.VERTICAL,
        description=(
            "Orientation de la vidéo : Vertical produit 704x1280 et "
            "Horizontal produit 1280x704."
        ),
    )

    @field_validator("orientation", mode="before")
    @classmethod
    def parse_orientation(cls, value: Any) -> Any:
        if isinstance(value, Orientation):
            return value
        if isinstance(value, str):
            try:
                return Orientation(value)
            except ValueError:
                return value
        return value

    @field_validator("prompts")
    @classmethod
    def validate_prompt_utf8_sizes(cls, prompts: List[str]) -> List[str]:
        for index, prompt in enumerate(prompts):
            size = len(prompt.encode("utf-8"))
            if size > MAX_PROMPT_UTF8_BYTES:
                raise ValueError(
                    f"prompts[{index}] occupe {size} octets UTF-8 ; "
                    f"la limite est {MAX_PROMPT_UTF8_BYTES} octets."
                )
        return prompts

    @model_validator(mode="after")
    def validate_transport_budgets(self) -> "CreateHDVideoInput":
        request_size = len(self.model_dump_json().encode("utf-8"))
        if request_size > DTS_INPUT_BUDGET_BYTES:
            raise ValueError(
                f"L’entrée DTS occupe {request_size} octets UTF-8 ; "
                f"le budget avec marge est {DTS_INPUT_BUDGET_BYTES} octets."
            )

        for index, prompt in enumerate(self.prompts):
            user_content = json.dumps(
                {
                    "videoid": self.videoid,
                    "prompt": prompt,
                    "pic1": self.ref_speaker1_filename,
                    "pic2": self.ref_speaker2_filename,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            user_content_size = len(user_content.encode("utf-8"))
            if (
                user_content_size
                > SERVICE_BUS_BODY_BUDGET_BYTES
                - SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES
            ):
                raise ValueError(
                    f"Le contenu utilisateur du message prompts[{index}] occupe "
                    f"{user_content_size} octets UTF-8 ; il ne laisse pas la marge "
                    "requise pour l’enveloppe Service Bus."
                )
        return self


class GetHDVideoResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: NonEmptyString = Field(
        description="Identifiant workflow_id retourné par create_hd_video."
    )


class Ltx25Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    prompt: NonEmptyString
    pic1: NonEmptyString
    pic2: NonEmptyString
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    type_prefix: NonEmptyString
    instance_id: NonEmptyString
    event_key: NonEmptyString
    dts_event_name: NonEmptyString
    seed: int = Field(ge=0, le=2_147_483_647)


class GenerationDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    prompt: NonEmptyString
    type_prefix: NonEmptyString
    event_key: NonEmptyString
    dts_event_name: NonEmptyString
    blob_path: NonEmptyString


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    prompt: NonEmptyString
    status: Literal["completed", "failed", "timeout"]
    blob_path: NonEmptyString
    num_frames: int | None = Field(default=None, ge=1)
    error: str | None = None


class HDVideoWorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    orientation: Orientation
    generations: list[GenerationResult]


class RunningWorkflowResult(BaseModel):
    status: Literal["running"] = "running"
    workflow_id: NonEmptyString
    poll_after_seconds: int = Field(gt=0)
    next: NonEmptyString


class CompletedWorkflowResult(BaseModel):
    status: Literal["completed"] = "completed"
    workflow_id: NonEmptyString
    result: HDVideoWorkflowOutput


class FailedWorkflowResult(BaseModel):
    status: Literal["failed"] = "failed"
    workflow_id: NonEmptyString
    error: NonEmptyString


class NotFoundWorkflowResult(BaseModel):
    status: Literal["not_found"] = "not_found"
    workflow_id: NonEmptyString
    error: NonEmptyString
