import { Counter, Gauge, Trend } from 'k6/metrics';
import k8s from 'k6/x/kubernetes';
import { sleep } from 'k6';

// Generic counters — incremented automatically by helper functions.
// These are universal: any Crossplane provider test creates, deletes, and
// can observe failures. They stay in the helpers because the helpers own
// the lifecycle operations.
//
// Standard lifecycle trend metrics (xp_time_to_ready, xp_time_to_update,
// xp_time_to_delete) are defined in the test scripts that need them. Tests
// should pass a resource_kind tag so one dashboard can compare measurements
// for different Crossplane resource kinds.
// See AGENTS.md "Dynamic Custom Metrics" for the convention.
export const xpResourcesCreated = new Counter('xp_resources_created'); // count
export const xpResourcesDeleted = new Counter('xp_resources_deleted'); // count
export const xpResourcesFailed = new Counter('xp_resources_failed');  // count

// Structured measurement evidence. These metrics are intentionally emitted by
// the helper so they remain available when only this single file is packaged
// beside a test script.
export const xpOperationDuration = new Trend('xp_operation_duration', true); // ms
export const xpMeasurementPhase = new Counter('xp_measurement_phase');
export const xpLoadProfile = new Gauge('xp_load_profile');

// Defaults and protocol bounds.
const DEFAULT_NAMESPACE = 'default';
const POLL_INTERVAL = 1; // seconds
const POLL_TIMEOUT = 120; // seconds
const MAX_TAG_LENGTH = 64;
const MAX_PROFILE_POINTS = 128;

const PROFILE_FIELDS = [
  ['vus', (value) => finiteNonNegative(value)],
  ['iterations', (value) => finiteNonNegative(value)],
  ['duration_seconds', (value) => durationSeconds(value)],
  ['rate', (value) => finiteNonNegative(value)],
  ['time_unit_seconds', (value) => durationSeconds(value)],
  ['start_offset_seconds', (value) => durationSeconds(value)],
  ['stage_duration_seconds', (value) => durationSeconds(value)],
  ['stage_target', (value) => finiteNonNegative(value)],
];

