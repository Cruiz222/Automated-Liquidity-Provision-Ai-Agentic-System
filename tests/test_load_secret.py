import os
import multiprocessing
import stat
import tempfile
import unittest
from pathlib import Path

from app.service import load_secret
from unittest.mock import patch



def _load_secret_in_worker(path):
    return load_secret(Path(path))


class TestLoadSecret(unittest.TestCase):
    def test_creates_a_persistent_owner_only_32_byte_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            first = load_secret(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(load_secret(path), first)


    def test_concurrent_initialization_returns_the_same_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            context = multiprocessing.get_context("spawn")

            with context.Pool(processes=4) as pool:
                results = pool.map(
                    _load_secret_in_worker,
                    [str(path)] * 8,
                )

            self.assertEqual(len(results), 8)
            self.assertTrue(all(len(secret) == 32 for secret in results))
            self.assertTrue(all(secret == results[0] for secret in results))
            self.assertEqual(load_secret(path), results[0])

    def test_rejects_group_or_other_writable_secret_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o770)
            path = directory / "secret.key"

            with self.assertRaisesRegex(PermissionError, "writable by group or others"):
                load_secret(path)

            self.assertFalse(path.exists())

    def test_rejects_symbolic_link_as_secret_directory(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symbolic links are not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "linked"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(PermissionError, "real directory"):
                load_secret(link / "secret.key")

    def test_syncs_secret_file_and_parent_directory_after_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            real_fsync = os.fsync
            with patch("app.service.os.fsync", wraps=real_fsync) as sync:
                load_secret(path)

            # One fsync for the secret's contents and one for its directory entry.
            self.assertEqual(sync.call_count, 2)

    def test_rejects_short_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.write_bytes(b"x" * 31)
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "exactly 32 bytes"):
                load_secret(path)

    def test_rejects_long_secret_files_instead_of_truncating_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.write_bytes(b"x" * 33)
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "exactly 32 bytes"):
                load_secret(path)

    def test_rejects_files_with_group_or_other_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.write_bytes(b"x" * 32)
            path.chmod(0o640)
            with self.assertRaisesRegex(PermissionError, "permissions are too broad"):
                load_secret(path)

    def test_rejects_symbolic_links(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.key"
            target.write_bytes(b"x" * 32)
            target.chmod(0o600)
            link = Path(tmp) / "secret.key"
            link.symlink_to(target)
            with self.assertRaises(PermissionError):
                load_secret(link)

    def test_rejects_non_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret-directory"
            path.mkdir()
            with self.assertRaises(PermissionError):
                load_secret(path)

    def test_rejects_fifo_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret-pipe"
            os.mkfifo(path, 0o600)
            with self.assertRaisesRegex(PermissionError, "regular file"):
                load_secret(path)


if __name__ == "__main__":
    unittest.main()
