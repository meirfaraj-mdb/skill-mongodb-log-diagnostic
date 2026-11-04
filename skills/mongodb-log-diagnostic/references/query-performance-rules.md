# Query performance rules for the report agent

> **Status: reference only, not wired in yet.** Neither `build_system_prompt()` nor the extract stage loads this
> file. It contains general MongoDB query, index, aggregation and schema rules. It contains no
> deployment-specific data: every name below is a placeholder.

## Notation
| Placeholder | Meaning |
|---|---|
| `<coll>`, `<foreignColl>` | a collection, a joined collection |
| `<eqField>`, `<eqField2>` | fields matched by equality (`{f: v}`, `$eq`, small `$in`) |
| `<sortField>`, `<sortField2>` | fields in the sort specification |
| `<rangeField>` | fields matched by a range or inequality (`$gt`, `$gte`, `$lt`, `$lte`, `$ne`, `$nin`, non-prefix `$regex`, `$exists`) |
| `<arrayField>` | a field holding an array (multikey when indexed) |
| `<groupKey>`, `<localField>`, `<foreignField>` | grouping key, `$lookup` join fields |
| `<projectedField>` | a field returned by the projection |
| `1` / `-1` | ascending / descending index direction |

## 0. How to apply these rules
1. **Apply a rule only when the extraction contains its trigger signals.** Cite the extracted values used; never
   copy values or names from this file.
2. **The schema, data distribution, cardinality and full index inventory are unknown.**
   - Every index, query or schema proposal is a **candidate to validate**, never an instruction.
   - Give the key order and the rule it comes from.
   - Give the validation steps: representative `explain("executionStats")`, existing index inventory and
     `$indexStats`, then a test on representative data.
3. **Keep findings separate.** One issue, one rule, one action. Do not offer several alternative indexes for the
   same shape unless they are ranked with their trade-off.
4. **Never reveal literals.** Query values are redacted (`?`). Command options (`$db`, read preference, read
   concern, `batchSize`, `limit`, `maxTimeMS`, `singleBatch`, `hint`) may carry logged values and may be used.
5. **Exclusions: not evidence of a missing customer index.**
   - Change streams (`$changeStream`), oplog tailing, `$natural` hints or sorts.
   - `admin`, `local`, `config`, `__mdb_internal*`, and Atlas-managed namespaces.
   - A collection scan on a small collection with low latency is not a finding.
6. **Rank by total work, not by the slowest single execution.** Use execution count × typical duration, total CPU
   time, and documents examined per document returned. A frequent moderate query usually outranks a rare slow one.

## 1. Signal → rule map
| Signal | Ideal | Rule | Meaning |
|---|---|---|---|
| plan `COLLSCAN` | — | §2, §3 | no usable index for the predicate, or the planner chose none |
| keys examined / returned | ≈ 1 | §2.1, §2.3 | loose index bounds: range or sort placed before equality, or a low-selectivity prefix |
| docs examined / returned | ≈ 1 | §2.4, §2.6 | documents fetched then discarded: predicate fields missing from the index |
| docs examined ≫ keys examined | — | §3 | collection scan or full fetch |
| keys ≈ returned and docs examined = 0 | ideal | — | covered query (`PROJECTION_COVERED`) |
| `hasSortStage` / `SORT` stage | absent | §2.2 | blocking in-memory sort |
| `usedDisk` | false | §4 | a blocking stage spilled to disk |
| `replanned` | false | §6 | cached plan evicted, plan instability |
| high yields with long duration | — | §3 | long scans; also check cache pressure (resource rules) |
| repeated `writeConflicts` | 0 | §7 | concurrent updates to the same documents |
| high storage bytes / time reading | low | resource rules | data read from disk into cache |
| high admission queue / ticket wait | ≈ 0 | resource rules | saturated node: a symptom, not a query defect by itself |
| high write-concern wait | low | §5 | secondaries slow to acknowledge |
| keys inserted per write ≫ 1 | ≈ number of indexes | §2.9 | index maintenance cost |

