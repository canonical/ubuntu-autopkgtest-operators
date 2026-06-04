import enum

import pydantic


class AlertLevels(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"


class AddAlertAction(pydantic.BaseModel):
    level: AlertLevels = pydantic.Field(
        description="Level of the alert. (info, warning, danger)"
    )
    message: str = pydantic.Field(description="The alert message to display.")


class RemoveAlertAction(pydantic.BaseModel):
    alert_id: int = pydantic.Field(description="ID of the alert to remove.")
