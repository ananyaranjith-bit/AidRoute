#!/usr/bin/env python3
"""
AidRoute - Emergency Resource Allocation System (console application).

Usage:
    python aidroute.py                       # run every tier / strategy demo
    python aidroute.py --tier 1              # Tier 1 only
    python aidroute.py --tier 2 --strategy fairness
    python aidroute.py --tier 3 --strategy urgency --ticks 8
    python aidroute.py --requests requests.csv

Tier 1 : single pass, urgency-sorted, nearest warehouse only, DIRECT roads,
         DAMAGED roads count at listed distance, BLOCKED roads do not exist.
Tier 2 : tick simulation, fallback to next-nearest warehouse (multi-source),
         DAMAGED = 3x distance, BLOCKED = impassable, swappable strategies
         (urgency-first / fairness-first), urgency escalation after 3 ticks.
Tier 3 : Tier 2 + Dijkstra over the full road graph (path reported) and an
         explicit UNREACHABLE status for zones with no path to any warehouse.

Concurrency note (out of scope): the simulation is single-threaded. In a real
deployment the Inventory object is the only shared mutable state, so guarding
`Inventory.take` with a lock (or using atomic compare-and-set on stock counts)
would be enough to make allocation thread-safe.
"""
from __future__ import annotations

import argparse
import copy
import csv
import heapq
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------- #
# Constants / enums
# --------------------------------------------------------------------------- #
DAMAGE_MULTIPLIER = 3
MAX_URGENCY = 5
ESCALATION_AGE = 3          # "older than 3 processing ticks"
INF = float("inf")


class Resource(Enum):
    WATER = "WATER"
    MEDICINE = "MEDICINE"
    FOOD = "FOOD"


class RoadStatus(Enum):
    OPEN = "OPEN"
    DAMAGED = "DAMAGED"
    BLOCKED = "BLOCKED"


class Status(Enum):
    FULFILLED = "FULFILLED"
    PARTIAL = "PARTIAL"
    UNFULFILLED = "UNFULFILLED"
    UNREACHABLE = "UNREACHABLE"


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #
@dataclass
class Warehouse:
    wid: str
    location: str
    stock: dict


@dataclass
class Zone:
    zid: str
    location: str


@dataclass
class Road:
    a: str
    b: str
    km: float
    status: RoadStatus


@dataclass(eq=False)          # eq=False -> hashable by identity (used as dict key)
class Request:
    order: int
    rid: str
    zone: str
    resource: Resource
    qty: int
    urgency: int
    tick: int = 1
    remaining: int = field(init=False)
    allocated: int = 0
    attempts: int = 0
    unreachable: bool = False
    cap_logged: bool = False
    sources: dict = field(default_factory=dict)   # warehouse id -> units

    def __post_init__(self):
        self.remaining = self.qty

    @property
    def status(self) -> Status:
        if self.remaining == 0:
            return Status.FULFILLED
        if self.allocated > 0:
            return Status.PARTIAL
        return Status.UNREACHABLE if self.unreachable else Status.UNFULFILLED


class Network:
    """Static world data: warehouses, zones, roads."""

    def __init__(self, warehouses, zones, roads):
        self.warehouses = {w.wid: w for w in warehouses}
        self.zones = {z.zid: z for z in zones}
        self.roads = roads


def build_network() -> Network:
    warehouses = [
        Warehouse("W1", "Dhunche (Rasuwa HQ)",
                  {Resource.WATER: 200, Resource.MEDICINE: 100, Resource.FOOD: 150}),
        Warehouse("W2", "Bidur (Nuwakot HQ)",
                  {Resource.WATER: 150, Resource.MEDICINE: 80, Resource.FOOD: 200}),
    ]
    zones = [Zone("Z1", "Syabrubesi"), Zone("Z2", "Timure"),
             Zone("Z3", "Devighat"), Zone("Z4", "Gorkha Bazar")]
    roads = [
        Road("W1", "Z1", 12, RoadStatus.OPEN),
        Road("W1", "Z2", 20, RoadStatus.DAMAGED),
        Road("W2", "Z3", 8, RoadStatus.OPEN),
        Road("W2", "Z4", 15, RoadStatus.BLOCKED),
        Road("Z1", "Z3", 10, RoadStatus.OPEN),
        Road("W1", "W2", 25, RoadStatus.OPEN),
    ]
    return Network(warehouses, zones, roads)


DEFAULT_REQUESTS = [
    # order, id, zone, resource, qty, urgency, tick
    (1, "R1", "Z1", "WATER", 250, 4, 1),
    (2, "R2", "Z3", "MEDICINE", 100, 5, 1),
    (3, "R3", "Z4", "WATER", 20, 5, 1),
    (4, "R4", "Z2", "FOOD", 500, 2, 2),
    (5, "R5", "Z1", "MEDICINE", 60, 2, 2),
    (6, "R6", "Z1", "FOOD", 200, 1, 2),
]

