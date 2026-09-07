"""Media downloading utilities for chat channels.

Provides unified media download functionality that can be reused across
different channel implementations (Discord, Feishu, Matrix, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from loguru import logger


class MediaDownloader:
    """Unified media download utility for chat channels.

    Handles downloading media files (images, videos, audio, documents)
    from URLs or attachment dictionaries.

    Example:
        downloader = MediaDownloader("telegram")
        path = await downloader.download("https://example.com/image.png")
    """

    def __init__(
        self,
        channel_name: str,
        media_dir: Path | None = None,
        timeout: float = 30.0,
    ):
        """Initialize the media downloader.

        Args:
            channel_name: Name of the channel (for directory naming).
            media_dir: Optional override for media directory.
            timeout: HTTP request timeout in seconds.
        """
        self._channel_name = channel_name
        if media_dir is None:
            from nanobot.config.paths import get_media_dir
            self._media_dir = get_media_dir(channel_name)
        else:
            self._media_dir = media_dir
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def download(
        self,
        url: str,
        filename: str | None = None,
    ) -> Path | None:
        """Download a media file from a URL.

        Args:
            url: The URL to download from.
            filename: Optional filename (without extension). If not provided,
                      one will be generated from the URL.

        Returns:
            Path to the downloaded file, or None on failure.
        """
        try:
            client = await self._get_client()
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()

            # Determine extension from content-type or URL
            content_type = response.headers.get("content-type", "")
            ext = self._guess_extension(content_type, url)

            if filename is None:
                import hashlib
                filename = hashlib.md5(url.encode()).hexdigest()[:16]

            file_path = self._media_dir / f"{filename}{ext}"
            self._media_dir.mkdir(parents=True, exist_ok=True)

            file_path.write_bytes(response.content)
            logger.debug("{}: downloaded {} to {}", self._channel_name, url, file_path)
            return file_path

        except Exception as e:
            logger.warning("{}: failed to download {}: {}", self._channel_name, url, e)
            return None

    async def download_from_attachment(
        self,
        attachment: dict[str, Any],
        http_client: httpx.AsyncClient | None = None,
    ) -> Path | None:
        """Download media from an attachment dictionary.

        Args:
            attachment: Attachment dict with keys like 'url', 'filename', etc.
            http_client: Optional HTTP client to use.

        Returns:
            Path to the downloaded file, or None on failure.
        """
        url = attachment.get("url") or attachment.get("download_url")
        if not url:
            return None

        filename = attachment.get("filename") or attachment.get("name")
        return await self.download(url, filename)

    @staticmethod
    def _guess_extension(content_type: str, url: str) -> str:
        """Guess file extension from content-type or URL."""
        # Try content-type first
        ct_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/ogg": ".ogg",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "application/pdf": ".pdf",
        }
        ct_lower = content_type.lower()
        for mime, ext in ct_map.items():
            if ct_lower.startswith(mime):
                return ext

        # Fallback to URL path
        if "." in url.split("/")[-1]:
            return "." + url.split(".")[-1].split("?")[0][:4]

        return ""