**Plan vocabulary.**
- `IXSCAN {…}`: index scan.
- `IDHACK` / `EXPRESS`: `_id` point-lookup fast path.
- `COUNT_SCAN`: count answered from the index.
- `DISTINCT_SCAN`: index skip-scan.
- `SORT_MERGE`: merge of sorted index ranges.
- `OR` / `SUBPLAN`: indexed `$or`.

## 2. Index design
### 2.1 ESR: Equality → Sort → Range
- **Rule.** Order compound index keys as equality fields, then sort fields, then range fields:
  `{<eqField>: 1, <sortField>: 1|-1, <rangeField>: 1}`.
- **Why.**
  - The equality prefix narrows the scan to contiguous keys.
  - Sort keys placed next are already ordered, so no blocking sort is needed.
  - A range placed before the sort key breaks the order and forces a `SORT` stage.
  - A range placed before an equality key makes the scan read every key in the range.
- **Trigger.** A shape with equality plus sort plus range predicates, together with a blocking sort or loose
  bounds (keys examined ≫ returned).
- Among several equality fields, order does not change correctness. Prefer the order that lets the index serve
  other shapes (shared prefix). Never claim "most selective first" without cardinality evidence.
- With two or more sort keys, directions must match the sort or its exact inverse:
  `{<sortField>: 1, <sortField2>: -1}` serves `{1, -1}` and `{-1, 1}`, but not `{1, 1}`.

### 2.2 Sort
- A blocking sort means the sort is not served by an index. Apply ESR so the sort keys directly follow the
  equality prefix.
- A blocking sort has a 100 MB memory limit. Above it, the sort spills to disk when disk use is allowed (the
  default since 6.0), otherwise it fails.
- `sort` + `limit` (top-k) bounds memory but still examines every matching document. Index the sort so the scan
  can stop early.
- Large `skip` values cost linear work. Propose range (keyset) pagination on an indexed, unique sort key, for
  example `{<sortField>: 1, _id: 1}` with a "greater than the last value seen" condition.

### 2.3 Range and low-selectivity predicates
- `$ne`, `$nin`, `$not` and `$exists: false` match most of the index. Do not propose an index whose only
  predicate is one of them. Prefer a positive equality when the application allows it.
- A range on a leading key followed by equality on a later key gives loose bounds. Reorder per ESR.
- With two ranges on different fields, only the first bounds the scan. The second only filters keys.

### 2.4 Fetch filter
- **Trigger.** Keys examined ≈ docs examined ≫ returned. The index narrows the candidates, but other predicates are
  evaluated after fetching the document.
- **Options.**
  - Add the filtered fields to the index (respecting ESR).
  - Use a partial index when a predicate is a fixed condition. It is used only when the query predicate implies
    the `partialFilterExpression`.

### 2.5 `$in`: behaviour depends on its size
- **Small list.** One point interval per value; it behaves as equality in ESR. Combined with a sort on the next
  key, the planner can explode the intervals into a `SORT_MERGE` and avoid a blocking sort. This happens only
  while the product of all `$in` list sizes in the predicate stays within the planner limit
  (`internalQueryMaxScansToExplode`).
- **Large list (above that limit, or several `$in` lists multiplied together).** The sort can no longer be merged,
  so treat the field as a range for sort planning: put the sort key before it, or drop the sort.
- **Very large list.** Planning and bound construction cost CPU on every execution, and the command size grows
  toward the 16 MB limit. Propose batching the values, a join (`$lookup`) when the values come from another query,
  or a range or denormalised flag that replaces the list.
- A redacted list appears as `"? (N values)"`; `≥N` means the list was truncated by the extractor. Use the
  extracted count when applying this rule.
- A one-value `$in` is treated as equality. Rewrite `$or` of equalities on the same field as `$in`.

