from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, List, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


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
        description="Liste non vide des prompts, avec une génération parallèle par prompt.",
    )
    orientation: Orientation = Field(
        default=Orientation.VERTICAL,
        description=(
            "Orientation de la vidéo : Vertical produit 720x1280 et "
            "Horizontal produit 1280x720."
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
