# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import contextlib
import itertools
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiolimiter import AsyncLimiter

from ..aiofile import BaseAsyncIOFileWriterFactory
from ..logs import LoggerFactory
from .download_file import (
    DownloadFile,
    DownloadFileCompressionVariant,
    HashObject,
    HashType,
)
from .format import format_size
from .proxy import Proxy
from .response import DownloadResponse
from .slow_rate_protector import SlowRateProtectorFactory
from .url import URL


class HashMismatchException(Exception):
    def __init__(
        self, hash_type: HashType, path: Path, expected: str, calculated: str
    ) -> None:
        super().__init__(
            f"Hash {hash_type.value} mismatch for path {path}: "
            f"{expected} != {calculated}"
        )

        self.hash_type = hash_type
        self.path = path
        self.expected = expected
        self.calculated = calculated


@dataclass
class DownloaderSettings:
    url: URL
    aiofile_factory: BaseAsyncIOFileWriterFactory
    proxy: Proxy
    http2_disable: bool
    user_agent: str
    semaphore: asyncio.Semaphore
    slow_rate_protector_factory: SlowRateProtectorFactory
    check_hashes: set[HashType]
    rate_limiter: AsyncLimiter | None = None
    verify_ca_certificate: bool | str = True
    client_certificate: str | None = None
    client_private_key: str | None = None


