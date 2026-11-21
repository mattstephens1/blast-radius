"""Split tests across workers to minimise: busiest worker + 2 s per worker."""
WORKER_MS = 2000


def best_split(nums, dur_ms, max_workers=8):
    nums = sorted(nums, key=lambda n: -dur_ms[n])
    if not nums:
        return []
    best = None
    for k in range(1, min(max_workers, len(nums)) + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in nums:
            i = loads.index(min(loads))
            ws[i].append(n); loads[i] += dur_ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]
