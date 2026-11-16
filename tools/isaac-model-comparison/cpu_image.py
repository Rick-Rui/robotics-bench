"""Verify an operator-exported Docker image before loading it on a CPU runner.

This is an infrastructure smoke test, never a model trial or benchmark score.
The archive hash and Docker image ID must come from the dispatching operator,
not an unsigned manifest downloaded alongside the archive.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid


def checked_archive(path, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Expected an exact SHA-256")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("CPU image archive checksum mismatch; nothing loaded")


def smoke(archive, expected, image_id):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Expected an exact Docker image ID")
    checked_archive(archive, expected)
    subprocess.run(["docker", "load", "--input", str(archive)], check=True, timeout=600)
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image_id]))[0]
    if info["Id"] != image_id or info["Os"] != "linux" or info["Architecture"] != "amd64":
        raise ValueError("Unexpected CPU image identity or platform")
    name = "ziwu-pr8-cpu-smoke-" + uuid.uuid4().hex[:12]
    try:
        # No daemon socket, host mount, GPU, credentials or model calls.
        subprocess.run([
            "docker", "run", "--name", name, "--network", "none",
            "--cpus", "2", "--memory", "6g", "--memory-swap", "6g",
            "--pids-limit", "128", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "1000:1000",
            image_id, "python", "-c",
            "import os,numpy,PIL; from pathlib import Path; "
            "assert os.getuid()==1000; "
            "p=Path('/app/work/smoke.txt'); p.write_text('ZIWU_CPU_OK'); "
            "assert p.read_text()=='ZIWU_CPU_OK'; print('ZIWU_CPU_OK')",
        ], check=True, timeout=120)
        container = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
        limits = container["HostConfig"]
        if (limits["NanoCpus"] != 2_000_000_000 or limits["Memory"] != 6 * 1024**3
                or limits["NetworkMode"] != "none" or container["State"]["ExitCode"] != 0):
            raise ValueError("CPU container limits or exit receipt differ")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, timeout=30)
    return {"schema": "ziwu.cpu-image-smoke.v1", "status": "passed",
            "archive_sha256": expected, "image_id": image_id,
            "cpus": 2, "memory_mib": 6144, "network": "none",
            "model_calls": 0, "trial_count": 0, "reward": None,
            "cleanup_confirmed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = smoke(args.archive, args.sha256, args.image_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