def load_requests(path: str | None = None) -> list:
    """Fresh Request objects every call (simulations mutate them)."""
    rows = []
    if path:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                rows.append((int(r["order"]), r["request_id"], r["zone"], r["resource"],
                             int(r["quantity"]), int(r["urgency"]), int(r["tick"])))
    else:
        rows = DEFAULT_REQUESTS
    return [Request(o, rid, z, Resource[res], q, u, t) for o, rid, z, res, q, u, t in rows]


class Inventory:
    """Mutable stock ledger (copy of warehouse stock so each run starts fresh)."""

    def __init__(self, network: Network):
        self.stock = {wid: dict(w.stock) for wid, w in network.warehouses.items()}

    def available(self, wid: str, res: Resource) -> int:
        return self.stock[wid].get(res, 0)

    def take(self, wid: str, res: Resource, n: int) -> None:
        assert 0 < n <= self.available(wid, res)
        self.stock[wid][res] -= n


# --------------------------------------------------------------------------- #
# Routing (pluggable): who is nearest, and by which path?
# --------------------------------------------------------------------------- #
@dataclass
class Route:
    warehouse_id: str
    distance: float
    path: list            # node ids from warehouse to zone
    damaged: bool = False


class RoutingStrategy(ABC):
    @abstractmethod
    def ranked_routes(self, zone_id: str) -> list:
        """Reachable warehouses, nearest first (ties broken by warehouse id)."""

    def route_to(self, zone_id: str, wid: str):
        return next((r for r in self.ranked_routes(zone_id) if r.warehouse_id == wid), None)


class DirectRouting(RoutingStrategy):
    """Only direct warehouse<->zone roads are considered (Tier 1 & 2).
    penalize=False -> DAMAGED counts at listed distance (Tier 1)
    penalize=True  -> DAMAGED costs 3x (Tier 2). BLOCKED never exists."""

    def __init__(self, network: Network, penalize: bool):
        self.net, self.penalize = network, penalize

    def ranked_routes(self, zone_id):
        routes = []
        for road in self.net.roads:
            if road.status is RoadStatus.BLOCKED:
                continue
            ends = {road.a, road.b}
            if zone_id in ends:
                other = (ends - {zone_id}).pop() if len(ends) == 2 else None
                if other in self.net.warehouses:
                    dmg = road.status is RoadStatus.DAMAGED
                    d = road.km * DAMAGE_MULTIPLIER if (dmg and self.penalize) else road.km
                    routes.append(Route(other, d, [other, zone_id], dmg))
        return sorted(routes, key=lambda r: (r.distance, r.warehouse_id))


class GraphRouting(RoutingStrategy):
    """Dijkstra over the full road graph (Tier 3). DAMAGED = 3x, BLOCKED removed.
    Zones and warehouses can both be intermediate hops."""

    def __init__(self, network: Network):
        self.net = network
        self.adj = {}
        for road in network.roads:
            if road.status is RoadStatus.BLOCKED:
                continue
            dmg = road.status is RoadStatus.DAMAGED
            w = road.km * DAMAGE_MULTIPLIER if dmg else road.km
            self.adj.setdefault(road.a, []).append((road.b, w, dmg))
            self.adj.setdefault(road.b, []).append((road.a, w, dmg))
        self._cache = {}

    def ranked_routes(self, zone_id):
        if zone_id in self._cache:
            return self._cache[zone_id]
        dist, prev = {zone_id: 0.0}, {}
        heap = [(0.0, zone_id)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, INF):
                continue
            for v, w, dmg in self.adj.get(u, []):
                nd = d + w
                if nd < dist.get(v, INF):
                    dist[v], prev[v] = nd, (u, dmg)
                    heapq.heappush(heap, (nd, v))
        routes = []
        for wid in self.net.warehouses:
            if wid in dist:
                path, node, used_dmg = [wid], wid, False
                while node != zone_id:              # walk back: warehouse -> zone
                    node, dmg = prev[node]
                    path.append(node)
                    used_dmg = used_dmg or dmg
                routes.append(Route(wid, dist[wid], path, used_dmg))
        routes.sort(key=lambda r: (r.distance, r.warehouse_id))
        self._cache[zone_id] = routes
        return routes


# --------------------------------------------------------------------------- #
# Allocation strategies (common interface -> swappable without touching the
# request-processing code in Simulator)
# --------------------------------------------------------------------------- #
class AllocationStrategy(ABC):
    name = "abstract"

    @abstractmethod
    def allocate(self, pending: list, inventory: Inventory, routing: RoutingStrategy) -> dict:
        """Allocate stock to `pending` requests. Returns {request: {warehouse: units}}
        in the order requests should be reported."""

    @staticmethod
    def _grant(req, wid, n, inventory, result):
        inventory.take(wid, req.resource, n)
        req.remaining -= n
        req.allocated += n
        req.sources[wid] = req.sources.get(wid, 0) + n
        result[req][wid] = result[req].get(wid, 0) + n

    @staticmethod
    def _by_urgency(reqs):
        return sorted(reqs, key=lambda r: (-r.urgency, r.order))