function finiteNonNegative(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function durationSeconds(value) {
  if (typeof value === 'number') return finiteNonNegative(value);
  if (typeof value !== 'string') return null;

  const match = value.trim().match(/^(\d+(?:\.\d+)?)(ms|s|m|h)$/i);
  if (!match) return null;
  const units = { ms: 0.001, s: 1, m: 60, h: 3600 };
  const seconds = Number(match[1]) * units[match[2].toLowerCase()];
  return finiteNonNegative(seconds);
}

function boundedTag(value, fallback = 'unknown') {
  const text = value === undefined || value === null ? fallback : String(value);
  return text.length > MAX_TAG_LENGTH ? text.slice(0, MAX_TAG_LENGTH) : text;
}

function operationReason(error) {
  if (isTimeoutError(error)) return 'timeout';
  if (error instanceof CrossplaneError || error?.name === 'CrossplaneError') {
    return 'reconcile_error';
  }
  return error instanceof Error ? 'api_error' : 'unknown';
}

function recordOperation(operation, resourceKind, elapsed, outcome, reason) {
  xpOperationDuration.add(Math.max(0, elapsed), {
    operation: boundedTag(operation),
    resource_kind: boundedTag(resourceKind),
    outcome,
    reason,
  });
}

/**
 * Run one client-observed operation and emit exactly one completion point.
 * The original return value and thrown error are preserved. Nested waits are
 * deliberately not wrapped, so a lifecycle helper contributes one point.
 */
export function measureOperation(operation, resourceKind, fn) {
  const start = Date.now();
  try {
    const result = fn();
    recordOperation(operation, resourceKind, Date.now() - start, 'success', 'none');
    return result;
  } catch (error) {
    const reason = operationReason(error);
    recordOperation(operation, resourceKind, Date.now() - start, reason === 'timeout' ? 'timeout' : 'failure', reason);
    throw error;
  }
}

/** Record a script lifecycle boundary with bounded phase, event, and optional resource-kind tags. */
export function recordMeasurementPhase(phase, event = 'boundary', resourceKind) {
  const tags = {
    stage: boundedTag(phase),
    field: boundedTag(event),
  };
  if (resourceKind !== undefined) tags.resource_kind = boundedTag(resourceKind);
  xpMeasurementPhase.add(1, tags);
}

function profileScenario(scenario, scenarioOptions, seen, points) {
  if (!scenarioOptions || typeof scenarioOptions !== 'object') return;
  const scenarioName = boundedTag(scenario, 'default');
  const executor = boundedTag(scenarioOptions.executor, 'default');
  const emit = (field, value, stage) => {
    const converter = PROFILE_FIELDS.find(([name]) => name === field)?.[1];
    const numericValue = converter ? converter(value) : null;
    if (numericValue === null || points.length >= MAX_PROFILE_POINTS) return;

    const tags = {
      scenario: scenarioName,
      executor,
      field,
    };
    if (stage !== undefined) tags.stage = String(stage);
    const key = JSON.stringify([tags, numericValue]);
    if (seen.has(key)) return;
    seen.add(key);
    xpLoadProfile.add(numericValue, tags);
    points.push(key);
  };

  for (const [field] of PROFILE_FIELDS) {
    const sourceField = {
      duration_seconds: 'duration',
      time_unit_seconds: 'timeUnit',
      start_offset_seconds: 'startTime',
    }[field] || field;
    emit(field, scenarioOptions[sourceField]);
  }

  const stages = Array.isArray(scenarioOptions.stages) ? scenarioOptions.stages : [];
  stages.forEach((stage, index) => {
    if (!stage || typeof stage !== 'object') return;
    emit('stage_duration_seconds', stage.duration, index);
    emit('stage_target', stage.target, index);
  });
}

/**
 * Record finite, script-declared k6 load options. Unsupported options are
 * intentionally ignored: this is declaration evidence, not static inference.
 */
export function recordLoadProfile(options = {}) {
  if (!options || typeof options !== 'object') return 0;

  const seen = new Set();
  const points = [];
  const scenarios = options.scenarios;
  if (scenarios && typeof scenarios === 'object' && !Array.isArray(scenarios)) {
    for (const [name, scenarioOptions] of Object.entries(scenarios)) {
      profileScenario(name, scenarioOptions, seen, points);
    }
  }

  const directFields = ['vus', 'iterations', 'duration', 'rate', 'timeUnit', 'startTime', 'stages'];
  if (directFields.some((field) => Object.prototype.hasOwnProperty.call(options, field))) {
    profileScenario(options.scenario || 'default', options, seen, points);
  }
  return points.length;
}

// Typed errors let instrumentation classify timeouts without parsing messages.
export class TimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = 'TimeoutError';
  }
}

export function isTimeoutError(error) {
  return error instanceof TimeoutError || error?.name === 'TimeoutError';
}

export class CrossplaneError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CrossplaneError';
  }
}

// xk6-kubernetes uses GroupKind strings (e.g. "Object.kubernetes.crossplane.io")
// and positional args: get(groupKind, name, namespace).
function groupKind(kind, apiVersion) {
  const group = apiVersion.split('/')[0];
  return `${kind}.${group}`;
}

/**
 * Minimal JS object → YAML serializer for k6 manifests.
 * xk6-kubernetes apply() requires a YAML string (not a JS object).
 * Handles nested maps, arrays, strings, numbers, booleans.
 */
export function toYAML(obj, indent = 0) {
  const prefix = '  '.repeat(indent);
  let out = '';
  for (const [key, value] of Object.entries(obj)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === 'object' && item !== null) {
          out += `${prefix}${key}:\n${toYAML(item, indent + 1)}`;
        } else {
          out += `${prefix}${key}: ${item}\n`;
        }
      }
    } else if (typeof value === 'object') {
      out += `${prefix}${key}:\n${toYAML(value, indent + 1)}`;
    } else if (typeof value === 'string') {
      out += `${prefix}${key}: "${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"\n`;
    } else {
      out += `${prefix}${key}: ${value}\n`;
    }
  }
  return out;
}

