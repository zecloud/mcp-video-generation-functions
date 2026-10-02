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


MAX_TRACKS = 64


class Orientation(str, Enum):
    VERTICAL = "Vertical"
    HORIZONTAL = "Horizontal"


class MusicLora(str, Enum):
    TWO_STEPS_FROM_HELL = "two_steps_from_hell"
    INDUSTRIAL_ROCK = "industrial_rock"


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


class TrackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: NonEmptyString = Field(
        description=(
            "Style musical du morceau "
            "(ex. « epic orchestral trailer, choir, taiko drums »)."
        )
    )
    lyrics: NonEmptyString = Field(
        description=(
            "Paroles complètes du morceau, balises de structure comprises "
            "(ex. [verse], [chorus]). Utilisez [instrumental] pour un morceau "
            "sans voix."
        )
    )
    lora: MusicLora | None = Field(
        default=None,
        description="LoRA optionnel appliqué à la génération du morceau.",
    )

    @field_validator("lora", mode="before")
    @classmethod
    def parse_lora(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return MusicLora(value)
            except ValueError:
                return value
        return value


class CreateMusicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString = Field(
        description=(
            "Identifiant du dossier de travail existant, partagé avec la vidéo."
        )
    )
    tracks: List[TrackSpec] = Field(
        min_length=1,
        max_length=MAX_TRACKS,
        description=(
            "Liste non vide des morceaux à générer ; chaque morceau porte son "
            "style, ses paroles et son LoRA optionnel, et donne lieu à une "
            "génération parallèle."
        ),
    )

    @field_validator("tracks", mode="before")
    @classmethod
    def decode_tracks(cls, value: Any) -> Any:
        # Le déclencheur MCP peut transmettre le tableau sous forme de chaîne JSON.
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @model_validator(mode="after")
    def validate_transport_budgets(self) -> "CreateMusicInput":
        request_size = len(self.model_dump_json(exclude_none=True).encode("utf-8"))
        if request_size > DTS_INPUT_BUDGET_BYTES:
            raise ValueError(
                f"L’entrée DTS occupe {request_size} octets UTF-8 ; "
                f"le budget avec marge est {DTS_INPUT_BUDGET_BYTES} octets."
            )

        for index, track in enumerate(self.tracks):
            message_content: dict[str, Any] = {
                "videoid": self.videoid,
                "style": track.style,
                "lyrics": track.lyrics,
            }
            if track.lora is not None:
                message_content["lora"] = track.lora.value
            user_content_size = len(
                json.dumps(
                    message_content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if (
                user_content_size
                > SERVICE_BUS_BODY_BUDGET_BYTES
                - SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES
            ):
                raise ValueError(
                    f"Le contenu utilisateur du message tracks[{index}] occupe "
                    f"{user_content_size} octets UTF-8 ; il ne laisse pas la marge "
                    "requise pour l’enveloppe Service Bus."
                )
        return self


class GetMusicResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: NonEmptyString = Field(
        description="Identifiant workflow_id retourné par create_music."
    )


class MusicMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    style: NonEmptyString
    lyrics: NonEmptyString
    lora: MusicLora | None = None
    type_prefix: NonEmptyString
    instance_id: NonEmptyString
    event_key: NonEmptyString
    dts_event_name: NonEmptyString
    seed: int = Field(ge=0, le=2_147_483_647)


DEFAULT_SCENE_MIN_SECONDS = 3.0
DEFAULT_SCENE_MAX_SECONDS = 8.0
MUSIC_VIDEO_OPTIONAL_TEXT_FIELDS: tuple[str, ...] = (
    "lyrics",
    "theme_style",
    "story",
    "whisper_language",
)
MUSIC_VIDEO_OPTIONAL_NUMBER_FIELDS: tuple[str, ...] = (
    "scene_min_seconds",
    "scene_max_seconds",
    "scene_bias",
)


MUSIC_PLAN_SCHEMA_VERSION = 1
MUSIC_PLAN_SUFFIX = "musicplan.json"
MUSIC_PLAN_FRAME_GRID = 8
MAX_BACKGROUNDS = 16
MAX_MUSIC_PLAN_SCENES = 512


def _validate_simple_blob_name(value: str, field_name: str) -> str:
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(
            f"{field_name} doit être un nom simple, sans séparateur de chemin."
        )
    return value


def music_plan_blob_name(videoid: str, music_track: str) -> str:
    """Nom simple du blob de plan attendu par le worker ltx25."""

    return f"{music_track}-{videoid}.{MUSIC_PLAN_SUFFIX}"


def _decode_json_object(value: Any) -> Any:
    # Le déclencheur MCP peut transmettre un objet/tableau sous forme de chaîne.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class BackgroundSpec(BaseModel):
    """Décor du clip : une image de référence et sa description de lieu."""

    model_config = ConfigDict(extra="forbid")

    filename: NonEmptyString = Field(
        description=(
            "Nom du fichier de référence du décor dans le dossier de travail. "
            "L'extension .png est ajoutée si elle est absente."
        )
    )
    description: NonEmptyString = Field(
        description=(
            "Description du lieu, reprise caractère pour caractère dans "
            "music_plan.locations à la même position."
        )
    )


class MusicPlanScene(BaseModel):
    """Scène d'un plan musical pré-calculé."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    frames: int = Field(gt=0)
    lyrics_raw: str | None = None
    lyrics: str | None = None
    instrumental: bool = False
    prompt: NonEmptyString

    @model_validator(mode="after")
    def validate_scene(self) -> "MusicPlanScene":
        if self.end <= self.start:
            raise ValueError(
                f"scenes[{self.index}] : end ({self.end:g}) doit être "
                f"strictement supérieur à start ({self.start:g})."
            )
        if (self.frames - 1) % MUSIC_PLAN_FRAME_GRID != 0:
            raise ValueError(
                f"scenes[{self.index}] : frames ({self.frames}) doit être sur "
                f"la grille 8k+1 du worker LTX."
            )
        return self


class MusicPlan(BaseModel):
    """Plan musical pré-calculé, sérialisé tel quel dans le blob du worker."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(
        default=MUSIC_PLAN_SCHEMA_VERSION,
        description="Version du contrat de plan ; seule la version 1 est acceptée.",
    )
    videoid: NonEmptyString
    music_track: NonEmptyString
    fps: int = Field(gt=0, le=240)
    duration_seconds: float = Field(gt=0)
    total_frames: int = Field(gt=0)
    subject: NonEmptyString
    locations: list[NonEmptyString] = Field(min_length=1, max_length=MAX_BACKGROUNDS)
    theme_style: str | None = None
    story: str | None = None
    lyrics_reference: str | None = None
    scene_min_seconds: float | None = None
    scene_max_seconds: float | None = None
    scene_bias: float | None = None
    whisper_model: str | None = None
    whisper_language: str | None = None
    llm_model: str | None = None
    scenes: list[MusicPlanScene] = Field(min_length=1, max_length=MAX_MUSIC_PLAN_SCENES)

    @field_validator("music_track")
    @classmethod
    def validate_music_track(cls, value: str) -> str:
        return _validate_simple_blob_name(value, "music_plan.music_track")

    @model_validator(mode="after")
    def validate_plan(self) -> "MusicPlan":
        if len(set(self.locations)) != len(self.locations):
            raise ValueError("music_plan.locations contient des doublons.")

        previous_end = 0.0
        for position, scene in enumerate(self.scenes):
            if scene.index != position:
                raise ValueError(
                    f"music_plan.scenes[{position}] : index {scene.index} "
                    "incohérent ; les scènes doivent être ordonnées à partir de 0."
                )
            if abs(scene.start - previous_end) > 1e-3:
                raise ValueError(
                    f"music_plan.scenes[{position}] : start ({scene.start:g}) ne "
                    f"prolonge pas la scène précédente ({previous_end:g}) ; les "
                    "scènes doivent couvrir la chanson sans trou ni recouvrement."
                )
            previous_end = scene.end

        if previous_end - self.duration_seconds > 1e-3:
            raise ValueError(
                f"music_plan : les scènes couvrent {previous_end:g} s au-delà de "
                f"duration_seconds ({self.duration_seconds:g})."
            )

        expected_total = 1 + sum(scene.frames - 1 for scene in self.scenes)
        if self.total_frames != expected_total:
            raise ValueError(
                f"music_plan.total_frames ({self.total_frames}) doit valoir "
                f"1 + somme(frames - 1) = {expected_total}."
            )
        if (self.total_frames - 1) % MUSIC_PLAN_FRAME_GRID != 0:
            raise ValueError(
                f"music_plan.total_frames ({self.total_frames}) doit être sur la "
                "grille 8k+1 du worker LTX."
            )

        for scene in self.scenes:
            self._scene_location(scene)
        return self

    def _scene_location(self, scene: MusicPlanScene) -> str:
        matches = [
            location
            for location in self.locations
            if scene.prompt.startswith(f"{location}: ")
            and scene.prompt[len(location) + 2 :].strip()
        ]
        if len(matches) != 1:
            raise ValueError(
                f"music_plan.scenes[{scene.index}] : prompt doit valoir "
                "« <location>: <action> » avec exactement un décor de "
                f"locations ; {len(matches)} correspondance(s) exacte(s)."
            )
        return matches[0]

    def scene_locations(self) -> list[str]:
        """Décor retenu pour chaque scène, dans l'ordre."""

        return [self._scene_location(scene) for scene in self.scenes]

    def serialize(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode("utf-8")


def _validate_scene_bounds(
    scene_min_seconds: float | None,
    scene_max_seconds: float | None,
) -> None:
    effective_min = (
        DEFAULT_SCENE_MIN_SECONDS if scene_min_seconds is None else scene_min_seconds
    )
    effective_max = (
        DEFAULT_SCENE_MAX_SECONDS if scene_max_seconds is None else scene_max_seconds
    )
    if effective_min > effective_max:
        raise ValueError(
            f"scene_min_seconds ({effective_min:g}) doit être inférieur ou égal "
            f"à scene_max_seconds ({effective_max:g}) ; valeurs par défaut du "
            f"worker : {DEFAULT_SCENE_MIN_SECONDS:g} / {DEFAULT_SCENE_MAX_SECONDS:g}."
        )


class CreateMusicVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString = Field(
        description=(
            "Identifiant du dossier de travail existant, partagé avec la "
            "musique et les images de référence."
        )
    )
    music_track: NonEmptyString = Field(
        description=(
            "type_prefix du morceau renvoyé par create_music "
            "(ex. « music-0123456789-001 ») ; le worker lit "
            "{music_track}-{videoid}.flac dans le dossier de travail."
        )
    )
    ref_speaker1_filename: NonEmptyString = Field(
        description=(
            "Nom du fichier de référence de l'interprète principal. "
            "L'extension .png est ajoutée si elle est absente."
        )
    )
    ref_speaker1_prompt: NonEmptyString = Field(
        description=(
            "Description visuelle de l'interprète principal "
            "(ex. « Lena, 25 ans, cheveux platine, robe à sequins argentés »)."
        )
    )
    ref_speaker2_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du deuxième interprète (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    ref_speaker2_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du deuxième interprète. "
            "Obligatoire si ref_speaker2_filename est fourni."
        ),
    )
    ref_speaker3_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du troisième interprète (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    ref_speaker3_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du troisième interprète. "
            "Obligatoire si ref_speaker3_filename est fourni."
        ),
    )
    ref_speaker4_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du quatrième interprète (optionnel). "
            "L'extension .png est ajoutée si elle est absente."
        ),
    )
    ref_speaker4_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description visuelle du quatrième interprète. "
            "Obligatoire si ref_speaker4_filename est fourni."
        ),
    )
    background_filename: NonEmptyString | None = Field(
        default=None,
        description=(
            "Nom du fichier de référence du décor unique (optionnel, hérité). "
            "L'extension .png est ajoutée si elle est absente. Utilisez "
            "backgrounds pour plusieurs décors."
        ),
    )
    background_prompt: NonEmptyString | None = Field(
        default=None,
        description=(
            "Description du ou des lieux du clip, utilisée par le LLM pour "
            "situer les scènes. Obligatoire si background_filename est fourni."
        ),
    )
    backgrounds: List[BackgroundSpec] | None = Field(
        default=None,
        max_length=MAX_BACKGROUNDS,
        description=(
            "Liste ordonnée des décors du clip ({filename, description}). "
            "Avec music_plan, locations[i] doit valoir backgrounds[i].description. "
            "Incompatible avec background_filename / background_prompt."
        ),
    )
    orientation: Orientation = Field(
        default=Orientation.VERTICAL,
        description=(
            "Orientation du clip : Vertical produit 704x1280 et "
            "Horizontal produit 1280x704."
        ),
    )
    lyrics: NonEmptyString | None = Field(
        default=None,
        description=(
            "Paroles de référence, telles qu'envoyées à create_music "
            "(optionnel) ; elles aident à corriger la transcription Whisper."
        ),
    )
    theme_style: NonEmptyString | None = Field(
        default=None,
        description=(
            "Direction artistique visuelle du clip (optionnel, "
            "ex. « néon rétro-futuriste, grain 35 mm »)."
        ),
    )
    story: NonEmptyString | None = Field(
        default=None,
        description="Trame narrative du clip (optionnel).",
    )
    scene_min_seconds: float | None = Field(
        default=None,
        gt=0,
        le=60,
        description=(
            "Durée minimale d'une scène en secondes (optionnel, défaut worker 3)."
        ),
    )
    scene_max_seconds: float | None = Field(
        default=None,
        gt=0,
        le=60,
        description=(
            "Durée maximale d'une scène en secondes (optionnel, défaut worker 8, "
            "plafonnée par le worker à la taille d'un bloc de génération)."
        ),
    )
    scene_bias: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        description=(
            "Biais de découpage entre -1 (scènes plus courtes) et +1 "
            "(scènes plus longues) ; défaut worker 0."
        ),
    )
    whisper_language: NonEmptyString | None = Field(
        default=None,
        max_length=16,
        description=(
            "Code langue des paroles pour Whisper (ex. « fr », « en ») ; "
            "détection automatique si absent."
        ),
    )
    music_plan: MusicPlan | None = Field(
        default=None,
        description=(
            "Plan musical pré-calculé, transmis en objet JSON. Le serveur MCP "
            "le valide, le sérialise et l'upload dans "
            "agentvideo/{videoid}/{music_track}-{videoid}.musicplan.json avant "
            "de démarrer l'orchestration ; le worker saute alors l'analyse "
            "audio et les appels LLM. Incompatible avec reuse_music_plan."
        ),
    )
    reuse_music_plan: bool = Field(
        default=False,
        description=(
            "Si vrai, réutilise le plan existant "
            "{music_track}-{videoid}.musicplan.json : re-rendu du clip sans "
            "nouvelle analyse audio ni appels LLM. Réservé aux re-rendus et "
            "incompatible avec music_plan."
        ),
    )

    @field_validator("music_plan", "backgrounds", mode="before")
    @classmethod
    def decode_json_payloads(cls, value: Any) -> Any:
        return _decode_json_object(value)

    @field_validator("orientation", mode="before")
    @classmethod
    def parse_orientation(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return Orientation(value)
            except ValueError:
                return value
        return value

    @field_validator("music_track")
    @classmethod
    def validate_music_track(cls, value: str) -> str:
        return _validate_simple_blob_name(value, "music_track")

    def background_specs(self) -> list[BackgroundSpec]:
        """Décors normalisés : la liste explicite, ou le décor legacy seul."""

        if self.backgrounds is not None:
            return list(self.backgrounds)
        if self.background_filename is not None and self.background_prompt is not None:
            return [
                BackgroundSpec(
                    filename=self.background_filename,
                    description=self.background_prompt,
                )
            ]
        return []

    def music_plan_blob_name(self) -> str:
        return music_plan_blob_name(self.videoid, self.music_track)

    def reference_specs(self) -> list[ReferenceSpec]:
        specs = [
            ReferenceSpec(
                file=_ensure_png_when_extensionless(getattr(self, filename_field)),
                prompt=getattr(self, prompt_field),
                is_background=False,
            )
            for filename_field, prompt_field, is_background in REFERENCE_FIELDS
            if not is_background and getattr(self, filename_field) is not None
        ]
        specs.extend(
            ReferenceSpec(
                file=_ensure_png_when_extensionless(background.filename),
                prompt=background.description,
                is_background=True,
            )
            for background in self.background_specs()
        )
        return specs

    def worker_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            name: getattr(self, name)
            for name in (
                *MUSIC_VIDEO_OPTIONAL_TEXT_FIELDS,
                *MUSIC_VIDEO_OPTIONAL_NUMBER_FIELDS,
            )
            if getattr(self, name) is not None
        }
        if self.music_plan is not None:
            # Le worker reçoit le nom simple du blob uploadé par le serveur MCP,
            # jamais le plan lui-même.
            options["music_plan"] = self.music_plan_blob_name()
        elif self.reuse_music_plan:
            options["music_plan"] = True
        return options

    @model_validator(mode="after")
    def validate_references_and_budgets(self) -> "CreateMusicVideoInput":
        if self.music_plan is not None and self.reuse_music_plan:
            raise ValueError(
                "music_plan et reuse_music_plan sont mutuellement exclusifs : "
                "music_plan fournit un plan pré-calculé, reuse_music_plan est "
                "réservé aux re-rendus du plan déjà présent dans le dossier."
            )

        legacy_background = (
            self.background_filename is not None or self.background_prompt is not None
        )
        if self.backgrounds is not None and legacy_background:
            raise ValueError(
                "backgrounds et background_filename / background_prompt sont "
                "mutuellement exclusifs : utilisez la liste ordonnée backgrounds."
            )

        inconsistent = []
        for filename_field, prompt_field, _ in REFERENCE_FIELDS:
            has_filename = getattr(self, filename_field) is not None
            has_prompt = getattr(self, prompt_field) is not None
            if has_filename and not has_prompt:
                inconsistent.append(f"{filename_field} fourni sans {prompt_field}")
            elif has_prompt and not has_filename:
                inconsistent.append(f"{prompt_field} fourni sans {filename_field}")
        if inconsistent:
            raise ValueError(
                "Références incohérentes : " + " ; ".join(inconsistent) + "."
            )

        _validate_scene_bounds(self.scene_min_seconds, self.scene_max_seconds)

        backgrounds = self.background_specs()
        if self.music_plan is not None:
            plan = self.music_plan
            if plan.videoid != self.videoid:
                raise ValueError(
                    f"music_plan.videoid ({plan.videoid!r}) doit valoir "
                    f"videoid ({self.videoid!r})."
                )
            if plan.music_track != self.music_track:
                raise ValueError(
                    f"music_plan.music_track ({plan.music_track!r}) doit valoir "
                    f"music_track ({self.music_track!r})."
                )
            if len(backgrounds) != len(plan.locations):
                raise ValueError(
                    f"backgrounds fournit {len(backgrounds)} décor(s) alors que "
                    f"music_plan.locations en déclare {len(plan.locations)} ; il "
                    "faut une image de référence par lieu, dans le même ordre."
                )
            mismatched = [
                f"locations[{index}] ({location!r}) != "
                f"backgrounds[{index}].description ({background.description!r})"
                for index, (location, background) in enumerate(
                    zip(plan.locations, backgrounds, strict=True)
                )
                if location != background.description
            ]
            if mismatched:
                raise ValueError(
                    "Décors incohérents avec le plan : " + " ; ".join(mismatched) + "."
                )

        # Le plan est uploadé en blob avant l'orchestration : il ne transite
        # jamais par l'entrée DTS, qui ne porte que le nom simple du blob.
        request_size = len(
            self.model_dump_json(exclude_none=True, exclude={"music_plan"}).encode(
                "utf-8"
            )
        )
        if request_size > DTS_INPUT_BUDGET_BYTES:
            raise ValueError(
                f"L’entrée DTS occupe {request_size} octets UTF-8 ; "
                f"le budget avec marge est {DTS_INPUT_BUDGET_BYTES} octets."
            )

        message_content: dict[str, Any] = {
            "videoid": self.videoid,
            "music_track": self.music_track,
            "references": [
                reference.model_dump(mode="json")
                for reference in self.reference_specs()
            ],
            **self.worker_options(),
        }
        user_content_size = len(
            json.dumps(
                message_content,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if (
            user_content_size
            > SERVICE_BUS_BODY_BUDGET_BYTES - SERVICE_BUS_DYNAMIC_ENVELOPE_BUDGET_BYTES
        ):
            raise ValueError(
                f"Le contenu utilisateur du message de clip occupe "
                f"{user_content_size} octets UTF-8 ; il ne laisse pas la marge "
                "requise pour l’enveloppe Service Bus."
            )
        return self


class GetMusicVideoResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: NonEmptyString = Field(
        description="Identifiant workflow_id retourné par create_music_video."
    )


class MusicVideoMessage(BaseModel):
    """Message du mode « music video » du worker ltx25 (sans prompt requis)."""

    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    music_track: NonEmptyString
    # ``True`` déclenche le mode reuse legacy du worker ; une chaîne nomme le
    # blob JSON à télécharger depuis agentvideo/{videoid}/.
    music_plan: Literal[True] | NonEmptyString | None = None
    references: list[ReferenceSpec] = Field(min_length=1)
    lyrics: NonEmptyString | None = None
    theme_style: NonEmptyString | None = None
    story: NonEmptyString | None = None
    scene_min_seconds: float | None = Field(default=None, gt=0)
    scene_max_seconds: float | None = Field(default=None, gt=0)
    scene_bias: float | None = Field(default=None, ge=-1, le=1)
    whisper_language: NonEmptyString | None = None
    width: int = Field(gt=0, multiple_of=64)
    height: int = Field(gt=0, multiple_of=64)
    type_prefix: NonEmptyString
    instance_id: NonEmptyString
    event_key: NonEmptyString
    dts_event_name: NonEmptyString
    seed: int = Field(ge=0, le=2_147_483_647)

    @field_validator("music_track")
    @classmethod
    def validate_music_track(cls, value: str) -> str:
        return _validate_simple_blob_name(value, "music_track")

    @field_validator("music_plan")
    @classmethod
    def validate_music_plan(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _validate_simple_blob_name(value, "music_plan")
        return value

    @model_validator(mode="after")
    def validate_scene_bounds(self) -> "MusicVideoMessage":
        _validate_scene_bounds(self.scene_min_seconds, self.scene_max_seconds)
        return self


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
    # Libellé de la génération : la narration côté vidéo, le style côté musique.
    prompt: NonEmptyString
    status: Literal["completed", "failed", "timeout"]
    blob_path: NonEmptyString
    # Préfixe du blob ; pour create_music, valeur à passer en music_track.
    type_prefix: str | None = None
    num_frames: int | None = Field(default=None, ge=1)
    # Nom du blob du plan musical renvoyé par le worker en mode « music video ».
    music_plan: str | None = None
    error: str | None = None


class HDVideoWorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    orientation: Orientation
    generations: list[GenerationResult]


class MusicWorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    generations: list[GenerationResult]


class MusicVideoArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    music_plan_path: NonEmptyString
    scenes_srt_path: NonEmptyString
    prompts_srt_path: NonEmptyString


class MusicVideoWorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoid: NonEmptyString
    orientation: Orientation
    music_track: NonEmptyString
    reuse_music_plan: bool = False
    artifacts: MusicVideoArtifacts
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


class CompletedMusicWorkflowResult(BaseModel):
    status: Literal["completed"] = "completed"
    workflow_id: NonEmptyString
    result: MusicWorkflowOutput


class CompletedMusicVideoWorkflowResult(BaseModel):
    status: Literal["completed"] = "completed"
    workflow_id: NonEmptyString
    result: MusicVideoWorkflowOutput


class FailedWorkflowResult(BaseModel):
    status: Literal["failed"] = "failed"
    workflow_id: NonEmptyString
    error: NonEmptyString


class NotFoundWorkflowResult(BaseModel):
    status: Literal["not_found"] = "not_found"
    workflow_id: NonEmptyString
    error: NonEmptyString
