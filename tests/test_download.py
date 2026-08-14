import asyncio
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import httpx

from apt_mirror.download import DownloadFile, HashSum, HashType
from apt_mirror.download.downloader import (
    Downloader,
    DownloaderSettings,
)
from apt_mirror.download.proxy import Proxy
from apt_mirror.download.protocols.http import HTTPDownloader
from apt_mirror.download.response import DownloadResponse
from apt_mirror.download.slow_rate_protector import (
    SlowRateException,
    SlowRateProtector,
    SlowRateProtectorFactory,
)
from apt_mirror.download.url import URL
if TYPE_CHECKING:
    from apt_mirror.aiofile import BaseAsyncIOFileWriterFactory
else:
    BaseAsyncIOFileWriterFactory = object


class SimulatedInterruption(BaseException):
    pass


class TestDownload(TestCase):
    def test_download_file_size(self):
        file_path = Path("/tmp/t")
        file = DownloadFile.from_hashed_path(
            file_path, 1024, HashSum(HashType.MD5, ""), use_by_hash=True
        )
        file.add_compression_variant(file_path.with_suffix(".bz2"), 512)
        file.add_compression_variant(file_path.with_suffix(".xz"), 777)
        file.add_compression_variant(file_path.with_suffix(".gz"), 256)

        self.assertEqual(file.size, 777)

    def test_slow_rate_protector(self):
        with patch(f"{SlowRateProtector.__module__}.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2024, 1, 1, 0, 0, 0)

            protector = SlowRateProtector("test", 60, 100)
            protector.rate(1)

            mock_datetime.now.return_value = datetime(2024, 1, 1, 0, 0, 59)
            protector.rate(1)

            mock_datetime.now.return_value = datetime(2024, 1, 1, 0, 1, 0)
            with self.assertRaises(SlowRateException):
                protector.rate(1)

            with self.assertRaises(SlowRateException):
                protector.rate(100 * 60 - 4)

            protector.rate(1)


class TestDownloader(IsolatedAsyncioTestCase):
    CONTENTS = [
        {
            "data": (b"hello", b"world!"),
            "hashes": {
                HashSum(
                    HashType.SHA512,
                    "4e6be41aade78bebbe95662312b581088bd860320ed99cfbe5ae8ab8cf355e95f6bac60220bb0dee2d66613111c18f8ce08319d014fbc07e74001693172551c1",
                ),
                HashSum(
                    HashType.SHA256,
                    "98d234db7e91f5ba026a25d0d6f17bc5ee0a347ea2216b0c9de06d43536d49f4",
                ),
                HashSum(
                    HashType.SHA1,
                    "3c608e47152c7b175e9d3c171002dc234bb00953",
                ),
                HashSum(
                    HashType.MD5,
                    "420e57b017066b44e05ea1577f6e2e12",
                ),
            },
        }
    ]

    class _AIOFileWriterFactory(BaseAsyncIOFileWriterFactory):
        class _AIOFileWriter:
            def __init__(self, path: Path, append: bool):
                self._fp = path.open("ab" if append else "wb")

            async def write(self, data: bytes) -> int:
                return self._fp.write(data)

            def close(self):
                self._fp.close()

        @asynccontextmanager
        async def open(self, path: Path, *, append: bool = False):
            writer = self._AIOFileWriter(path, append)
            try:
                yield writer
            finally:
                writer.close()

        async def test_storage(self, *test_paths: Path): ...

    class _Downloader(Downloader):
        RETRY_TIMEOUT = 0

        def __init__(self, *, settings: DownloaderSettings, target_root_path: Path):
            super().__init__(settings=settings)
            self.response_chunks: Sequence[bytes] = []
            self.response = DownloadResponse(_stream=self.response_stream, size=0)
            self.target_root_path = target_root_path
            self.cancel_after_stream = False
            self.requested_offsets: list[int] = []

        async def response_stream(self):
            for chunk in self.response_chunks:
                yield chunk
            if self.cancel_after_stream:
                raise SimulatedInterruption()

        def set_response(
            self,
            *data: bytes,
            start_offset: int = 0,
            total_size: int | None = None,
        ):
            self.response_chunks = data
            self.response.start_offset = start_offset
            self.response.size = total_size or start_offset + sum(
                len(p) for p in data
            )

        @asynccontextmanager
        async def stream(self, source_path: Path, offset: int = 0):
            self.requested_offsets.append(offset)
            yield self.response

    @contextmanager
    def get_downloader(self, check_hashes: set[HashType] | None = None):
        if not check_hashes:
            check_hashes = {t for t in HashType}

        with TemporaryDirectory() as tmpdir:
            yield (
                self._Downloader(
                    settings=DownloaderSettings(
                        url=URL.from_string("http://localhost.local/repo"),
                        aiofile_factory=self._AIOFileWriterFactory(),
                        proxy=Proxy(False, None, None, None, None),
                        http2_disable=False,
                        user_agent="apt-mirror2-test",
                        semaphore=asyncio.Semaphore(1),
                        slow_rate_protector_factory=SlowRateProtectorFactory(
                            False, 0, 0
                        ),
                        check_hashes=check_hashes,
                    ),
                    target_root_path=Path(tmpdir),
                )
            )

    def get_download_file(self, path: Path, size: int, hash_sums: Iterable[HashSum]):
        download_file = DownloadFile.from_path(path)

        for hash_sum in hash_sums:
            download_file.add_compression_variant(
                path,
                size,
                hash_sum,
            )

        return download_file

    async def test_download_file(self):
        file_path = Path("dists/test/InRelease")
        content = self.CONTENTS[0]["data"]

        with self.get_downloader() as downloader:
            downloader.set_response(*content)
            content = b"".join(content)

            source_file = self.get_download_file(file_path, len(content), [])

            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.downloaded_files_count, 1)
            self.assertEqual(downloader.downloaded_files_size, len(content))
            self.assertFalse(downloader.has_errors())
            self.assertFalse(downloader.has_missing())
            self.assertEqual(
                (downloader.target_root_path / file_path).read_bytes(),
                content,
            )

    async def test_download_hashes(self):
        file_path = Path("dists/test/InRelease")
        content: tuple[bytes] = self.CONTENTS[0]["data"]
        content_hashes: set[HashSum] = self.CONTENTS[0]["hashes"]

        with self.get_downloader() as downloader:
            downloader.set_response(*content)
            content_bytes = b"".join(content)

            source_file = self.get_download_file(
                file_path,
                len(content_bytes),
                (),
            )

            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.downloaded_files_count, 1)
            self.assertEqual(downloader.downloaded_files_size, len(content_bytes))
            self.assertFalse(downloader.has_errors())
            self.assertFalse(downloader.has_missing())
            self.assertEqual(
                (downloader.target_root_path / file_path).read_bytes(),
                content_bytes,
            )

            downloader.reset_stats()
            (downloader.target_root_path / file_path).unlink()

            broken_hashes = deepcopy(content_hashes)
            for h in broken_hashes:
                if h.type == HashType.MD5:
                    h.hash = "broken"
                    break

            source_file = self.get_download_file(
                file_path,
                len(content_bytes),
                broken_hashes,
            )

            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.downloaded_files_count, 0)
            self.assertEqual(downloader.downloaded_files_size, 0)
            self.assertTrue(downloader.has_errors())
            self.assertFalse(downloader.has_missing())
            self.assertFalse((downloader.target_root_path / file_path).exists())

        with self.get_downloader(check_hashes={HashType.SHA512}) as downloader:
            downloader.set_response(*content)
            content_bytes = b"".join(content)

            broken_hashes = deepcopy(content_hashes)
            for h in broken_hashes:
                if h.type == HashType.MD5:
                    h.hash = "broken"
                    break

            source_file = self.get_download_file(
                file_path,
                len(content_bytes),
                broken_hashes,
            )

            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.downloaded_files_count, 1)
            self.assertEqual(downloader.downloaded_files_size, len(content_bytes))
            self.assertFalse(downloader.has_errors())
            self.assertFalse(downloader.has_missing())
            self.assertEqual(
                (downloader.target_root_path / file_path).read_bytes(),
                content_bytes,
            )

    async def test_interrupted_download_resumes_from_persistent_partial(self):
        file_path = Path("pool/test/package.deb")
        content: tuple[bytes] = self.CONTENTS[0]["data"]
        content_bytes = b"".join(content)
        content_hashes: set[HashSum] = self.CONTENTS[0]["hashes"]

        with self.get_downloader() as downloader:
            source_file = self.get_download_file(
                file_path, len(content_bytes), content_hashes
            )
            target = downloader.target_root_path / file_path
            target.parent.mkdir(parents=True)
            target.write_bytes(b"previous complete version")

            downloader.set_response(
                content[0], start_offset=0, total_size=len(content_bytes)
            )
            downloader.cancel_after_stream = True
            with self.assertRaises(SimulatedInterruption):
                await downloader.download_file(
                    source_file, downloader.target_root_path
                )

            partial = downloader.get_partial_path(
                downloader.target_root_path, file_path
            )
            self.assertEqual(partial.read_bytes(), content[0])
            self.assertEqual(target.read_bytes(), b"previous complete version")

            downloader.cancel_after_stream = False
            downloader.set_response(
                content[1],
                start_offset=len(content[0]),
                total_size=len(content_bytes),
            )
            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.requested_offsets, [0, len(content[0])])
            self.assertEqual(target.read_bytes(), content_bytes)
            self.assertFalse(partial.exists())
            self.assertEqual(downloader.downloaded_files_size, len(content[1]))

    async def test_range_ignored_restarts_partial_safely(self):
        file_path = Path("pool/test/package.deb")
        content: tuple[bytes] = self.CONTENTS[0]["data"]
        content_bytes = b"".join(content)
        content_hashes: set[HashSum] = self.CONTENTS[0]["hashes"]

        with self.get_downloader() as downloader:
            source_file = self.get_download_file(
                file_path, len(content_bytes), content_hashes
            )
            partial = downloader.get_partial_path(
                downloader.target_root_path, file_path
            )
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"wrong")

            # start_offset=0 models an HTTP/200 response from a server which
            # ignored the requested Range header.
            downloader.set_response(
                *content, start_offset=0, total_size=len(content_bytes)
            )
            await downloader.download_file(source_file, downloader.target_root_path)

            self.assertEqual(downloader.requested_offsets, [5])
            self.assertEqual(
                (downloader.target_root_path / file_path).read_bytes(),
                content_bytes,
            )
            self.assertFalse(partial.exists())

    async def test_http_range_response_reports_total_size_and_offset(self):
        requested_ranges: list[str | None] = []

        def handler(request: httpx.Request):
            requested_ranges.append(request.headers.get("Range"))
            return httpx.Response(
                206,
                headers={
                    "Content-Range": "bytes 5-10/11",
                    "Content-Length": "6",
                },
                content=b"world!",
            )

        settings = DownloaderSettings(
            url=URL.from_string("http://localhost.local/repo"),
            aiofile_factory=self._AIOFileWriterFactory(),
            proxy=Proxy(False, None, None, None, None),
            http2_disable=False,
            user_agent="apt-mirror2-test",
            semaphore=asyncio.Semaphore(1),
            slow_rate_protector_factory=SlowRateProtectorFactory(False, 0, 0),
            check_hashes={t for t in HashType},
        )
        downloader = HTTPDownloader(settings=settings)
        await downloader._httpx.aclose()  # pylint: disable=W0212
        downloader._httpx = httpx.AsyncClient(  # pylint: disable=W0212
            transport=httpx.MockTransport(handler),
            base_url="http://localhost.local/repo/",
        )
        try:
            async with downloader.stream(Path("pool/test.deb"), offset=5) as response:
                body = b"".join([chunk async for chunk in response.stream()])
                self.assertEqual(response.start_offset, 5)
                self.assertEqual(response.size, 11)
                self.assertEqual(body, b"world!")
        finally:
            await downloader._httpx.aclose()  # pylint: disable=W0212

        self.assertEqual(requested_ranges, ["bytes=5-"])
