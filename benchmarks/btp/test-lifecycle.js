#!/usr/bin/env node
const assert = require('node:assert/strict');
const { makeHarness, loadSources } = require('./tests/k6-harness');

const kinds = ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential'];
const benchmarkAdmin = ['benchmark', 'example.invalid'].join(String.fromCharCode(64));
const happy = makeHarness();
assert.equal(happy.options.scenarios.create_delete.vus, 1);
assert.equal(happy.options.scenarios.create_delete.iterations, 1);
assert.equal(happy.options.scenarios.create_delete.maxDuration, '110m');
assert.equal(Array.from(happy.options.thresholds.xp_lifecycle_success).join(','), 'rate==1');
happy.run();
assert.deepEqual(happy.created.map((resource) => resource.kind), kinds.flatMap((kind) => Array(5).fill(kind)));
assert.deepEqual(happy.deleted.map((resource) => resource.kind), [...kinds].reverse().flatMap((kind) => Array(5).fill(kind)));
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_time_to_ready').length, 25);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_time_to_delete').length, 25);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'create').length, 25);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'delete').length, 25);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_lifecycle_phase_duration').length, 25 * 4);
assert.deepEqual(happy.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [1]);
assert.ok(happy.metrics.some((metric) => metric.name === 'xp_measurement_phase' && metric.tags.stage === 'all_resources_ready'));
assert.ok(happy.created.every((resource) => resource.metadata.namespace === undefined), 'managed CRs are cluster-scoped');
const credential = happy.created.at(-1);
assert.equal(credential.spec.forProvider.readOnly, true);
assert.deepEqual(Array.from(happy.created[5].spec.forProvider.directoryAdmins), [benchmarkAdmin, 'directory-admin-two']);
assert.ok(credential.spec.writeConnectionSecretToRef.name.endsWith('-secret'));
assert.equal(credential.spec.writeConnectionSecretToRef.namespace, 'default');
const readyIndex = happy.events.findIndex((event) => event.type === 'all_ready');
const deleteIndex = happy.events.findIndex((event) => event.type === 'delete');
assert.ok(readyIndex >= 0 && readyIndex < deleteIndex);
assert.equal(happy.events.filter((event) => event.type === 'ready').length, 25);
assert.ok(happy.metrics.some((metric) => metric.name === 'xp_measurement_phase' && metric.tags.stage === 'readiness' && metric.tags.field === 'observed'));

