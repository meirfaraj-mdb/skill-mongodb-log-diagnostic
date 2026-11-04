// $collStats (storage + latency + query exec counters) for every non-system collection on the connected node.
// Skips admin/local/config, __mdb_internal*, system.* collections and views. Never reads documents.
// MDIAG_WT_DETAILS=true keeps the full WiredTiger blobs (default false: reusable/total bytes only).
// Optional settings (run.sh / run.cmd set them with --eval "var MDIAG_HOST='..', MDIAG_DIR='..'; load('<this file>')"):
//   MDIAG_HOST  host name stored in the file      default: the connected node (hello.me / connection host)
//   MDIAG_DIR   <out>/<date>/<host_dir> folder    default: none -> the JSON is printed to the shell, e.g.
//               mongosh "mongodb://host:27017/?directConnection=true" -u diag_reader --quiet --file THIS.js > out.json
// The JSON is also printed to the shell when files cannot be written (no require: legacy mongo shell).
const HOST = (typeof MDIAG_HOST !== 'undefined' && MDIAG_HOST) || (() => {
  try { const h = db.hello ? db.hello().me : null; return String(h || db.getMongo().host).split(',')[0].replace(/^mongodb(\+srv)?:\/\//, '').replace(/[/?].*$/, '').replace(/:\d+$/, ''); }
  catch (e) { return 'unknown'; } })();
const TO_FILE = typeof MDIAG_DIR !== 'undefined' && !!MDIAG_DIR && typeof require === 'function';
const plain = (x) => typeof EJSON !== 'undefined' ? JSON.parse(EJSON.stringify(x, undefined, 0, { relaxed: true })) : JSON.parse(JSON.stringify(x));
const envelope = (source, basis, extra) => Object.assign({ schema_version: 2, source: source, host: HOST, temporal_basis: basis,
  collector: 'curl-sample', retrievedAt: new Date().toISOString() }, extra);
function emit(sub, file, doc, note) {        // write <MDIAG_DIR>/<sub>/<file>, or print the JSON to the shell
  if (!TO_FILE) { print(JSON.stringify(doc)); return; }
  const fs = require('fs'), path = require('path'), f = path.join(MDIAG_DIR, sub, file);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, JSON.stringify(doc));
  print('  wrote ' + f + (note ? ' (' + note + ')' : ''));
}

const WT = typeof MDIAG_WT_DETAILS !== 'undefined' && !!MDIAG_WT_DETAILS;
const out = [], errors = [];
const pick = (b) => b ? { 'file bytes available for reuse': b['file bytes available for reuse'], 'file size in bytes': b['file size in bytes'] } : undefined;
const slim = (s) => {
  if (!s || WT) return s;
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
emit('collStats', 'coll-stats.json', envelope('mongodb_coll_stats', 'current_storage_snapshot_and_cumulative_latency_counters',
  { data: {}, collections: out, errors: errors, collections_scanned: out.length, wiredtiger_details: WT }),
  out.length + ' collections, ' + errors.length + ' errors');
