"""Build CPU-only Harbor fixtures for remote Isaac transport acceptance."""
import argparse
import json
from pathlib import Path
import shutil


def build(destination, image, gravity):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ['environment', 'tests', 'solution']:
        (destination / name).mkdir()
    (destination / 'environment/Dockerfile').write_text('FROM ' + image + '\nWORKDIR /app\n')
    (destination / 'tests/Dockerfile').write_text('FROM ' + image + '\nWORKDIR /tests\nCOPY . /tests\n')
    # Only the trusted verifier joins the CPU host network to reach the SSH tunnel.
    (destination / 'tests/docker-compose.yaml').write_text('services:\n  main:\n    build:\n      context: ${CONTEXT_DIR}\n    network_mode: host\n    command: ["sh", "-c", "sleep infinity"]\n')
    for name in ['protocol.py', 'client.py', 'harbor_verify.py']:
        shutil.copy2(Path(__file__).with_name(name), destination / 'tests' / name)
    (destination / 'tests/test.sh').write_text('#!/bin/bash\nset -euo pipefail\npython /tests/harbor_verify.py\n')
    candidate = json.dumps({'steps': 120, 'gravity': gravity, 'initial_height': 1.0})
    (destination / 'solution/solve.sh').write_text("#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\ncat > /app/output/candidate.json <<'CANDIDATE'\n" + candidate + '\nCANDIDATE\n')
    (destination / 'instruction.md').write_text('# Remote Isaac transport fixture\n\nWrite a JSON object with steps (120), gravity (9.81), and initial_height (1.0) to /app/output/candidate.json. The verifier runs a fresh PhysX cube drop on a remote RTX 4090. This is an infrastructure acceptance fixture, not a benchmark or model evaluation.\n')
    (destination / 'task.toml').write_text('''schema_version = "1.4"
artifacts = ["/app/output/candidate.json"]
[metadata]
name = "ziwu/remote-isaac-transport"
[agent]
timeout_sec = 60.0
[environment]
network_mode = "no-network"
cpus = 1
memory_mb = 512
gpus = 0
workdir = "/app"
[verifier]
environment_mode = "separate"
timeout_sec = 480.0
[verifier.env]
ZIWU_REMOTE_TOKEN = "${ZIWU_REMOTE_TOKEN}"
ZIWU_REMOTE_URL = "${ZIWU_REMOTE_URL}"
[verifier.environment]
network_mode = "public"
cpus = 1
memory_mb = 512
gpus = 0
workdir = "/tests"
''')
    job = {'job_name': destination.name, 'jobs_dir': str(destination.parent / 'harbor-results'),
           'n_attempts': 1, 'n_concurrent_trials': 1,
           'environment': {'type': 'docker', 'delete': True},
           'agents': [{'name': 'oracle'}], 'tasks': [{'path': str(destination)}]}
    config = destination.parent / (destination.name + '-job.json')
    config.write_text(json.dumps(job, indent=2) + '\n')
    return config


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--gravity', type=float, default=9.81)
    args = parser.parse_args()
    print(build(args.destination, args.image, args.gravity))
