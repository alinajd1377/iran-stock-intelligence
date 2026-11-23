"""TSETMC HTTP adapter; endpoint contracts require live validation."""
import httpx
from ..config import settings

class TsetmcClient:
    def __init__(self, timeout: float = 30):
        self.base_url = settings.tsetmc_base_url.rstrip("/")
        self.timeout = timeout

    async def instrument_search(self, query: str) -> dict:
        url = f"{self.base_url}/Instrument/GetInstrumentSearch/{query}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
