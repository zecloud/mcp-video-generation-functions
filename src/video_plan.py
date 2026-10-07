"""Strict, content-addressed VideoPlan v1 contract shared with the LTX worker."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

MAX_VIDEO_PLAN_BYTES = 4 * 1024 * 1024
MAX_VIDEO_PLAN_SCENES = 512
MAX_VIDEO_PLAN_DURATION_SECONDS = 3600
VIDEO_PLAN_FPS = 24
PlanText = Annotated[str, StringConstraints(strict=True, min_length=1)]


def exact_plan_text(value: str) -> str:
    if value != value.strip():
        raise ValueError("VideoPlan texte doit être exact, sans espaces périphériques.")
    return value


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Clé JSON dupliquée : {key}.")
        result[key] = value
    return result


def decode_video_plan(value: Any) -> Any:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_VIDEO_PLAN_BYTES:
            raise ValueError("video_plan dépasse 4 MiB.")
        return json.loads(value, object_pairs_hook=reject_duplicate_keys)
    return value


def validate_plan_blob_name(value: str) -> str:
    if not re.fullmatch(r"[^/\\\s]+-[0-9a-f]{64}\.videoplan\.json", value):
        raise ValueError("video_plan doit être un nom simple adressé par SHA256.")
    return value


class VideoPlanScene(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    frames: int = Field(ge=9)
    prompt: PlanText
    location: PlanText | None = None

    @field_validator("prompt", "location")
    @classmethod
    def exact_text(cls, value):
        return exact_plan_text(value) if value is not None else None

    @model_validator(mode="after")
    def validate_frames(self) -> "VideoPlanScene":
        if (self.frames - 1) % 8:
            raise ValueError("video_plan.scenes.frames doit être sur la grille 8k+1.")
        return self


class VideoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: Literal[1] = 1
    videoid: PlanText
    fps: int = VIDEO_PLAN_FPS
    duration_seconds: float = Field(gt=0, le=MAX_VIDEO_PLAN_DURATION_SECONDS)
    total_frames: int = Field(ge=9, le=MAX_VIDEO_PLAN_DURATION_SECONDS * VIDEO_PLAN_FPS + 1)
    locations: list[PlanText] = Field(default_factory=list, max_length=16)
    scenes: list[VideoPlanScene] = Field(min_length=1, max_length=MAX_VIDEO_PLAN_SCENES)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("video_plan.schema_version doit être un entier strict.")
        return value

    @field_validator("locations")
    @classmethod
    def exact_locations(cls, values):
        return [exact_plan_text(value) for value in values]

    @field_validator("videoid")
    @classmethod
    def simple_videoid(cls, value):
        if "/" in value or "\\" in value or value in {".", ".."} or any(c.isspace() for c in value):
            raise ValueError("video_plan.videoid doit être un nom simple sans espaces.")
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> "VideoPlan":
        if self.fps != VIDEO_PLAN_FPS:
            raise ValueError("video_plan.fps doit valoir 24.")
        if len(set(self.locations)) != len(self.locations):
            raise ValueError("video_plan.locations contient des doublons.")
        intervals = 0
        for index, scene in enumerate(self.scenes):
            if scene.index != index:
                raise ValueError("video_plan.scenes doit être ordonné et indexé depuis 0.")
            expected_start = intervals / self.fps
            intervals += scene.frames - 1
            expected_end = intervals / self.fps
            if not (math.isclose(scene.start, expected_start, rel_tol=0, abs_tol=1e-6)
                    and math.isclose(scene.end, expected_end, rel_tol=0, abs_tol=1e-6)):
                raise ValueError("video_plan start/end doivent être dérivés des frames sans trou ni recouvrement.")
            if scene.location is not None and scene.location not in self.locations:
                raise ValueError("video_plan.scene.location doit appartenir à locations.")
            if len(self.locations) > 1 and scene.location is None:
                raise ValueError("video_plan.scene.location est requis pour plusieurs décors.")
        if self.total_frames != intervals + 1:
            raise ValueError("video_plan.total_frames doit valoir 1 + somme(frames - 1).")
        if not math.isclose(self.duration_seconds, intervals / self.fps, rel_tol=0, abs_tol=1e-6):
            raise ValueError("video_plan.duration_seconds doit valoir (total_frames - 1) / fps.")
        if len(self.serialize()) > MAX_VIDEO_PLAN_BYTES:
            raise ValueError("video_plan dépasse 4 MiB.")
        return self

    @classmethod
    def from_json(cls, content: bytes | str) -> "VideoPlan":
        if isinstance(content, bytes):
            if len(content) > MAX_VIDEO_PLAN_BYTES:
                raise ValueError("video_plan dépasse 4 MiB.")
            content = content.decode("utf-8")
        return cls.model_validate(decode_video_plan(content))

    def serialize(self) -> bytes:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def blob_name(self) -> str:
        return f"{self.videoid}-{hashlib.sha256(self.serialize()).hexdigest()}.videoplan.json"
