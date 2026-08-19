# SPDX-License-Identifier: GPL-3.0-or-later

"""Create and apply verifiable, incremental bundles for air-gapped mirrors.

The bundle is intentionally based only on the Python standard library.  The
external host can therefore run the normal apt-mirror process in WSL and use
this module to copy the resulting mirror onto removable media.  The internal
host verifies every file in the final snapshot, including files which were not
present in the incremental payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

FORMAT_VERSION = 1
MANIFEST_FORMAT = "apt-mirror2-offline-manifest"
BUNDLE_FORMAT = "apt-mirror2-offline-bundle"
VOLUME_FORMAT = "apt-mirror2-offline-volume"
ACK_FORMAT = "apt-mirror2-offline-ack"
REPAIR_FORMAT = "apt-mirror2-offline-repair"
STATE_DIRECTORY = ".apt-mirror-offline"
DOWNLOAD_PARTIAL_DIRECTORY = ".apt-mirror2-partial"
BUFFER_SIZE = 4 * 1024 * 1024
ACTION_REQUIRED = 3
VOLUME_NAME_PATTERN = re.compile(r"^volume-[0-9]{4}$")
BUNDLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class OfflineError(RuntimeError):
    """An invalid or unsafe offline operation."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str

    @classmethod
    def from_dict(cls, value: Any) -> "FileEntry":
        if not isinstance(value, dict):
            raise OfflineError("Invalid file entry in manifest")

        try:
            path = _validated_relative_path(value["path"])
            size = int(value["size"])
            digest = str(value["sha256"]).lower()
        except (KeyError, TypeError, ValueError) as ex:
            raise OfflineError("Invalid file entry in manifest") from ex

        if size < 0 or len(digest) != 64:
            raise OfflineError(f"Invalid size or SHA256 for {path}")
        try:
            bytes.fromhex(digest)
        except ValueError as ex:
            raise OfflineError(f"Invalid SHA256 for {path}") from ex

        return cls(path=path, size=size, sha256=digest)

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class VolumeFileEntry:
    path: str
    stored_path: str
    size: int
    sha256: str

    @classmethod
    def from_dict(cls, value: Any) -> "VolumeFileEntry":
        if not isinstance(value, dict):
            raise OfflineError("Invalid volume file entry")

        try:
            path = _validated_relative_path(value["path"])
            size = int(value["size"])
            digest = str(value["sha256"]).lower()
        except (KeyError, TypeError, ValueError) as ex:
            raise OfflineError("Invalid volume file entry") from ex

        stored_path_value = value.get("stored_path")
        if stored_path_value is None:
            if not _windows_exfat_safe_relative_path(path):
                raise OfflineError(f"Missing stored path for unsafe volume entry: {path}")
            stored_path = path
        else:
            stored_path = _validated_relative_path(stored_path_value)
            if _stored_relative_path(path) != stored_path:
                raise OfflineError(f"Invalid stored path for {path}")

        if size < 0 or len(digest) != 64:
            raise OfflineError(f"Invalid size or SHA256 for {path}")
        try:
            bytes.fromhex(digest)
        except ValueError as ex:
            raise OfflineError(f"Invalid SHA256 for {path}") from ex

        return cls(path=path, stored_path=stored_path, size=size, sha256=digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "stored_path": self.stored_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Manifest:
    snapshot_id: str
    created_at: str
    files: tuple[FileEntry, ...]

    @property
    def by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": MANIFEST_FORMAT,
            "version": FORMAT_VERSION,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "hash": "sha256",
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "files": [entry.as_dict() for entry in self.files],
        }


@dataclass(frozen=True)
class VerificationIssue:
    path: str
    reason: str
    expected_sha256: str
    actual_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "path": self.path,
            "reason": self.reason,
            "expected_sha256": self.expected_sha256,
        }
        if self.actual_sha256 is not None:
            result["actual_sha256"] = self.actual_sha256
        return result


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validated_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise OfflineError(f"Unsafe relative path: {value!r}")

    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise OfflineError(f"Unsafe relative path: {value!r}")

    normalized = path.as_posix()
    if normalized == STATE_DIRECTORY or normalized.startswith(f"{STATE_DIRECTORY}/"):
        raise OfflineError(f"Reserved relative path: {value!r}")
    return normalized


def _is_windows_exfat_safe_component(part: str) -> bool:
    return (
        part
        and part not in (".", "..")
        and not any(character in '<>:"|?*\x00' or ord(character) < 32 for character in part)
        and not part.endswith((" ", "."))
        and part.split(".", maxsplit=1)[0].casefold() not in WINDOWS_RESERVED_NAMES
    )


def _windows_exfat_safe_relative_path(value: str) -> bool:
    try:
        path = PurePosixPath(_validated_relative_path(value))
    except OfflineError:
        return False
    return all(_is_windows_exfat_safe_component(part) for part in path.parts)


