import pydantic


class AddRemoteAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the remote.")
    index: int = pydantic.Field(description="Index of the new remote.")
    token: str = pydantic.Field(
        description="LXD client token to connect to the remote."
    )


class RemoveRemoteAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the remote.")
    index: int = pydantic.Field(description="Index of the remote to remove.")
