/*
 * mongodb_collect_observability.js — manual, read-only observability snapshot for ONE node.
 *
 * Standalone mongosh script (no dependency on the agent code). Produces exactly the files the
 * diagnostic agent reads in `existing_bucket` mode:
 *
 *   <MDIAG_OUT_DIR>/<log-date>/<host_dir>/queryStats/query-stats.json        ($queryStats)
 *   <MDIAG_OUT_DIR>/<log-date>/<host_dir>/indexDefinitions/index-definitions.json
 *   <MDIAG_OUT_DIR>/<log-date>/<host_dir>/indexStats/index-stats.json        ($indexStats)
 *   <MDIAG_OUT_DIR>/<log-date>/<host_dir>/collStats/coll-stats.json          ($collStats)
 *   <MDIAG_OUT_DIR>/<log-date>/cluster/manifests/mongodb-<host_dir>.json     (run receipt)
 *
 * Usage (direct connection to each data-bearing node; or a mongos for $queryStats on sharded):
 *   MDIAG_OUT_DIR=./observability-manual MDIAG_TIMEZONE=Asia/Jerusalem \
 *   mongosh "mongodb://node1.example.net:27017/?directConnection=true&tls=true" \
 *           -u diag_reader --authenticationDatabase admin --quiet \
 *           --file mongodb_collect_observability.js
 *
 * Settings: environment variables (preferred) or `globalThis.CONFIG = {...}` set via --eval.
 *   MDIAG_OUT_DIR              output root (default ./observability-manual)
 *   MDIAG_LOG_DATE             YYYY-MM-DD day folder (default: yesterday in MDIAG_TIMEZONE)
 *   MDIAG_TIMEZONE             IANA zone for the default day (default UTC)
 *   MDIAG_HOST_DIR             node folder name (default: hostname from hello.me, [A-Za-z0-9._-])
 *   MDIAG_INCLUDE_PORT         true -> node folder is host_port (several mongod per host)
 *   MDIAG_INCLUDE_DBS          comma list; only these databases
 *   MDIAG_EXCLUDE_DBS          comma list; skipped databases (admin/config/local always skipped)
 *   MDIAG_SKIP                 comma list of: query_stats,index_definitions,index_stats,coll_stats
 *   MDIAG_MAX_COLLECTIONS      default 5000 (namespaces over the cap -> truncated=true)
 *   MDIAG_MAX_QUERY_STATS      default 1000 entries, sorted by total execution time
 *   MDIAG_MAX_TIME_MS          per-command maxTimeMS, default 30000
 *   MDIAG_QUERYSTATS_HMAC_KEY  base64 key (>=32 bytes) -> $queryStats transformIdentifiers
 *                              (field/collection names HMAC-hashed; needs queryStatsReadTransformed)
 *   MDIAG_WT_DETAILS           true -> keep full storageStats.wiredTiger / indexDetails blobs
 *   MDIAG_REDACT               false -> do not apply the agent-equivalent redaction (default true)
 *   MDIAG_READ_PREFERENCE      default secondaryPreferred (needed when connected to a secondary)
 *
 * Never reads documents: only listDatabases, listCollections, listIndexes, $indexStats, $collStats,
 * $queryStats, hello and serverStatus. No write, no profiler change, no setParameter.
 */
