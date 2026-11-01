// $queryStats on the connected node (top 1000 by total exec time) -> process.env.OUT. Read-only.
const rows = db.getSiblingDB('admin').aggregate([{ $queryStats: {} },
  { $sort: { 'metrics.totalExecMicros.sum': -1 } }, { $limit: 1000 }]).toArray();
const fs = require('fs'); fs.mkdirSync(require('path').dirname(process.env.OUT), { recursive: true });
fs.writeFileSync(process.env.OUT, JSON.stringify({ schema_version: 2, source: 'mongodb_queryStats', host: process.env.H,
  temporal_basis: 'cumulative_since_entry_creation_or_reset', collector: 'curl-sample', retrievedAt: new Date().toISOString(),
  data: JSON.parse(EJSON.stringify(rows, undefined, 0, { relaxed: true })) }));
print('  wrote ' + process.env.OUT + ' (' + rows.length + ' shapes)');
