// $indexStats + getIndexes for every user collection on the connected node. Never reads documents.
const istats = [], defs = [], errors = [];
const plain = (x) => JSON.parse(EJSON.stringify(x, undefined, 0, { relaxed: true }));
for (const d of db.adminCommand({ listDatabases: 1, nameOnly: true }).databases) {
  if (['admin', 'local', 'config'].includes(d.name)) continue;
  const sdb = db.getSiblingDB(d.name);
  for (const c of sdb.getCollectionInfos({ type: 'collection' }, { nameOnly: true })) {
    if (c.name.startsWith('system.')) continue;
    const ns = d.name + '.' + c.name, coll = sdb.getCollection(c.name);
    try { defs.push({ namespace: ns, type: 'collection', indexes: plain(coll.getIndexes()) }); } catch (e) { errors.push({ namespace: ns, step: 'listIndexes', error: String(e) }); }
    try { istats.push({ namespace: ns, type: 'collection', indexes: plain(coll.aggregate([{ $indexStats: {} }]).toArray()) }); } catch (e) { errors.push({ namespace: ns, step: 'indexStats', error: String(e) }); }
  }
}
const fs = require('fs'), path = require('path'), base = process.env.DIR;
const write = (sub, file, source, basis, cols) => {
  fs.mkdirSync(path.join(base, sub), { recursive: true });
  fs.writeFileSync(path.join(base, sub, file), JSON.stringify({ schema_version: 2, source: source, host: process.env.H,
    temporal_basis: basis, collector: 'curl-sample', retrievedAt: new Date().toISOString(), data: {}, collections: cols, errors: errors }));
  print('  wrote ' + path.join(base, sub, file) + ' (' + cols.length + ' collections)');
};
write('indexStats', 'index-stats.json', 'mongodb_index_stats', 'since_index_counter_reset', istats);
write('indexDefinitions', 'index-definitions.json', 'mongodb_index_definitions', 'current_snapshot', defs);
