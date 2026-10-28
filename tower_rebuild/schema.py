"""The interchange contract. All image coordinates refer to the ORIGINAL page.

Page coordinates are normalized [0, 1], x right, y down. Lengths are mm.
IDs identify instances, never a BOM mark alone. LLMs cannot approve reviews.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = r"^[A-Za-z0-9_-]+$"
Point2 = tuple[float, float]
Point3 = tuple[float, float, float]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Evidence(Strict):
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_box(self):
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("bbox must be a nonempty normalized original-page rectangle")
        return self


class BomRow(Strict):
    mark: str = Field(min_length=1)
    specification: str = Field(min_length=1)
    length_mm: float | None = Field(default=None, gt=0)
    quantity: int = Field(gt=0)
    evidence: Evidence


class BomResult(Strict):
    rows: list[BomRow]
    issues: list[str] = Field(default_factory=list)


class Region(Strict):
    id: str = Field(pattern=ID)
    kind: Literal["bom", "front", "face", "reference"]
    title: str
    evidence: Evidence


class Layout(Strict):
    regions: list[Region]
    issues: list[str] = Field(default_factory=list)


class Calibration(Strict):
    axis: Literal["x", "y", "distance"]
    start: Point2
    end: Point2
    length_mm: float = Field(gt=0)
    evidence: Evidence


class NodeObservation(Strict):
    id: str = Field(pattern=ID)
    xy: Point2
    evidence: Evidence


class EdgeObservation(Strict):
    id: str = Field(pattern=ID)
    mark: str | None = None  # An unread/missing callout stays unknown; validation blocks assembly.
    a: str
    b: str
    line_style: Literal["solid", "dashed", "reference"]
    evidence: Evidence


class Dimension(Strict):
    id: str = Field(pattern=ID)
    a: str
    b: str
    kind: Literal["distance", "x", "y"]
    value_mm: float = Field(gt=0)
    tolerance_mm: float = Field(default=1, gt=0, le=10)
    evidence: Evidence


class Face(Strict):
    id: str = Field(pattern=ID)
    title: str
    evidence: Evidence
    coordinate_units: Literal["page", "mm"] = "page"
    # mm is for independently measured/manual input; VLM extraction uses page.
    origin: Point2
    uniform_scale: bool = False
    calibrations: list[Calibration] = Field(default_factory=list)
    nodes: list[NodeObservation]
    edges: list[EdgeObservation]
    dimensions: list[Dimension] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class NodeRef(Strict):
    view: str
    node: str


class EdgeRef(Strict):
    view: str
    edge: str


class Joint(Strict):
    id: str = Field(pattern=ID)
    observations: list[NodeRef] = Field(min_length=1)
    evidence: Evidence


class Member(Strict):
    id: str = Field(pattern=ID)
    mark: str
    occurrences: list[EdgeRef] = Field(min_length=1)
    # Ordered against the first occurrence's a,b. Positive = end inset.
    # No implicit equality between cut length and joint span.
    end_offsets_mm: Point2 | None = None
    offset_evidence: Evidence | None = None


class Placement(Strict):
    view: str
    origin_guess: Point3 = (0, 0, 0)
    u_guess: Point3 = (1, 0, 0)
    v_guess: Point3 = (0, 0, 1)
    fixed_rotation: bool = False
    orientation_evidence: Evidence | None = None


class Relation(Strict):
    first: str
    second: str
    kind: Literal["parallel", "perpendicular"]
    evidence: Evidence


class Assembly(Strict):
    anchor_view: str
    joints: list[Joint]
    members: list[Member]
    placements: list[Placement]
    relations: list[Relation] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class Drawing(Strict):
    schema_version: Literal["1.0"] = "1.0"
    title: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_sizes: list[Point2] = Field(min_length=1)
    bom: list[BomRow]
    faces: list[Face]
    assembly: Assembly
    issues: list[str] = Field(default_factory=list)


class Audit(Strict):
    # Always advisory. An empty list is NOT an approval.
    findings: list[str]


class Review(Strict):
    drawing_sha256: str
    result_sha256: str
    reviewer: str = Field(min_length=1)
    accepted: bool = False
    checks: list[Literal["bom", "callouts", "joints", "faces", "dimensions", "assembly"]]
    notes: str = ""


class Settings(Strict):
    provider: Literal["vllm", "anthropic"] = "vllm"
    anthropic_output_mode: Literal["json_schema", "tool"] = "json_schema"
    base_url: str = "http://127.0.0.1:8000/v1"
    allow_remote: bool = False
    model: str = ""
    api_key_env: str = "VLLM_API_KEY"
    request_timeout_seconds: float = Field(default=180, ge=1)
    max_tokens: int = Field(default=12000, ge=512)
    image_max_side: int = Field(default=2400, ge=512, le=6000)
    bom_tile_height: int = Field(default=1200, ge=512, le=2400)
    projection_tolerance_mm: float = Field(default=3, gt=0, le=20)
    angle_tolerance_deg: float = Field(default=0.2, gt=0, le=2)
    solver_starts: int = Field(default=5, ge=2, le=20)
    solver_max_evaluations: int = Field(default=1500, ge=50)
    audit: bool = True