/**
 * Returns a new k8s client instance.
 */
export function k8sClient() {
  const configPath = __ENV.XP_DIADROMOS_TARGET_KUBECONFIG || __ENV.KUBECONFIG;
  if (configPath) {
    return new k8s.Kubernetes({ config_path: configPath });
  }

  return new k8s.Kubernetes();
}

/**
 * Create a Crossplane resource and wait until it is ready.
 * @param {object} manifest - Full Kubernetes manifest (apiVersion, kind, metadata, spec…)
 * @param {object} [opts] - { namespace, timeout, interval, apiVersion, trendMetric, trendTags }
 *   opts.trendMetric — optional k6 Trend metric to record time-to-ready (ms)
 *   opts.trendTags — optional tags, including the standard resource_kind tag
 * @returns {object} The ready resource
 */
export function createAndWait(manifest, opts = {}) {
  return measureOperation('create', manifest?.kind, () => {
    const client = k8sClient();
    const ns = opts.namespace || manifest.metadata?.namespace || DEFAULT_NAMESPACE;
    const timeout = opts.timeout || POLL_TIMEOUT;
    const interval = opts.interval || POLL_INTERVAL;

    const start = Date.now();

    // client.apply() expects a YAML string; client.create() accepts a JS object.
    client.create(manifest);
    xpResourcesCreated.add(1);

    const kind = manifest.kind;
    const name = manifest.metadata.name;
    const apiVersion = opts.apiVersion || manifest.apiVersion;
    if (!apiVersion) {
      throw new Error('apiVersion is required: pass opts.apiVersion or manifest.apiVersion');
    }

    const resource = waitForReady(kind, name, { namespace: ns, timeout, interval, apiVersion });

    const elapsed = Date.now() - start;
    if (opts.trendMetric) {
      opts.trendMetric.add(elapsed, opts.trendTags || {});
    }

    console.log(`[XP-OPS] created ${kind}/${name} (ready in ${(elapsed / 1000).toFixed(1)}s)`);

    return resource;
  });
}

/**
 * Apply a Crossplane resource (server-side apply) and wait until it is ready.
 * Use this instead of createAndWait when the same resource will later be
 * updated via client.apply() — server-side apply tracks field ownership by
 * manager name, and mixing client.create() with client.apply() causes
 * "conflict with <manager>" errors on subsequent applies.
 *
 * @param {object} manifest - Full Kubernetes manifest (apiVersion, kind, metadata, spec…)
 * @param {object} [opts] - { namespace, timeout, interval, apiVersion, trendMetric, trendTags }
 *   opts.trendMetric — optional k6 Trend metric to record time-to-ready (ms)
 *   opts.trendTags — optional tags, including the standard resource_kind tag
 * @returns {object} The ready resource
 */
export function applyAndWait(manifest, opts = {}) {
  return measureOperation('apply', manifest?.kind, () => {
    const client = k8sClient();
    const ns = opts.namespace || manifest.metadata?.namespace || DEFAULT_NAMESPACE;
    const timeout = opts.timeout || POLL_TIMEOUT;
    const interval = opts.interval || POLL_INTERVAL;

    const start = Date.now();

    client.apply(toYAML(manifest));
    xpResourcesCreated.add(1);

    const kind = manifest.kind;
    const name = manifest.metadata.name;
    const apiVersion = opts.apiVersion || manifest.apiVersion;
    if (!apiVersion) {
      throw new Error('apiVersion is required: pass opts.apiVersion or manifest.apiVersion');
    }

    const resource = waitForReady(kind, name, { namespace: ns, timeout, interval, apiVersion });

    const elapsed = Date.now() - start;
    if (opts.trendMetric) {
      opts.trendMetric.add(elapsed, opts.trendTags || {});
    }

    console.log(`[XP-OPS] applied ${kind}/${name} (ready in ${(elapsed / 1000).toFixed(1)}s)`);

    return resource;
  });
}