(function () {
  'use strict';
  const VERSION = '1.0.0';
  const COLLECTOR = { name: 'manual-mongosh-collector', version: VERSION };
  const fs = require('fs');
  const path = require('path');
  const ENV = (typeof process !== 'undefined' && process.env) ? process.env : {};
  const GCFG = (typeof CONFIG !== 'undefined' && CONFIG) ? CONFIG : {};
  const out = (msg) => (typeof print === 'function' ? print(msg) : console.log(msg));

  function opt(key, envName, dflt) {
    if (GCFG[key] !== undefined && GCFG[key] !== null) return GCFG[key];
    if (ENV[envName] !== undefined && ENV[envName] !== '') return ENV[envName];
    return dflt;
  }
  const asBool = (v) => v === true || /^(1|true|yes|on)$/i.test(String(v));
  const asList = (v) => Array.isArray(v) ? v : (v ? String(v).split(',').map((s) => s.trim()).filter(Boolean) : []);
  const asInt = (v, d) => { const n = parseInt(v, 10); return Number.isFinite(n) && n > 0 ? n : d; };

  const cfg = {
    outDir: opt('outDir', 'MDIAG_OUT_DIR', './observability-manual'),
    logDate: opt('logDate', 'MDIAG_LOG_DATE', ''),
    timezone: opt('timezone', 'MDIAG_TIMEZONE', 'UTC'),
    hostDir: opt('hostDir', 'MDIAG_HOST_DIR', ''),
    includePort: asBool(opt('includePort', 'MDIAG_INCLUDE_PORT', false)),
    includeDbs: asList(opt('includeDbs', 'MDIAG_INCLUDE_DBS', '')),
    excludeDbs: asList(opt('excludeDbs', 'MDIAG_EXCLUDE_DBS', '')),
    skip: asList(opt('skip', 'MDIAG_SKIP', '')),
    maxCollections: asInt(opt('maxCollections', 'MDIAG_MAX_COLLECTIONS', 5000), 5000),
    maxQueryStats: asInt(opt('maxQueryStats', 'MDIAG_MAX_QUERY_STATS', 1000), 1000),
    maxTimeMS: asInt(opt('maxTimeMS', 'MDIAG_MAX_TIME_MS', 30000), 30000),
    hmacKey: opt('hmacKey', 'MDIAG_QUERYSTATS_HMAC_KEY', ''),
    wtDetails: asBool(opt('wtDetails', 'MDIAG_WT_DETAILS', false)),
    redact: !/^(0|false|no|off)$/i.test(String(opt('redact', 'MDIAG_REDACT', true))),
    readPreference: opt('readPreference', 'MDIAG_READ_PREFERENCE', 'secondaryPreferred'),
  };
  const KINDS = ['query_stats', 'index_definitions', 'index_stats', 'coll_stats'];
  cfg.skip.forEach((k) => { if (!KINDS.includes(k)) throw new Error('MDIAG_SKIP: unknown kind ' + k); });
  const enabled = (kind) => !cfg.skip.includes(kind);
  // The agent's sanitizer caps arrays at 10000 entries; stay below so nothing is silently cut.
  if (cfg.redact && cfg.maxQueryStats > 10000) cfg.maxQueryStats = 10000;
  if (cfg.redact && cfg.maxCollections > 10000) cfg.maxCollections = 10000;

  // ------------------------------------------------------------------ dates
  function ymdInZone(d, tz) {
    // en-CA formats as YYYY-MM-DD.
    return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
  }
  function defaultLogDate(tz) {
    const [y, m, d] = ymdInZone(new Date(), tz).split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10);
  }
  const logDate = cfg.logDate || defaultLogDate(cfg.timezone);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(logDate) || isNaN(Date.parse(logDate + 'T00:00:00Z'))) {
    throw new Error('MDIAG_LOG_DATE must be YYYY-MM-DD, got ' + logDate);
  }

  // ------------------------------------------------------------------ redaction (same rules as the agent)
  const SECRET_RE = /password|credential|token|secret|api.?key/i;
  const SHAPE_KEYS = new Set(['filter', 'query', 'q', 'pipeline', 'parsedQuery', 'queryShape', 'sample_query_shape',
    'partialFilterExpression', 'indexBounds', 'command']);
  const DROP_KEYS = new Set(['remote', 'clientIp', 'ipAddress', 'uri', 'connectionString', 'comment']);
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  function shape(v, depth) {
    if (depth > 12) return '?';
    if (isObj(v)) {
      const o = {};
      Object.keys(v).slice(0, 100).forEach((k) => { if (!SECRET_RE.test(k)) o[k] = shape(v[k], depth + 1); });
      return o;
    }
    if (Array.isArray(v)) return v.slice(0, 30).map((x) => shape(x, depth + 1));
    return '?';
  }
  function sanitize(v, depth) {
    depth = depth || 0;
    if (depth > 16) return '<depth limit>';
    if (Array.isArray(v)) return v.slice(0, 10000).map((x) => sanitize(x, depth + 1));
    if (isObj(v)) {
      const o = {};
      for (const [k, item] of Object.entries(v)) {
        if (SECRET_RE.test(k)) continue;
        if (k === 'client') {
          if (isObj(item) && isObj(item.application) && typeof item.application.name === 'string') {
            o[k] = { application: { name: item.application.name.slice(0, 160) } };
          }
          continue;
        }
        if (DROP_KEYS.has(k)) continue;
        if (SHAPE_KEYS.has(k) && item !== null && typeof item === 'object') {
          o[k] = shape(item, 0);
          if (k === 'queryShape' && isObj(item)) {
            if (isObj(item.cmdNs)) {
              const c = {};
              ['db', 'coll'].forEach((x) => { if (x in item.cmdNs) c[x] = String(item.cmdNs[x]).slice(0, 160); });
              o[k].cmdNs = c;
            }
            if (typeof item.command === 'string') o[k].command = item.command.slice(0, 40);
          }
          continue;
        }
        o[k] = sanitize(item, depth + 1);
      }
      return o;
    }
    if (typeof v === 'string' && /(?:mongodb(?:\+srv)?|https?):\/\/[^\s]*@/.test(v)) return '<redacted credential-bearing URI>';
    return v;
  }

  // BSON -> plain JSON (relaxed Extended JSON: dates become {"$date": ISO}, small longs become numbers).
  function toPlain(doc) {
    if (typeof EJSON !== 'undefined') return JSON.parse(EJSON.stringify(doc, undefined, 0, { relaxed: true }));
    return JSON.parse(JSON.stringify(doc));
  }
  function errInfo(e) {
    return { error_type: (e && (e.codeName || e.name)) || 'Error', code: e && e.code,
      message: String((e && e.message) || e).slice(0, 300) };
  }

  // ------------------------------------------------------------------ node identity
  const admin = db.getSiblingDB('admin');
  try { db.getMongo().setReadPref(cfg.readPreference); } catch (e) { /* older shells */ }
  let hello = admin.runCommand({ hello: 1 });
  if (!hello || !hello.ok) hello = admin.runCommand({ isMaster: 1 });          // < 4.4.2
  if (hello && hello.ismaster !== undefined && hello.isWritablePrimary === undefined) hello.isWritablePrimary = hello.ismaster;
  let serverStatus = null;
  try {
    serverStatus = admin.runCommand({ serverStatus: 1, repl: 0, metrics: 0, locks: 0, wiredTiger: 0, tcmalloc: 0,
      transactions: 0, opLatencies: 0, network: 0, opcounters: 0, opcountersRepl: 0, asserts: 0, connections: 0 });
  } catch (e) { serverStatus = null; }
  const isMongos = hello.msg === 'isdbgrid';
  if (hello.arbiterOnly) throw new Error('Connected to an arbiter: it holds no data. Connect to a data-bearing node.');
  const hostPort = hello.me || (serverStatus && serverStatus.ok ? serverStatus.host : '') || '';
  const hostname = hostPort.replace(/:\d+$/, '');
  if (!cfg.hostDir && !hostPort) throw new Error('Cannot determine the node name; set MDIAG_HOST_DIR.');
  const hostDir = (cfg.hostDir || (cfg.includePort ? hostPort.replace(':', '_') : hostname)).replace(/[^A-Za-z0-9._-]/g, '_');
  const node = {
    host: hostPort || hostDir, host_dir: hostDir,
    role: isMongos ? 'mongos' : hello.isWritablePrimary ? 'primary' : hello.secondary ? 'secondary' : 'other',
    setName: hello.setName || null,
    version: (serverStatus && serverStatus.version) || (function () { try { return db.version(); } catch (e) { return null; } })(),
    uptimeSeconds: serverStatus && serverStatus.ok ? Number(serverStatus.uptime) : null,
    localTime: serverStatus && serverStatus.ok && serverStatus.localTime ? new Date(serverStatus.localTime).toISOString() : null,
  };

  // ------------------------------------------------------------------ output
  const written = [];
  function writeJson(rel, obj) {
    const full = path.join(cfg.outDir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    const tmp = full + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
    fs.renameSync(tmp, full);
    written.push({ path: rel, bytes: fs.statSync(full).size });
    return full;
  }
  const ARTIFACTS = {
    query_stats: ['queryStats', 'query-stats.json'],
    index_definitions: ['indexDefinitions', 'index-definitions.json'],
    index_stats: ['indexStats', 'index-stats.json'],
    coll_stats: ['collStats', 'coll-stats.json'],
  };
  const artifactPath = (kind) => [logDate, hostDir, ARTIFACTS[kind][0], ARTIFACTS[kind][1]].join('/');
  function envelope(source, basis, data, extra) {
    const e = Object.assign({ schema_version: 2, source: source, host: node.host, retrievedAt: new Date().toISOString(),
      temporal_basis: basis, data: data }, extra || {}, { node: node, collector: COLLECTOR });
    return cfg.redact ? sanitize(e) : e;
  }

  const receipt = { collector: COLLECTOR, log_date: logDate, node: node, started: new Date().toISOString(),
    settings: Object.assign({}, cfg, { hmacKey: cfg.hmacKey ? '<set>' : '' }), artifacts: {}, errors: [] };
  const t0 = Date.now();

  // ------------------------------------------------------------------ $queryStats
  if (enabled('query_stats')) {
    try {
      const stage = cfg.hmacKey
        ? { $queryStats: { transformIdentifiers: { algorithm: 'hmac-sha-256', hmacKey: BinData(8, cfg.hmacKey) } } }
        : { $queryStats: {} };
      const cursor = admin.aggregate([stage, { $sort: { 'metrics.totalExecMicros.sum': -1 } },
        { $limit: cfg.maxQueryStats + 1 }], { maxTimeMS: cfg.maxTimeMS });
      const rows = [];
      let truncated = false;
      while (cursor.hasNext()) {
        const r = cursor.next();
        if (rows.length >= cfg.maxQueryStats) { truncated = true; break; }
        rows.push(toPlain(r));
      }
      if (typeof cursor.close === 'function') cursor.close();
      writeJson(artifactPath('query_stats'), envelope('mongodb_queryStats', 'cumulative_since_entry_creation_or_reset', rows, {
        truncated: truncated, sort: 'metrics.totalExecMicros.sum desc', transformIdentifiers: Boolean(cfg.hmacKey),
        limitations: ['Not a rolling 24-hour series. Entries can reset or be evicted; use comparable snapshots for deltas.',
          isMongos ? 'Collected on mongos: covers queries routed through this mongos only.'
            : 'Collected on this mongod only; on sharded clusters routed queries are tracked on mongos.'],
      }));
      receipt.artifacts.query_stats = { status: 'collected', entries: rows.length, truncated: truncated };
    } catch (e) {
      receipt.artifacts.query_stats = Object.assign({ status: 'failed' }, errInfo(e));
      receipt.errors.push(Object.assign({ kind: 'query_stats' }, errInfo(e)));
    }
  }

  // ------------------------------------------------------------------ per-collection loop
  const wantColl = enabled('index_definitions') || enabled('index_stats') || enabled('coll_stats');
  const defs = [], istats = [], cstats = [], nsErrors = [], views = [];
  let truncated = false, scanned = 0;
  function slimStorage(s) {
    if (!s || cfg.wtDetails) return s;
    const reuse = (b) => b ? { 'file bytes available for reuse': b['file bytes available for reuse'],
      'file size in bytes': b['file size in bytes'] } : undefined;
    if (s.wiredTiger) { s.wiredTigerSummary = reuse(s.wiredTiger['block-manager']); delete s.wiredTiger; }
    if (s.indexDetails) {
      const sum = {};
      Object.entries(s.indexDetails).forEach(([n, d]) => { sum[n] = reuse(d && d['block-manager']); });
      s.indexDetailsSummary = sum;
      delete s.indexDetails;
    }
    return s;
  }
  if (wantColl) {
    try {
      const SYSTEM_DBS = new Set(['admin', 'config', 'local']);
      const res = admin.runCommand({ listDatabases: 1, nameOnly: true, authorizedDatabases: true });
      const dbs = (res.databases || []).map((d) => d.name)
        .filter((n) => !SYSTEM_DBS.has(n) && !n.startsWith('__mdb_internal'))
        .filter((n) => !cfg.includeDbs.length || cfg.includeDbs.includes(n))
        .filter((n) => !cfg.excludeDbs.includes(n))
        .sort();
      outer:
      for (const dbName of dbs) {
        const d = db.getSiblingDB(dbName);
        let infos;
        try {
          infos = d.getCollectionInfos({}, { nameOnly: false, authorizedCollections: true });
        } catch (e) {
          nsErrors.push(Object.assign({ database: dbName, step: 'listCollections' }, errInfo(e)));
          continue;
        }
        infos.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
        for (const info of infos) {
          if (info.name.startsWith('system.')) continue;
          if (info.type === 'view') { views.push(dbName + '.' + info.name); continue; }
          if (scanned >= cfg.maxCollections) { truncated = true; break outer; }
          scanned += 1;
          const ns = dbName + '.' + info.name;
          const coll = d.getCollection(info.name);
          const ctype = info.type || 'collection';
          if (enabled('index_definitions')) {
            try {
              defs.push({ namespace: ns, type: ctype, indexes: coll.getIndexes().map(toPlain), truncated: false });
            } catch (e) { nsErrors.push(Object.assign({ namespace: ns, step: 'listIndexes' }, errInfo(e))); }
          }
          if (enabled('index_stats')) {
            try {
              const rows = coll.aggregate([{ $indexStats: {} }], { maxTimeMS: cfg.maxTimeMS }).toArray().map(toPlain);
              istats.push({ namespace: ns, type: ctype, indexes: rows, truncated: false });
            } catch (e) { nsErrors.push(Object.assign({ namespace: ns, step: 'indexStats' }, errInfo(e))); }
          }
          if (enabled('coll_stats')) {
            const spec = { storageStats: { scale: 1 }, latencyStats: { histograms: false }, count: {}, queryExecStats: {} };
            let rows = null;
            try {
              rows = coll.aggregate([{ $collStats: spec }], { maxTimeMS: cfg.maxTimeMS }).toArray();
            } catch (e) {
              try { // queryExecStats is not accepted everywhere (e.g. some time series / older servers)
                delete spec.queryExecStats;
                rows = coll.aggregate([{ $collStats: spec }], { maxTimeMS: cfg.maxTimeMS }).toArray();
              } catch (e2) { nsErrors.push(Object.assign({ namespace: ns, step: 'collStats' }, errInfo(e2))); }
            }
            if (rows) {
              rows = rows.map((r) => { const p = toPlain(r); p.storageStats = slimStorage(p.storageStats); return p; });
              cstats.push({ namespace: ns, type: ctype, stats: rows, truncated: false });
            }
          }
          if (scanned % 200 === 0) out('[mdiag] ' + scanned + ' collections scanned...');
        }
      }
    } catch (e) {
      receipt.errors.push(Object.assign({ kind: 'collections', step: 'listDatabases' }, errInfo(e)));
    }
    const common = { errors: nsErrors, truncated: truncated, collections_scanned: scanned, views_skipped: views.length };
    if (enabled('index_definitions')) {
      writeJson(artifactPath('index_definitions'), envelope('mongodb_index_definitions', 'current_snapshot', {},
        Object.assign({ collections: defs }, common)));
      receipt.artifacts.index_definitions = { status: 'collected', namespaces: defs.length, truncated: truncated };
    }
    if (enabled('index_stats')) {
      writeJson(artifactPath('index_stats'), envelope('mongodb_index_stats', 'since_index_counter_reset', {},
        Object.assign({ collections: istats }, common, { limitations: [
          'accesses.ops is cumulative since accesses.since (node restart, index (re)build or 7.0+ counter reset); per node.'] })));
      receipt.artifacts.index_stats = { status: 'collected', namespaces: istats.length, truncated: truncated };
    }
    if (enabled('coll_stats')) {
      writeJson(artifactPath('coll_stats'), envelope('mongodb_coll_stats',
        'current_storage_snapshot_and_cumulative_latency_counters', {},
        Object.assign({ collections: cstats }, common, { wiredtiger_details: cfg.wtDetails })));
      receipt.artifacts.coll_stats = { status: 'collected', namespaces: cstats.length, truncated: truncated };
    }
    if (nsErrors.length) receipt.errors.push({ kind: 'namespaces', count: nsErrors.length });
  }

  receipt.finished = new Date().toISOString();
  receipt.elapsed_seconds = Math.round((Date.now() - t0) / 100) / 10;
  receipt.files = written.slice();
  receipt.status = receipt.errors.length ? 'partial' : 'completed';
  writeJson([logDate, 'cluster', 'manifests', 'mongodb-' + hostDir + '.json'].join('/'), cfg.redact ? sanitize(receipt) : receipt);
  out(JSON.stringify({ status: receipt.status, node: hostDir, log_date: logDate, out_dir: cfg.outDir,
    artifacts: receipt.artifacts, namespace_errors: nsErrors.length, elapsed_seconds: receipt.elapsed_seconds }, null, 2));
})();