def _stored_path_component(part: str) -> str:
    if _is_windows_exfat_safe_component(part) and "%" not in part:
        return part
    if part.endswith((" ", ".")) or part.split(".", maxsplit=1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        encoded = "".join(f"%{byte:02X}" for byte in part.encode("utf-8"))
    else:
        pieces: list[str] = []
        for character in part:
            if character == "%" or ord(character) < 32 or character in '<>:"|?*':
                pieces.append("".join(f"%{byte:02X}" for byte in character.encode("utf-8")))
            else:
                pieces.append(character)
        encoded = "".join(pieces)
    if len(encoded) > 240:
        encoded = "~" + hashlib.sha256(part.encode("utf-8")).hexdigest()
    return encoded


def _stored_relative_path(value: str) -> str:
    path = PurePosixPath(_validated_relative_path(value))
    return "/".join(_stored_path_component(part) for part in path.parts)


def _path(root: Path, relative: str) -> Path:
    relative = _validated_relative_path(relative)
    root = root.resolve(strict=False)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    parent = root
    for part in PurePosixPath(relative).parts[:-1]:
        parent /= part
        if parent.is_symlink():
            raise OfflineError(f"Symlinked directories are not allowed: {parent}")
    return candidate


def _validated_volume_name(value: Any) -> str:
    name = str(value)
    if not VOLUME_NAME_PATTERN.fullmatch(name):
        raise OfflineError(f"Invalid volume name: {value!r}")
    return name


def _bundle_volume_descriptions(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw_volumes = metadata.get("volumes")
    if not isinstance(raw_volumes, list) or not raw_volumes:
        raise OfflineError("Bundle has no volume descriptions")
    descriptions: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_volume in raw_volumes:
        if not isinstance(raw_volume, dict):
            raise OfflineError("Invalid bundle volume description")
        name = _validated_volume_name(raw_volume.get("name"))
        if name in names:
            raise OfflineError(f"Duplicate bundle volume: {name}")
        names.add(name)
        descriptions.append(raw_volume)
    return descriptions


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(path, _json_bytes(value))


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{size} B"
    return f"{value:.1f} {units[index]}"


@dataclass
class ProgressReporter:
    total_files: int
    total_bytes: int
    stream: Any | None = None
    last_bucket: int = -1

    def note(self, message: str) -> None:
        print(message, file=self.stream if self.stream is not None else sys.stderr, flush=True)

    def advance(self, completed_files: int, completed_bytes: int) -> None:
        if self.total_files <= 0:
            return
        percent = min(100, completed_files * 100 // self.total_files)
        bucket = 100 if percent == 100 else percent // 2 * 2
        if bucket != self.last_bucket or completed_files == self.total_files:
            self.last_bucket = bucket
            self.note(
                f"  {percent:3d}% ({completed_files}/{self.total_files} files, "
                f"{_format_bytes(completed_bytes)}/{_format_bytes(self.total_bytes)})"
            )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rt", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as ex:
        raise OfflineError(f"Unable to read JSON file {path}: {ex}") from ex
    if not isinstance(value, dict):
        raise OfflineError(f"JSON root must be an object: {path}")
    return value


def _snapshot_id(files: Iterable[FileEntry]) -> str:
    digest = hashlib.sha256()
    for entry in files:
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_manifest(path: Path) -> Manifest:
    value = _read_json(path)
    if value.get("format") != MANIFEST_FORMAT or value.get("version") != FORMAT_VERSION:
        raise OfflineError(f"Unsupported offline manifest: {path}")

    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise OfflineError(f"Manifest has no file list: {path}")
    files = tuple(
        sorted((FileEntry.from_dict(item) for item in raw_files), key=lambda e: e.path)
    )
    if len({entry.path for entry in files}) != len(files):
        raise OfflineError(f"Manifest contains duplicate paths: {path}")

    calculated_id = _snapshot_id(files)
    if value.get("snapshot_id") != calculated_id:
        raise OfflineError(f"Manifest snapshot ID does not match its contents: {path}")
    if value.get("file_count") not in (None, len(files)):
        raise OfflineError(f"Manifest file count is invalid: {path}")

    return Manifest(calculated_id, str(value.get("created_at", "")), files)


def _load_hash_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        value = _read_json(path)
    except OfflineError:
        return {}
    if value.get("version") != FORMAT_VERSION or not isinstance(value.get("files"), dict):
        return {}
    return value["files"]


def build_manifest(
    root: Path, cache_path: Path | None = None, rehash: bool = False
) -> Manifest:
    root = root.resolve()
    if not root.is_dir():
        raise OfflineError(f"Mirror root is not a directory: {root}")

    old_cache = {} if rehash or cache_path is None else _load_hash_cache(cache_path)
    new_cache: dict[str, dict[str, Any]] = {}
    entries: list[FileEntry] = []
    casefold_paths: dict[str, str] = {}

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path == root and STATE_DIRECTORY in directory_names:
            directory_names.remove(STATE_DIRECTORY)
        if DOWNLOAD_PARTIAL_DIRECTORY in directory_names:
            directory_names.remove(DOWNLOAD_PARTIAL_DIRECTORY)
        directory_names.sort()
        file_names.sort()

        for directory_name in directory_names:
            if (current_path / directory_name).is_symlink():
                raise OfflineError(
                    "Directory symlinks are not supported: "
                    f"{current_path / directory_name}"
                )

        for file_name in file_names:
            absolute = current_path / file_name
            if absolute.is_symlink() or not absolute.is_file():
                raise OfflineError(f"Only regular mirror files are supported: {absolute}")
            relative = _validated_relative_path(absolute.relative_to(root).as_posix())
            folded = relative.casefold()
            if folded in casefold_paths and casefold_paths[folded] != relative:
                raise OfflineError(
                    "The mirror contains paths which collide on Windows media: "
                    f"{casefold_paths[folded]} and {relative}"
                )
            casefold_paths[folded] = relative

            before = absolute.stat()
            cached = old_cache.get(relative)
            if (
                isinstance(cached, dict)
                and cached.get("size") == before.st_size
                and cached.get("mtime_ns") == before.st_mtime_ns
                and isinstance(cached.get("sha256"), str)
            ):
                digest = cached["sha256"]
            else:
                digest = _sha256(absolute)
            after = absolute.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OfflineError(f"Mirror changed while it was being scanned: {absolute}")

            entry = FileEntry(relative, after.st_size, digest)
            entries.append(entry)
            new_cache[relative] = {
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "sha256": digest,
            }

    entries.sort(key=lambda entry: entry.path)
    manifest = Manifest(_snapshot_id(entries), _now(), tuple(entries))
    if cache_path is not None:
        _write_json_atomic(cache_path, {"version": FORMAT_VERSION, "files": new_cache})
    return manifest


def _copy_verified(source: Path, destination: Path, entry: FileEntry) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != entry.size or _sha256(temporary) != entry.sha256:
            raise OfflineError(f"File changed or media write failed while copying {entry.path}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _load_feedback(state_dir: Path, feedback_dir: Path | None) -> set[str]:
    if feedback_dir is None or not feedback_dir.is_dir():
        return set()

    ack_path = feedback_dir / "ack.json"
    if ack_path.is_file():
        ack = _read_json(ack_path)
        if ack.get("format") != ACK_FORMAT or ack.get("version") != FORMAT_VERSION:
            raise OfflineError(f"Invalid acknowledgement: {ack_path}")
        snapshot_id = str(ack.get("snapshot_id", ""))
        known_manifest = state_dir / "manifests" / f"{snapshot_id}.json"
        manifest = load_manifest(known_manifest)
        if manifest.snapshot_id != snapshot_id:
            raise OfflineError(f"Unknown acknowledged snapshot: {snapshot_id}")
        _write_json_atomic(state_dir / "accepted-manifest.json", manifest.as_dict())

    repair_path = feedback_dir / "repair-request.json"
    if not repair_path.is_file():
        return set()
    repair = _read_json(repair_path)
    if repair.get("format") != REPAIR_FORMAT or repair.get("version") != FORMAT_VERSION:
        raise OfflineError(f"Invalid repair request: {repair_path}")
    raw_files = repair.get("files")
    if not isinstance(raw_files, list):
        raise OfflineError(f"Invalid repair request file list: {repair_path}")
    installed_snapshot_id = repair.get("installed_snapshot_id")
    if installed_snapshot_id is not None:
        known_manifest = state_dir / "manifests" / f"{installed_snapshot_id}.json"
        installed_manifest = load_manifest(known_manifest)
        if installed_manifest.snapshot_id != installed_snapshot_id:
            raise OfflineError(f"Unknown installed snapshot: {installed_snapshot_id}")
        _write_json_atomic(
            state_dir / "accepted-manifest.json", installed_manifest.as_dict()
        )
    return {
        _validated_relative_path(item["path"] if isinstance(item, dict) else item)
        for item in raw_files
    }


def _partition_volumes(entries: Sequence[FileEntry], maximum: int) -> list[list[FileEntry]]:
    if not entries:
        return [[]]
    if maximum <= 0:
        return [list(entries)]

    result: list[list[FileEntry]] = []
    current: list[FileEntry] = []
    current_size = 0
    for entry in entries:
        if entry.size > maximum:
            raise OfflineError(
                f"{entry.path} ({entry.size} bytes) is larger than the configured volume size"
            )
        if current and current_size + entry.size > maximum:
            result.append(current)
            current = []
            current_size = 0
        current.append(entry)
        current_size += entry.size
    if current:
        result.append(current)
    return result


def export_bundle(
    source: Path,
    bundle: Path,
    state_dir: Path,
    feedback_dir: Path | None = None,
    volume_size: int = 0,
    rehash_source: bool = False,
    show_progress: bool = False,
) -> dict[str, Any]:
    source = source.resolve()
    state_resolved = state_dir.resolve(strict=False)
    bundle_resolved = bundle.resolve(strict=False)
    for name, candidate in (("State directory", state_resolved), ("Bundle", bundle_resolved)):
        try:
            candidate.relative_to(source)
        except ValueError:
            continue
        raise OfflineError(f"{name} must not be inside the mirror source: {candidate}")
    if bundle.exists():
        raise OfflineError(f"Bundle destination already exists: {bundle}")
    state_dir.mkdir(parents=True, exist_ok=True)
    repair_paths = _load_feedback(state_dir, feedback_dir)
    accepted_path = state_dir / "accepted-manifest.json"
    base = load_manifest(accepted_path) if accepted_path.is_file() else None
    if show_progress:
        print("Scanning source mirror and computing hashes...", file=sys.stderr, flush=True)
    target = build_manifest(source, state_dir / "hash-cache.json", rehash_source)

    base_files = base.by_path if base else {}
    target_files = target.by_path
    changed = [
        entry
        for entry in target.files
        if base_files.get(entry.path) != entry or entry.path in repair_paths
    ]
    deleted = [
        entry for path, entry in sorted(base_files.items()) if path not in target_files
    ]
    partitions = _partition_volumes(changed, volume_size)
    progress = (
        ProgressReporter(len(changed), sum(entry.size for entry in changed))
        if show_progress
        else None
    )
    if progress is not None:
        if changed:
            progress.note(
                f"Copying verified payloads: {len(changed)} file(s), "
                f"{_format_bytes(progress.total_bytes)} across {len(partitions)} volume(s)."
            )
        else:
            progress.note("No payload files changed; writing metadata only.")
    bundle_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"-{target.snapshot_id[:12]}-{uuid.uuid4().hex[:8]}"
    )
    volume_descriptions = [
        {
            "name": f"volume-{index:04d}",
            "file_count": len(entries),
            "payload_bytes": sum(entry.size for entry in entries),
            "files": [entry.path for entry in entries],
        }
        for index, entries in enumerate(partitions, start=1)
    ]
    volume_entries = [
        [
            VolumeFileEntry(
                path=entry.path,
                stored_path=_stored_relative_path(entry.path),
                size=entry.size,
                sha256=entry.sha256,
            )
            for entry in entries
        ]
        for entries in partitions
    ]

    partial = bundle.with_name(f".{bundle.name}.{uuid.uuid4().hex}.partial")
    try:
        partial.mkdir(parents=True)
        manifest_bytes = _json_bytes(target.as_dict())
        metadata = {
            "format": BUNDLE_FORMAT,
            "version": FORMAT_VERSION,
            "bundle_id": bundle_id,
            "created_at": _now(),
            "base_snapshot_id": base.snapshot_id if base else None,
            "target_snapshot_id": target.snapshot_id,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "changed_file_count": len(changed),
            "deleted_file_count": len(deleted),
            "payload_bytes": sum(entry.size for entry in changed),
            "deleted": [entry.as_dict() for entry in deleted],
            "volumes": volume_descriptions,
        }
        metadata_bytes = _json_bytes(metadata)
        _write_bytes_atomic(partial / "manifest.json", manifest_bytes)
        _write_bytes_atomic(partial / "bundle.json", metadata_bytes)

        copied_files = 0
        copied_bytes = 0
        for description, entries, stored_entries in zip(
            volume_descriptions, partitions, volume_entries, strict=True
        ):
            volume_root = partial / "volumes" / description["name"]
            volume_root.mkdir(parents=True)
            volume_metadata = {
                "format": VOLUME_FORMAT,
                "version": FORMAT_VERSION,
                "bundle_id": bundle_id,
                "name": description["name"],
                "volume_count": len(partitions),
                "bundle_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "files": [entry.as_dict() for entry in stored_entries],
            }
            _write_bytes_atomic(volume_root / "bundle.json", metadata_bytes)
            _write_bytes_atomic(volume_root / "manifest.json", manifest_bytes)
            _write_json_atomic(volume_root / "volume.json", volume_metadata)
            for entry, stored_entry in zip(entries, stored_entries, strict=True):
                _copy_verified(
                    _path(source, entry.path),
                    _path(volume_root / "payload", stored_entry.stored_path),
                    entry,
                )
                copied_files += 1
                copied_bytes += entry.size
                if progress is not None:
                    progress.advance(copied_files, copied_bytes)
            _write_bytes_atomic(volume_root / "READY", b"ready\n")

        _write_bytes_atomic(partial / "READY", b"ready\n")
        bundle.parent.mkdir(parents=True, exist_ok=True)
        partial.rename(bundle)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise

    _write_json_atomic(
        state_dir / "manifests" / f"{target.snapshot_id}.json", target.as_dict()
    )
    print(
        f"Created {bundle}: {len(changed)} changed/new files, {len(deleted)} deletions, "
        f"{sum(entry.size for entry in changed)} payload bytes, {len(partitions)} volume(s)."
    )
    return metadata


def _load_bundle(
    bundle: Path, require_all_volumes: bool = True
) -> tuple[dict[str, Any], Manifest]:
    if not (bundle / "READY").is_file():
        raise OfflineError(f"Bundle is incomplete (READY is missing): {bundle}")
    metadata_path = bundle / "bundle.json"
    manifest_path = bundle / "manifest.json"
    metadata = _read_json(metadata_path)
    if metadata.get("format") != BUNDLE_FORMAT or metadata.get("version") != FORMAT_VERSION:
        raise OfflineError(f"Unsupported offline bundle: {bundle}")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != metadata.get("manifest_sha256"):
        raise OfflineError("Bundle manifest checksum mismatch")
    manifest = load_manifest(manifest_path)
    if manifest.snapshot_id != metadata.get("target_snapshot_id"):
        raise OfflineError("Bundle target snapshot does not match its manifest")

    raw_volumes = _bundle_volume_descriptions(metadata)
    if require_all_volumes:
        expected_changed: set[str] = set()
        for raw_volume in raw_volumes:
            name = _validated_volume_name(raw_volume.get("name"))
            volume_root = bundle / "volumes" / name
            volume = _validate_volume(
                volume_root,
                metadata,
                manifest,
                verify_payload=True,
                expected_name=name,
            )
            files = raw_volume.get("files")
            if not isinstance(files, list):
                raise OfflineError(f"Invalid file list for {name}")
            described = [_validated_relative_path(path) for path in files]
            actual = [FileEntry.from_dict(value).path for value in volume["files"]]
            if described != actual:
                raise OfflineError(f"Volume file list differs from bundle metadata: {name}")
            for path in described:
                if path in expected_changed:
                    raise OfflineError(f"Payload path occurs in more than one volume: {path}")
                expected_changed.add(path)
        if len(expected_changed) != int(metadata.get("changed_file_count", -1)):
            raise OfflineError("Bundle changed-file count is inconsistent")
    return metadata, manifest


def _validate_volume(
    volume_root: Path,
    bundle_metadata: dict[str, Any],
    manifest: Manifest,
    verify_payload: bool,
    expected_name: str | None = None,
) -> dict[str, Any]:
    if not (volume_root / "READY").is_file():
        raise OfflineError(f"Volume is incomplete: {volume_root}")
    volume = _read_json(volume_root / "volume.json")
    if volume.get("format") != VOLUME_FORMAT or volume.get("version") != FORMAT_VERSION:
        raise OfflineError(f"Unsupported volume: {volume_root}")
    if volume.get("bundle_id") != bundle_metadata.get("bundle_id"):
        raise OfflineError(f"Volume belongs to a different bundle: {volume_root}")
    name = _validated_volume_name(volume.get("name"))
    if expected_name is not None and name != expected_name:
        raise OfflineError(f"Volume name mismatch: {volume_root}")
    if _sha256(volume_root / "bundle.json") != volume.get("bundle_sha256"):
        raise OfflineError(f"Bundle metadata checksum mismatch in {volume_root}")
    if _sha256(volume_root / "manifest.json") != volume.get("manifest_sha256"):
        raise OfflineError(f"Manifest checksum mismatch in {volume_root}")
    if _read_json(volume_root / "bundle.json") != bundle_metadata:
        raise OfflineError(f"Bundle metadata differs in {volume_root}")
    if load_manifest(volume_root / "manifest.json").snapshot_id != manifest.snapshot_id:
        raise OfflineError(f"Manifest differs in {volume_root}")

    raw_files = volume.get("files")
    if not isinstance(raw_files, list):
        raise OfflineError(f"Volume has no file list: {volume_root}")
    target_files = manifest.by_path
    entries = [VolumeFileEntry.from_dict(value) for value in raw_files]
    for entry in entries:
        if target_files.get(entry.path) != FileEntry(entry.path, entry.size, entry.sha256):
            raise OfflineError(f"Volume entry is absent from target manifest: {entry.path}")
        if verify_payload:
            payload = _path(volume_root / "payload", entry.stored_path)
            if not payload.is_file() or payload.stat().st_size != entry.size:
                raise OfflineError(f"Payload is missing or truncated: {entry.path}")
            if _sha256(payload) != entry.sha256:
                raise OfflineError(f"Payload checksum mismatch: {entry.path}")
    return volume


def stage_volumes(source: Path, staging_dir: Path) -> tuple[Path, bool]:
    if (source / "volume.json").is_file():
        volume_roots = [source]
        metadata, manifest = (
            _read_json(source / "bundle.json"),
            load_manifest(source / "manifest.json"),
        )
    else:
        metadata, manifest = _load_bundle(source, require_all_volumes=False)
        volume_roots = sorted((source / "volumes").glob("volume-*"))
    if metadata.get("format") != BUNDLE_FORMAT or metadata.get("version") != FORMAT_VERSION:
        raise OfflineError(f"Unsupported offline bundle metadata: {source}")
    if manifest.snapshot_id != metadata.get("target_snapshot_id"):
        raise OfflineError("Volume target snapshot does not match its manifest")

    bundle_id = str(metadata.get("bundle_id", ""))
    if not BUNDLE_ID_PATTERN.fullmatch(bundle_id):
        raise OfflineError("Invalid bundle ID")
    descriptions = {
        _validated_volume_name(item["name"]): item
        for item in _bundle_volume_descriptions(metadata)
    }
    destination = staging_dir / bundle_id
    destination.mkdir(parents=True, exist_ok=True)
    metadata_bytes = _json_bytes(metadata)
    manifest_bytes = _json_bytes(manifest.as_dict())
    for name, data in (("bundle.json", metadata_bytes), ("manifest.json", manifest_bytes)):
        target = destination / name
        if target.is_file() and target.read_bytes() != data:
            raise OfflineError(f"Staging directory contains another bundle: {destination}")
        _write_bytes_atomic(target, data)

    for volume_root in volume_roots:
        volume = _validate_volume(
            volume_root, metadata, manifest, verify_payload=True
        )
        volume_name = _validated_volume_name(volume.get("name"))
        if volume_name not in descriptions:
            raise OfflineError(f"Volume is not listed by the bundle: {volume_name}")
        described_files = descriptions[volume_name].get("files")
        if not isinstance(described_files, list):
            raise OfflineError(f"Invalid bundle file list: {volume_name}")
        if [_validated_relative_path(path) for path in described_files] != [
            FileEntry.from_dict(value).path for value in volume["files"]
        ]:
            raise OfflineError(
                f"Volume file list differs from bundle metadata: {volume_name}"
            )
        target = destination / "volumes" / volume_name
        if target.exists():
            _validate_volume(
                target,
                metadata,
                manifest,
                verify_payload=True,
                expected_name=volume_name,
            )
            print(f"Volume already staged: {volume_name}")
            continue
        temporary_parent = target.parent / f".stage-{uuid.uuid4().hex}"
        partial = temporary_parent / target.name
        temporary_parent.mkdir(parents=True)
        try:
            shutil.copytree(volume_root, partial)
            _validate_volume(
                partial,
                metadata,
                manifest,
                verify_payload=True,
                expected_name=volume_name,
            )
            partial.rename(target)
        except Exception:
            shutil.rmtree(temporary_parent, ignore_errors=True)
            raise
        finally:
            if temporary_parent.exists():
                temporary_parent.rmdir()
        print(f"Staged {volume_name}")

    expected = set(descriptions)
    present = {path.name for path in (destination / "volumes").glob("volume-*")}
    complete = expected <= present
    if complete:
        _write_bytes_atomic(destination / "READY", b"ready\n")
        print(f"All {len(expected)} volume(s) are staged at {destination}")
    else:
        print(
            f"Staged {len(expected & present)}/{len(expected)} volume(s) at "
            f"{destination}"
        )
    return destination, complete


def verify_snapshot(root: Path, manifest: Manifest) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    for entry in manifest.files:
        path = _path(root, entry.path)
        if path.is_symlink():
            issues.append(VerificationIssue(entry.path, "symlink", entry.sha256))
            continue
        if not path.is_file():
            issues.append(VerificationIssue(entry.path, "missing", entry.sha256))
            continue
        if path.stat().st_size != entry.size:
            issues.append(VerificationIssue(entry.path, "size", entry.sha256))
            continue
        actual = _sha256(path)
        if actual != entry.sha256:
            issues.append(VerificationIssue(entry.path, "sha256", entry.sha256, actual))
    return issues


def find_unexpected_files(
    root: Path, expected_paths: set[str], known_deleted_paths: set[str]
) -> list[FileEntry]:
    root = root.resolve()
    unexpected: list[FileEntry] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path == root and STATE_DIRECTORY in directory_names:
            directory_names.remove(STATE_DIRECTORY)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            directory_path = current_path / directory_name
            if directory_path.is_symlink():
                raise OfflineError(
                    "Unexpected directory symlink in managed mirror: "
                    f"{directory_path}"
                )
        for file_name in file_names:
            path = current_path / file_name
            relative = _validated_relative_path(path.relative_to(root).as_posix())
            if relative in expected_paths or relative in known_deleted_paths:
                continue
            if path.is_symlink() or not path.is_file():
                raise OfflineError(
                    f"Unexpected non-regular file in managed mirror: {path}"
                )
            unexpected.append(FileEntry(relative, path.stat().st_size, _sha256(path)))
    return unexpected


def _write_repair(
    feedback_dirs: Iterable[Path],
    manifest: Manifest,
    issues: Sequence[VerificationIssue],
    installed_snapshot_id: str | None,
) -> None:
    value = {
        "format": REPAIR_FORMAT,
        "version": FORMAT_VERSION,
        "target_snapshot_id": manifest.snapshot_id,
        "installed_snapshot_id": installed_snapshot_id,
        "created_at": _now(),
        "files": [issue.as_dict() for issue in issues],
    }
    for directory in feedback_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(directory / "repair-request.json", value)
        (directory / "ack.json").unlink(missing_ok=True)


def _write_ack(feedback_dirs: Iterable[Path], manifest: Manifest) -> None:
    value = {
        "format": ACK_FORMAT,
        "version": FORMAT_VERSION,
        "snapshot_id": manifest.snapshot_id,
        "verified_at": _now(),
    }
    for directory in feedback_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(directory / "ack.json", value)
        (directory / "repair-request.json").unlink(missing_ok=True)
        (directory / "deletions-pending.json").unlink(missing_ok=True)


def _deletion_allowed(metadata: dict[str, Any], allow_large: bool) -> bool:
    if allow_large:
        return True
    deleted = [FileEntry.from_dict(value) for value in metadata.get("deleted", [])]
    deleted_bytes = sum(entry.size for entry in deleted)
    target_count = int(metadata.get("target_file_count", 0))
    target_bytes = int(metadata.get("target_bytes", 0))
    count_ratio = len(deleted) / max(1, target_count + len(deleted))
    size_ratio = deleted_bytes / max(1, target_bytes + deleted_bytes)
    return count_ratio < 0.4 and size_ratio < 0.4


def _handle_deletions(
    root: Path,
    metadata: dict[str, Any],
    policy: str,
    allow_large: bool,
    feedback_dirs: Iterable[Path],
) -> bool:
    deleted = [FileEntry.from_dict(value) for value in metadata.get("deleted", [])]
    existing = [entry for entry in deleted if _path(root, entry.path).exists()]
    if not existing:
        return True

    print(f"The target snapshot no longer contains {len(existing)} local file(s):")
    for entry in existing[:20]:
        print(f"  DELETE {entry.path}")
    if len(existing) > 20:
        print(f"  ... and {len(existing) - 20} more")

    pending = {
        "format": BUNDLE_FORMAT,
        "version": FORMAT_VERSION,
        "bundle_id": metadata.get("bundle_id"),
        "created_at": _now(),
        "files": [entry.as_dict() for entry in existing],
    }
    for directory in feedback_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(directory / "deletions-pending.json", pending)

    should_delete = policy == "apply"
    if policy == "prompt" and sys.stdin.isatty():
        should_delete = (
            input("Type DELETE to apply these upstream deletions: ").strip()
            == "DELETE"
        )
    if not should_delete:
        print("Deletions were reported but not applied; no acknowledgement was written.")
        return False
    if not _deletion_allowed(metadata, allow_large):
        print(
            "Deletion safety threshold (40%) was reached; use "
            "--allow-large-deletes after review."
        )
        return False

    for entry in existing:
        path = _path(root, entry.path)
        if path.is_file() or path.is_symlink():
            path.unlink()
        parent = path.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    print(f"Applied {len(existing)} deletion(s).")
    return True


def import_bundle(
    bundle: Path,
    destination: Path,
    feedback_dir: Path | None = None,
    delete_policy: str = "prompt",
    allow_large_deletes: bool = False,
) -> int:
    metadata, manifest = _load_bundle(bundle, require_all_volumes=True)
    destination.mkdir(parents=True, exist_ok=True)
    internal_state = destination / STATE_DIRECTORY
    feedback_dirs = [internal_state]
    if feedback_dir is not None and feedback_dir.resolve() != internal_state.resolve():
        feedback_dirs.append(feedback_dir)

    installed_manifest_path = internal_state / "manifest.json"
    base_snapshot_id = metadata.get("base_snapshot_id")
    if installed_manifest_path.is_file():
        installed = load_manifest(installed_manifest_path)
        if installed.snapshot_id != base_snapshot_id:
            raise OfflineError(
                "Bundle base snapshot does not match the installed snapshot "
                f"({base_snapshot_id!r} != {installed.snapshot_id!r})"
            )
    elif base_snapshot_id is not None:
        raise OfflineError("Incremental bundle cannot be applied before its base snapshot")

    target_files = manifest.by_path
    for raw_volume in _bundle_volume_descriptions(metadata):
        volume_name = _validated_volume_name(raw_volume["name"])
        volume_root = bundle / "volumes" / volume_name
        volume = _read_json(volume_root / "volume.json")
        for raw_entry in volume["files"]:
            entry = VolumeFileEntry.from_dict(raw_entry)
            file_entry = FileEntry(entry.path, entry.size, entry.sha256)
            if target_files.get(entry.path) != file_entry:
                raise OfflineError(f"Payload entry does not match manifest: {entry.path}")
            _copy_verified(
                _path(volume_root / "payload", entry.stored_path),
                _path(destination, entry.path),
                file_entry,
            )

    issues = verify_snapshot(destination, manifest)
    if issues:
        _write_repair(
            feedback_dirs,
            manifest,
            issues,
            installed.snapshot_id if installed_manifest_path.is_file() else None,
        )
        print(f"Final mirror verification failed for {len(issues)} file(s):")
        for issue in issues[:20]:
            print(f"  {issue.reason.upper()} {issue.path}")
        if len(issues) > 20:
            print(f"  ... and {len(issues) - 20} more")
        print("A repair-request.json was written; carry it back to the external host.")
        return 2

    metadata_with_totals = dict(metadata)
    known_deleted = [
        FileEntry.from_dict(value) for value in metadata.get("deleted", [])
    ]
    unexpected = find_unexpected_files(
        destination,
        set(target_files),
        {entry.path for entry in known_deleted},
    )
    if unexpected:
        print(
            f"Found {len(unexpected)} unexpected file(s) outside the managed snapshot."
        )
    metadata_with_totals["deleted"] = [
        entry.as_dict() for entry in (*known_deleted, *unexpected)
    ]
    metadata_with_totals["target_file_count"] = len(manifest.files)
    metadata_with_totals["target_bytes"] = manifest.total_bytes
    if not _handle_deletions(
        destination,
        metadata_with_totals,
        delete_policy,
        allow_large_deletes,
        feedback_dirs,
    ):
        return ACTION_REQUIRED

    _write_json_atomic(installed_manifest_path, manifest.as_dict())
    _write_ack(feedback_dirs, manifest)
    print(f"Imported and fully verified snapshot {manifest.snapshot_id}.")
    return 0


def verify_installed(destination: Path, feedback_dir: Path | None = None) -> int:
    internal_state = destination / STATE_DIRECTORY
    manifest_path = internal_state / "manifest.json"
    if not manifest_path.is_file():
        raise OfflineError(f"No installed offline manifest at {manifest_path}")
    manifest = load_manifest(manifest_path)
    issues = verify_snapshot(destination, manifest)
    feedback_dirs = [internal_state]
    if feedback_dir is not None and feedback_dir.resolve() != internal_state.resolve():
        feedback_dirs.append(feedback_dir)
    if issues:
        _write_repair(feedback_dirs, manifest, issues, manifest.snapshot_id)
        print(f"Verification found {len(issues)} corrupt or missing file(s).")
        return 2
    _write_ack(feedback_dirs, manifest)
    print(f"Verified {len(manifest.files)} files in snapshot {manifest.snapshot_id}.")
    return 0


def parse_size(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in ("0", "off", "none"):
        return 0
    if not normalized:
        raise argparse.ArgumentTypeError("Size must not be empty")
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    suffix = normalized[-1]
    try:
        if suffix in multipliers:
            result = int(normalized[:-1]) * multipliers[suffix]
        else:
            result = int(normalized)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f"Invalid size: {value}") from ex
    if result < 0:
        raise argparse.ArgumentTypeError("Size must not be negative")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apt-mirror-offline",
        description=(
            "Create and apply verified incremental bundles for an air-gapped "
            "APT mirror."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="Create an incremental bundle")
    export.add_argument("source", type=Path, help="External mirror root")
    export.add_argument("bundle", type=Path, help="New bundle directory")
    export.add_argument(
        "--state-dir", type=Path, required=True, help="Persistent external state"
    )
    export.add_argument(
        "--feedback-dir", type=Path, help="ACK/repair feedback from the internal host"
    )
    export.add_argument(
        "--volume-size",
        type=parse_size,
        default=0,
        help="Maximum payload per optical volume, e.g. 4300M; 0 disables splitting",
    )
    export.add_argument(
        "--rehash-source", action="store_true", help="Ignore the source hash cache"
    )

    stage = subparsers.add_parser("stage", help="Stage one or more optical volumes")
    stage.add_argument("source", type=Path, help="Mounted disc, volume, or complete bundle")
    stage.add_argument("staging_dir", type=Path, help="Persistent staging directory")

    importer = subparsers.add_parser("import", help="Import and fully verify a bundle")
    importer.add_argument("bundle", type=Path)
    importer.add_argument("destination", type=Path)
    importer.add_argument("--feedback-dir", type=Path)
    importer.add_argument(
        "--delete-policy", choices=("prompt", "apply", "report"), default="prompt"
    )
    importer.add_argument("--allow-large-deletes", action="store_true")

    verify = subparsers.add_parser("verify", help="Verify an installed mirror snapshot")
    verify.add_argument("destination", type=Path)
    verify.add_argument("--feedback-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            export_bundle(
                args.source,
                args.bundle,
                args.state_dir,
                args.feedback_dir,
                args.volume_size,
                args.rehash_source,
                show_progress=True,
            )
            return 0
        if args.command == "stage":
            _, complete = stage_volumes(args.source, args.staging_dir)
            return 0 if complete else ACTION_REQUIRED
        if args.command == "import":
            return import_bundle(
                args.bundle,
                args.destination,
                args.feedback_dir,
                args.delete_policy,
                args.allow_large_deletes,
            )
        if args.command == "verify":
            return verify_installed(args.destination, args.feedback_dir)
    except OfflineError as ex:
        print(f"apt-mirror-offline: {ex}", file=sys.stderr)
        return 2
    raise AssertionError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