/**
 * Delete a Crossplane resource and wait until it is fully removed.
 * @param {string} kind  - Resource kind (e.g. 'Object')
 * @param {string} name - Resource name
 * @param {object} [opts] - { namespace, timeout, interval, apiVersion, trendMetric, trendTags, onDeleteAccepted, onKubernetesObjectAbsent }
 *   opts.trendMetric — optional k6 Trend metric to record time-to-deletion (ms)
 *   opts.trendTags — optional tags, including the standard resource_kind tag
 *   opts.onDeleteAccepted — optional callback after the Kubernetes delete request succeeds
 *   opts.onKubernetesObjectAbsent — optional callback after Kubernetes reports the object absent
 */
export function deleteAndWait(kind, name, opts = {}) {
  return measureOperation('delete', kind, () => {
    const client = k8sClient();
    const ns = opts.namespace || DEFAULT_NAMESPACE;
    const timeout = opts.timeout || POLL_TIMEOUT;
    const interval = opts.interval || POLL_INTERVAL;
    const apiVersion = opts.apiVersion;
    if (!apiVersion) {
      throw new Error('apiVersion is required: pass opts.apiVersion');
    }

    const start = Date.now();

    client.delete(groupKind(kind, apiVersion), name, ns);
    opts.onDeleteAccepted?.(Date.now() - start);

    xpResourcesDeleted.add(1);

    waitForDeletion(kind, name, { namespace: ns, timeout, interval, apiVersion });
    opts.onKubernetesObjectAbsent?.(Date.now() - start);

    const elapsed = Date.now() - start;
    if (opts.trendMetric) {
      opts.trendMetric.add(elapsed, opts.trendTags || {});
    }

    console.log(`[XP-OPS] deleted ${kind}/${name} (gone in ${(elapsed / 1000).toFixed(1)}s)`);
  });
}

/**
 * Poll until a Crossplane resource reaches Ready status.
 * @param {string} kind
 * @param {string} name
 * @param {object} [opts] - { namespace, timeout, interval, apiVersion }
 * @returns {object} The ready resource
 */
const SAFE_CONDITION_REASONS = {
  ReconcileSuccess: 'reconcile_success',
  ReconcileError: 'reconcile_error',
  Creating: 'creating',
  Deleting: 'deleting',
  Unavailable: 'unavailable',
  LateInitialize: 'late_initialize',
  AsyncOperation: 'async_operation',
  Waiting: 'waiting',
  ReconcilePaused: 'reconcile_paused',
  CannotInitializeManagedResource: 'cannot_initialize',
  CannotConnectToProvider: 'cannot_connect_provider',
  CannotGetReferencedObject: 'cannot_get_reference',
  CannotResolveReferences: 'cannot_resolve_references',
  CannotCreateExternalResource: 'cannot_create_external_resource',
  CannotObserveExternalResource: 'cannot_observe_external_resource',
  CannotUpdateExternalResource: 'cannot_update_external_resource',
  CannotDeleteExternalResource: 'cannot_delete_external_resource',
  ReferenceResolutionFailed: 'reference_resolution_failed',
};

