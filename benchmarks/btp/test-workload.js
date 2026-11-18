#!/usr/bin/env node
const assert = require('node:assert/strict');
const { makeHarness } = require('./tests/k6-harness');

const kinds = ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential'];
const perKind = 5;
const benchmarkAdmin = ['benchmark', 'example.invalid'].join(String.fromCharCode(64));
const happy = makeHarness();
assert.equal(happy.options.scenarios.create_delete.executor, 'shared-iterations');
assert.equal(happy.options.scenarios.create_delete.vus, 1);
assert.equal(happy.options.scenarios.create_delete.iterations, 1);
happy.run();
assert.deepEqual(happy.created.map((resource) => resource.kind), kinds.flatMap((kind) => Array(perKind).fill(kind)));
assert.deepEqual(happy.deleted.map((resource) => resource.kind), [...kinds].reverse().flatMap((kind) => Array(perKind).fill(kind)));
for (const kind of kinds) assert.equal(happy.created.filter((resource) => resource.kind === kind).length, perKind);
const namesByKind = Object.fromEntries(kinds.map((kind) => [kind, happy.created.filter((resource) => resource.kind === kind)]));
for (const kind of kinds) {
  assert.equal(new Set(namesByKind[kind].map((resource) => resource.metadata.name)).size, perKind, `${kind} names are unique`);
  assert.ok(namesByKind[kind].every((resource) => resource.metadata.namespace === undefined), 'managed resources are cluster-scoped');
}
const subaccounts = namesByKind.Subaccount.map((resource) => resource.metadata.name);
const directories = namesByKind.Directory.map((resource) => resource.metadata.name);
assert.deepEqual(namesByKind.Entitlement.map((resource) => resource.spec.forProvider.subaccountRef.name), subaccounts);
assert.deepEqual(namesByKind.DirectoryEntitlement.map((resource) => resource.spec.forProvider.directoryRef.name), directories);
assert.deepEqual(namesByKind.SubaccountApiCredential.map((resource) => resource.spec.forProvider.subaccountRef.name), subaccounts);
assert.deepEqual(namesByKind.Directory.flatMap((resource) => Array.from(resource.spec.forProvider.directoryAdmins)), Array(5).fill([benchmarkAdmin, 'directory-admin-two']).flat());
assert.ok(namesByKind.SubaccountApiCredential.every((resource) => resource.spec.forProvider.readOnly === true));
const credentialSecretNames = namesByKind.SubaccountApiCredential.map((resource) => resource.spec.writeConnectionSecretToRef.name);
assert.equal(new Set(credentialSecretNames).size, perKind);
assert.ok(namesByKind.SubaccountApiCredential.every((resource) => resource.spec.writeConnectionSecretToRef.namespace === 'default'));
assert.ok([...happy.created.map((resource) => resource.metadata.name), ...credentialSecretNames,
  ...namesByKind.Subaccount.map((resource) => resource.spec.forProvider.subdomain)].every((name) =>
  name.length <= 63 && /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(name)));
const readyBoundary = happy.events.findIndex((event) => event.type === 'all_ready');
const firstDelete = happy.events.findIndex((event) => event.type === 'delete');
assert.equal(happy.created.length, 25);
assert.equal(happy.events.filter((event) => event.type === 'ready').length, 25);
assert.ok(readyBoundary >= 0 && readyBoundary < firstDelete, 'all-ready marker precedes first deletion');
assert.equal(happy.events.filter((event) => event.type === 'ready').at(-1).name,
  happy.events.slice(0, readyBoundary).filter((event) => event.type === 'ready').at(-1).name,
  'last Ready observation precedes the all-ready marker');
for (const metric of happy.metrics.filter((entry) => ['xp_time_to_ready', 'xp_time_to_delete'].includes(entry.name))) {
  assert.deepEqual(Object.keys(metric.tags), ['resource_kind'], 'lifecycle trends carry no instance identity');
}

for (const failure of [
  { kind: 'Subaccount', instance: 1 },
  { kind: 'Entitlement', instance: 3 },
  { kind: 'SubaccountApiCredential', instance: 5 },
]) {
  const failedCreate = makeHarness({ createFailure: failure });
  assert.throws(() => failedCreate.run(), /create\/readiness failed/);
  assert.ok(failedCreate.deleted.length > 0 || failure.instance === 1, 'accepted earlier objects enter cleanup');
  assert.ok(failedCreate.deleted.every((resource) => failedCreate.created.some((created) => created.kind === resource.kind && created.metadata.name === resource.name)), 'only accepted creates are cleaned up');
  assert.equal(failedCreate.metrics.filter((metric) => metric.name === 'xp_lifecycle_success')[0].value, 0);
}
const failedReadiness = makeHarness({ readyFailure: 'DirectoryEntitlement' });
assert.throws(() => failedReadiness.run(), /create\/readiness failed \(timeout\)/);
assert.equal(failedReadiness.deleted.length, 16, 'cleanup covers every accepted create through the failure point');
const failedDelete = makeHarness({ deleteFailure: 'DirectoryEntitlement' });
assert.throws(() => failedDelete.run(), /delete failed for owned DirectoryEntitlement/);
assert.equal(failedDelete.deleted.length, 25, 'cleanup continues after a delete error');

const missingAdmin = makeHarness({ secondDirectoryAdmin: '' });
assert.throws(() => missingAdmin.run(), /create\/readiness failed \(api_error\)/);
assert.equal(missingAdmin.created.length, 0, 'settings fail before creating resources');
const duplicateAdmins = makeHarness({ secondDirectoryAdmin: benchmarkAdmin.toUpperCase() });
assert.throws(() => duplicateAdmins.run(), /create\/readiness failed \(api_error\)/);
assert.equal(duplicateAdmins.created.length, 0);

console.log('Five-instance workload declarations and lifecycle integration fixtures passed.');
