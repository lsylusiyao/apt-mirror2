import json
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase

from apt_mirror.offline import (
    OfflineError,
    export_bundle,
    import_bundle,
    stage_volumes,
    verify_installed,
)


class TestOfflineMirror(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.feedback = self.root / "feedback"
        self.internal = self.root / "internal"
        (self.source / "pool").mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_incremental_delete_and_corruption_repair(self):
        (self.source / "pool" / "a.deb").write_bytes(b"alpha")
        (self.source / "pool" / "b.deb").write_bytes(b"bravo")

        first = self.root / "first"
        first_metadata = export_bundle(
            self.source, first, self.state, rehash_source=True
        )
        self.assertIsNone(first_metadata["base_snapshot_id"])
        self.assertEqual(first_metadata["changed_file_count"], 2)
        self.internal.mkdir()
        (self.internal / "stale-file").write_bytes(b"old")
        self.assertEqual(
            import_bundle(
                first,
                self.internal,
                self.feedback,
                delete_policy="apply",
            ),
            0,
        )
        self.assertFalse((self.internal / "stale-file").exists())

        (self.source / "pool" / "a.deb").write_bytes(b"alpha-2")
        (self.source / "pool" / "b.deb").unlink()
        (self.source / "pool" / "c.deb").write_bytes(b"charlie")
        second = self.root / "second"
        second_metadata = export_bundle(
            self.source,
            second,
            self.state,
            feedback_dir=self.feedback,
            rehash_source=True,
        )
        self.assertEqual(second_metadata["changed_file_count"], 2)
        self.assertEqual(
            {entry["path"] for entry in second_metadata["deleted"]},
            {"pool/b.deb"},
        )

        self.assertEqual(
            import_bundle(
                second,
                self.internal,
                self.feedback,
                delete_policy="report",
            ),
            3,
        )
        self.assertTrue((self.internal / "pool" / "b.deb").exists())
        self.assertTrue((self.feedback / "deletions-pending.json").is_file())
        self.assertEqual(
            import_bundle(
                second,
                self.internal,
                self.feedback,
                delete_policy="apply",
            ),
            0,
        )
        self.assertFalse((self.internal / "pool" / "b.deb").exists())

        # The next increment only adds d.deb. Damage an older, unchanged file
        # before importing it; final full-snapshot verification must catch it.
        (self.source / "pool" / "d.deb").write_bytes(b"delta")
        third = self.root / "third"
        third_metadata = export_bundle(
            self.source,
            third,
            self.state,
            feedback_dir=self.feedback,
            rehash_source=True,
        )
        self.assertEqual(
            {
                path
                for volume in third_metadata["volumes"]
                for path in volume["files"]
            },
            {"pool/d.deb"},
        )
        (self.internal / "pool" / "a.deb").write_bytes(b"damaged")
        self.assertEqual(
            import_bundle(
                third,
                self.internal,
                self.feedback,
                delete_policy="apply",
            ),
            2,
        )
        repair = json.loads(
            (self.feedback / "repair-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            repair["installed_snapshot_id"], second_metadata["target_snapshot_id"]
        )
        self.assertEqual([entry["path"] for entry in repair["files"]], ["pool/a.deb"])

        fourth = self.root / "fourth"
        fourth_metadata = export_bundle(
            self.source,
            fourth,
            self.state,
            feedback_dir=self.feedback,
            rehash_source=True,
        )
        self.assertEqual(
            fourth_metadata["base_snapshot_id"],
            second_metadata["target_snapshot_id"],
        )
        changed_paths = {
            path
            for volume in fourth_metadata["volumes"]
            for path in volume["files"]
        }
        self.assertEqual(changed_paths, {"pool/a.deb", "pool/d.deb"})
        self.assertEqual(
            import_bundle(
                fourth,
                self.internal,
                self.feedback,
                delete_policy="apply",
            ),
            0,
        )
        self.assertEqual((self.internal / "pool" / "a.deb").read_bytes(), b"alpha-2")
        self.assertEqual(verify_installed(self.internal, self.feedback), 0)

    def test_corrupt_payload_is_rejected_before_import(self):
        (self.source / "pool" / "a.deb").write_bytes(b"payload")
        bundle = self.root / "bundle"
        export_bundle(self.source, bundle, self.state, rehash_source=True)
        payload = bundle / "volumes" / "volume-0001" / "payload" / "pool" / "a.deb"
        payload.write_bytes(b"PAYLOAD")

        with self.assertRaisesRegex(OfflineError, "Payload checksum mismatch"):
            import_bundle(bundle, self.internal, delete_policy="apply")
        self.assertFalse((self.internal / "pool" / "a.deb").exists())

    def test_export_ignores_resumable_download_partials(self):
        (self.source / "pool" / "a.deb").write_bytes(b"complete")
        partial = (
            self.source
            / "archive.kylinos.cn"
            / ".apt-mirror2-partial"
            / "pool"
            / "large.deb"
        )
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"incomplete")

        bundle = self.root / "bundle"
        metadata = export_bundle(
            self.source, bundle, self.state, rehash_source=True
        )

        self.assertEqual(metadata["changed_file_count"], 1)
        self.assertEqual(
            {
                path
                for volume in metadata["volumes"]
                for path in volume["files"]
            },
            {"pool/a.deb"},
        )

    def test_optical_volumes_can_be_staged_separately(self):
        (self.source / "pool" / "a.deb").write_bytes(b"12345678")
        (self.source / "pool" / "b.deb").write_bytes(b"abcdefgh")
        bundle = self.root / "bundle"
        export_bundle(
            self.source,
            bundle,
            self.state,
            volume_size=8,
            rehash_source=True,
        )
        staged_root = self.root / "staged"
        first_disc = self.root / "mounted-cdrom"
        shutil.copytree(bundle / "volumes" / "volume-0001", first_disc)
        destination, complete = stage_volumes(
            first_disc, staged_root
        )
        self.assertFalse(complete)
        destination_again, complete = stage_volumes(
            bundle / "volumes" / "volume-0002", staged_root
        )
        self.assertEqual(destination_again, destination)
        self.assertTrue(complete)
        self.assertEqual(
            import_bundle(destination, self.internal, delete_policy="apply"), 0
        )
