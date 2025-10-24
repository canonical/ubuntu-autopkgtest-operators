import pydantic


class AddRemoteAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the remote.")
    token: str = pydantic.Field(
        description="LXD client token to connect to the remote."
    )


class RemoveRemoteAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the remote.")
