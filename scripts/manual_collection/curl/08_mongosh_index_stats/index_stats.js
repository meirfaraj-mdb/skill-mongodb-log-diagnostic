// $indexStats + getIndexes for every user collection on the connected node. Never reads documents.
// Printed to the shell (no MDIAG_DIR): two JSON lines, 1st = index-stats.json, 2nd = index-definitions.json.
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

const istats = [], defs = [], errors = [];
for (const d of db.adminCommand({ listDatabases: 1, nameOnly: true, authorizedDatabases: true }).databases) {
  if (['admin', 'local', 'config'].includes(d.name) || d.name.startsWith('__mdb_internal')) continue;
  const sdb = db.getSiblingDB(d.name);
  for (const c of sdb.getCollectionInfos({ type: 'collection' }, { nameOnly: true, authorizedCollections: true })) {
    if (c.name.startsWith('system.')) continue;
    const ns = d.name + '.' + c.name, coll = sdb.getCollection(c.name);
    try { defs.push({ namespace: ns, type: 'collection', indexes: plain(coll.getIndexes()) }); } catch (e) { errors.push({ namespace: ns, step: 'listIndexes', error: String(e) }); }
    try { istats.push({ namespace: ns, type: 'collection', indexes: plain(coll.aggregate([{ $indexStats: {} }]).toArray()) }); } catch (e) { errors.push({ namespace: ns, step: 'indexStats', error: String(e) }); }
  }
}
emit('indexStats', 'index-stats.json',
  envelope('mongodb_index_stats', 'since_index_counter_reset', { data: {}, collections: istats, errors: errors }), istats.length + ' collections');
emit('indexDefinitions', 'index-definitions.json',
  envelope('mongodb_index_definitions', 'current_snapshot', { data: {}, collections: defs, errors: errors }), defs.length + ' collections');
