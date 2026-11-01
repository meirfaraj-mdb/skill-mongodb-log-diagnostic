// $collStats (storage + latency + query exec counters) for every non-system collection on the connected node.
// Skips admin/local/config, __mdb_internal*, system.* collections and views. Never reads documents.
const out = [], errors = [];
const plain = (x) => JSON.parse(EJSON.stringify(x, undefined, 0, { relaxed: true }));
const pick = (b) => b ? { 'file bytes available for reuse': b['file bytes available for reuse'], 'file size in bytes': b['file size in bytes'] } : undefined;
const slim = (s) => {           // drop the large WiredTiger blobs, keep reusable/total bytes (set MDIAG_WT_DETAILS=1 to keep all)
  if (!s || process.env.MDIAG_WT_DETAILS) return s;
  if (s.wiredTiger) { s.wiredTigerSummary = pick(s.wiredTiger['block-manager']); delete s.wiredTiger; }
  if (s.indexDetails) { const t = {}; for (const [n, d] of Object.entries(s.indexDetails)) t[n] = pick(d && d['block-manager']); s.indexDetailsSummary = t; delete s.indexDetails; }
  return s;
};
const dbs = db.adminCommand({ listDatabases: 1, nameOnly: true, authorizedDatabases: true }).databases.map((d) => d.name)
  .filter((n) => !['admin', 'local', 'config'].includes(n) && !n.startsWith('__mdb_internal')).sort();
for (const name of dbs) {
  const sdb = db.getSiblingDB(name);
  let infos = [];
  try { infos = sdb.getCollectionInfos({ type: { $in: ['collection', 'timeseries'] } }, { nameOnly: true, authorizedCollections: true }); }
  catch (e) { errors.push({ database: name, step: 'listCollections', error: String(e) }); continue; }
  for (const c of infos) {
    if (c.name.startsWith('system.')) continue;
    const ns = name + '.' + c.name, coll = sdb.getCollection(c.name);
    const spec = { storageStats: { scale: 1 }, latencyStats: { histograms: false }, count: {}, queryExecStats: {} };
    let rows;
    try { rows = coll.aggregate([{ $collStats: spec }]).toArray(); }
    catch (e) {                 // queryExecStats is not accepted everywhere (time series, older servers)
      delete spec.queryExecStats;
      try { rows = coll.aggregate([{ $collStats: spec }]).toArray(); }
      catch (e2) { errors.push({ namespace: ns, step: 'collStats', error: String(e2) }); continue; }
    }
    out.push({ namespace: ns, type: c.type || 'collection', stats: rows.map((r) => { const p = plain(r); p.storageStats = slim(p.storageStats); return p; }) });
  }
}
const fs = require('fs'), path = require('path'), f = path.join(process.env.DIR, 'collStats', 'coll-stats.json');
fs.mkdirSync(path.dirname(f), { recursive: true });
fs.writeFileSync(f, JSON.stringify({ schema_version: 2, source: 'mongodb_coll_stats', host: process.env.H,
  temporal_basis: 'current_storage_snapshot_and_cumulative_latency_counters', collector: 'curl-sample',
  retrievedAt: new Date().toISOString(), data: {}, collections: out, errors: errors,
  collections_scanned: out.length, wiredtiger_details: !!process.env.MDIAG_WT_DETAILS }));
print('  wrote ' + f + ' (' + out.length + ' collections, ' + errors.length + ' errors)');
