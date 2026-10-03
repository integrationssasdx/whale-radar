#!/usr/bin/env node
'use strict';

// whale-radar analyze
// Reads one JSON document from stdin, writes one JSON document to stdout.
// Nothing is persisted. Validation errors are reported as {"error":"CODE"}.

const fs = require('fs');

const WINDOW_MS = 3600 * 1000;

const TRANSFER_FIELDS = [
  'id', 'timestamp', 'chain', 'asset',
  'from_address', 'to_address', 'amount', 'usd_value',
];
const ROUTE_FIELDS = ['id', 'min_score', 'severity', 'chains', 'assets', 'target'];

const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}
function isNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

// Strict RFC3339 date-time in UTC: trailing Z or zero offset, real calendar date.
const TS_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]00:00)$/;
function parseTimestamp(s) {
  if (typeof s !== 'string') return null;
  const m = TS_RE.exec(s);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  const h = +m[4], mi = +m[5], se = +m[6];
  const ms = Date.UTC(y, mo - 1, d, h, mi, se);
  if (!Number.isFinite(ms)) return null;
  const dt = new Date(ms);
  if (
    dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 ||
    dt.getUTCDate() !== d || dt.getUTCHours() !== h ||
    dt.getUTCMinutes() !== mi || dt.getUTCSeconds() !== se
  ) {
    return null;
  }
  return ms;
}

