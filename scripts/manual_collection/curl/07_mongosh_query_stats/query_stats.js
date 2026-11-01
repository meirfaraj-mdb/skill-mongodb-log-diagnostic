// $queryStats on the connected node (top 1000 by total exec time). Read-only.
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

const rows = db.getSiblingDB('admin').aggregate([{ $queryStats: {} },
  { $sort: { 'metrics.totalExecMicros.sum': -1 } }, { $limit: 1000 }]).toArray();
emit('queryStats', 'query-stats.json',
  envelope('mongodb_queryStats', 'cumulative_since_entry_creation_or_reset', { data: plain(rows) }), rows.length + ' shapes');
