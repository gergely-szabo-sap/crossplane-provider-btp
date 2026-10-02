#!/usr/bin/env node
const assert = require('node:assert/strict');
const { makeHarness } = require('./tests/k6-harness');

const kinds = ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential'];
const happy = makeHarness();
assert.equal(happy.options.scenarios.create_delete.executor, 'shared-iterations');
assert.equal(happy.options.scenarios.create_delete.vus, 1);
assert.equal(happy.options.scenarios.create_delete.iterations, 1);
happy.run();
assert.deepEqual(happy.created.map((resource) => resource.kind), kinds);
assert.deepEqual(happy.deleted.map((resource) => resource.kind), [...kinds].reverse());
assert.deepEqual(happy.created.map((resource) => resource.spec.forProvider.subaccountRef?.name).filter(Boolean), [happy.created[0].metadata.name, happy.created[0].metadata.name]);
assert.equal(happy.created[3].spec.forProvider.directoryRef.name, happy.created[1].metadata.name);
assert.deepEqual(Array.from(happy.created[1].spec.forProvider.directoryAdmins), ['benchmark@example.invalid', 'directory-admin-two']);
assert.ok(happy.created.every((resource) => resource.metadata.namespace === undefined), 'all managed resources are cluster-scoped');
assert.equal(happy.created[4].spec.forProvider.readOnly, true);
assert.equal(happy.created[4].spec.writeConnectionSecretToRef.namespace, 'default');
assert.ok(happy.created[4].spec.writeConnectionSecretToRef.name.length <= 63);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_lifecycle_phase_duration').length, kinds.length * 4);

const missingAdmin = makeHarness({ secondDirectoryAdmin: '' });
assert.throws(() => missingAdmin.run(), /create\/readiness failed \(api_error\)/);
assert.equal(missingAdmin.created.length, 0, 'settings fail before creating resources');

const duplicateAdmins = makeHarness({ secondDirectoryAdmin: 'BENCHMARK@example.invalid' });
assert.throws(() => duplicateAdmins.run(), /create\/readiness failed \(api_error\)/);
assert.equal(duplicateAdmins.created.length, 0);

const failedCreate = makeHarness({ createFailure: 'Entitlement' });
assert.throws(() => failedCreate.run(), /create\/readiness failed/);
assert.deepEqual(failedCreate.deleted.map((resource) => resource.kind), ['Directory', 'Subaccount']);

const failedReadiness = makeHarness({ readyFailure: 'DirectoryEntitlement' });
assert.throws(() => failedReadiness.run(), /create\/readiness failed \(timeout\)/);
assert.deepEqual(failedReadiness.deleted.map((resource) => resource.kind), ['DirectoryEntitlement', 'Entitlement', 'Directory', 'Subaccount']);

const failedDelete = makeHarness({ deleteFailure: 'DirectoryEntitlement' });
assert.throws(() => failedDelete.run(), /delete failed for owned DirectoryEntitlement/);
assert.deepEqual(failedDelete.deleted.map((resource) => resource.kind), [...kinds].reverse(), 'cleanup continues after an error');

console.log('Workload declarations and lifecycle integration fixtures passed.');
