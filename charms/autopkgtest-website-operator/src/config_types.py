import pydantic


class WebsiteConfig(pydantic.BaseModel):
    hostname: str
    swift_auth_url: str
    swift_password: pydantic.SecretStr
    swift_project_domain_name: str
    swift_project_name: str
    swift_storage_url: str
    swift_user_domain_name: str
    swift_username: str
