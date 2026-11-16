"""Bounded native runtime check; no task code, model calls or installs."""
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys


def main():
    output, mode = Path(sys.argv[1]), sys.argv[2]
    packages = {}
    for name in ('torch', 'isaacsim', 'isaacsim-kernel', 'isaaclab', 'harbor'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    report = {'python': platform.python_version(), 'packages': packages,
              'cuda_visible_devices': os.environ['CUDA_VISIBLE_DEVICES'], 'status': 'failed'}
    try:
        import torch
        assert torch.cuda.is_available(), 'CUDA unavailable'
        assert torch.cuda.device_count() == 1, 'Expected exactly one visible CUDA device'
        values = torch.arange(4096, dtype=torch.int64, device='cuda:0')
        actual = int((values * values).sum().item())
        assert actual == 4095 * 4096 * 8191 // 6, 'CUDA arithmetic mismatch'
        torch.cuda.synchronize()
        report.update(cuda='passed', torch_cuda=torch.version.cuda,
                      device=torch.cuda.get_device_name(0), result=actual)
        if mode == 'isaac':
            # CUDA is UUID-masked. Kit's Vulkan device uses its physical index.
            from isaacsim import SimulationApp
            app = SimulationApp({'headless': True, 'multi_gpu': False,
                                 'active_gpu': int(os.environ['ZIWU_GPU_INDEX']),
                                 'physics_gpu': 0})
            try:
                for _ in range(10):
                    app.update()
                report['isaac_boot'] = 'passed'
                report['status'] = 'passed'
                # Kit can terminate the interpreter from close(), bypassing
                # Python finally blocks. Persist after updates; the parent also
                # requires a zero process exit code before accepting this receipt.
                output.write_text(json.dumps(report, indent=2) + '\n')
            finally:
                app.close()
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = '{}: {}'.format(type(exc).__name__, exc)
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
