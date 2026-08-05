from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from config.constants import Status

class BBox(BaseModel):
    page: int
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    normalized: bool = True

class ValidationResult(BaseModel):
    id: str
    status: Status
    category: str
    sheet: Optional[str] = None
    section: Optional[str] = None
    item: Optional[str] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    message: str
    page: Optional[int] = None
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
