#!/usr/bin/env node
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(new URL('./k6/crossplane-helpers.js', `file://${__filename}`), 'utf8')
  .replace("import { Counter, Gauge, Trend } from 'k6/metrics';", '')
  .replace("import k8s from 'k6/x/kubernetes';", '')
  .replace("import { sleep } from 'k6';", '')
  .replace(/\bexport\s+/g, '');

class Metric {
  add() {}
}

function createHarness(get) {
  let now = 0;
  class FakeDate extends Date {
    static now() { return now; }
  }
  class Kubernetes {
    get(...args) { return get(...args); }
  }
  const context = {
    Counter: Metric,
    Gauge: Metric,
    Trend: Metric,
    Date: FakeDate,
    __ENV: {},
    console: { log() {} },
    k8s: { Kubernetes },
    sleep(seconds) { now += seconds * 1000; },
  };
  vm.runInNewContext(`${source}\nthis.wait = waitForReady;`, context);
  return context;
}

function wait(context, get, { timeout = 2 } = {}) {
  context.k8s.Kubernetes = class {
    get(...args) { return get(...args); }
  };
  let observation;
  try {
    context.wait('SubaccountApiCredential', 'private-resource-name', {
      apiVersion: 'security.btp.sap.crossplane.io/v1alpha1',
      namespace: 'default',
      timeout,
      interval: 1,
      requireReady: true,
      onObservation(value) { observation = value; },
    });
  } catch (error) {
    return { observation, error };
  }
  return { observation };
}

const readyResource = {
  metadata: { generation: 2, annotations: { 'crossplane.io/external-name': 'private-external-name' } },
  spec: { forProvider: { subaccountId: 'private-subaccount-id' } },
  status: { atProvider: { id: 'private-id', name: 'private-name', credentialType: 'Secrets', subaccountId: 'private-subaccount-id', certificateReceived: 'private-certificate' }, conditions: [
    { type: 'Ready', status: 'False', reason: 'private reason', message: 'private condition message', observedGeneration: 1, lastTransitionTime: '1970-01-01T00:00:00Z' },
    { type: 'Synced', status: 'True', message: 'private sync message', observedGeneration: 2, lastTransitionTime: '1970-01-01T00:00:00Z' },
  ] },
};
let reads = 0;
const success = wait(createHarness(() => null), () => {
  reads += 1;
  if (reads === 1) return readyResource;
  return {
    ...readyResource,
    status: { ...readyResource.status, conditions: [
      { type: 'Ready', status: 'True', reason: 'ReconcileSuccess', observedGeneration: 2, lastTransitionTime: '1970-01-01T00:00:00Z', message: 'private final message' },
      { type: 'Synced', status: 'True', reason: 'ReconcileSuccess', observedGeneration: 2, lastTransitionTime: '1970-01-01T00:00:00Z' },
    ] },
  };
});
assert.deepEqual(JSON.parse(JSON.stringify(success.observation)), {
  ready: { status: 'true', reason: 'reconcile_success', generation: 'current', transitionAge: 'under_1m', messageCategory: 'not_applicable' },
  synced: { status: 'true', reason: 'reconcile_success', generation: 'current', transitionAge: 'under_1m', messageCategory: 'not_applicable' },
  history: {
    readyStatuses: ['false', 'true'], readyReasons: ['other', 'reconcile_success'], readyMessageCategories: ['not_applicable', 'unknown'],
    syncedStatuses: ['true'], syncedReasons: ['absent', 'reconcile_success'], syncedMessageCategories: ['not_applicable'],
    atProviderID: ['present'], externalName: ['present'],
  },
  subaccountID: 'present', atProviderID: 'present', atProviderName: 'present',
  atProviderSubaccountID: 'present', certificateReceived: 'present', credentialType: 'secrets',
  externalName: 'present', pollCountBucket: '2_to_10', pollErrorCountBucket: '0',
  lastPoll: 'ok', pollErrors: [],
});

const timeoutContext = createHarness(() => null);
const timedOut = wait(timeoutContext, () => ({
  spec: { forProvider: { subaccountId: 'private-subaccount-id' } },
  status: { conditions: [
    { type: 'Ready', status: 'False', reason: 'private reason', message: '403 Forbidden: private credential details' },
    { type: 'Synced', status: 'True', message: 'private sync message' },
  ] },
}));
assert.equal(timedOut.error?.name, 'TimeoutError');
assert.deepEqual(JSON.parse(JSON.stringify(timedOut.observation)), {
  ready: { status: 'false', reason: 'other', generation: 'unavailable', transitionAge: 'absent', messageCategory: 'authorization' },
  synced: { status: 'true', reason: 'absent', generation: 'unavailable', transitionAge: 'absent', messageCategory: 'not_applicable' },
  history: {
    readyStatuses: ['false'], readyReasons: ['other'], readyMessageCategories: ['authorization'],
    syncedStatuses: ['true'], syncedReasons: ['absent'], syncedMessageCategories: ['not_applicable'],
    atProviderID: ['absent'], externalName: ['absent'],
  },
  subaccountID: 'present', atProviderID: 'absent', atProviderName: 'absent',
  atProviderSubaccountID: 'absent', certificateReceived: 'absent', credentialType: 'absent',
  externalName: 'absent', pollCountBucket: '2_to_10', pollErrorCountBucket: '0',
  lastPoll: 'ok', pollErrors: [],
});

const pollingContext = createHarness(() => null);
const pollingFailure = wait(pollingContext, () => {
  const error = new Error('private token and server response');
  error.statusCode = 403;
  throw error;
});
assert.equal(pollingFailure.error?.name, 'TimeoutError');
assert.deepEqual(JSON.parse(JSON.stringify(pollingFailure.observation)), {
  ready: { status: 'absent', reason: 'absent', generation: 'unavailable', transitionAge: 'absent', messageCategory: 'absent' },
  synced: { status: 'absent', reason: 'absent', generation: 'unavailable', transitionAge: 'absent', messageCategory: 'absent' },
  history: {
    readyStatuses: [], readyReasons: [], readyMessageCategories: [],
    syncedStatuses: [], syncedReasons: [], syncedMessageCategories: [],
    atProviderID: [], externalName: [],
  },
  subaccountID: 'absent', atProviderID: 'absent', atProviderName: 'absent',
  atProviderSubaccountID: 'absent', certificateReceived: 'absent', credentialType: 'absent',
  externalName: 'absent', pollCountBucket: '2_to_10', pollErrorCountBucket: '2_to_5',
  lastPoll: 'forbidden', pollErrors: ['forbidden'],
});

for (const result of [success, timedOut, pollingFailure]) {
  const serialized = JSON.stringify(result.observation);
  for (const secret of ['private-resource-name', 'private-subaccount-id', 'private reason', 'private credential details', 'private sync message', 'private token', 'server response']) {
    assert.ok(!serialized.includes(secret), `observation leaked ${secret}`);
  }
}

console.log('Credential readiness observation fixtures passed.');
