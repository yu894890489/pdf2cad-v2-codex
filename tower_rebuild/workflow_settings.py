"""Settings for the native drawing workflow; secrets are environment variables."""
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .io import read_json
from .schema import Settings, Strict


class VisionReviewSettings(Strict):
    enabled: bool = False
    max_views: int = Field(default=24, ge=1, le=100)
    service: Settings = Field(default_factory=Settings)

    @model_validator(mode='after')
    def configured_service(self):
        if self.enabled and not self.service.model.strip():
            raise ValueError('启用视觉复核前请填写 vision_review.service.model')
        return self


class OcrSettings(Strict):
    mode: Literal['auto', 'disabled'] = 'auto'
    render_scale: float = Field(default=3, ge=2, le=5)
    tile_pixels: int = Field(default=1800, ge=800, le=2600)
    overlap_pixels: int = Field(default=200, ge=80, le=400)
    min_confidence: float = Field(default=.75, ge=.5, le=1)
    review_confidence: float = Field(default=.93, ge=.5, le=1)
    threads: int = Field(default=4, ge=1, le=16)

    @model_validator(mode='after')
    def confidence_order(self):
        if self.review_confidence < self.min_confidence:
            raise ValueError('ocr.review_confidence 必须不低于 ocr.min_confidence')
        return self


class WorkflowSettings(Strict):
    schema_version: Literal['1.0'] = '1.0'
    name: str = '本地矢量图纸'
    diagnostic_tolerance_mm: float = Field(default=3, ge=0, le=20)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    vision_review: VisionReviewSettings = Field(default_factory=VisionReviewSettings)


def load_workflow_settings(config=None):
    if isinstance(config, WorkflowSettings):
        return config.model_copy(deep=True)
    if isinstance(config, (str, Path)):
        config = read_json(config)
    return WorkflowSettings.model_validate(config or {})