class Downloader(ABC):
    BUFFER_SIZE = 8 * 1024 * 1024
    RETRY_TIMEOUT = 5
    PARTIAL_DIRECTORY = ".apt-mirror2-partial"

    def __init__(self, *, settings: DownloaderSettings):
        self._log = LoggerFactory.get_logger(
            self,
            logger_id=settings.url,
        )

        self._settings = settings

        # Download queue. Reseted in download()
        self._sources: list[DownloadFile] = []
        # Downloaded files
        self._downloaded: list[DownloadFileCompressionVariant] = []
        # Unmodified files
        self._unmodified: list[DownloadFileCompressionVariant] = []
        # Either missing on server files or files with errors
        self._missing_sources: set[Path] = set()
        self._download_start = datetime.now()

        self.reset_stats()

        self.__post_init__()

    def __post_init__(self):  # noqa: B027
        pass

    def reset_stats(self):
        self._downloaded_count = 0
        self._downloaded_size = 0
        self._unmodified_count = 0
        self._unmodified_size = 0
        self._missing_count = 0
        self._missing_size = 0
        self._error_count = 0
        self._error_size = 0

    def reset_paths(self):
        self._downloaded: list[DownloadFileCompressionVariant] = []
        self._unmodified: list[DownloadFileCompressionVariant] = []
        self._missing_sources: set[Path] = set()

    def add(self, *args: DownloadFile):
        self._sources.extend(a for a in args)

        self.reset_stats()

    @property
    def queue_files_count(self) -> int:
        return len(self._sources)

    @property
    def queue_files_size(self) -> int:
        return sum(file.size for file in self._sources)

    @property
    def queue_files_formatted_size(self) -> str:
        return format_size(self.queue_files_size)

    @property
    def downloaded_files_count(self) -> int:
        return self._downloaded_count

    @property
    def downloaded_files_size(self) -> int:
        return self._downloaded_size

    @property
    def error_files_count(self) -> int:
        return self._error_count

    @property
    def error_files_size(self) -> int:
        return self._error_size

    @property
    def missing_files_count(self) -> int:
        return self._missing_count

    @property
    def missing_files_size(self) -> int:
        return self._missing_size

    @property
    def unmodified_files_count(self) -> int:
        return self._unmodified_count

    @property
    def unmodified_files_size(self) -> int:
        return self._unmodified_size

    async def download(self, target_root_path: Path):
        async def remove_finished_tasks(tasks: set[asyncio.Task[Any]]):
            done_tasks, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            tasks.difference_update(done_tasks)

        self._download_start = datetime.now()
        tasks: set[asyncio.Task[Any]] = set()
        progress_task = asyncio.create_task(self.progress_logger())

        while self._sources:
            source_file = self._sources.pop()

            file_unmodified = False
            if source_file.check_size:
                for variant in source_file.iter_variants():
                    target_path = target_root_path / variant.get_source_path()

                    try:
                        stat = target_path.stat()
                        if stat.st_size == source_file.size:
                            self._unmodified_count += 1
                            self._unmodified_size += source_file.size
                            self._unmodified.append(variant)

                            file_unmodified = True
                            break
                    except FileNotFoundError:
                        pass

            if file_unmodified:
                continue

            tasks.add(
                asyncio.create_task(self.download_file(source_file, target_root_path))
            )

            if len(tasks) >= 128:
                await remove_finished_tasks(tasks)

        while tasks:
            await remove_finished_tasks(tasks)

        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task

        self.log_status("Download finished")

    def need_update(self, path: Path, size: int | None, date: datetime | None) -> bool:
        if path.exists() and date and size:
            stat = path.stat()
            target_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            target_size = stat.st_size

            if date == target_date and size == target_size:
                return False

        return True

    async def progress_logger(self):
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return

            self.log_status("Download progress")

    def log_status(self, message: str):
        download_rate = format_size(
            self._downloaded_size
            / (datetime.now().timestamp() - self._download_start.timestamp()),
            suffix="B/sec",
        )
        self._log.info(
            message
            + f": {self._downloaded_count} ({format_size(self._downloaded_size)},"
            f" {download_rate});"
            " unmodified:"
            f" {self._unmodified_count} ({format_size(self._unmodified_size)});"
            f" missing: {self._missing_count} ({format_size(self._missing_size)});"
            f" errors: {self._error_count} ({format_size(self._error_size)})"
        )

    @classmethod
    def get_partial_path(cls, target_root_path: Path, source_path: Path) -> Path:
        return target_root_path / cls.PARTIAL_DIRECTORY / source_path

    @classmethod
    def _prune_partial_directories(
        cls, partial_path: Path, target_root_path: Path
    ) -> None:
        partial_root = target_root_path / cls.PARTIAL_DIRECTORY
        parent = partial_path.parent
        while parent.is_relative_to(partial_root):
            try:
                parent.rmdir()
            except OSError:
                break
            if parent == partial_root:
                break
            parent = parent.parent

    @classmethod
    def _remove_partial(cls, partial_path: Path, target_root_path: Path) -> None:
        partial_path.unlink(missing_ok=True)
        cls._prune_partial_directories(partial_path, target_root_path)

    def _new_hashes(
        self, variant: DownloadFileCompressionVariant
    ) -> dict[HashType, HashObject]:
        return {
            hash_type: hash_type.get_hash_function()()
            for hash_type in variant.hashes
            if hash_type in self._settings.check_hashes
        }

    @classmethod
    async def _hash_file(
        cls, path: Path, hashes: dict[HashType, HashObject]
    ) -> int:
        size = 0
        with path.open("rb") as fp:
            while chunk := fp.read(cls.BUFFER_SIZE):
                size += len(chunk)
                for hash_function in hashes.values():
                    hash_function.update(chunk)
                await asyncio.sleep(0)
        return size

    @staticmethod
    def _check_hashes(
        source_path: Path,
        variant: DownloadFileCompressionVariant,
        hashes: dict[HashType, HashObject],
    ) -> None:
        for hash_type, hash_function in hashes.items():
            expected_hash = variant.hashes[hash_type].hash.lower()
            calculated_hash = hash_function.hexdigest().lower()
            if expected_hash != calculated_hash:
                raise HashMismatchException(
                    hash_type,
                    source_path,
                    expected_hash,
                    calculated_hash,
                )

    async def download_file(self, source_file: DownloadFile, target_root_path: Path):
        async def retry(
            message: str | None = None, sleep: bool = True, skip_try: bool = False
        ):
            nonlocal tries
            if message:
                self._log.warning(message)

            if not skip_try:
                tries -= 1

            if sleep:
                await asyncio.sleep(self.RETRY_TIMEOUT)

        error = False
        for variant in source_file.iter_variants():
            expected_size = variant.size

            for source_path in variant.get_all_paths():
                target_path = target_root_path / source_path
                partial_path = self.get_partial_path(target_root_path, source_path)
                mirror_paths = [
                    target_root_path / path for path in variant.get_all_paths()
                ]

                tries = 10
                while tries > 0:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    partial_path.parent.mkdir(parents=True, exist_ok=True)

                    resume_offset = 0
                    if expected_size > 0 and partial_path.exists():
                        partial_size = partial_path.stat().st_size
                        if partial_size > expected_size:
                            self._remove_partial(partial_path, target_root_path)
                        elif partial_size == expected_size:
                            hashes = self._new_hashes(variant)
                            actual_size = await self._hash_file(partial_path, hashes)
                            try:
                                if actual_size != expected_size:
                                    raise OSError(
                                        "Partial file size changed while validating"
                                    )
                                self._check_hashes(source_path, variant, hashes)
                            except (HashMismatchException, OSError) as ex:
                                self._log.warning(
                                    f"Discarding invalid completed partial file for"
                                    f" {source_path}: {ex}"
                                )
                                self._remove_partial(partial_path, target_root_path)
                                error = True
                            else:
                                os.replace(partial_path, target_path)
                                self._prune_partial_directories(
                                    partial_path, target_root_path
                                )
                                if mirror_paths:
                                    self.link_or_copy(target_path, *mirror_paths)
                                self._downloaded_count += 1
                                self._downloaded.append(variant)
                                self._missing_sources.difference_update(
                                    variant.get_all_paths()
                                )
                                self._log.info(
                                    f"Recovered completed partial file for"
                                    f" {source_path}"
                                )
                                return
                        elif partial_size > 0:
                            resume_offset = partial_size
                    elif partial_path.exists():
                        # Files without a trusted expected size (for example a
                        # mutable Release file) are deliberately restarted.
                        self._remove_partial(partial_path, target_root_path)

                    async with (
                        self._settings.semaphore,
                        self.stream(source_path, offset=resume_offset) as response,
                    ):
                        if response.retry:
                            await retry(skip_try=True)
                            continue

                        if response.restart:
                            self._remove_partial(partial_path, target_root_path)
                            await retry(
                                f"Server could not resume {source_path}"
                                f" ({response.error}). Restarting from byte 0...",
                                sleep=False,
                            )
                            error = True
                            continue

                        if response.missing:
                            if source_file.ignore_errors or source_file.ignore_missing:
                                break

                            await retry(
                                f"File {source_path} is missing from server."
                                " Retrying..."
                            )
                            continue

                        if response.error:
                            if source_file.ignore_errors:
                                break

                            await retry(
                                f"Received error `{response.error}` while downloading"
                                f" {source_path}. Retrying..."
                            )
                            error = True
                            continue

                        if (
                            expected_size > 0
                            and response.size
                            and response.size > 0
                            and expected_size != response.size
                        ):
                            if source_file.ignore_errors:
                                break

                            self._remove_partial(partial_path, target_root_path)
                            await retry(
                                f"Server reported size {response.size} differs from"
                                f" expected size {expected_size} for file"
                                f" {source_path}. Retrying..."
                            )
                            error = True
                            continue

                        if response.size and not self.need_update(
                            target_path, response.size, response.date
                        ):
                            self._remove_partial(partial_path, target_root_path)
                            self._unmodified_count += 1
                            self._unmodified_size += response.size

                            if mirror_paths:
                                self.link_or_copy(target_path, *mirror_paths)

                            self._downloaded.append(variant)
                            self._missing_sources.difference_update(
                                variant.get_all_paths()
                            )

                            return

                        if response.start_offset not in (0, resume_offset):
                            self._remove_partial(partial_path, target_root_path)
                            await retry(
                                f"Server returned unexpected resume offset"
                                f" {response.start_offset} for {source_path};"
                                " restarting...",
                                sleep=False,
                            )
                            error = True
                            continue

                        append = response.start_offset > 0
                        hashes = self._new_hashes(variant)
                        size = 0
                        if append:
                            try:
                                size = await self._hash_file(partial_path, hashes)
                            except FileNotFoundError:
                                size = -1
                            if size != response.start_offset:
                                self._remove_partial(partial_path, target_root_path)
                                await retry(
                                    f"Partial file changed before resuming"
                                    f" {source_path}; restarting...",
                                    sleep=False,
                                )
                                error = True
                                continue
                            self._log.info(
                                f"Resuming {source_path} at byte {size}"
                            )
                        elif resume_offset:
                            self._log.info(
                                f"Server ignored Range for {source_path};"
                                " restarting from byte 0"
                            )

                        received_size = 0
                        download_error: Exception | None = None
                        async with self._settings.aiofile_factory.open(
                            partial_path, append=append
                        ) as fp:
                            try:
                                slow_rate_protector_factory = (
                                    self._settings.slow_rate_protector_factory
                                )
                                slow_rate_protector = (
                                    slow_rate_protector_factory.for_target(
                                        variant.get_source_path()
                                    )
                                )
                                async for chunk in response.stream():
                                    if self._settings.rate_limiter:
                                        await self._settings.rate_limiter.acquire(
                                            min(
                                                len(chunk),
                                                self._settings.rate_limiter.max_rate,
                                            )
                                        )

                                    size += len(chunk)
                                    received_size += len(chunk)
                                    slow_rate_protector.rate(len(chunk))
                                    await fp.write(chunk)

                                    for hash_function in hashes.values():
                                        hash_function.update(chunk)

                                self._check_hashes(source_path, variant, hashes)

                            except Exception as ex:  # pylint: disable=W0718
                                download_error = ex

                        if download_error:
                            if isinstance(download_error, HashMismatchException):
                                self._remove_partial(partial_path, target_root_path)
                            await retry(
                                "An error "
                                f"`{download_error.__class__.__qualname__}: "
                                f"{download_error}` occurred while downloading file"
                                f" {source_path}. Retrying..."
                            )
                            error = True
                            continue

                        final_expected_size = (
                            expected_size if expected_size > 0 else response.size
                        )
                        if final_expected_size and final_expected_size != size:
                            if size > final_expected_size:
                                self._remove_partial(partial_path, target_root_path)
                            await retry(
                                f"Downloaded size {size} differs from expected size"
                                f" {final_expected_size} for file"
                                f" {source_path}. Retrying..."
                            )
                            error = True
                            continue

                        if response.date:
                            os.utime(
                                partial_path,
                                (response.date.timestamp(), response.date.timestamp()),
                            )

                        os.replace(partial_path, target_path)
                        self._prune_partial_directories(
                            partial_path, target_root_path
                        )

                        if mirror_paths:
                            self.link_or_copy(target_path, *mirror_paths)

                        self._downloaded_count += 1
                        self._downloaded_size += received_size

                        self._downloaded.append(variant)
                        self._missing_sources.difference_update(variant.get_all_paths())
                        return

        if source_file.ignore_errors:
            self._log.info(f"Unable to download `{source_file.path}`: ignoring")
            return

        if source_file.ignore_missing and not error:
            self._log.info(f"Optional file `{source_file.path}` is missing on server")
            return

        self._missing_sources.update(
            itertools.chain.from_iterable(
                v.get_all_paths() for v in source_file.compression_variants.values()
            )
        )

        if not error:
            self._missing_count += 1
            self._missing_size += source_file.size

            self._log.warning(
                f"Unable to download {source_file.path}: file is missing on server"
            )
            return

        self._error_count += 1
        self._error_size += source_file.size

        self._log.error(f"Unable to download {source_file.path}: no more tries")

    def get_downloaded_files(self) -> list[DownloadFileCompressionVariant]:
        return self._downloaded.copy()

    def get_unmodified_files(self) -> list[DownloadFileCompressionVariant]:
        return self._unmodified.copy()

    def get_all_files(self) -> list[DownloadFileCompressionVariant]:
        return self.get_downloaded_files() + self.get_unmodified_files()

    def get_downloaded_files_paths(self) -> set[Path]:
        return (
            set(
                itertools.chain.from_iterable(
                    v.get_all_paths() for v in self.get_all_files()
                )
            )
            - self.get_missing_sources()
        )

    def get_missing_sources(self):
        return self._missing_sources.copy()

    def has_errors(self):
        return self._error_count > 0

    def has_missing(self):
        return self._missing_count > 0

    @staticmethod
    def link_or_copy(source: Path, *targets: Path):
        if len(targets) > 1:
            Downloader.link_or_copy(source, targets[0])
            source = targets[0]

        for target in targets:
            if target == source:
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)

            try:
                target.hardlink_to(source)
            except OSError:
                shutil.copy2(source, target)

    @asynccontextmanager
    @abstractmethod
    async def stream(
        self, source_path: Path, offset: int = 0
    ) -> AsyncGenerator[DownloadResponse, None]:
        yield  # type: ignore