class UrgencyFirstStrategy(AllocationStrategy):
    """(a) Strict urgency-first: highest urgency drains stock first;
    ties -> order received. multi_source=False gives the Tier 1 behaviour
    (nearest warehouse with stock only, no second warehouse)."""
    name = "urgency-first"

    def __init__(self, multi_source: bool = True):
        self.multi_source = multi_source

    def allocate(self, pending, inventory, routing):
        result = {}
        for req in self._by_urgency(pending):
            result[req] = {}
            for route in routing.ranked_routes(req.zone):
                if req.remaining == 0:
                    break
                avail = inventory.available(route.warehouse_id, req.resource)
                if avail <= 0:
                    continue
                self._grant(req, route.warehouse_id, min(avail, req.remaining), inventory, result)
                if not self.multi_source:
                    break
        return result


class FairnessFirstStrategy(AllocationStrategy):
    """(b) Fairness-first: round-robin ONE unit at a time across all pending
    requests (arrival order within a round) until stock runs out. Each unit
    comes from the nearest warehouse that still has that resource."""
    name = "fairness-first (round-robin)"

    def allocate(self, pending, inventory, routing):
        result = {r: {} for r in self._by_urgency(pending)}      # report order only
        routes = {r: routing.ranked_routes(r.zone) for r in pending}
        active = sorted(pending, key=lambda r: r.order)
        while active:
            still_active = []
            for req in active:
                wid = next((rt.warehouse_id for rt in routes[req]
                            if inventory.available(rt.warehouse_id, req.resource) > 0), None)
                if wid is None:
                    continue                    # nothing left for this request
                self._grant(req, wid, 1, inventory, result)
                if req.remaining > 0:
                    still_active.append(req)
            active = still_active
        return result


STRATEGIES = {"urgency": UrgencyFirstStrategy, "fairness": FairnessFirstStrategy}


