"""Codal adapter boundary.

The public search endpoint and its response schema can change. Keep transport,
normalization and persistence separate; validate live responses before relying on
this adapter in production.
"""
import httpx
from ..config import settings

class CodalClient:
    def __init__(self, timeout: float = 30):
        self.base_url = settings.codal_base_url.rstrip("/")
        self.timeout = timeout

    async def search(self, symbol: str, page: int = 1) -> dict:
        url = f"{self.base_url}/api/search/v2/q"
        params = {"Symbol": symbol, "PageNumber": page}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected Codal response: expected an object")
        return payload
