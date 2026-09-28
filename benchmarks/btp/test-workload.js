#!/usr/bin/env node
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(new URL('./k6/subaccount-create-delete.js', `file://${__filename}`), 'utf8')
  .replace("import * as xp from './crossplane-helpers.js';", '')
  .replace("import { Trend } from 'k6/metrics';", '')
  .replace('export const options', 'const options')
  .replace('export default function ()', 'function workload()');

function harness({ createFailure, readyFailure, readyTimeout = false, deleteFailure, subaccountAdmin = 'benchmark@example.invalid', secondDirectoryAdmin = 'directory-admin-two' } = {}) {
  const created = [];
  const deleted = [];
  const ready = [];
  const metrics = [];
  const phaseEvents = [];
  const client = {
    create(manifest) {
      if (createFailure === manifest.kind) throw new Error('simulated create failure');
      created.push(manifest);
    },
  };
  const xp = {
    k8sClient: () => client,
    measureOperation(_op, _kind, fn) { return fn(); },
    waitForReady(kind, name) {
      ready.push(kind);
      if (readyFailure === kind) {
        const error = new Error('simulated readiness failure');
        if (readyTimeout) error.name = 'TimeoutError';
        throw error;
      }
    },
    deleteAndWait(kind, name, opts = {}) {
      deleted.push({ kind, name });
      if (deleteFailure === kind) throw new Error('simulated delete failure');
      opts.onDeleteAccepted?.(1);
      opts.onKubernetesObjectAbsent?.(2);
    },
    xpResourcesCreated: { add() {} },
    xpResourcesFailed: { add() {} },
    recordMeasurementPhase(phase, event, resourceKind) {
      phaseEvents.push({ phase, event, resourceKind });
    },
  };
  class Trend {
    constructor(name) { this.name = name; }
    add(value, tags) { metrics.push({ name: this.name, value, tags }); }
  }
  const context = {
    xp, Trend, __ENV: {
      XP_DIADROMOS_BTP_RUN_ID: 'offline-test',
      XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN: subaccountAdmin,
      XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN: secondDirectoryAdmin,
      GITHUB_RUN_ATTEMPT: '1',
    }, Date, console,
  };
  vm.runInNewContext(`${source}\nthis.runWorkload = workload; this.runOptions = options;`, context);
  return { run: context.runWorkload, options: context.runOptions, created, deleted, ready, metrics, phaseEvents };
}

const kinds = ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential'];
const happy = harness();
assert.equal(happy.options.scenarios.default.executor, 'shared-iterations');
assert.equal(happy.options.scenarios.default.vus, 1);
assert.equal(happy.options.scenarios.default.iterations, 1);
assert.equal(happy.options.scenarios.default.maxDuration, '1h');
happy.run();
assert.deepEqual(happy.created.map((x) => x.kind), kinds);
assert.deepEqual(happy.ready, kinds);
assert.deepEqual(happy.deleted.map((x) => x.kind), [...kinds].reverse());
assert.equal(happy.metrics.filter((x) => x.name === 'xp_lifecycle_phase_duration').length, kinds.length * 4);
assert.ok(happy.phaseEvents.some((x) => x.phase === 'create_request' && x.event === 'requested' && x.resourceKind === 'DirectoryEntitlement'));
assert.ok(happy.phaseEvents.some((x) => x.phase === 'create_request' && x.event === 'accepted' && x.resourceKind === 'DirectoryEntitlement'));
assert.ok(happy.phaseEvents.some((x) => x.phase === 'readiness' && x.event === 'observed' && x.resourceKind === 'DirectoryEntitlement'));
assert.ok(happy.phaseEvents.some((x) => x.phase === 'delete_request' && x.event === 'accepted' && x.resourceKind === 'DirectoryEntitlement'));
assert.ok(happy.phaseEvents.some((x) => x.phase === 'kubernetes_absence_wait' && x.event === 'observed' && x.resourceKind === 'DirectoryEntitlement'));
assert.equal(new Set(happy.created.map((x) => x.metadata.name)).size, kinds.length);
assert.deepEqual(Array.from(happy.created[1].spec.forProvider.directoryAdmins), ['benchmark@example.invalid', 'directory-admin-two']);
assert.equal(happy.created[1].metadata.namespace, undefined, 'Directory is cluster-scoped');
assert.equal(happy.created[3].metadata.namespace, undefined, 'DirectoryEntitlement is cluster-scoped');
assert.equal(happy.created[4].spec.forProvider.readOnly, true);
assert.ok(happy.created[4].spec.writeConnectionSecretToRef.name.length <= 63);

const missingSecondAdmin = harness({ secondDirectoryAdmin: '' });
assert.throws(() => missingSecondAdmin.run(), /XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN is required/);
assert.equal(missingSecondAdmin.created.length, 0, 'validate both Directory admins before creating resources');

const duplicateAdmins = harness({ secondDirectoryAdmin: 'benchmark@example.invalid' });
assert.throws(() => duplicateAdmins.run(), /Directory admins must be distinct/);
assert.equal(duplicateAdmins.created.length, 0, 'reject duplicate Directory admins before creating resources');

const readyFail = harness({ readyFailure: 'Entitlement' });
assert.throws(() => readyFail.run(), /create\/readiness failed/);
assert.deepEqual(readyFail.deleted.map((x) => x.kind), ['Entitlement', 'Directory', 'Subaccount']);
assert.ok(readyFail.phaseEvents.some((x) => x.phase === 'readiness' && x.event === 'failed' && x.resourceKind === 'Entitlement'));

const readyTimeout = harness({ readyFailure: 'DirectoryEntitlement', readyTimeout: true });
assert.throws(() => readyTimeout.run(), /create\/readiness failed \(timeout\)/);
assert.ok(readyTimeout.metrics.some((x) => x.name === 'xp_lifecycle_phase_duration' && x.tags.phase === 'readiness' && x.tags.outcome === 'timeout' && x.tags.reason === 'timeout'));

const partialCreate = harness({ createFailure: 'Entitlement' });
assert.throws(() => partialCreate.run(), /create\/readiness failed/);
assert.deepEqual(partialCreate.deleted.map((x) => x.kind), ['Directory', 'Subaccount']);

const deleteFail = harness({ deleteFailure: 'DirectoryEntitlement' });
assert.throws(() => deleteFail.run(), /delete failed for owned DirectoryEntitlement/);
assert.ok(deleteFail.phaseEvents.some((x) => x.phase === 'delete_request' && x.event === 'failed' && x.resourceKind === 'DirectoryEntitlement'));
assert.deepEqual(deleteFail.deleted.map((x) => x.kind), [...kinds].reverse(), 'cleanup continues after a delete error');

console.log('Workload manifest and cleanup fixtures passed.');
