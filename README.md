# AidRoute: Emergency Resource Allocation System

A Java-style console application, implemented in Python 3, that allocates limited relief supplies (water, medicine, food) from warehouses to disaster-affected zones, taking road damage into account.

> **Scenario (fictional):** flash floods along the Trishuli River cut off villages across Rasuwa, Nuwakot, Dhading, Gorkha and Chitwan. All data (stock, distances, road conditions) is invented for the exercise.

---

## Table of contents

1. [Features at a glance](#1-features-at-a-glance)
2. [Requirements and installation](#2-requirements-and-installation)
3. [Project files](#3-project-files)
4. [Quick start](#4-quick-start)
5. [Command-line reference](#5-command-line-reference)
6. [The data](#6-the-data)
7. [What each tier does](#7-what-each-tier-does)
8. [Architecture](#8-architecture)
9. [Algorithms explained](#9-algorithms-explained)
10. [Sample output](#10-sample-output)
11. [Output format reference](#11-output-format-reference)
12. [Assumptions](#12-assumptions)
13. [Known limitations](#13-known-limitations)
14. [Extending the program](#14-extending-the-program)
15. [Verifying behaviour: practice changes](#15-verifying-behaviour-practice-changes)
16. [Concurrency note](#16-concurrency-note)
17. [FAQ](#17-faq)
18. [Future improvements](#18-future-improvements)

---

## 1. Features at a glance

| Area | Implemented |
|---|---|
| Tier 1 | Single pass, urgency-sorted (ties by order received), nearest warehouse with stock, partial fill with shortfall, exact output format |
| Tier 2 | Tick simulation, fallback to next-nearest warehouse, DAMAGED = 3x distance, BLOCKED = impassable, two interchangeable allocation strategies, urgency escalation after 3 ticks (cap 5, logged) |
| Tier 3 | Dijkstra shortest paths over the full road graph, path reporting, explicit `UNREACHABLE` status, final summary report |
| Robustness | Never crashes on unmet demand; every outcome is one of FULFILLED / PARTIAL / UNFULFILLED / UNREACHABLE |
| Extras | Optional CSV input, selectable tier/strategy/tick count, contention demo dataset |

## 2. Requirements and installation

- **Python 3.7 or newer** (check with `python --version`).
- **No third-party packages.** Only the standard library is used (`argparse`, `csv`, `heapq`, `dataclasses`, `enum`, `abc`).
- Nothing to build or install.

On macOS/Linux use `python3`; on Windows, if `python` is not recognised, use `py`.

## 3. Project files

| File | Purpose |
|---|---|
| `aidroute.py` | The whole application (single source file) |
| `README.md` | One-page submission README (assignment deliverable) |
| `README_DETAILED.md` | This document |
| `requests.csv` | The fixed five-request dataset in CSV form |
| `contention_demo.csv` | Small dataset where two requests compete for the same water, used to show how the two strategies differ |
| `AidRoute_Code_Study_Guide.pdf` | Line-by-line study guide of the code |

## 4. Quick start

```bash
cd aidroute
python aidroute.py            # runs Tier 1, then Tier 2 and Tier 3 with both strategies
```

To see one thing at a time:

```bash
python aidroute.py --tier 1
python aidroute.py --tier 2 --strategy urgency
python aidroute.py --tier 3 --strategy fairness
```

## 5. Command-line reference

| Option | Values | Default | Meaning |
|---|---|---|---|
| `--tier` | `1`, `2`, `3`, `all` | `all` | Which tier(s) to run |
| `--strategy` | `urgency`, `fairness`, `both` | `both` | Allocation strategy for Tier 2 and 3 (Tier 1 always uses urgency order) |
| `--ticks` | integer | `8` | Number of ticks to simulate. A minimum of 5 is enforced so escalation can trigger |
| `--requests` | path to CSV | built-in dataset | Load requests from a file |
| `-h`, `--help` | none | none | Show help |

### Every useful command

```bash
python aidroute.py
python aidroute.py -h
python aidroute.py --tier 1
python aidroute.py --tier 2
python aidroute.py --tier 2 --strategy urgency
python aidroute.py --tier 2 --strategy fairness
python aidroute.py --tier 2 --ticks 10
python aidroute.py --tier 3
python aidroute.py --tier 3 --strategy urgency
python aidroute.py --tier 3 --strategy fairness
python aidroute.py --tier 3 --ticks 10
python aidroute.py --requests requests.csv
python aidroute.py --tier 3 --strategy urgency  --requests contention_demo.csv
python aidroute.py --tier 3 --strategy fairness --requests contention_demo.csv
python aidroute.py > output.txt       
```

### CSV format

```csv
order,request_id,zone,resource,quantity,urgency,tick
1,R1,Z1,WATER,250,4,1
```

Resources are `WATER`, `MEDICINE`, `FOOD`. Input is assumed to be well-formed.

## 6. The data

### Warehouses

| ID | Location | WATER | MEDICINE | FOOD |
|---|---|---|---|---|
| W1 | Dhunche (Rasuwa HQ) | 200 | 100 | 150 |
| W2 | Bidur (Nuwakot HQ) | 150 | 80 | 200 |

### Zones

| ID | Location |
|---|---|
| Z1 | Syabrubesi |
| Z2 | Timure |
| Z3 | Devighat |
| Z4 | Gorkha Bazar |

### Roads (undirected)

| From | To | km | Status | Effect |
|---|---|---|---|---|
| W1 | Z1 | 12 | OPEN | usable |
| W1 | Z2 | 20 | DAMAGED | Tier 1: 20 km; Tier 2/3: 60 km (3x) |
| W2 | Z3 | 8 | OPEN | usable |
| W2 | Z4 | 15 | BLOCKED | never used |
| Z1 | Z3 | 10 | OPEN | only used as a hop in Tier 3 |
| W1 | W2 | 25 | OPEN | only used as a hop in Tier 3 |

### Requests (fixed dataset)

| Order | ID | Zone | Resource | Qty | Urgency | Tick |
|---|---|---|---|---|---|---|
| 1 | R1 | Z1 | WATER | 250 | 4 | 1 |
| 2 | R2 | Z3 | MEDICINE | 100 | 5 | 1 |
| 3 | R3 | Z4 | WATER | 20 | 5 | 1 |
| 4 | R4 | Z2 | FOOD | 500 | 2 | 2 |
| 5 | R5 | Z1 | MEDICINE | 60 | 2 | 2 |

Two facts explain most results: **Z4's only road is blocked** (so Z4 is unreachable), and **Z2's only road is damaged** (reachable, but 60 km effective).

## 7. What each tier does

| | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| Time model | one pass, ticks ignored | ticks 1..N | ticks 1..N |
| Distance model | direct warehouse-zone roads; DAMAGED at listed km | direct roads; DAMAGED = 3x | full graph via Dijkstra; DAMAGED = 3x |
| Warehouses per request | nearest one with stock only | as many as needed, nearest first | as many as needed, nearest first |
| Strategy | urgency order | urgency-first or fairness-first | urgency-first or fairness-first |
| Escalation | no | yes | yes |
| Zone with no route | UNFULFILLED | UNFULFILLED | UNREACHABLE |
| Path printed | no | no | yes |

### Request statuses

| Status | Meaning |
|---|---|
| `FULFILLED` | the full quantity was allocated |
| `PARTIAL` | some units were allocated, some are still missing |
| `UNFULFILLED` | a route exists (or Tier 1/2), but no stock could be allocated |
| `UNREACHABLE` | Tier 3 only: no path exists from the zone to any warehouse |

## 8. Architecture

```
   requests (built-in or CSV)                 build_network()
              |                                     |
        load_requests()                          Network (warehouses, zones, roads)
              |                                     |
        [Request, ...]              +---------------+---------------+
              |                     |                               |
              |              RoutingStrategy                   Inventory (stock copy)
              |        (DirectRouting / GraphRouting)               |
              +-------------------> Simulator <---- AllocationStrategy
                        (ticks, escalation,        (UrgencyFirst / FairnessFirst)
                         printing, summary)
```

| Component | Responsibility |
|---|---|
| `Request` | Data and state of one request (remaining, allocated, urgency, flags); `status` is computed |
| `Network` | Static warehouses, zones and roads |
| `Inventory` | The only mutable stock ledger; each run gets its own copy |
| `RoutingStrategy` | Returns reachable warehouses, nearest first. Implementations: `DirectRouting`, `GraphRouting` |
| `AllocationStrategy` | Divides stock among pending requests. Implementations: `UrgencyFirstStrategy`, `FairnessFirstStrategy` |
| `Simulator` | Runs the single pass or the ticks, applies escalation, prints results and the summary |

The **Strategy pattern** is used twice. `Simulator` only calls `strategy.allocate(...)` and `routing.ranked_routes(...)`, so swapping either does not change the request-processing code.

## 9. Algorithms explained

### 9.1 Urgency-first allocation

1. Sort pending requests by `(-urgency, order)`: highest urgency first, ties by order received.
2. For each request, walk its warehouses nearest first.
3. Take `min(stock available, units still needed)` from each warehouse with stock.
4. Tier 1 stops after the first warehouse that supplied anything (single source); Tier 2/3 continue until the request is satisfied or warehouses are exhausted.

### 9.2 Fairness-first allocation (round-robin)

1. Keep the list of active requests in arrival order.
2. In each round, every active request receives **one unit** from the nearest warehouse that still has that resource.
3. A request that is satisfied, or has no reachable stock left, leaves the list.
4. Repeat until nobody can receive anything more.

Under contention this equalises shares instead of letting the most urgent request take everything.

### 9.3 Direct routing (Tier 1 and 2)

Only roads joining a warehouse directly to the zone are used. BLOCKED roads are ignored. With `penalize=False` (Tier 1) DAMAGED roads count at their listed distance; with `penalize=True` (Tier 2) they cost 3x.

### 9.4 Graph routing with Dijkstra (Tier 3)

- Build an undirected weighted graph from all non-BLOCKED roads; DAMAGED weights are tripled.
- Run Dijkstra from the zone; distances to both warehouses are read from one run.
- Zones and warehouses may be intermediate hops, e.g. `W1 -> Z1 -> Z3` (22 km) or `W2 -> W1 -> Z2` (85 km).
- Path is rebuilt through predecessor links; results are cached per zone.
- No warehouse reachable means `UNREACHABLE`.

Worked distances:

| Zone | Nearest | Second |
|---|---|---|
| Z1 | W1: 12 km | W2: 18 km via Z3 |
| Z2 | W1: 60 km (damaged) | W2: 85 km via W1 |
| Z3 | W2: 8 km | W1: 22 km via Z1 |
| Z4 | none | none (unreachable) |

### 9.5 Escalation (requirement 9)

At the start of each tick, any request that still has unfulfilled units and is **older than 3 ticks** (`current_tick - submit_tick > 3`) has its urgency raised by 1, capped at 5. Each change is logged as `[ESCALATION]`; a request already at the cap is logged once. With the fixed dataset R1 escalates at tick 5, R4 at ticks 6, 7 and 8.

Since stock never refills, escalation does not change quantities here; it changes processing order and is visible in the log.

## 10. Sample output

### Tier 1

```
[Zone Z3] requested 100 MEDICINE (urgency 5) -> allocated 80 from W2 (PARTIAL)
    shortfall: 20 MEDICINE (R2)
[Zone Z4] requested 20 WATER (urgency 5) -> allocated 0 from none (UNFULFILLED)
    shortfall: 20 WATER (R3)
[Zone Z1] requested 250 WATER (urgency 4) -> allocated 200 from W1 (PARTIAL)
    shortfall: 50 WATER (R1)
[Zone Z2] requested 500 FOOD (urgency 2) -> allocated 150 from W1 (PARTIAL)
    shortfall: 350 FOOD (R4)
[Zone Z1] requested 60 MEDICINE (urgency 2) -> allocated 60 from W1 (FULFILLED)

=== Final Allocation Report ===
Z1: WATER 200/250 (PARTIAL)
Z1: MEDICINE 60/60 (FULFILLED)
Z2: FOOD 150/500 (PARTIAL)
Z3: MEDICINE 80/100 (PARTIAL)
Z4: WATER 0/20 (UNFULFILLED)
Total requests: 5 | Fulfilled: 1 | Partial: 3 | Unfulfilled: 1 | Unreachable: 0
```

### Tier 3 (urgency-first), excerpt

```
[Zone Z3] requested 100 MEDICINE (urgency 5) -> allocated 100 from W2 (80) + W1 (20) (FULFILLED)
    route W2->Z3: W2 -> Z3 (8 km)
    route W1->Z3: W1 -> Z1 -> Z3 (22 km)
[Zone Z4] requested 20 WATER (urgency 5) -> allocated 0 from none (UNREACHABLE)
    reason: no path to any warehouse (all connecting roads are BLOCKED)

=== Final Allocation Report ===
Z1: WATER 250/250 (FULFILLED)
Z1: MEDICINE 60/60 (FULFILLED)
Z2: FOOD 350/500 (PARTIAL)
Z3: MEDICINE 100/100 (FULFILLED)
Z4: WATER 0/20 (UNREACHABLE)
Total requests: 5 | Fulfilled: 3 | Partial: 1 | Unfulfilled: 0 | Unreachable: 1
```

### Strategy comparison under contention (`contention_demo.csv`, Tier 3)

| Strategy | Z1 water | Z3 water |
|---|---|---|
| Urgency-first | 300/300 (FULFILLED) | 50/300 (PARTIAL) |
| Fairness-first | 175/300 (PARTIAL) | 175/300 (PARTIAL) |

## 11. Output format reference

The main line of each processed request follows the required format exactly:

```
[Zone <ID>] requested <qty> <RESOURCE> (urgency <n>) -> allocated <qty> from <WarehouseID> (<FULFILLED|PARTIAL|UNFULFILLED>)
```

Notes:

- When several warehouses supply one request, the source reads `W2 (80) + W1 (20)`; when none, `none`.
- Tier 3 also prints `UNREACHABLE` as a status.
- Indented lines under a request give the shortfall, the reason (unreachable), and in Tier 3 the route with kilometres.
- Tick simulations print `--- Tick N ---` headers, `submitted ...` lines, `[ESCALATION]` lines, and a compact `(no new allocation possible for: ...)` line for retries that found no stock.
- The final report prints one line per zone and resource (`Z1: WATER 50/50 (FULFILLED)`), a totals line, and the remaining warehouse stock.

## 12. Assumptions

1. Each request is one participant in fairness round-robin (not each zone).
2. Tier 1 and 2 use only direct warehouse-zone roads; zone-zone and warehouse-warehouse roads matter only in Tier 3.
3. "Nearest warehouse with available stock" means the nearest warehouse holding some stock of that resource.
4. In Tier 1 and 2, Z4 shows as `UNFULFILLED`; `UNREACHABLE` is a Tier 3 status.
5. Stock is never replenished, so partially served requests stay partial.
6. "Older than 3 ticks" means `current_tick - submitted_tick > 3`.
7. Escalation is applied at the start of a tick, before allocation, so the new urgency affects that tick's order.
8. Ties in distance are broken by warehouse id.
9. Input files are well-formed (as stated by the assignment).

## 13. Known limitations

- Fairness only rebalances within the same tick; stock given in earlier ticks is not taken back.
- With this dataset, Tier 1 and 2 produce the same numbers, because every reachable zone has exactly one direct warehouse road. Fallback across warehouses shows up in Tier 3.
- Graph routing results are cached per zone and assume the map does not change during a run.
- The `copy` import is unused (harmless).
- The `--strategy` choices are listed manually in `main()`.

## 14. Extending the program

### Add a new allocation strategy

```python
class LargestShortfallStrategy(AllocationStrategy):
    name = "largest-shortfall-first"

    def allocate(self, pending, inventory, routing):
        result = {}
        for req in sorted(pending, key=lambda r: (-r.remaining, r.order)):
            result[req] = {}
            for route in routing.ranked_routes(req.zone):
                if req.remaining == 0:
                    break
                avail = inventory.available(route.warehouse_id, req.resource)
                if avail > 0:
                    self._grant(req, route.warehouse_id, min(avail, req.remaining),
                                inventory, result)
        return result

STRATEGIES["shortfall"] = LargestShortfallStrategy
```

Then add `"shortfall"` to the `--strategy` choices in `main()`. `Simulator` is not modified.

### Change the map or stock

Edit `build_network()`: warehouses and stock, zones, and roads with their `RoadStatus`.

### Change the rules

Constants at the top of the file: `DAMAGE_MULTIPLIER` (3), `MAX_URGENCY` (5), `ESCALATION_AGE` (3).

## 15. Verifying behaviour: practice changes

These were run and confirmed:

| Change in `build_network()` | Command | Result |
|---|---|---|
| W2-Z4 from BLOCKED to OPEN | `--tier 3 --strategy urgency` | Z4 reachable via W2 (15 km); R3 fulfilled with 20 water; Fulfilled 4, Partial 1, Unreachable 0 |
| W1-Z2 from DAMAGED to BLOCKED | `--tier 3 --strategy urgency` | Z2 becomes UNREACHABLE (R4 gets 0/500); Fulfilled 3, Unreachable 2 |
| none | `--tier 3 --requests contention_demo.csv` (both strategies) | Urgency-first: 300 and 50; fairness-first: 175 and 175 |

## 16. Concurrency note

The assignment excludes real multithreading. If requests arrived concurrently, the only shared mutable state is `Inventory`. Guarding the availability check and `take` with a single lock (or an atomic compare-and-set on stock counts) would prevent overselling. Requests, routes and the network are read-mostly.

## 17. FAQ

**Why do both strategies give identical output on the fixed data?**
No two requests compete for the same resource from the same warehouse, so there is nothing to share out. Use `contention_demo.csv` to see the difference.

**Why is Z4 UNFULFILLED in Tier 1/2 but UNREACHABLE in Tier 3?**
The UNREACHABLE status is a Tier 3 requirement (item 11). Tiers 1 and 2 have no such status.

**Why does the Tier 2 output not show a second warehouse?**
Only direct warehouse-zone roads count in Tier 2, and each zone has at most one.

**Why do lines print "no new allocation possible"?**
Requests that are still unresolved are retried every tick; when stock is exhausted the retry finds nothing, so a compact line is printed instead of repeating full lines.

**Does escalation change the totals?**
Not with the fixed data (no restocking). It changes the processing order and is logged.

**The task says Java. Why Python?**
This version was requested in Python. The design (interfaces, strategy classes, simulator) maps one-to-one to Java classes, so it can be ported directly.

## 18. Future improvements

- Unit tests for strategies, Dijkstra (ties, unreachable nodes) and escalation timing.
- Restocking events so that retries and escalation change outcomes.
- Fairness weighted by urgency or population, or per zone instead of per request.
- Road capacity and delivery time as well as distance.
- Cache invalidation when road status changes mid-run.
- Input validation and a JSON input format.
- A lock-protected `Inventory` for concurrent request intake.#   A i d R o u t e  
 