function safeConditionObservation(resource, type) {
  const conditions = Array.isArray(resource?.status?.conditions) ? resource.status.conditions : [];
  const condition = conditions.find((candidate) => candidate?.type === type);
  if (!condition) return { status: 'absent', reason: 'absent', generation: 'unavailable', transitionAge: 'absent' };

  const status = condition.status === 'True' || condition.status === true
    ? 'true'
    : condition.status === 'False' || condition.status === false ? 'false' : 'other';
  const reason = typeof condition.reason === 'string'
    ? (SAFE_CONDITION_REASONS[condition.reason] || 'other')
    : 'absent';

  const generation = resource?.metadata?.generation;
  const observedGeneration = condition.observedGeneration;
  let generationState = 'unavailable';
  if (generation !== undefined && generation !== null) {
    const current = Number(generation);
    if (!Number.isSafeInteger(current) || current < 0) generationState = 'invalid';
    else if (observedGeneration === undefined || observedGeneration === null) generationState = 'not_reported';
    else {
      const observed = Number(observedGeneration);
      if (!Number.isSafeInteger(observed) || observed < 0) generationState = 'invalid';
      else if (observed < current) generationState = 'stale';
      else if (observed > current) generationState = 'ahead';
      else generationState = 'current';
    }
  }

  let transitionAge = 'absent';
  if (condition.lastTransitionTime !== undefined && condition.lastTransitionTime !== null) {
    const transitionedAt = Date.parse(condition.lastTransitionTime);
    if (!Number.isFinite(transitionedAt)) transitionAge = 'invalid';
    else {
      const age = Date.now() - transitionedAt;
      if (age < 0) transitionAge = 'future';
      else if (age < 60_000) transitionAge = 'under_1m';
      else if (age < 300_000) transitionAge = '1_to_5m';
      else if (age < 600_000) transitionAge = '5_to_10m';
      else transitionAge = 'over_10m';
    }
  }
  return { status, reason, generation: generationState, transitionAge };
}

function safePollingErrorCategory(error) {
  const message = String(error?.message || '').toLowerCase();
  const status = [error?.status, error?.statusCode, error?.code]
    .filter((value) => value !== undefined && value !== null)
    .map((value) => String(value).toLowerCase())
    .join(' ');
  const text = `${status} ${message}`;
  if (/(^|\D)401(\D|$)|unauthorized/.test(text)) return 'unauthorized';
  if (/(^|\D)403(\D|$)|forbidden/.test(text)) return 'forbidden';
  if (/(^|\D)404(\D|$)|not found/.test(text)) return 'not_found';
  return 'other_error';
}

function safeReadinessObservation(resource, lastPoll, pollErrors, pollCount, pollErrorCount) {
  const subaccountID = resource?.spec?.forProvider?.subaccountId;
  const atProvider = resource?.status?.atProvider || {};
  const externalName = resource?.metadata?.annotations?.['crossplane.io/external-name'];
  const pollCountBucket = pollCount <= 1 ? '1' : pollCount <= 10 ? '2_to_10' : pollCount <= 60 ? '11_to_60' : pollCount <= 600 ? '61_to_600' : 'over_600';
  return {
    ready: safeConditionObservation(resource, 'Ready'),
    synced: safeConditionObservation(resource, 'Synced'),
    subaccountID: typeof subaccountID === 'string' && subaccountID.trim() !== '' ? 'present' : 'absent',
    atProviderID: typeof atProvider.id === 'string' && atProvider.id.trim() !== '' ? 'present' : 'absent',
    atProviderName: typeof atProvider.name === 'string' && atProvider.name.trim() !== '' ? 'present' : 'absent',
    atProviderSubaccountID: typeof atProvider.subaccountId === 'string' && atProvider.subaccountId.trim() !== '' ? 'present' : 'absent',
    certificateReceived: typeof atProvider.certificateReceived === 'string' && atProvider.certificateReceived.trim() !== '' ? 'present' : 'absent',
    credentialType: atProvider.credentialType === 'Secrets' ? 'secrets'
      : atProvider.credentialType === 'Certificates' ? 'certificates'
        : atProvider.credentialType == null || atProvider.credentialType === '' ? 'absent' : 'other',
    externalName: typeof externalName === 'string' && externalName.trim() !== '' ? 'present' : 'absent',
    pollCountBucket,
    pollErrorCountBucket: pollErrorCount === 0 ? '0' : pollErrorCount === 1 ? '1' : pollErrorCount <= 5 ? '2_to_5' : 'over_5',
    lastPoll,
    pollErrors: [...pollErrors].sort(),
  };
}

