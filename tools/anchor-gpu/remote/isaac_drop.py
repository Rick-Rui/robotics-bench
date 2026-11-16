"""Fresh, bounded PhysX rollout. This is infrastructure evidence, not EmbodiedSWE."""
import json
import os
from pathlib import Path
import socket
import sys

from protocol import SCHEMA, digest, validate


def main():
    request = validate(json.loads(Path(sys.argv[1]).read_text()))
    destination = Path(sys.argv[2])
    from isaacsim import SimulationApp
    app = SimulationApp({'headless': True, 'multi_gpu': False,
                         'active_gpu': int(os.environ.get('ZIWU_GPU_INDEX', request['gpu'])), 'physics_gpu': 0})
    try:
        import numpy as np
        import torch
        from isaacsim.core.api import World
        from isaacsim.core.api.objects import DynamicCuboid, FixedCuboid
        assert torch.cuda.device_count() == 1
        params = request['parameters']
        world = World(physics_dt=1 / 60, rendering_dt=1 / 60,
                      stage_units_in_meters=1.0, backend='torch', device='cuda:0')
        physics = world.get_physics_context()
        physics.enable_gpu_dynamics(True)
        physics.set_broadphase_type('GPU')
        physics.set_gravity(-params['gravity'])
        world.scene.add(FixedCuboid('/World/floor', position=np.array([0., 0., -0.1]),
                                   scale=np.array([10., 10., 0.2])))
        cube = world.scene.add(DynamicCuboid('/World/cube', name='cube', mass=1.0,
                                           position=np.array([0., 0., params['initial_height']]),
                                           scale=np.array([0.2, 0.2, 0.2])))
        world.reset()
        trajectory = []
        for step in range(params['steps'] + 1):
            position, _ = cube.get_world_pose()
            trajectory.append({'step': step, 'z': float(position[2])})
            if step < params['steps']:
                world.step(render=False)
        success = abs(trajectory[-1]['z'] - 0.1) < 0.05
        receipt = {'schema': SCHEMA, 'request_sha256': digest(request), 'status': 'passed',
                   'hostname': socket.gethostname(), 'pid': os.getpid(),
                   'cuda_visible_devices': os.environ['CUDA_VISIBLE_DEVICES'],
                   'device': torch.cuda.get_device_name(0), 'gpu_dynamics': True,
                   'trajectory': trajectory, 'score': float(success), 'success': success}
        # Kit close can terminate Python. The supervisor additionally checks exit status.
        destination.write_text(json.dumps(receipt, indent=2) + '\n')
    finally:
        app.close()


if __name__ == '__main__':
    main()
