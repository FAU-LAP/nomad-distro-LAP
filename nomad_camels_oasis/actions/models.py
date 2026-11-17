from typing import Any

from pydantic import BaseModel, Field


class CAMELSMeasurementInput(BaseModel):
    user_id: str
    # Dashboard-initiated Actions are user-owned but are not tied to an ELN
    # entry/upload. ELN-triggered Actions still provide their upload ID.
    upload_id: str | None = None
    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    # Chosen by OASIS before the start, so that a retried start is
    # recognised by CAMELS (remote computers) instead of starting twice.
    run_id: str | None = None