### 2.6 Covered queries
- **Rule.** A query is covered when every filtered and projected field is in the index and `_id` is excluded or is
  itself indexed: `{<eqField>: 1, <projectedField>: 1}` plus a projection of only those fields.
- **When to propose it.** Only for frequent, latency-sensitive shapes that return few fields.
- **Limits.** A multikey index generally cannot cover a query on the array field. Covering increases index size,
  and with it cache use and write cost.

### 2.7 `$or`
- An `$or` is indexed only if every clause can use an index. One unindexed clause turns the whole query into a
  collection scan.
- Each clause is planned independently. For a shared equality outside the `$or`, `{<eqField>: 1, <eqField2>: 1}`
  per clause is the pattern.
- `$or` with a sort gives `SORT_MERGE` only when every clause's index provides the sort order. Otherwise it is a
  blocking sort.
- Many clauses multiply planning cost. Rewrite same-field clauses as `$in`.

### 2.8 Regex, case-insensitivity, text search
- A case-sensitive prefix regex (`^…`) gives tight bounds. Non-prefix or case-insensitive (`/…/i`) regexes scan
  the whole index or collection.
- For case-insensitive matching, propose a collation index (strength 1 or 2) queried with the same collation, or a
  normalised lower-case field.
- For contains or free-text search, propose a text index, or Atlas Search on Atlas.

### 2.9 Index hygiene
- Every index is maintained on each insert, and on each update of an indexed field. More indexes mean higher
  write latency, more cache use, and a larger working set.
- **Redundant prefix.** `{<eqField>: 1}` is redundant when `{<eqField>: 1, <eqField2>: 1}` exists, unless it is
  unique, partial or sparse, TTL, or collation-specific.
  - Propose removal only after usage statistics show no use on all nodes over a representative period.
  - Hide the index first (`hideIndex`) as a reversible test.
- The limit is 64 indexes per collection. One well-ordered compound index usually beats several single-field
  indexes; index intersection is rarely chosen.
- Schedule large index builds (rolling builds where available) and watch replication lag.
- A unique index build that fails on duplicate keys needs data cleanup. Never propose dropping the uniqueness
  constraint.

## 3. Collection scans
- A frequent shape that collection-scans with a high docs-examined / returned ratio is an index candidate. Build
  the candidate index from its equality, sort and range fields using ESR.
- **Expected scans, not findings:**
  - change streams and oplog reads;
  - `$natural` order;
  - full exports and analytics reads;
  - small collections;
  - filterless counts.
- A scan can happen even with an index when the predicate cannot use it: `$where`, a non-indexable `$expr`, a
  type mismatch, a case-insensitive regex, or a collation mismatch. State the cause instead of proposing a new
  index.

## 4. Aggregation pipelines
1. **Filter early.**
   - Only a leading `$match` or `$sort` (or `$geoNear`) can use an index.
   - Put `$match` before `$unwind`, `$lookup`, `$group` and computed stages.
   - A `$match` on computed fields, or placed after `$group` or `$unwind`, is not pushed down.
2. **Shrink early.** Use `$project` or `$unset` before memory-heavy stages only to drop large fields. The optimiser
   already tracks the fields it needs, so this is not a generic fix.
3. **Keep `$sort` + `$limit` adjacent** so they coalesce into a top-k sort.
4. **`$group`.**
   - It is blocking, with a 100 MB per-stage limit before spilling.
   - Group after filtering. Avoid `$push` of whole documents: memory, and the 16 MB result limit.
   - **DISTINCT_SCAN rewrite** ("first or last per key"):
     `$sort {<groupKey>: 1, <sortField>: -1}` → `$group {_id: "$<groupKey>", v: {$first: "$<field>"}}`,
     backed by the index `{<groupKey>: 1, <sortField>: -1}`, can run as an index skip-scan.
5. **`$lookup`.**
   - Index `<foreignField>` on `<foreignColl>` (for pipeline lookups: the `$expr` equality fields). Otherwise each
     input document scans the foreign collection.
   - Filter and limit the input before the lookup.
   - Lookups that always join the same data suggest embedding. Present this as a design question.
