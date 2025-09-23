import pydantic


class WebsiteConfig(pydantic.BaseModel):
    hostname: str