export function waitForReady(kind, name, opts = {}) {
  const client = k8sClient();
  const ns = opts.namespace || DEFAULT_NAMESPACE;
  const timeout = opts.timeout || POLL_TIMEOUT;
  const interval = opts.interval || POLL_INTERVAL;
  const apiVersion = opts.apiVersion;
  if (!apiVersion) {
    throw new Error('apiVersion is required: pass opts.apiVersion');
  }

  const deadline = Date.now() + timeout * 1000;
  let lastError = null;
  let lastPoll = 'not_found';
  let lastResource;
  const pollErrors = new Set();
  let observationEmitted = false;
  let pollCount = 0;
  let pollErrorCount = 0;
  const emitObservation = (resource) => {
    if (observationEmitted || typeof opts.onObservation !== 'function') return;
    observationEmitted = true;
    opts.onObservation(safeReadinessObservation(resource, lastPoll, pollErrors, pollCount, pollErrorCount));
  };

  while (Date.now() < deadline) {
    pollCount += 1;
    let resource;
    try {
      resource = client.get(groupKind(kind, apiVersion), name, ns);
      lastResource = resource;
      lastPoll = 'ok';
    } catch (e) {
      lastPoll = safePollingErrorCategory(e);
      pollErrors.add(lastPoll);
      pollErrorCount += 1;
      lastError = e;
      sleep(interval);
      continue;
    }

    const conditions = Array.isArray(resource?.status?.conditions) ? resource.status.conditions : [];
    const ready = conditions.find(
      (c) => c.type === 'Ready' || (!opts.requireReady && c.type === 'Synced')
    );

    if (ready && (ready.status === 'True' || ready.status === true)) {
      emitObservation(resource);
      return resource;
    }

    // Check for explicit failure without exposing the condition reason/message.
    const failed = conditions.find(
      (c) => c.type === 'Ready' && (c.status === 'False' || c.status === false) && c.reason === 'ReconcileError'
    );
    if (failed) {
      emitObservation(resource);
      xpResourcesFailed.add(1);
      console.log(`[XP-OPS] failed ${kind}/${name}: reconciliation error`);
      throw new CrossplaneError(`Resource ${kind}/${name} failed reconciliation`);
    }

    sleep(interval);
  }

  emitObservation(lastResource);
  xpResourcesFailed.add(1);
  console.log(`[XP-OPS] failed ${kind}/${name}: timed out after ${timeout}s`);
  throw new TimeoutError(
    `Timed out waiting for ${kind}/${name} to become Ready within ${timeout}s` +
    (lastError ? ' (Kubernetes API polling error)' : '')
  );
}

/**
 * Poll until a Crossplane resource is fully deleted.
 * @param {string} kind
 * @param {string} name
 * @param {object} [opts] - { namespace, timeout, interval, apiVersion }
 */
export function waitForDeletion(kind, name, opts = {}) {
  const client = k8sClient();
  const ns = opts.namespace || DEFAULT_NAMESPACE;
  const timeout = opts.timeout || POLL_TIMEOUT;
  const interval = opts.interval || POLL_INTERVAL;
  const apiVersion = opts.apiVersion;
  if (!apiVersion) {
    throw new Error('apiVersion is required: pass opts.apiVersion');
  }

  const deadline = Date.now() + timeout * 1000;

  while (Date.now() < deadline) {
    try {
      client.get(groupKind(kind, apiVersion), name, ns);
      // Still exists — keep waiting.
    } catch (e) {
      // Only an explicit Kubernetes NotFound response proves deletion. Auth,
      // transport, and API errors must not be mistaken for absence.
      const message = String(e?.message || e).toLowerCase();
      if (message.includes('404') || message.includes('not found')) return;
    }

    sleep(interval);
  }

  console.log(`[XP-OPS] failed ${kind}/${name}: timed out after ${timeout}s`);
  throw new TimeoutError(
    `Timed out waiting for ${kind}/${name} to be deleted within ${timeout}s`
  );
}

