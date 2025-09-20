import pydantic

class JanitorConfig(pydantic.BaseModel):
    # charm
    releases: str