# --------------------------------------------------------------------------- #
# Simulator - request processing; knows nothing about HOW stock is divided
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self, network, strategy, routing, requests,
                 report_unreachable=False, show_routes=False):
        self.net, self.strategy, self.routing = network, strategy, routing
        self.requests = requests
        self.inventory = Inventory(network)
        self.report_unreachable = report_unreachable
        self.show_routes = show_routes

    # ---- Tier 1: single pass, no ticks --------------------------------- #
    def run_single_pass(self):
        for r in self.requests:
            self._flag_unreachable(r)
        pending = [r for r in self.requests if not r.unreachable]
        result = self.strategy.allocate(pending, self.inventory, self.routing)
        for r in self.requests:
            if r.unreachable:
                result.setdefault(r, {})
        self._report_batch(result)

    # ---- Tier 2/3: tick simulation ------------------------------------- #
    def run_ticks(self, max_ticks: int):
        submitted = []
        for tick in range(1, max_ticks + 1):
            print(f"\n--- Tick {tick} ---")
            new = [r for r in self.requests if r.tick == tick]
            for r in new:
                self._flag_unreachable(r)
                print(f"  submitted {r.rid}: {r.qty} {r.resource.value} for {r.zone} "
                      f"(urgency {r.urgency})")
            submitted += new
            self._escalate(tick, submitted)

            pending = [r for r in submitted if r.remaining > 0 and not r.unreachable]
            result = self.strategy.allocate(pending, self.inventory, self.routing)
            for r in new:                       # unreachable ones are reported once
                if r.unreachable:
                    result.setdefault(r, {})
            self._report_batch(result)

    def _flag_unreachable(self, r):
        if self.report_unreachable and not self.routing.ranked_routes(r.zone):
            r.unreachable = True

    def _escalate(self, tick, submitted):
        for r in submitted:
            age = tick - r.tick
            if r.remaining > 0 and not r.unreachable and age > ESCALATION_AGE:
                if r.urgency < MAX_URGENCY:
                    old, r.urgency = r.urgency, r.urgency + 1
                    print(f"  [ESCALATION] {r.rid} urgency {old} -> {r.urgency} "
                          f"(pending {age} ticks, {r.remaining} units still unfulfilled)")
                elif not r.cap_logged:
                    r.cap_logged = True
                    print(f"  [ESCALATION] {r.rid} eligible (pending {age} ticks) but urgency "
                          f"already at cap {MAX_URGENCY}")

    # ---- reporting ------------------------------------------------------ #
    def _report_batch(self, result):
        silent = []
        for r, grant in result.items():
            r.attempts += 1
            got = sum(grant.values())
            if r.attempts > 1 and got == 0:
                silent.append(r.rid)
                continue
            self._print_request(r, grant, got)
        if silent:
            print(f"  (no new allocation possible for: {', '.join(silent)} - still pending)")

    def _print_request(self, r, grant, got):
        if grant:
            src = " + ".join(f"{w} ({n})" for w, n in grant.items()) if len(grant) > 1 \
                else next(iter(grant))
        else:
            src = "none"
        print(f"[Zone {r.zone}] requested {r.qty} {r.resource.value} (urgency {r.urgency}) "
              f"-> allocated {got} from {src} ({r.status.value})")
        if r.status is Status.UNREACHABLE:
            print("    reason: no path to any warehouse (all connecting roads are BLOCKED)")
        elif r.remaining > 0:
            print(f"    shortfall: {r.remaining} {r.resource.value} ({r.rid})")
        if self.show_routes:
            for wid in grant:
                rt = self.routing.route_to(r.zone, wid)
                pen = f", includes {DAMAGE_MULTIPLIER}x DAMAGED penalty" if rt.damaged else ""
                print(f"    route {wid}->{r.zone}: {' -> '.join(rt.path)} ({rt.distance:g} km{pen})")

    def summary(self):
        reqs = self.requests
        groups = {}
        for r in reqs:
            groups.setdefault((r.zone, r.resource), []).append(r)
        print("\n=== Final Allocation Report ===")
        for (zone, res) in sorted(groups, key=lambda k: (k[0], list(Resource).index(k[1]))):
            rs = groups[(zone, res)]
            req_total = sum(r.qty for r in rs)
            alloc_total = sum(r.allocated for r in rs)
            if alloc_total == req_total:
                st = Status.FULFILLED
            elif alloc_total > 0:
                st = Status.PARTIAL
            elif all(r.unreachable for r in rs):
                st = Status.UNREACHABLE
            else:
                st = Status.UNFULFILLED
            print(f"{zone}: {res.value} {alloc_total}/{req_total} ({st.value})")
        count = lambda s: sum(1 for r in reqs if r.status is s)
        print(f"Total requests: {len(reqs)} | Fulfilled: {count(Status.FULFILLED)} | "
              f"Partial: {count(Status.PARTIAL)} | Unfulfilled: {count(Status.UNFULFILLED)} | "
              f"Unreachable: {count(Status.UNREACHABLE)}")
        print("\nRemaining warehouse stock:")
        for wid, st in self.inventory.stock.items():
            print(f"  {wid}: " + ", ".join(f"{r.value} {st[r]}" for r in Resource))


# --------------------------------------------------------------------------- #
# Tier drivers
# --------------------------------------------------------------------------- #
def banner(text):
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def run_tier1(path):
    banner("TIER 1 - single pass, urgency-sorted, nearest warehouse only")
    net = build_network()
    sim = Simulator(net, UrgencyFirstStrategy(multi_source=False),
                    DirectRouting(net, penalize=False), load_requests(path))
    sim.run_single_pass()
    sim.summary()


def run_tier2(path, strategy_key, ticks):
    strat = STRATEGIES[strategy_key]()
    banner(f"TIER 2 - tick simulation, fallback warehouses, strategy: {strat.name}")
    net = build_network()
    sim = Simulator(net, strat, DirectRouting(net, penalize=True), load_requests(path))
    sim.run_ticks(ticks)
    sim.summary()


def run_tier3(path, strategy_key, ticks):
    strat = STRATEGIES[strategy_key]()
    banner(f"TIER 3 - full-graph shortest paths + UNREACHABLE, strategy: {strat.name}")
    net = build_network()
    sim = Simulator(net, strat, GraphRouting(net), load_requests(path),
                    report_unreachable=True, show_routes=True)
    sim.run_ticks(ticks)
    sim.summary()


def main():
    p = argparse.ArgumentParser(description="AidRoute - emergency resource allocation")
    p.add_argument("--tier", choices=["1", "2", "3", "all"], default="all")
    p.add_argument("--strategy", choices=["urgency", "fairness", "both"], default="both")
    p.add_argument("--ticks", type=int, default=8, help="ticks to simulate (Tier 2/3, min 5)")
    p.add_argument("--requests", help="optional CSV of requests (default: built-in dataset)")
    a = p.parse_args()
    ticks = max(a.ticks, 5)
    strategies = ["urgency", "fairness"] if a.strategy == "both" else [a.strategy]

    if a.tier in ("1", "all"):
        run_tier1(a.requests)
    if a.tier in ("2", "all"):
        for s in strategies:
            run_tier2(a.requests, s, ticks)
    if a.tier in ("3", "all"):
        for s in strategies:
            run_tier3(a.requests, s, ticks)


if __name__ == "__main__":
    main()