6. **`$unwind`** multiplies documents. Filter the array (`$filter`) or the documents first.
7. **`$facet`** sub-pipelines cannot use indexes. Put the indexed `$match` or `$sort` before `$facet`.
8. **Counts.** A filtered count can use `COUNT_SCAN` when covered. Use the metadata-based estimated count when an
   approximate total is enough.
9. **`allowDiskUse` / spills** are symptoms of memory-heavy stages. Fix the pipeline before relying on disk use,
   because spilling adds disk I/O.

## 5. Writes
- **Slow single-document updates.** Check write conflicts (hot document), write-concern wait, the number of indexes
  touched, document growth (array pushes), and cache pressure.
- **Unitary writes with high parallelism.** Propose unordered bulk writes when ordering is not required.
- **Upserts** need an index, ideally unique, on the query predicate.
- **Frequent retryable `findAndModify`** increases oplog churn (see resource rules).
- **TTL deletes** run in batches and can produce delete bursts. Correlate them with TTL activity before
  attributing them to the application.

## 6. Plan cache and stability
- Replanning with variable latency means the cached plan does not suit all parameter values (data skew).
- Fix options, in order:
  1. An index that is good for all values (ESR).
  2. Splitting the shape.
  3. A `hint`, only as a last resort, because it pins the plan even after the data changes.
- Query settings and plan-cache filters are operational tools, not primary fixes.

## 7. Schema patterns and anti-patterns (inferable without the schema)
Always present these as "pattern suggested by the signals", never as confirmed.

| Anti-pattern | Trigger signals | Direction |
|---|---|---|
| Unbounded arrays | repeated array-push updates, multikey index on `<arrayField>`, growing update latency and response size | bucket or subset pattern; cap arrays (`$push` with `$slice`) |
| Bloated documents | high bytes read per returned document; large responses despite narrow projections | subset pattern, projection, split hot and cold fields |
| Too many indexes | high keys inserted per write, slow inserts | remove unused or redundant indexes after usage review (§2.9) |
| Massive number of collections | many low-traffic namespaces | consolidate with a discriminator field |
| Case-insensitive search without collation | `/…/i` shapes with full scans | collation index (§2.8) |
| Separating data accessed together | frequent `$lookup` between the same collections | embedding or extended reference |
| Hot document / monotonic counter | repeated write conflicts on one shape | distribute writes, redesign the counter |

## 8. Client options visible in shapes
- **Secondary reads with majority or linearizable read concern on hot paths.** Check replication lag first.
  Without `maxStalenessSeconds`, secondary reads can be stale.
- **Expensive user-facing shapes without `maxTimeMS`.** Propose a server-side time bound. Never propose raising
  timeouts as a fix.
- **`batchSize`.** A very small value means many round-trips; a very large one means memory spikes (16 MB per
  batch). Tune it only from batch and return counts.
- **`singleBatch` with `limit`.** Expected for point reads; not a finding.
- **A `hint` present.** The plan is forced. Re-check it when the data distribution changes.

## 9. Sharded clusters
- Queries without the shard key, or its prefix, are scatter-gather. Add the shard key to hot queries before adding
  indexes.
- An index needed by routed queries must exist on every shard.
- Jumbo chunks and hot shards are not visible in mongod slow logs. Do not infer them without balancer or chunk
  data.

## 10. Required structure of a query finding
1. **Title.** Priority, issue type and the identifiers from the extraction (namespace, query hash).
2. **Signals.** The extracted values that triggered the rule.
3. **Rule.** The section of this file that applies.
4. **Candidate.** The index, rewrite or setting, written with the shape's own fields in rule order.
5. **Validation.** `explain("executionStats")` expectation, index-inventory check, and representative test.
6. **Risk or trade-off.** Write cost, index size, plan pinning.
7. **Confidence.** Lower it when there is no explain or index inventory.
