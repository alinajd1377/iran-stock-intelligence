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

    async def market_watch(self) -> dict:
        """Fetch the current market-wide instrument universe from TSETMC."""
        url = f"{self.base_url}/ClosingPrice/GetMarketWatch"
        params = {"market": 0, "withBestLimits": "false", "hEven": 0, "RefID": 0}
        params.update({f"paperTypes[{i}]": i + 1 for i in range(9)})
        headers = {"User-Agent": "Mozilla/5.0 IranStockIntelligence/1.0", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected TSETMC market-watch response")
        return payload