function analyze(rawText) {
  let root;
  try {
    root = JSON.parse(rawText);
  } catch (_) {
    return { error: 'INPUT_NOT_JSON' };
  }

  // ---- structure / field types ----
  if (!isPlainObject(root)) return { error: 'INVALID_INPUT_SCHEMA' };
  if (!Array.isArray(root.transfers)) return { error: 'INVALID_INPUT_SCHEMA' };
  if (!Array.isArray(root.routes)) return { error: 'INVALID_INPUT_SCHEMA' };
  if (!isNumber(root.whale_threshold_usd)) return { error: 'INVALID_INPUT_SCHEMA' };

  for (const t of root.transfers) {
    if (!isPlainObject(t)) return { error: 'INVALID_INPUT_SCHEMA' };
    for (const f of TRANSFER_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(t, f)) {
        return { error: 'INVALID_INPUT_SCHEMA' };
      }
    }
    if (typeof t.id !== 'string' || typeof t.timestamp !== 'string' ||
        typeof t.chain !== 'string' || typeof t.asset !== 'string' ||
        typeof t.from_address !== 'string' || typeof t.to_address !== 'string') {
      return { error: 'INVALID_INPUT_SCHEMA' };
    }
    if (!isNumber(t.amount) || !isNumber(t.usd_value)) {
      return { error: 'INVALID_INPUT_SCHEMA' };
    }
  }

  for (const r of root.routes) {
    if (!isPlainObject(r)) return { error: 'INVALID_INPUT_SCHEMA' };
    for (const f of ROUTE_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(r, f)) {
        return { error: 'INVALID_INPUT_SCHEMA' };
      }
    }
    if (typeof r.id !== 'string' || typeof r.severity !== 'string' ||
        typeof r.target !== 'string') {
      return { error: 'INVALID_INPUT_SCHEMA' };
    }
    if (!isNumber(r.min_score)) return { error: 'INVALID_INPUT_SCHEMA' };
    if (!Array.isArray(r.chains) || !Array.isArray(r.assets)) {
      return { error: 'INVALID_INPUT_SCHEMA' };
    }
    for (const c of r.chains) {
      if (typeof c !== 'string') return { error: 'INVALID_INPUT_SCHEMA' };
    }
    for (const a of r.assets) {
      if (typeof a !== 'string') return { error: 'INVALID_INPUT_SCHEMA' };
    }
  }

  // ---- duplicate transfer ids ----
  const seenIds = new Set();
  for (const t of root.transfers) {
    if (seenIds.has(t.id)) return { error: 'DUPLICATE_TRANSFER_ID' };
    seenIds.add(t.id);
  }

  // ---- transfer values: text non-empty, time, numerics ----
  const times = new Array(root.transfers.length);
  for (let i = 0; i < root.transfers.length; i++) {
    const t = root.transfers[i];
    if (t.id === '' || t.chain === '' || t.asset === '' ||
        t.from_address === '' || t.to_address === '') {
      return { error: 'INVALID_TRANSFER_VALUE' };
    }
    const ms = parseTimestamp(t.timestamp);
    if (ms === null) return { error: 'INVALID_TRANSFER_VALUE' };
    times[i] = ms;
    if (!(t.amount > 0)) return { error: 'INVALID_TRANSFER_VALUE' };
    if (t.usd_value < 0) return { error: 'INVALID_TRANSFER_VALUE' };
  }

  // ---- threshold ----
  if (!(root.whale_threshold_usd > 0)) return { error: 'INVALID_THRESHOLD' };

  // ---- route domain rules ----
  for (const r of root.routes) {
    if (r.id === '' || r.target === '') return { error: 'INVALID_ROUTE' };
    if (r.min_score < 0 || r.min_score > 100) return { error: 'INVALID_ROUTE' };
    if (r.severity !== 'info' && r.severity !== 'warning' && r.severity !== 'critical') {
      return { error: 'INVALID_ROUTE' };
    }
    if (r.chains.length === 0 || r.assets.length === 0) {
      return { error: 'INVALID_ROUTE' };
    }
    for (const c of r.chains) {
      if (c === '') return { error: 'INVALID_ROUTE' };
    }
    for (const a of r.assets) {
      if (a === '') return { error: 'INVALID_ROUTE' };
    }
  }

  const transfers = root.transfers;
  const threshold = root.whale_threshold_usd;

  // ---- fund-flow graph ----
  const nodeMap = new Map();
  const getNode = (addr) => {
    let n = nodeMap.get(addr);
    if (!n) {
      n = {
        id: addr,
        sent_usd: 0,
        received_usd: 0,
        sent_count: 0,
        received_count: 0,
      };
      nodeMap.set(addr, n);
    }
    return n;
  };
  // from -> to -> aggregated edge
  const edgeMap = new Map();
  for (const t of transfers) {
    const from = getNode(t.from_address);
    const to = getNode(t.to_address);
    from.sent_usd += t.usd_value;
    from.sent_count += 1;
    to.received_usd += t.usd_value;
    to.received_count += 1;

    let out = edgeMap.get(t.from_address);
    if (!out) {
      out = new Map();
      edgeMap.set(t.from_address, out);
    }
    let e = out.get(t.to_address);
    if (!e) {
      e = { usd_total: 0, count: 0, transfer_ids: [] };
      out.set(t.to_address, e);
    }
    e.usd_total += t.usd_value;
    e.count += 1;
    e.transfer_ids.push(t.id);
  }

  const nodes = Array.from(nodeMap.keys()).sort(cmpStr).map((addr) => nodeMap.get(addr));

  const edges = [];
  for (const from of Array.from(edgeMap.keys()).sort(cmpStr)) {
    const out = edgeMap.get(from);
    for (const to of Array.from(out.keys()).sort(cmpStr)) {
      const e = out.get(to);
      e.transfer_ids.sort(cmpStr);
      edges.push({
        id: from + '->' + to,
        from_address: from,
        to_address: to,
        usd_total: e.usd_total,
        count: e.count,
        transfer_ids: e.transfer_ids,
      });
    }
  }

  // ---- whales: transfers reaching the threshold ----
  const whales = transfers
    .filter((t) => t.usd_value >= threshold)
    .map((t) => ({
      id: t.id,
      timestamp: t.timestamp,
      chain: t.chain,
      asset: t.asset,
      from_address: t.from_address,
      to_address: t.to_address,
      amount: t.amount,
      usd_value: t.usd_value,
    }))
    .sort((a, b) => cmpStr(a.id, b.id));

  // ---- anomaly scores ----
  const scores = transfers.map((t, i) => {
    const now = times[i];
    const earliest = now - WINDOW_MS;

    let score = 0;
    const reasons = [];

    const valuePart = Math.min(40, (t.usd_value / threshold) * 20);
    if (valuePart > 0) {
      score += valuePart;
      reasons.push('VALUE');
    }

    // same-sender look-back window, closed interval [now - 3600s, now]
    const sameSender = [];
    for (let j = 0; j < transfers.length; j++) {
      const o = transfers[j];
      if (o.from_address === t.from_address && times[j] >= earliest && times[j] <= now) {
        sameSender.push(o);
      }
    }
    if (sameSender.length >= 5) {
      score += 25;
      reasons.push('BURST');
    }
    const recipients = new Set(sameSender.map((o) => o.to_address));
    if (recipients.size >= 3) {
      score += 20;
      reasons.push('FAN_OUT');
    }

    // reverse transfer, same asset, equal amount inside the look-back window
    let roundTrip = false;
    for (let j = 0; j < transfers.length; j++) {
      const o = transfers[j];
      if (o.id === t.id) continue;
      if (times[j] < earliest || times[j] > now) continue;
      if (o.from_address === t.to_address && o.to_address === t.from_address &&
          o.asset === t.asset && o.amount === t.amount) {
        roundTrip = true;
        break;
      }
    }
    if (roundTrip) {
      score += 15;
      reasons.push('ROUND_TRIP');
    }

    return { id: t.id, score, reasons };
  });
  scores.sort((a, b) => (b.score - a.score) || cmpStr(a.id, b.id));

  // ---- alert routing ----
  const scoreById = new Map(scores.map((s) => [s.id, s]));
  const routes = root.routes.slice().sort((a, b) => cmpStr(a.id, b.id));
  const transfersById = transfers.slice().sort((a, b) => cmpStr(a.id, b.id));

  const alerts = [];
  for (const r of routes) {
    const chainMatch = (chain) => r.chains.includes('*') || r.chains.includes(chain);
    const assetMatch = (asset) => r.assets.includes('*') || r.assets.includes(asset);
    for (const t of transfersById) {
      const s = scoreById.get(t.id);
      if (s.score >= r.min_score && chainMatch(t.chain) && assetMatch(t.asset)) {
        alerts.push({
          route_id: r.id,
          transfer_id: t.id,
          severity: r.severity,
          reasons: s.reasons.slice(),
          target: r.target,
        });
      }
    }
  }

  return {
    data: {
      graph: { nodes, edges },
      whales,
      scores,
      alerts,
    },
  };
}

function main(argv) {
  if (argv.length !== 1 || argv[0] !== 'analyze') {
    process.stderr.write('usage: whale-radar analyze\n');
    process.exit(2);
  }
  const input = fs.readFileSync(0, 'utf8');
  const result = analyze(input);
  process.stdout.write(JSON.stringify(result) + '\n');
  if (Object.prototype.hasOwnProperty.call(result, 'error')) {
    process.exit(1);
  }
}

main(process.argv.slice(2));
