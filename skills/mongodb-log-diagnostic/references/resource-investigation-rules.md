# Resource investigation rules for the report agent

> **Status: reference only, not wired in yet.** Neither `build_system_prompt()` nor the extract stage loads this
> file. It turns MongoDB hardware and server metrics into diagnosis rules.
>
> Sources:
> - KB 000020087 "Understanding Atlas Metrics and How to Use Them to Address Performance Issues"
> - KB 000022332 "My disk IOPS regularly spike to the maximum configured IOPS"
> - KB 000020484 "What triggers auto-scaling in Atlas?"
> - WiredTiger documentation
>
> The metrics are named as in Atlas. The same signals exist on Enterprise / Ops Manager (`serverStatus`, FTDC,
> OS metrics), so the rules apply to both; Atlas-only items are marked **(Atlas)**.

## 0. Principles
1. **A symptom is not a cause.** These are almost always *consequences* of a workload or hardware bottleneck:
   ticket depletion, queueing, high execution time, replication lag, application-thread eviction. Walk back to
   the cause: query targeting, cache activity, disk, CPU.
2. **Averages over maxima.**
   - "Max" charts (Max Disk IOPS, Max Normalized System CPU, Max System Memory) show the peak within each
     granularity window.
   - Assess sustained load with the average metrics.
   - Report a peak only when it aligns in time with latency or errors.
   - CPU occasionally touching 100% is not, by itself, a performance issue.
3. **Correlate in time.** A resource finding needs overlapping time windows with slow operations, errors or
   connection churn. Use the pipeline's `correlation_margin_minutes` (default 5). Temporal overlap shows
   correlation, not causation; say so.
4. **Tune before scaling.** First fix the workload: indexes, query targeting, write batching. Then consider
   capacity (IOPS, tier, RAM). State both when the evidence supports both.
5. **Per node.** A primary and its secondaries have different loads. One node over threshold is enough for
   Atlas auto-scaling (§9), but in a report it is a node-specific finding.

## 1. Pipeline thresholds (current `agent/resource_investigation.py` defaults)
These are configurable via `MONGODB_LOG_DIAG_RESOURCE_THRESHOLDS`.

| Key | Default | Use |
|---|---|---|
| `cpu_peak_percent` | 90 | peak CPU trigger |
| `cpu_sustained_percent` / `cpu_sustained_minutes` | 70 / 20 | sustained CPU trigger |
| `memory_percent` | 75 | system memory trigger |
| `disk_queue_depth` (min `disk_min_points` points) | 10 (3) | disk saturation trigger |
| `connection_memory_mb` | 1.0 | memory estimate per connection |

Interpretation bands below come from the KBs. Use them to *qualify* a triggered signal (good / watch / bad),
not to invent triggers.

## 2. CPU
**Metrics.**
- Normalized System CPU: all processes, 0–100% divided by cores, broken into user / kernel / iowait / steal.
- Normalized Process CPU: the mongod process only.
- The Max variants of both.

**Bands.**

| Normalized CPU (average) | Assessment |
|---|---|
| < 50% | healthy, capacity available |
| 50–80% | healthy **if** no large or persistent spikes |
| > 80% sustained | warning: near limits, cannot absorb growth |
| frequent sharp spikes to high values | workload increase or inefficient queries |

**Diagnosis rules.**
- **High user CPU + bad query targeting.** Index keys and documents scanned is the most common cause. Cite the
  top `cpuNanos` groups and their scan ratios, and apply the query rules (ESR, COLLSCAN).
- **High iowait.** The CPU is waiting on disk: a **disk bottleneck**. Go to §4, not to CPU scaling.
- **High steal.** The hypervisor withholds CPU.
  - **(Atlas)** On AWS M10/M20 (burstable), steal rises when the CPU credit balance is exhausted.
  - Baseline sustained CPU is AWS M10/M20 20%, Azure M10/M20 25%, GCP M10 50% (M20 GCP is not burstable).
  - A burstable cluster "at 20%" can be saturated.
- **Kernel CPU high with connection churn.** Many connections opened or closed in big batches, or
  authentication storms (SCRAM is CPU-expensive). Check connection-pool settings (§7).
- **CPU with cache churn.** When worker *and application* threads evict (§3), CPU rises together with latency.
- High CPU also slows secondaries' replication. Check replication lag (§8).

**Remediation order.** Query/index fixes; then connection-pool fixes; then (when sustained above 80% after
tuning) a larger tier or more cores. On burstable tiers, recommend a non-burstable tier when the baseline is
exceeded.

## 3. WiredTiger cache and memory
### 3.1 How the WiredTiger cache works
- The cache defaults to max(50% of (RAM − 1 GB), 256 MB). It holds uncompressed collection and index pages.
  The OS filesystem cache holds compressed on-disk blocks; memory outside both is used by connections,
  aggregation / sort buffers and in-memory sorts.
- **Cache fill ratio:** bytes in cache divided by the configured maximum.
- **Dirty fill ratio:** modified, not yet written bytes divided by the cache size.
- Eviction uses **background worker threads** first. When it cannot keep up, **application threads** (the
  ones serving client requests) must evict before continuing, which adds latency directly to operations.
  Evicting dirty pages costs more because they must be reconciled (written) first.

