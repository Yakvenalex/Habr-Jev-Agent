import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


async def authorize(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
):
    token = credentials.credentials if credentials else ""
    matches = [
        hmac.compare_digest(token.encode(), allowed.encode())
        for allowed in request.app.state.settings.access_tokens
    ]
    if not credentials or credentials.scheme.lower() != "bearer" or not any(matches):
        raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"})
