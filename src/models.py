from __future__ import annotations

from enum import Enum
import json
from pathlib import PurePath
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


class ReferenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: NonEmptyString = Field(
        description="Nom du fichier de référence (image) dans le conteneur."
    )
    prompt: NonEmptyString = Field(
        description="Description visuelle de l'identité associée à la référence."
    )
    is_background: bool = False


def _ensure_png_when_extensionless(filename: str) -> str:
    return filename if PurePath(filename).suffix else f"{filename}.png"


REFERENCE_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("ref_speaker1_filename", "ref_speaker1_prompt", False),
    ("ref_speaker2_filename", "ref_speaker2_prompt", False),
    ("ref_speaker3_filename", "ref_speaker3_prompt", False),
    ("ref_speaker4_filename", "ref_speaker4_prompt", False),
    ("background_filename", "background_prompt", True),
)


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
    ref_speaker3_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du troisième intervenant (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    ref_speaker4_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du quatrième intervenant (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    background_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du décor (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    ref_speaker1_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du premier intervenant "
            "(ex. « Anna, 30 ans, cheveux roux ondulés, veste en cuir noire »). "
            "Dès qu'un prompt de référence est fourni, toutes les références "
            "doivent avoir leur prompt (schéma references[])."
        ),
    )
    ref_speaker2_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du second intervenant. "
            "Obligatoire dès qu'un prompt de référence est fourni."
        ),
    )
    ref_speaker3_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du troisième intervenant. "
            "Obligatoire si ref_speaker3_filename est fourni en mode references[]."
        ),
    )
    ref_speaker4_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du quatrième intervenant. "
            "Obligatoire si ref_speaker4_filename est fourni en mode references[]."
        ),
    )
    background_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du décor. "
            "Obligatoire si background_filename est fourni en mode references[]."
        ),
    )
    prompts: List[NonEmptyString] = Field(
        min_length=1,
        max_length=MAX_PROMPTS,
        description=(
            "Liste non vide des narrations complètes, avec une génération "
            "parallèle par narration ; le worker les découpe en plans."
        ),
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
        # Laisse la place au plus petit contenu utilisateur possible
        # (videoid, noms de fichiers) autour de la narration.
        per_prompt_budget = (
            MAX_PROMPT_UTF8_BYTES
            - len(
                json.dumps(
                    {"videoid": "", "prompt": "", "pic1": "", "pic2": ""},
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        )
        for index, prompt in enumerate(prompts):
            size = len(prompt.encode("utf-8"))
            if size > per_prompt_budget:
                raise ValueError(
                    f"prompts[{index}] occupe {size} octets UTF-8 ; "
                    f"la limite est {per_prompt_budget} octets."
                )
        return prompts

    @model_validator(mode="after")
    def validate_transport_budgets(self) -> "CreateHDVideoInput":
        request_size = len(self.model_dump_json(exclude_none=True).encode("utf-8"))
        if request_size > DTS_INPUT_BUDGET_BYTES:
            raise ValueError(
                f"L’entrée DTS occupe {request_size} octets UTF-8 ; "
                f"le budget avec marge est {DTS_INPUT_BUDGET_BYTES} octets."
            )

        provided = [
            (filename_field, prompt_field, is_background)
            for filename_field, prompt_field, is_background in REFERENCE_FIELDS
            if getattr(self, filename_field) is not None
            or getattr(self, prompt_field) is not None
        ]
        uses_references = any(
            getattr(self, prompt_field) is not None
            for _, prompt_field, _ in provided
        )

        references_payload: List[dict[str, Any]] | None = None
        legacy_payload: dict[str, str] | None = None
        if uses_references:
            missing_prompts = [
                f"{filename_field} fourni sans {prompt_field}"
                for filename_field, prompt_field, _ in provided
                if getattr(self, prompt_field) is None
            ]
            missing_filenames = [
                f"{prompt_field} fourni sans {filename_field}"
                for filename_field, prompt_field, _ in provided
                if getattr(self, filename_field) is None
            ]
            if missing_prompts or missing_filenames:
                raise ValueError(
                    "Références incohérentes : "
                    + " ; ".join(missing_prompts + missing_filenames)
                    + "."
                )
            references_payload = [
                {
                    "file": _ensure_png_when_extensionless(
                        getattr(self, filename_field)
                    ),
                    "prompt": getattr(self, prompt_field),
                    "is_background": is_background,
                }
                for filename_field, prompt_field, is_background in provided
            ]
        else:
            legacy_payload = {}
            legacy_keys = ("pic1", "pic2", "pic3", "pic4", "background")
            for legacy_key, (filename_field, _, _) in zip(
                legacy_keys, REFERENCE_FIELDS
            ):
                filename = getattr(self, filename_field)
                if filename is not None:
                    legacy_payload[legacy_key] = _ensure_png_when_extensionless(
                        filename
                    )

        for index, prompt in enumerate(self.prompts):
            message_content: dict[str, Any] = {
                "videoid": self.videoid,
                "prompt": prompt,
            }
            if references_payload is not None:
                message_content["references"] = references_payload
            else:
                message_content.update(legacy_payload)
            user_content = json.dumps(
                message_content,
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


class VideoMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    prompt: NonEmptyString
    pic1: NonEmptyString | None = None
    pic2: NonEmptyString | None = None
    pic3: NonEmptyString | None = None
    pic4: NonEmptyString | None = None
    background: NonEmptyString | None = None
    references: list[ReferenceSpec] | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
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
