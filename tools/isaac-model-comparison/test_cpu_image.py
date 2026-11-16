import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from cpu_image import checked_archive, smoke


def test_corrupted_archive_never_reaches_docker():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar.gz"
        archive.write_bytes(b"corrupted archive")
        with patch("cpu_image.subprocess.run") as run:
            with pytest.raises(ValueError, match="checksum mismatch"):
                smoke(archive, "0" * 64, "sha256:" + "1" * 64)
            run.assert_not_called()


def test_exact_archive_digest():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar.gz"
        archive.write_bytes(b"image bytes")
        checked_archive(archive, hashlib.sha256(b"image bytes").hexdigest())
