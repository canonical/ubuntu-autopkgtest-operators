import pydantic
from ops.model import Secret


class WebsiteConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    hostname: str
    swift_auth_url: str
    swift_project_domain_name: str
    swift_project_name: str
    swift_storage_url: str
    swift_user_domain_name: str
    swift_username: str
    swift_juju_secret: Secret | None = None
