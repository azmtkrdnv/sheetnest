"""Order optimisation and the public ``nest()`` entry point.

The greedy packer is deterministic for a given part order, so the search runs
over orders: a few classic heuristics first (largest area, longest side, ...),
then a randomised local search (swap / move / block-reverse mutations with
simulated-annealing acceptance) until the time limit. Several processes run
independent searches with different seeds and the best layout wins.
"""
from __future__ import annotations

import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional, Sequence

from .model import NestResult, NestSettings, Part, Sheet
from .nester import NestContext, Packer, layout_cost


def _heuristic_orders(parts: Sequence[Part], idxs: Sequence[int]) -> List[List[int]]:
    def expand(key):
        order = sorted(idxs, key=key)
        seq = []
        for i in order:
            seq.extend([i] * parts[i].quantity)
        return seq

    def size(i):
        return parts[i].geometry.size

    orders = [
        expand(lambda i: (-parts[i].priority, -parts[i].geometry.polygon(0.5).convex_hull.area)),
        expand(lambda i: (-parts[i].priority, -parts[i].geometry.area)),
        expand(lambda i: (-parts[i].priority, -max(size(i)))),
        expand(lambda i: (-parts[i].priority, -size(i)[1], -size(i)[0])),
        expand(lambda i: (-parts[i].priority, -size(i)[0], -size(i)[1])),
    ]
    # interleaved: big parts with small fillers alternating
    big = expand(lambda i: (-parts[i].priority, -parts[i].geometry.area))
    inter = []
    lo, hi = 0, len(big) - 1
    while lo <= hi:
        inter.append(big[lo]); lo += 1
        if lo <= hi and len(inter) % 4 == 0:
            inter.append(big[hi]); hi -= 1
    orders.append(inter)
    uniq, seen = [], set()
    for o in orders:
        t = tuple(o)
        if t not in seen:
            seen.add(t)
            uniq.append(o)
    return uniq


def _mutate(order: List[int], rng: random.Random, parts: Sequence[Part]) -> List[int]:
    o = list(order)
    n = len(o)
    if n < 2:
        return o
    kinds = rng.randint(1, 3)
    for _ in range(kinds):
        r = rng.random()
        i, j = rng.randrange(n), rng.randrange(n)
        if o[i] == o[j]:
            # find a different part to swap with
            for _ in range(10):
                j = rng.randrange(n)
                if o[i] != o[j]:
                    break
        if r < 0.45:
            if parts[o[i]].priority == parts[o[j]].priority:
                o[i], o[j] = o[j], o[i]
        elif r < 0.8:
            x = o.pop(i)
            o.insert(j, x)
        else:
            a, b = min(i, j), max(i, j)
            if b - a > 1:
                o[a:b] = o[a:b][::-1]
    # keep priority classes in order
    if any(p.priority for p in parts):
        o.sort(key=lambda k: -parts[k].priority)
    return o


WEIGHTS = (0.02, 0.05, 0.1, 0.2)


def _search(args):
    parts, sheets, settings, sheet_ids, part_ids, seed, deadline, start_orders = args
    ctx = NestContext(parts, sheets, settings)
    packer = Packer(ctx, sheet_ids)
    rng = random.Random(seed)
    seen = {}
    evals = 0
    multi_variant = [i for i in part_ids if len(ctx.variants[i]) > 1]

    def evaluate(order, w, forced):
        nonlocal evals
        k = (tuple(order), w, tuple(sorted(forced.items())))
        if k in seen:
            return seen[k]
        layouts, rem = packer.run(order, w, forced)
        evals += 1
        r = (layout_cost(ctx, layouts, rem), order, w, forced, layouts, rem)
        seen[k] = r
        return r

    best = None
    for o in start_orders:
        r = evaluate(o, 0.05, {})
        if best is None or r[0] < best[0]:
            best = r
        if time.time() > deadline:
            break
    distinct = len(set(best[1]))
    cur = best
    temp0 = max(best[0] * 0.005, 1.0)
    stale = 0
    while time.time() < deadline and stale < 400:
        order, w, forced = cur[1], cur[2], dict(cur[3])
        changed = False
        if rng.random() < 0.15:
            w = rng.choice(WEIGHTS)
            changed = w != cur[2]
        if multi_variant and rng.random() < 0.2:
            # pin (or release) one part type to a single rotation
            pi = rng.choice(multi_variant)
            if pi in forced and rng.random() < 0.5:
                del forced[pi]
            else:
                forced[pi] = rng.randrange(len(ctx.variants[pi]))
            changed = True
        if distinct > 1 and (not changed or rng.random() < 0.5):
            order = _mutate(order, rng, parts)
        before = len(seen)
        r = evaluate(order, w, forced)
        stale = stale + 1 if len(seen) == before else 0
        frac = max(0.0, (deadline - time.time())) / max(1e-6, settings.time_limit)
        temp = temp0 * frac
        if r[0] < cur[0] or (temp > 0 and rng.random() < math.exp(-(r[0] - cur[0]) / temp)):
            cur = r
        if r[0] < best[0] - 1e-9:
            best = r
    return best[0], best[4], best[5], evals