/**
 * Delete all resources of a given kind whose name starts with prefix.
 * Useful in teardown() to clean up resources created during the test
 * without tracking individual names across VUs.
 *
 * @param {string} kind - Resource kind (e.g. 'Object')
 * @param {string} prefix - Name prefix to match (e.g. 'xp-burst-')
 * @param {object} [opts] - { namespace, apiVersion }
 * @returns {number} Number of resources deleted
 */
export function cleanupByPrefix(kind, prefix, opts = {}) {
  const client = k8sClient();
  const ns = opts.namespace || DEFAULT_NAMESPACE;
  const apiVersion = opts.apiVersion;
  if (!apiVersion) {
    throw new Error('apiVersion is required: pass opts.apiVersion');
  }

  const gk = groupKind(kind, apiVersion);
  const resources = client.list(gk, ns);
  let deleted = 0;

  for (const res of resources) {
    const name = res.metadata?.name || '';
    if (name.startsWith(prefix)) {
      client.delete(gk, name, ns);
      xpResourcesDeleted.add(1);
      deleted++;
    }
  }

  console.log(`[XP-OPS] cleanup ${kind}: deleted ${deleted} resources with prefix ${prefix}`);
  return deleted;
}

/**
 * Poll until all resources of a given kind whose name starts with prefix
 * are fully deleted. Assumes deletes have already been issued (e.g. via
 * cleanupByPrefix).
 *
 * Use this when you need to ensure resources are gone before proceeding
 * (e.g. deleting Entitlements before Subaccounts they reference), but
 * want to record deletion counts BEFORE waiting — so metrics are
 * captured even if the wait times out.
 *
 * @param {string} kind - Resource kind (e.g. 'Entitlement')
 * @param {string} prefix - Name prefix to match (e.g. 'xp-btp-load-ent-')
 * @param {object} [opts] - { namespace, apiVersion, timeout, interval }
 */
export function waitForDeletionByPrefix(kind, prefix, opts = {}) {
  const client = k8sClient();
  const ns = opts.namespace || DEFAULT_NAMESPACE;
  const timeout = opts.timeout || POLL_TIMEOUT;
  const interval = opts.interval || POLL_INTERVAL;
  const apiVersion = opts.apiVersion;
  if (!apiVersion) {
    throw new Error('apiVersion is required: pass opts.apiVersion');
  }

  const gk = groupKind(kind, apiVersion);
  const deadline = Date.now() + timeout * 1000;
  while (Date.now() < deadline) {
    try {
      const resources = client.list(gk, ns);
      const remaining = resources.filter(
        (r) => (r.metadata?.name || '').startsWith(prefix)
      );
      if (remaining.length === 0) return;
    } catch (e) {
      // Transient list error — keep polling.
    }
    sleep(interval);
  }

  throw new Error(
    `Timed out waiting for ${kind} with prefix '${prefix}' to be deleted within ${timeout}s`
  );
}

/**
 * Delete all resources of a given kind whose name starts with prefix,
 * then poll until all matching resources are fully deleted.
 *
 * Combines cleanupByPrefix (issue deletes) with polling for actual deletion.
 * Useful in teardown() when you need to ensure resources are gone before
 * proceeding (e.g. deleting Entitlements before Subaccounts they reference).
 *
 * IMPORTANT: If you need to record deletion counts in custom Counter metrics,
 * prefer calling cleanupByPrefix() + waitForDeletionByPrefix() separately
 * so you can increment your Counter BEFORE the wait-for-deletion phase
 * (which may time out and prevent subsequent code from running).
 *
 * @param {string} kind - Resource kind (e.g. 'Object')
 * @param {string} prefix - Name prefix to match (e.g. 'xp-btp-load-ent-')
 * @param {object} [opts] - { namespace, apiVersion, timeout, interval }
 * @returns {number} Number of resources deleted
 */
export function cleanupAndWaitByPrefix(kind, prefix, opts = {}) {
  const deleted = cleanupByPrefix(kind, prefix, opts);
  if (deleted === 0) return 0;
  waitForDeletionByPrefix(kind, prefix, opts);
  return deleted;
}
