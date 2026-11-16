"""GPUs of the pool that this repository may use.

The pool host has four GPUs (0-3). The lanes this repository uses come from the environment variable
GPU_POOL_LANES (a comma list; workflows set it from the repository variable of the same name) and default to all
four. The host job server must enable the same cards (tools/anchor-gpu/remote/deploy.py config "gpus").

    python lanes.py list      # JSON list of the lanes
    python lanes.py pick N    # JSON list of N distinct lanes chosen at random (for /validate acceptance)
"""
import json
import os
import random
import sys

POOL = range(4)
DEFAULT = (0, 1, 2, 3)
AUTO = 'auto'


def lanes(env=None):
    raw = (os.environ if env is None else env).get('GPU_POOL_LANES', '').strip()
    if not raw:
        return DEFAULT
    try:
        out = tuple(int(x) for x in raw.split(','))
    except ValueError:
        raise ValueError(f'GPU_POOL_LANES must be a comma list of GPU indices, not {raw!r}') from None
    if not out or len(set(out)) != len(out) or any(x not in POOL for x in out):
        raise ValueError(f'GPU_POOL_LANES must list distinct GPUs from 0-3, not {raw!r}')
    return out


def parse_gpu(value, env=None):
    """'auto' (the host picks a free enabled card) or one of lanes()."""
    if value is None or str(value).strip().lower() == AUTO:
        return AUTO
    try:
        gpu = int(str(value).strip())
    except ValueError:
        raise ValueError(f'GPU must be "auto" or one of {list(lanes(env))}, not {value!r}') from None
    if gpu not in lanes(env):
        raise ValueError(f'GPU {gpu} is not a lane of this repository; lanes: {list(lanes(env))}')
    return gpu


def pick(n, env=None, rng=random):
    available = list(lanes(env))
    if n > len(available):
        raise ValueError(f'{n} distinct lanes needed, only {len(available)} configured: {available}')
    return sorted(rng.sample(available, n))


def main(argv):
    if argv[:1] == ['list'] and len(argv) == 1:
        print(json.dumps(list(lanes())))
    elif argv[:1] == ['pick'] and len(argv) == 2:
        print(json.dumps(pick(int(argv[1]))))
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