const createRejected = makeHarness({ createFailure: 'Entitlement' });
assert.throws(() => createRejected.run(), /create\/readiness failed \(api_error\)/);
assert.equal(createRejected.deleted.length, 10, 'only the ten accepted parent creates are registered');
assert.deepEqual(createRejected.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const readyFailed = makeHarness({ readyFailure: 'Entitlement' });
assert.throws(() => readyFailed.run(), /create\/readiness failed \(timeout\)/);
assert.equal(readyFailed.deleted.length, 11, 'cleanup includes the readiness-failed accepted object');
assert.ok(readyFailed.metrics.some((metric) => metric.name === 'xp_lifecycle_phase_duration' && metric.tags.stage === 'readiness' && metric.tags.outcome === 'timeout'));

const createBudgetFailed = makeHarness({ readyFailure: 'Subaccount', env: { XP_DIADROMOS_BTP_CREATE_BUDGET: '2' } });
assert.throws(() => createBudgetFailed.run(), /create\/readiness failed \(timeout\)/);
assert.equal(createBudgetFailed.created.length, 1);
assert.deepEqual(createBudgetFailed.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const cleanupBudgetExpired = makeHarness({ holdDeleted: true, env: { XP_DIADROMOS_BTP_CLEANUP_BUDGET: '2' } });
assert.throws(() => cleanupBudgetExpired.run(), /delete failed for owned/);
assert.equal(cleanupBudgetExpired.deleted.length, 25, 'cleanup issues every delete despite an expired phase deadline');
assert.equal(cleanupBudgetExpired.metrics.filter((metric) => metric.name === 'xp_time_to_delete').length, 0, 'unobserved absence has no successful deletion trend');
assert.deepEqual(cleanupBudgetExpired.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const deletionFailed = makeHarness({ deleteFailure: ['DirectoryEntitlement', 'Directory'] });
assert.throws(() => deletionFailed.run(), /delete failed for owned DirectoryEntitlement \(api_error\)/);
assert.deepEqual(deletionFailed.deleted.map((resource) => resource.kind), [...kinds].reverse().flatMap((kind) => Array(5).fill(kind)), 'cleanup continues after multiple delete errors');
assert.equal(deletionFailed.metrics.filter((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'delete' && metric.tags.outcome === 'failure').length, 10);
assert.ok(!deletionFailed.logs.some((line) => line.includes('synthetic delete failure')), 'raw errors are not logged');

const invalidAdmins = makeHarness({ secondDirectoryAdmin: benchmarkAdmin.toUpperCase() });
assert.throws(() => invalidAdmins.run(), /create\/readiness failed \(api_error\)/);
assert.equal(invalidAdmins.created.length, 0);
assert.deepEqual(invalidAdmins.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const invalidBudget = makeHarness({ env: { XP_DIADROMOS_BTP_CREATE_BUDGET: 'Infinity' } });
assert.throws(() => invalidBudget.run(), /create\/readiness failed \(api_error\)/);
assert.equal(invalidBudget.created.length, 0, 'invalid budgets fail before any create');
assert.deepEqual(invalidBudget.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);
const invalidCleanupBudget = makeHarness({ env: { XP_DIADROMOS_BTP_CLEANUP_BUDGET: '0' } });
assert.throws(() => invalidCleanupBudget.run(), /create\/readiness failed \(api_error\)/);
assert.equal(invalidCleanupBudget.created.length, 0);
assert.deepEqual(invalidCleanupBudget.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const runId = '12345678901234567890';
const attempt = '1234567890';
const names = Array.from({ length: 5 }, (_, index) => happy.helper.buildResourceNames(runId, attempt, index + 1));
const repeatedNames = Array.from({ length: 5 }, (_, index) => happy.helper.buildResourceNames(runId, attempt, index + 1));
assert.equal(JSON.stringify(names), JSON.stringify(repeatedNames), 'identical run identity inputs reproduce exact names');
assert.notEqual(happy.helper.buildResourceNames(runId, '2', 1).Subaccount, names[0].Subaccount, 'attempt changes ownership names');
assert.notEqual(happy.helper.buildResourceNames('12345678901234567891', attempt, 1).Subaccount, names[0].Subaccount, 'run ID changes ownership names');
assert.throws(() => happy.helper.buildResourceNames('123456789012345678901', attempt, 1), /1-20 digit numeric identifier/);
assert.throws(() => happy.helper.buildResourceNames(runId, '10000000000', 1), /1-10 digit numeric identifier/);
assert.throws(() => happy.helper.buildResourceNames('run id', attempt, 1), /1-20 digit numeric identifier/);
for (const field of ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential', 'subdomain', 'connectionSecret']) {
  const values = names.map((entry) => entry[field]);
  assert.equal(new Set(values).size, 5, `${field} preserves distinct instance indices`);
  assert.ok(values.every((name) => name.length <= 63 && /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(name)));
}

const { helperOriginal, scenarioOriginal, scenarioSource } = loadSources();
const helperImports = [...helperOriginal.matchAll(/^import .* from ['"]([^'"]+)['"];?$/gm)].map((match) => match[1]);
const scenarioImports = [...scenarioOriginal.matchAll(/^import .* from ['"]([^'"]+)['"];?$/gm)].map((match) => match[1]);
assert.ok(helperImports.every((specifier) => specifier.startsWith('k6')), `unexpected helper runtime import: ${helperImports.join(', ')}`);
assert.deepEqual(scenarioImports, ['./crossplane-helpers.js'], 'scenario packages only with the configured helper');
assert.ok(scenarioSource.includes('xp.runOwnedResourceLifecycle'), 'scenario delegates lifecycle to the provider-local helper');

console.log('Owned-resource lifecycle fixtures passed.');
