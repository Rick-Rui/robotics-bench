"""Data-only contract for the native Isaac transport acceptance workload."""
import hashlib
import json
import math
import re

SCHEMA = 'ziwu.remote-isaac.v1'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate(request):
    required = {'workload', 'gpu', 'parameters'}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {'scheduling'}:
        raise ValueError('Expected workload, gpu, parameters and optional scheduling')
    if request['workload'] not in ('isaac-drop-v1', 'bimanual-session-v1'):
        raise ValueError('Unregistered workload; arbitrary host code is not accepted')
    if request['gpu'] != 'auto' and (type(request['gpu']) is not int or request['gpu'] not in range(4)):
        raise ValueError('gpu must be auto or an integer from 0 to 3')
    if 'scheduling' in request:
        scheduling = request['scheduling']
        if not isinstance(scheduling, dict) or set(scheduling) != {'trial', 'phase'}:
            raise ValueError('Expected scheduling trial and phase')
        if not isinstance(scheduling['trial'], str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', scheduling['trial']):
            raise ValueError('Invalid trial identifier')
        if scheduling['phase'] not in ('feedback', 'final'):
            raise ValueError('phase must be feedback or final')
    params = request['parameters']
    if request['workload'] == 'bimanual-session-v1':
        if request['gpu'] not in (1, 2) or type(request['gpu']) is not int:
            raise ValueError('Bimanual sessions require explicit GPU 1 or 2')
        if (not isinstance(params, dict) or set(params) != {'online_limit','task_digest'}
                or type(params['online_limit']) is not int or not 300 <= params['online_limit'] <= 6600):
            raise ValueError('Bimanual online_limit must be an integer from 300 to 6600')
        if not isinstance(params['task_digest'], str) or not re.fullmatch('[0-9a-f]{64}',params['task_digest']):
            raise ValueError('Invalid frozen task identity')
        return request
    if not isinstance(params, dict) or set(params) != {'steps', 'gravity', 'initial_height'}:
        raise ValueError('Expected steps, gravity and initial_height')
    if type(params['steps']) is not int or not 60 <= params['steps'] <= 600:
        raise ValueError('steps must be an integer from 60 to 600')
    for name, low, high in [('gravity', 0, 20), ('initial_height', 0.5, 3)]:
        value = params[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid ' + name)
    return request


def validate_result(result, request):
    if (result.get('schema') != SCHEMA or result.get('request_sha256') != digest(request)
            or result.get('status') != 'passed'):
        raise ValueError('Missing, stale or failed Isaac receipt')
    if request['workload'] == 'bimanual-session-v1':
        if result.get('reward') not in (0, 1) or type(result['reward']) is not int:
            raise ValueError('Invalid binary task reward')
        metrics = result.get('metrics', {})
        if metrics.get('status') == 'no_online_strategy':
            if result['reward'] != 0 or metrics.get('trusted_inputs_verified') is not True:
                raise ValueError('Invalid no-action receipt')
        elif (metrics.get('status') != 'complete' or type(metrics.get('consistency_passed')) is not bool
              or result['reward'] != int(metrics.get('success') is True and metrics['consistency_passed'])):
            raise ValueError('Incomplete or contradictory replay result')
        return result
    trajectory = result.get('trajectory', [])
    if len(trajectory) != request['parameters']['steps'] + 1:
        raise ValueError('Incomplete trajectory')
    for index, point in enumerate(trajectory):
        if (point.get('step') != index or type(point.get('z')) not in (int, float)
                or not math.isfinite(point['z'])):
            raise ValueError('Invalid trajectory')
    # Independent recomputation from the returned trajectory, not a client score.
    expected = float(abs(trajectory[-1]['z'] - 0.1) < 0.05)
    if result.get('score') != expected or result.get('success') is not bool(expected):
        raise ValueError('Inconsistent physical outcome')
    return result