| Ratio | Default threshold | Behaviour | Impact |
|---|---|---|---|
| Cache fill | 80% (`eviction_target`) | worker threads start evicting | normal |
| Cache fill | 95% (`eviction_trigger`) | **application threads** recruited | latency, tickets held longer |
| Dirty fill | 5% (`eviction_dirty_target`) | worker threads write dirty pages | normal |
| Dirty fill | 20% (`eviction_dirty_trigger`) | **application threads** evict dirty pages | severe degradation, including replication |

**Rules.**
- A cache fill around 80% is the *expected* steady state, not a finding.
- Sustained fill **≥ 95%**, or dirty **≥ 20%**, aligned with latency means **cache pressure**.
- **Working set larger than cache.**
  - Symptoms: high "read into cache", page faults, read IOPS, `bytesRead` / `timeReadingMicros` in slow ops.
  - Causes: scans (query targeting), or data plus indexes legitimately larger than RAM.
  - Fix queries first; then add RAM (tier). Compression (zstd) only reduces disk I/O and size; it does not
    enlarge the uncompressed cache.
- **High dirty fill.** Write-heavy bursts, large multi-document updates, bulk loads, or long-running
  transactions pinning history. Propose write batching or throttling, and spread bulk loads over time.
- Ticket depletion plus cache above 95% means cache-bound (§6).
- **Cache Activity.** Large "read into" values with high query targeting point to inefficient queries. Large
  "written from" values point to write-heavy pressure on the dirty thresholds.

### 3.2 System memory, swap, page faults
- **System Memory.** Low or unstable *available* memory (not matching the workload) degrades performance and
  risks OOM restarts.
  - Consumers: large indexes, many connections (**~1 MB each, outside the WT cache**), complex
    sorts / aggregations, other processes.
  - "Max System Memory" is computed differently; do not compare it directly with System Memory.
- **Swap used** means memory is insufficient. Treat it as a capacity finding unless a transient job explains it.
- **Page faults.** Occasional faults are normal. Frequent and continuous faults mean memory contention:
  increased workload, unoptimised queries, or under-provisioned RAM.
- **Memory estimate to quote:** connections × 1 MB + WT cache size + aggregation/sort usage. Do not invent the
  latter; say "not measured".

## 4. Disk: IOPS, latency, queue depth (disk pressure)
**Metrics.**
- Disk IOPS (read/write, average): **(Atlas)** on AWS/Azure the IOPS budget is shared between reads and
  writes; on GCP it is split evenly.
- Max Disk IOPS: peak within the window.
- Disk Latency: ms per read/write.
- Disk Queue Depth: average outstanding requests.

**Bands.**

| Metric | Good | Bad |
|---|---|---|
| Disk IOPS | low relative to the provisioned limit | consistently near or above the limit |
| Disk latency | single-digit ms | sustained spikes |
| Queue depth | low | **> 10 consistently** |
| CPU iowait | low | elevated with latency |

**Rule: what is (not) under-provisioning.** High IOPS alone is not a problem. A disk is under-provisioned only
when high IOPS are **consistent and accompanied by** increased execution times, elevated latency, iowait, and
queue depth above 10.

**Checkpoint spikes are expected.**
- Writes go to the cache and the journal (write-ahead log) first. WiredTiger checkpoints run every **60 s** and
  end with an fsync, so write IOPS spike roughly every minute.
- Durability is guaranteed by the journal, not by the checkpoint.
- A periodic 60 s sawtooth in Max Disk IOPS alone is **not** a finding.

**Other legitimate write activity:**
- `allowDiskUse` spills: aggregation stages over 100 MB write temporary files.
- Eviction reconciliation of dirty pages (§3).
- OS dirty-page flushing (`vm.dirty_*` thresholds; Enterprise: tune per the production notes).

**Read IOPS causes:** unindexed or poorly targeted queries fetching from disk instead of cache; working set
larger than cache.
**Write IOPS causes:** write-intensive workloads, many indexes per write, oplog churn, spills.

**Remediation order.**
1. Fix the queries that read from disk (query targeting, missing index).
2. Reduce write amplification (bulk unordered writes, fewer indexes, smaller documents).
3. Compression (zstd versus default snappy).
   - It can reduce disk size and IOPS at some CPU cost.
   - It needs a resync to apply to existing data. **(Atlas)** it requires a Support request.
   - Validate it on one node first.
4. Raise provisioned IOPS or the storage class, or **(Atlas)** the cluster tier (more RAM means a bigger cache
   and fewer reads).

## 5. Query targeting and scan-and-order (the bridge to query rules)
- **Query targeting:** scanned keys / returned and scanned objects / returned; **ideal = 1**. High values mean
  unoptimised or uncovered queries. Expect consequences in execution time, CPU, cache "read into", and read
  IOPS. Link each resource finding to the top slow groups by scan ratio (see `query-performance-rules.md`).
- **Scan and Order:** in-memory sorts per second. They raise memory outside the cache and CPU. Apply ESR /
  sort rules.

