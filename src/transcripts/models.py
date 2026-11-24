from typing import Annotated
from pydantic import BaseModel, Field, ConfigDict, model_validator
import math


class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: float = Field(ge=0, le=172800, allow_inf_nan=False)
    end: float = Field(ge=0, le=172800, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start or not self.text.strip():
            raise ValueError("Invalid segment")
        return self


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    language: str = Field(min_length=1, max_length=32)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    software: str = Field(max_length=256)
    options: dict = Field(default_factory=dict)
    segments: list[Segment] = Field(min_length=1, max_length=50000)

    @model_validator(mode="after")
    def validate_segments(self):
        starts = [s.start for s in self.segments]
        if starts != sorted(starts):
            raise ValueError("Segments must be in chronological order")
        if sum(len(s.text) for s in self.segments) > 8_000_000:
            raise ValueError("Transcript too large")
        return self


class Lease(BaseModel):
    lease_token: str = Field(min_length=20, max_length=128)


class Failure(Lease):
    code: Annotated[str, Field(pattern=r"^(download_failed|inference_failed|worker_interrupted|result_invalid|no_speech)$")]
