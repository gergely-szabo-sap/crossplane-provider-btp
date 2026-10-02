#!/usr/bin/env node
const assert = require('node:assert/strict');
const { makeHarness, loadSources } = require('./tests/k6-harness');

const kinds = ['Subaccount', 'Directory', 'Entitlement', 'DirectoryEntitlement', 'SubaccountApiCredential'];
const happy = makeHarness();
assert.equal(happy.options.scenarios.create_delete.vus, 1);
assert.equal(happy.options.scenarios.create_delete.iterations, 1);
assert.equal(happy.options.scenarios.create_delete.maxDuration, '110m');
assert.equal(Array.from(happy.options.thresholds.xp_lifecycle_success).join(','), 'rate==1');
happy.run();
assert.deepEqual(happy.created.map((resource) => resource.kind), kinds);
assert.deepEqual(happy.deleted.map((resource) => resource.kind), [...kinds].reverse());
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_time_to_ready').length, kinds.length);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_time_to_delete').length, kinds.length);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'create').length, kinds.length);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'delete').length, kinds.length);
assert.equal(happy.metrics.filter((metric) => metric.name === 'xp_lifecycle_phase_duration').length, kinds.length * 4);
assert.deepEqual(happy.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [1]);
assert.ok(happy.metrics.some((metric) => metric.name === 'xp_measurement_phase' && metric.tags.stage === 'all_resources_ready'));
assert.ok(happy.created.every((resource) => resource.metadata.namespace === undefined), 'managed CRs are cluster-scoped');
assert.equal(happy.created[4].spec.forProvider.readOnly, true);
assert.deepEqual(Array.from(happy.created[1].spec.forProvider.directoryAdmins), ['benchmark@example.invalid', 'directory-admin-two']);
assert.equal(happy.created[4].spec.writeConnectionSecretToRef.name, `${happy.created[4].metadata.name.slice(0, 55)}-secret`);
assert.equal(happy.created[4].spec.writeConnectionSecretToRef.namespace, 'default');
assert.ok(happy.metrics.some((metric) => metric.name === 'xp_measurement_phase' && metric.tags.stage === 'readiness' && metric.tags.field === 'observed'));

const createRejected = makeHarness({ createFailure: 'Entitlement' });
assert.throws(() => createRejected.run(), /create\/readiness failed \(api_error\)/);
assert.deepEqual(createRejected.deleted.map((resource) => resource.kind), ['Directory', 'Subaccount'], 'only accepted creates are registered');
assert.equal(createRejected.deleted.length, 2);
assert.deepEqual(createRejected.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const readyFailed = makeHarness({ readyFailure: 'Entitlement' });
assert.throws(() => readyFailed.run(), /create\/readiness failed \(timeout\)/);
assert.deepEqual(readyFailed.deleted.map((resource) => resource.kind), ['Entitlement', 'Directory', 'Subaccount']);
assert.ok(readyFailed.metrics.some((metric) => metric.name === 'xp_lifecycle_phase_duration' && metric.tags.stage === 'readiness' && metric.tags.outcome === 'timeout'));

const createBudgetFailed = makeHarness({ readyFailure: 'Subaccount', env: { XP_DIADROMOS_BTP_CREATE_BUDGET: '2' } });
assert.throws(() => createBudgetFailed.run(), /create\/readiness failed \(timeout\)/);
assert.equal(createBudgetFailed.created.length, 1);
assert.deepEqual(createBudgetFailed.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const cleanupBudgetExpired = makeHarness({ holdDeleted: true, env: { XP_DIADROMOS_BTP_CLEANUP_BUDGET: '2' } });
assert.throws(() => cleanupBudgetExpired.run(), /delete failed for owned/);
assert.equal(cleanupBudgetExpired.deleted.length, kinds.length, 'cleanup issues every delete despite an expired phase deadline');
assert.equal(cleanupBudgetExpired.metrics.filter((metric) => metric.name === 'xp_time_to_delete').length, 0, 'unobserved absence has no successful deletion trend');
assert.deepEqual(cleanupBudgetExpired.metrics.filter((metric) => metric.name === 'xp_lifecycle_success').map((metric) => metric.value), [0]);

const deletionFailed = makeHarness({ deleteFailure: 'DirectoryEntitlement' });
assert.throws(() => deletionFailed.run(), /delete failed for owned DirectoryEntitlement \(api_error\)/);
assert.deepEqual(deletionFailed.deleted.map((resource) => resource.kind), [...kinds].reverse(), 'cleanup continues after delete errors');
assert.ok(deletionFailed.metrics.some((metric) => metric.name === 'xp_operation_duration' && metric.tags.operation === 'delete' && metric.tags.outcome === 'failure'));
assert.ok(!deletionFailed.logs.some((line) => line.includes('synthetic delete failure')), 'raw errors are not logged');

const invalidAdmins = makeHarness({ secondDirectoryAdmin: 'BENCHMARK@example.invalid' });
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

const names = happy.helper.buildResourceNames('A run identity that is much longer than a DNS label and contains spaces', 'attempt/with/a/long/value', 1_700_000_000_000);
const repeatedNames = happy.helper.buildResourceNames('A run identity that is much longer than a DNS label and contains spaces', 'attempt/with/a/long/value', 1_700_000_000_000);
const resourceNames = [names.Subaccount, names.Directory, names.Entitlement, names.DirectoryEntitlement, names.SubaccountApiCredential];
assert.equal(JSON.stringify(names), JSON.stringify(repeatedNames), 'frozen clocks and identical inputs produce deterministic identities');
assert.equal(new Set(resourceNames).size, kinds.length);
assert.ok([...resourceNames, names.subdomain, names.connectionSecret].every((name) => name.length <= 63 && /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(name)));

const { helperOriginal, scenarioOriginal, scenarioSource } = loadSources();
const helperImports = [...helperOriginal.matchAll(/^import .* from ['"]([^'"]+)['"];?$/gm)].map((match) => match[1]);
const scenarioImports = [...scenarioOriginal.matchAll(/^import .* from ['"]([^'"]+)['"];?$/gm)].map((match) => match[1]);
assert.ok(helperImports.every((specifier) => specifier.startsWith('k6')), `unexpected helper runtime import: ${helperImports.join(', ')}`);
assert.deepEqual(scenarioImports, ['./crossplane-helpers.js'], 'scenario packages only with the configured helper');
assert.ok(scenarioSource.includes('xp.runOwnedResourceLifecycle'), 'scenario delegates lifecycle to the provider-local helper');

console.log('Owned-resource lifecycle fixtures passed.');