## 6. Concurrency: tickets, queues, connections
- **Tickets.**
  - Before 7.0: 128 read and 128 write tickets by default.
  - 7.0+: execution control adjusts them dynamically within **4–128**, so a low "available" count is not
    automatically a problem.
  - **Ticket depletion is a consequence, not a cause.** Look for the bottleneck: disk (§4), CPU (§2), cache
    above 95% (§3), bad query targeting, many scan-and-order operations.
  - In slow logs, high `queues.execution.totalTimeQueuedMicros` means admission wait.
- **Connections.**
  - Each costs about 1 MB of RAM outside the cache.
  - Churn (open/close in batches) costs CPU, and TLS / SCRAM handshakes are expensive.
  - Causes: pools that are too large per application instance multiplied by instance count, missing pool
    reuse (a client created per request), serverless or Lambda clients without reuse, retry storms.
  - Propose pool sizing (`maxPoolSize`, `minPoolSize`, `maxIdleTimeMS`) and client reuse.
  - **Never** propose larger pools or longer timeouts as the first response to a server bottleneck.
- **Operation execution time** (avg read / write / command ms): a rise means poorly indexed queries **or** a
  resource shortage. Distinguish the two using query targeting.

## 7. Replication
- **Replication lag** is accurate only above 1–2 s.
  - Causes: write throughput beyond secondaries' capacity; heavy secondary reads; resource contention on the
    secondary (CPU, disk, cache).
  - Impact: `w:majority` write latency, **flow control** throttling on the primary, cache pressure on the
    primary (history kept for majority read concern), and stale secondary reads without `maxStalenessSeconds`.
- **Oplog GB/hour** measures churn by size, not by operation count.
  - Causes: bulk loads, batch jobs, retryable `findAndModify` at high rates, large updates rewriting big
    documents.
  - With a fixed oplog size, churn shortens the **oplog window**. With a minimum retention period, it grows
    disk usage.
- **Oplog window / replication headroom.** When lag exceeds the window (headroom reaches 0), the secondary
  needs a full initial sync. Report headroom below a few hours as a P0/P1 risk on production.
- In slow logs, `waitForWriteConcernDurationMillis` and flow-control fields tie query latency to replication.

## 8. Correlation playbook (resource → cause)

| Observed | Check next | Likely cause | Report as |
|---|---|---|---|
| CPU > 80% sustained + high query targeting | top `cpuNanos` groups, COLLSCAN | inefficient queries | query finding + resource consequence |
| CPU iowait high + queue depth > 10 + latency up | read vs write IOPS, cache activity | disk bottleneck | disk pressure |
| Read IOPS near limit + "read into cache" high | scan ratios, working set vs cache | scans / working set > cache | query fix first, then RAM / IOPS |
| Write IOPS periodic spikes every 60 s only | latency, queue | checkpoints (expected) | not a finding |
| Cache ≥ 95% or dirty ≥ 20% + latency | write rate, bulk jobs, long txns | cache pressure | P1 (P0 if replication affected) |
| Tickets depleted / queued | disk, CPU, cache | downstream symptom | symptom, cite root cause |
| Connections spike + kernel CPU | app pool config, auth rate | connection churn | client configuration finding |
| Replication lag rising + oplog GB/h spike | batch / bulk jobs, secondary resources | write burst or weak secondary | replication risk; headroom |
| Swap used / available memory very low | connections, index size, sorts | memory exhaustion | capacity risk |
| Steal high on M10/M20 **(Atlas)** | CPU credit / baseline | burstable limits | tier recommendation |

## 9. Scaling (Atlas auto-scaling and capacity advice)
- **Scale-up triggers (M30+):** average normalized user + kernel CPU, **or** memory utilization, **> 90% for
  10 minutes** or **> 75% for 1 hour**.
  - M10/M20 use burstable-relative thresholds (for example AWS: above 15% average over 1 h scales up).
  - Analytics nodes: above 75% for 1 hour.
- **Scale-down:** CPU under 45% and WT cache under 90% for 4 h (and the last 10 min), projected memory on the
  lower tier under 60%, and no scaling in the last 24 h.
- Decisions use **averages**: a short 95% burst may never trigger scaling. Scaling goes one tier at a time and
  takes time; it does not run while other cluster changes are in progress, and it does not help during a
  sudden bulk load. Recommend **proactive scaling** before planned spikes.
- In a sharded cluster, tier auto-scaling applies to all shards unless independent shard scaling is enabled.
  The config server replica set does not auto-scale.
- **Enterprise:** there is no auto-scaling. Translate the same bands into capacity-planning advice (RAM for the
  working set, IOPS headroom, CPU < 80% sustained).

## 10. Wording rules
- Give the metric, the window (UTC), average **and** peak, the threshold band, and the correlated slow groups
  or errors.
- Use "consistent with", "correlates with" and "likely contributes"; reserve "caused by" for when the evidence
  chain is explicit.
- Always end a resource finding with the validation metric to watch after the fix (same metric, same window
  length, same time of day).
- Do not recommend changing Atlas-managed components, or WiredTiger internals such as eviction thresholds or
  cache size, as a customer action. On Enterprise, change them only with MongoDB guidance.
