import time

import httpx
import jwt

from app.config import Settings


class GhostClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.ghost_url + "/ghost/api/admin/",
            timeout=settings.external_timeout_seconds,
            transport=transport,
        )

    def headers(self):
        key = self.settings.ghost_admin_api_key.get_secret_value()
        try:
            key_id, secret = key.split(":", 1)
            secret_bytes = bytes.fromhex(secret)
            if len(secret_bytes) != 32 or not key_id:
                raise ValueError
        except ValueError:
            raise ValueError("Ghost Admin API key is not configured correctly") from None
        now = int(time.time())
        token = jwt.encode(
            {"iat": now, "exp": now + 300, "aud": "/admin/"},
            secret_bytes,
            algorithm="HS256",
            headers={"kid": key_id},
        )
        return {"Authorization": f"Ghost {token}", "Accept-Version": "v6.0"}

    async def find(self, slug: str):
        response = await self.http.get(
            "posts/",
            params={
                "filter": f"slug:{slug}",
                "formats": "html",
                "limit": "1",
            },
            headers=self.headers(),
        )
        response.raise_for_status()
        posts = response.json()["posts"]
        return posts[0] if posts else None

    async def publish(self, publication):
        response = await self.http.post(
            "posts/",
            params={"source": "html"},
            headers=self.headers(),
            json={
                "posts": [
                    {
                        "title": publication.title,
                        "slug": publication.slug,
                        "html": publication.content_html,
                        "custom_excerpt": publication.summary,
                        "tags": [{"name": tag} for tag in publication.tags],
                        "status": "published",
                    }
                ]
            },
        )
        response.raise_for_status()
        return response.json()["posts"][0]

    async def ready(self):
        response = await self.http.get("site/", headers=self.headers())
        response.raise_for_status()
        return True

    async def close(self):
        await self.http.aclose()
