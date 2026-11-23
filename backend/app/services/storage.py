"""Content-addressed raw document storage."""
from hashlib import sha256
from pathlib import Path
import re
import httpx
from ..config import settings

async def download_document(url: str, root: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
    content = response.content
    digest = sha256(content).hexdigest()
    suffix = Path(url.split("?")[0]).suffix.lower()
    if not re.fullmatch(r"\\.[a-z0-9]{1,8}", suffix): suffix = ".bin"
    directory = Path(root or settings.raw_data_dir) / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}{suffix}"
    target.write_bytes(content)
    return {"path": str(target), "sha256": digest, "size": len(content), "content_type": response.headers.get("content-type")}
