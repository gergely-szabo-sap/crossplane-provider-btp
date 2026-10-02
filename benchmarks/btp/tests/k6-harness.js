#!/usr/bin/env node
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.resolve(__dirname, '..');

function stripModuleSyntax(source) {
  return source
    .replace(/^import .*;\s*$/gm, '')
    .replace(/\bexport\s+(?=(?:const|class|function))/g, '')
    .replace(/\bexport\s+default\s+function/g, 'function');
}

function loadSources() {
  const helperOriginal = fs.readFileSync(path.join(root, 'k6/crossplane-helpers.js'), 'utf8');
  const scenarioOriginal = fs.readFileSync(path.join(root, 'k6/subaccount-create-delete.js'), 'utf8');
  const helperSource = stripModuleSyntax(helperOriginal);
  const scenarioSource = scenarioOriginal
    .replace("import * as xp from './crossplane-helpers.js';", 'const xp = this.__xp;')
    .replace('export const options', 'const options')
    .replace('export default function ()', 'function workload()');
  return { helperOriginal, scenarioOriginal, helperSource, scenarioSource };
}

function makeHarness({
  env = {}, createFailure, readyFailure, deleteFailure, holdDeleted = false,
  subaccountAdmin = 'benchmark@example.invalid',
  secondDirectoryAdmin = 'directory-admin-two',
  now = 1_700_000_000_000,
} = {}) {
  let clock = now;
  class TestDate extends Date {
    static now() { return clock; }
  }
  const metrics = [];
  const created = [];
  const deleted = [];
  const events = [];
  const objects = new Map();
  const logs = [];
  const add = (name, value, tags) => {
    metrics.push({ name, value, tags: tags || {} });
    if (name === 'xp_measurement_phase' && tags?.stage === 'all_resources_ready') events.push({ type: 'all_ready' });
  };
  class Metric {
    constructor(name) { this.name = name; }
    add(value, tags) { add(this.name, value, tags); }
  }
  const client = {
    create(manifest) {
      if (createFailure === manifest.kind ||
          (createFailure && typeof createFailure === 'object' && createFailure.kind === manifest.kind &&
           manifest.metadata.name.endsWith(`-${createFailure.instance}`))) throw new Error('synthetic API failure');
      created.push(manifest);
      events.push({ type: 'create', kind: manifest.kind, name: manifest.metadata.name });
      objects.set(`${manifest.kind}/${manifest.metadata.name}`, manifest);
    },
    get(groupKind, name) {
      const kind = groupKind.split('.')[0];
      if (readyFailure === kind) throw new Error('synthetic transient GET failure');
      const object = objects.get(`${kind}/${name}`);
      if (!object) throw new Error('404 Not Found');
      events.push({ type: 'ready', kind, name });
      return { status: { conditions: [{ type: 'Ready', status: 'True' }] } };
    },
    delete(groupKind, name) {
      const kind = groupKind.split('.')[0];
      deleted.push({ kind, name });
      events.push({ type: 'delete', kind, name });
      if (deleteFailure === kind || (Array.isArray(deleteFailure) && deleteFailure.includes(kind))) throw new Error('synthetic delete failure');
      if (!holdDeleted) objects.delete(`${kind}/${name}`);
    },
  };
  class Kubernetes { constructor() { return client; } }
  const context = {
    __ENV: {
      XP_DIADROMOS_BTP_RUN_ID: 'offline-test',
      XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN: subaccountAdmin,
      XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN: secondDirectoryAdmin,
      GITHUB_RUN_ATTEMPT: '1',
      ...env,
    },
    Date: TestDate,
    console: { log: (...args) => logs.push(args.join(' ')) },
    sleep: (seconds) => { clock += seconds * 1000; },
    k8s: { Kubernetes },
    Counter: Metric,
    Gauge: Metric,
    Rate: Metric,
    Trend: Metric,
  };
  const { helperSource, scenarioSource } = loadSources();
  const helperNames = [
    'TimeoutError', 'CrossplaneError', 'measureOperation', 'recordMeasurementPhase',
    'k8sClient', 'waitForReady', 'deleteAndWait', 'buildResourceNames',
    'managedResource', 'runOwnedResourceLifecycle', 'xpResourcesCreated',
    'xpResourcesDeleted', 'xpResourcesFailed',
  ];
  vm.runInNewContext(`${helperSource}\nthis.__helper = { ${helperNames.join(', ')} };`, context);
  const xp = context.__helper;
  xp.k8sClient = () => client;
  context.__xp = xp;
  vm.runInNewContext(`${scenarioSource}\nthis.__workload = workload; this.__options = options;`, context);
  return {
    run: context.__workload,
    options: context.__options,
    helper: xp,
    client,
    created,
    deleted,
    events,
    objects,
    metrics,
    logs,
    get now() { return clock; },
  };
}

module.exports = { makeHarness, loadSources };