def _groups(parts: Sequence[Part], sheets: Sequence[Sheet]):
    """Group parts by material/thickness and find matching sheets for each group."""
    groups: Dict[tuple, List[int]] = {}
    for i, p in enumerate(parts):
        key = ((p.material or "").lower(), round(p.thickness, 4) if p.thickness else None)
        groups.setdefault(key, []).append(i)
    out = []
    for key, idxs in groups.items():
        sids = [j for j, s in enumerate(sheets) if all(s.accepts(parts[i]) for i in idxs)]
        label = " ".join(x for x in (key[0].upper() if key[0] else "", f"t={key[1]:g}" if key[1] else "") if x)
        out.append((label, idxs, sids))
    return out


def nest(parts: Sequence[Part], sheets: Sequence[Sheet], settings: Optional[NestSettings] = None,
         progress=None) -> NestResult:
    """Nest ``parts`` onto ``sheets``. Returns a :class:`NestResult`."""
    settings = settings or NestSettings()
    t0 = time.time()
    result = NestResult(list(parts), list(sheets), settings)
    workers = settings.workers or min(4, os.cpu_count() or 1)
    groups = _groups(parts, sheets)
    total_q = sum(p.quantity for p in parts) or 1
    for label, idxs, sids in groups:
        if not sids:
            for i in idxs:
                result.unplaced[i] = parts[i].quantity
            result.messages.append(f"no sheet matches material/thickness of group '{label or 'default'}'")
            continue
        # parts that can never fit any sheet
        ctx = NestContext(parts, sheets, settings)
        fit_idxs = []
        for i in idxs:
            ok = any(not ctx.ifp(s, v).is_empty for s in sids for v in ctx.variants[i])
            if ok:
                fit_idxs.append(i)
            else:
                result.unplaced[i] = parts[i].quantity
                result.messages.append(f"{parts[i].name} ({parts[i].geometry.size[0]:.0f} x "
                                       f"{parts[i].geometry.size[1]:.0f}) is larger than every matching sheet")
        if not fit_idxs:
            continue
        share = sum(parts[i].quantity for i in fit_idxs) / total_q
        budget = settings.time_limit * share
        starts = _heuristic_orders(parts, fit_idxs)
        deadline = time.time() + budget
        n_inst = sum(parts[i].quantity for i in fit_idxs)
        use_pool = workers > 1 and budget > 0.5 and n_inst > 3
        if use_pool:
            jobs = []
            for w in range(workers):
                so = starts if w == 0 else [starts[w % len(starts)]]
                jobs.append((list(parts), list(sheets), settings, sids, fit_idxs, settings.seed * 1000 + w, deadline, so))
            try:
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    outs = list(ex.map(_search, jobs))
            except Exception as exc:  # e.g. no fork in restricted environments
                result.messages.append(f"parallel search unavailable ({exc}); running single process")
                outs = [_search(jobs[0])]
        else:
            outs = [_search((list(parts), list(sheets), settings, sids, fit_idxs, settings.seed, deadline, starts))]
        best = min(outs, key=lambda o: o[0])
        result.evaluations += sum(o[3] for o in outs)
        for l in best[1]:
            l.group = label
        result.layouts.extend(best[1])
        for p, _ in best[2]:
            result.unplaced[p] = result.unplaced.get(p, 0) + 1
        if best[2]:
            result.messages.append("sheet stock ran out before all parts were placed")
        if progress:
            progress(label, best[1])
    result.elapsed = time.time() - t0
    return result
