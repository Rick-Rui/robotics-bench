"""Non-preemptive, measured-service fairness for whole Isaac rollouts.

The trusted dispatcher assigns trial/phase. These are scheduling hints, never
permission to execute code. A trial's active reservations count toward service,
so multiple free GPUs do not all select the same previously idle trial.
"""
import math


def metadata(request):
    return request.get('scheduling', {'trial': 'legacy', 'phase': 'final'})


def choose(rows, gpu, service, recent_phases, now, aging_seconds=120):
    eligible = [row for row in rows if row['request']['gpu'] in (gpu, 'auto')]
    if not eligible:
        return None
    oldest = min(eligible, key=lambda row: (row['created'], row['id']))
    # Aging overrides phase preference and service debt, including late arrivals.
    if now - oldest['created'] >= aging_seconds:
        return oldest
    phases = {metadata(row['request'])['phase'] for row in eligible}
    phase = 'feedback' if 'feedback' in phases else 'final'
    # At most three consecutive feedback dispatches while a final is eligible.
    # This is a dispatch share, NOT a wall-time or preemption guarantee.
    if 'final' in phases and recent_phases[:3] == ['feedback'] * 3:
        phase = 'final'
    eligible = [row for row in eligible if metadata(row['request'])['phase'] == phase]
    return min(eligible, key=lambda row: (
        service.get((row['owner'], metadata(row['request'])['trial']), 0),
        row['created'], row['id']))


def cpu_target(gpus, maximum, cpu_seconds, gpu_seconds):
    """Estimate producers needed to feed G consumers, plus one jitter spare.

    These are measured stage durations (including model I/O), not core usage.
    The operator's CPU/RAM-derived hard slot limit always wins.
    """
    if cpu_seconds is None:
        return maximum
    return min(maximum, max(1, math.ceil(gpus * cpu_seconds / max(1, gpu_seconds)) + 1))


def window(gpus, cpu_slots, queued, running, preparing, ready=0):
    """CPU preparation credits for a bounded, two-rollout-per-card prefetch.

    Count outstanding requests, not just actively executing GPU processes.
    A card occupied by another Anchor job therefore also applies backpressure.
    Caller must additionally enforce Docker CPU/RAM quotas.
    """
    if gpus < 1 or cpu_slots < 1 or min(queued, running, preparing, ready) < 0:
        raise ValueError('Invalid resource counts')
    # The soft watermark stops new producers. Producers already working may
    # finish, so reserve space for their results as well as two prefetched jobs.
    if queued + ready >= 2 * gpus:
        return 0
    return max(0, min(cpu_slots - preparing,
                      3 * gpus + cpu_slots - queued - running - preparing - ready))
