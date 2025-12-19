from pydantic import BaseModel
from datetime import datetime
from typing import Literal, Optional, List

class DoseRequest(BaseModel):
    # возраст теперь необязателен
    child_age_months: Optional[int] = None
    child_weight_kg: float
    drug_key: Literal["paracetamol", "ibuprofen"]
    route: Literal["oral"] = "oral"
    concentration_mg_per_ml: float
    last_dose_at: Optional[datetime] = None
    daily_total_mg: float = 0.0

class DoseResult(BaseModel):
    ok: bool
    message: str
    dose_mg: Optional[float] = None
    dose_ml: Optional[float] = None
    min_next_time: Optional[datetime] = None
    daily_remaining_mg: Optional[float] = None
    flags: List[str] = []

class ChildProfile(BaseModel):
    """Профиль ребенка"""
    profile_id: int
    user_id: int
    child_name: Optional[str] = None
    child_age_months: Optional[int] = None
    child_weight_kg: Optional[float] = None
    created_at: datetime
    updated_at: datetime
