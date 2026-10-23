import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.web_auth import current_user, require_same_origin

bearer = HTTPBearer(auto_error=False)


async def authorize(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
):
    if credentials is None and current_user(request):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_same_origin(request)
        return
    token = credentials.credentials if credentials else ""
    matches = [
        hmac.compare_digest(token.encode(), allowed.encode())
        for allowed in request.app.state.settings.access_tokens
    ]
    if not credentials or credentials.scheme.lower() != "bearer" or not any(matches):
        raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"})
