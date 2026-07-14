"""Local filesystem storage client for Chainlit's data layer.

Without a storage_provider, SQLAlchemyDataLayer silently drops elements
(uploaded images/documents), so resumed chats would lose their attachments.
This client writes blobs under public/uploads/ and serves them back via
Chainlit's /public static route.
"""

import shutil
from pathlib import Path
from typing import Any, Dict, Union
from urllib.parse import quote

from chainlit.data.storage_clients.base import BaseStorageClient


class LocalStorageClient(BaseStorageClient):
    def __init__(self, base_dir: str = "public/uploads"):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, object_key: str) -> Path:
        # Flatten the key (it may contain slashes) into a safe filename
        safe = object_key.replace("\\", "/").replace("/", "__")
        path = (self.base_dir / safe).resolve()
        if not path.is_relative_to(self.base_dir):
            raise ValueError(f"Invalid object key: {object_key}")
        return path

    def _url_for(self, object_key: str) -> str:
        safe = object_key.replace("\\", "/").replace("/", "__")
        return f"/public/uploads/{quote(safe)}"

    async def upload_file(
        self,
        object_key: str,
        data: Union[bytes, str],
        mime: str = "application/octet-stream",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        path = self._path_for(object_key)
        if path.exists() and not overwrite:
            return {}
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)
        return {"object_key": object_key, "url": self._url_for(object_key)}

    async def get_read_url(self, object_key: str) -> str:
        return self._url_for(object_key)

    async def download_file(self, object_key: str) -> bytes:
        return self._path_for(object_key).read_bytes()

    async def delete_file(self, object_key: str) -> bool:
        path = self._path_for(object_key)
        if path.exists():
            path.unlink()
            return True
        return False

    async def close(self) -> None:
        pass
