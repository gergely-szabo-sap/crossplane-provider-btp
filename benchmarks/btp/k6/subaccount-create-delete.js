import * as xp from './crossplane-helpers.js';
import { Trend } from 'k6/metrics';

const xpTimeToReady = new Trend('xp_time_to_ready', true);
const xpTimeToDelete = new Trend('xp_time_to_delete', true);
const xpLifecyclePhaseDuration = new Trend('xp_lifecycle_phase_duration', true);
export const options = {
  scenarios: {
    default: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '1h',
    },
  },
};

const READY_TIMEOUT = Number(__ENV.XP_DIADROMOS_BTP_READY_TIMEOUT || '600');
const DELETE_TIMEOUT = Number(__ENV.XP_DIADROMOS_BTP_DELETE_TIMEOUT || '600');
const REGION = __ENV.XP_DIADROMOS_BTP_REGION || 'eu10';
const SUBACCOUNT_ADMIN = __ENV.XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN;
const SECOND_DIRECTORY_ADMIN = __ENV.XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN;
const RUN_ID = __ENV.XP_DIADROMOS_BTP_RUN_ID || __ENV.GITHUB_RUN_ID;
const NS = 'default';

function safePart(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+|-+$/g, '').slice(0, 16) || 'run';
}

function failureCategory(error) {
  if (error?.name === 'TimeoutError') return { outcome: 'timeout', reason: 'timeout' };
  if (error?.name === 'CrossplaneError') return { outcome: 'failure', reason: 'reconcile_error' };
  return { outcome: 'failure', reason: 'api_error' };
}

function recordPhase(kind, phase, startedAt, outcome, reason, event) {
  xpLifecyclePhaseDuration.add(Math.max(0, Date.now() - startedAt), {
    resource_kind: kind,
    stage: phase,
    outcome,
    reason,
  });
  xp.recordMeasurementPhase(phase, event, kind);
}

const CREDENTIAL_OBSERVATION_VALUES = {
  conditionStatus: ['true', 'false', 'absent', 'other'],
  conditionReason: ['absent', 'reconcile_success', 'reconcile_error', 'creating', 'deleting', 'unavailable', 'late_initialize', 'async_operation', 'waiting', 'reconcile_paused', 'cannot_initialize', 'cannot_connect_provider', 'cannot_get_reference', 'cannot_resolve_references', 'cannot_create_external_resource', 'cannot_observe_external_resource', 'cannot_update_external_resource', 'cannot_delete_external_resource', 'reference_resolution_failed', 'other'],
  messageCategory: ['absent', 'not_applicable', 'authentication', 'authorization', 'rate_limited', 'timeout', 'transport', 'remote_server', 'not_found', 'conflict', 'validation', 'reference', 'unsupported', 'unknown'],
  generation: ['current', 'stale', 'ahead', 'not_reported', 'invalid', 'unavailable', 'other'],
  transitionAge: ['absent', 'invalid', 'future', 'under_1m', '1_to_5m', '5_to_10m', 'over_10m', 'other'],
  presence: ['present', 'absent'],
  credentialType: ['secrets', 'certificates', 'absent', 'other'],
  pollCount: ['1', '2_to_10', '11_to_60', '61_to_600', 'over_600'],
  pollErrorCount: ['0', '1', '2_to_5', 'over_5'],
  lastPoll: ['ok', 'not_found', 'unauthorized', 'forbidden', 'other_error'],
  pollError: ['not_found', 'unauthorized', 'forbidden', 'other_error'],
};

function recordCredentialReadinessObservation(observation) {
  const safeValue = (group, value, fallback = 'other') => CREDENTIAL_OBSERVATION_VALUES[group].includes(value) ? value : fallback;
  const condition = (type) => {
    const value = observation?.[type] || {};
    return {
      status: safeValue('conditionStatus', value.status),
      reason: value.reason == null ? 'absent' : safeValue('conditionReason', value.reason),
      generation: value.generation == null ? 'unavailable' : safeValue('generation', value.generation),
      transitionAge: value.transitionAge == null ? 'absent' : safeValue('transitionAge', value.transitionAge),
      messageCategory: value.messageCategory == null ? 'absent' : safeValue('messageCategory', value.messageCategory),
    };
  };
  const safe = {
    ready: condition('ready'),
    synced: condition('synced'),
    subaccountID: safeValue('presence', observation?.subaccountID, 'absent'),
    atProviderID: safeValue('presence', observation?.atProviderID, 'absent'),
    atProviderName: safeValue('presence', observation?.atProviderName, 'absent'),
    atProviderSubaccountID: safeValue('presence', observation?.atProviderSubaccountID, 'absent'),
    certificateReceived: safeValue('presence', observation?.certificateReceived, 'absent'),
    credentialType: safeValue('credentialType', observation?.credentialType, 'other'),
    externalName: safeValue('presence', observation?.externalName, 'absent'),
    pollCount: safeValue('pollCount', observation?.pollCountBucket, 'over_600'),
    pollErrorCount: safeValue('pollErrorCount', observation?.pollErrorCountBucket, 'over_5'),
    lastPoll: safeValue('lastPoll', observation?.lastPoll, 'other_error'),
    pollErrors: Array.isArray(observation?.pollErrors)
      ? [...new Set(observation.pollErrors.filter((value) => CREDENTIAL_OBSERVATION_VALUES.pollError.includes(value)))].sort()
      : [],
    history: observation?.history && typeof observation.history === 'object' ? observation.history : {},
  };
  const safeHistory = (key, allowed) => Array.isArray(safe.history[key])
    ? [...new Set(safe.history[key].filter((value) => allowed.includes(value)))].sort()
    : [];
  const history = {
    readyStatuses: safeHistory('readyStatuses', CREDENTIAL_OBSERVATION_VALUES.conditionStatus),
    readyReasons: safeHistory('readyReasons', CREDENTIAL_OBSERVATION_VALUES.conditionReason),
    readyMessageCategories: safeHistory('readyMessageCategories', CREDENTIAL_OBSERVATION_VALUES.messageCategory),
    syncedStatuses: safeHistory('syncedStatuses', CREDENTIAL_OBSERVATION_VALUES.conditionStatus),
    syncedReasons: safeHistory('syncedReasons', CREDENTIAL_OBSERVATION_VALUES.conditionReason),
    syncedMessageCategories: safeHistory('syncedMessageCategories', CREDENTIAL_OBSERVATION_VALUES.messageCategory),
    atProviderID: safeHistory('atProviderID', CREDENTIAL_OBSERVATION_VALUES.presence),
    externalName: safeHistory('externalName', CREDENTIAL_OBSERVATION_VALUES.presence),
  };

  const categories = [
    `ready_${safe.ready.status}`, `ready_reason_${safe.ready.reason}`,
    `ready_generation_${safe.ready.generation}`, `ready_transition_age_${safe.ready.transitionAge}`,
    `ready_message_${safe.ready.messageCategory}`,
    `synced_${safe.synced.status}`, `synced_reason_${safe.synced.reason}`,
    `synced_generation_${safe.synced.generation}`, `synced_transition_age_${safe.synced.transitionAge}`,
    `synced_message_${safe.synced.messageCategory}`,
    ...history.readyStatuses.map((value) => `ready_seen_status_${value}`),
    ...history.readyReasons.map((value) => `ready_seen_reason_${value}`),
    ...history.readyMessageCategories.map((value) => `ready_seen_message_${value}`),
    ...history.syncedStatuses.map((value) => `synced_seen_status_${value}`),
    ...history.syncedReasons.map((value) => `synced_seen_reason_${value}`),
    ...history.syncedMessageCategories.map((value) => `synced_seen_message_${value}`),
    ...history.atProviderID.map((value) => `at_provider_id_seen_${value}`),
    ...history.externalName.map((value) => `external_name_seen_${value}`),
    `subaccount_id_${safe.subaccountID}`, `at_provider_id_${safe.atProviderID}`,
    `at_provider_name_${safe.atProviderName}`, `at_provider_subaccount_id_${safe.atProviderSubaccountID}`,
    `certificate_received_${safe.certificateReceived}`, `credential_type_${safe.credentialType}`,
    `external_name_${safe.externalName}`, `poll_count_${safe.pollCount}`,
    `poll_error_count_${safe.pollErrorCount}`, `poll_last_${safe.lastPoll}`,
    ...(safe.pollErrors.length > 0 ? safe.pollErrors.map((value) => `poll_error_${value}_seen`) : ['poll_error_none_seen']),
  ];
  for (const category of categories) {
    xp.recordMeasurementPhase('credential_readiness', category, 'SubaccountApiCredential');
  }

  console.log([
    '[XP-OBS] Allowlisted SubaccountApiCredential observation (sanitized YAML; not a full resource dump):',
    'apiVersion: security.btp.sap.crossplane.io/v1alpha1',
    'kind: SubaccountApiCredential',
    'spec:',
    '  forProvider:',
    `    subaccountId: ${safe.subaccountID === 'present' ? '<present>' : '<absent>'}`,
    'status:',
    '  conditions:',
    '    - type: Ready',
    `      status: ${safe.ready.status}`,
    `      reasonCategory: ${safe.ready.reason}`,
    `      observedGeneration: ${safe.ready.generation}`,
    `      transitionAge: ${safe.ready.transitionAge}`,
    `      messageCategory: ${safe.ready.messageCategory}`,
    '    - type: Synced',
    `      status: ${safe.synced.status}`,
    `      reasonCategory: ${safe.synced.reason}`,
    `      observedGeneration: ${safe.synced.generation}`,
    `      transitionAge: ${safe.synced.transitionAge}`,
    `      messageCategory: ${safe.synced.messageCategory}`,
    '  atProvider:',
    `    id: ${safe.atProviderID === 'present' ? '<present>' : '<absent>'}`,
    `    name: ${safe.atProviderName === 'present' ? '<present>' : '<absent>'}`,
    `    subaccountId: ${safe.atProviderSubaccountID === 'present' ? '<present>' : '<absent>'}`,
    `    certificateReceived: ${safe.certificateReceived === 'present' ? '<present>' : '<absent>'}`,
    `    credentialType: ${safe.credentialType}`,
    `    externalName: ${safe.externalName}`,
    'history:',
    `  readyStatusesSeen: [${history.readyStatuses.join(', ')}]`,
    `  readyReasonsSeen: [${history.readyReasons.join(', ')}]`,
    `  readyMessageCategoriesSeen: [${history.readyMessageCategories.join(', ')}]`,
    `  syncedStatusesSeen: [${history.syncedStatuses.join(', ')}]`,
    `  syncedReasonsSeen: [${history.syncedReasons.join(', ')}]`,
    `  syncedMessageCategoriesSeen: [${history.syncedMessageCategories.join(', ')}]`,
    `  atProviderIDPresenceSeen: [${history.atProviderID.join(', ')}]`,
    `  externalNamePresenceSeen: [${history.externalName.join(', ')}]`,
    'polling:',
    `  requestCount: ${safe.pollCount}`,
    `  errorCount: ${safe.pollErrorCount}`,
    `  lastResult: ${safe.lastPoll}`,
    '  errorCategories:',
    ...(safe.pollErrors.length > 0 ? safe.pollErrors : ['none']).map((category) => `    - ${category}`),
  ].join('\n'));
}

function resource(kind, apiVersion, name, spec, namespaced = true) {
  return {
    apiVersion,
    kind,
    metadata: { ...(namespaced ? { namespace: NS } : {}), name },
    spec: {
      providerConfigRef: { name: 'default' },
      forProvider: spec,
      ...(kind === 'SubaccountApiCredential' ? {
        writeConnectionSecretToRef: { name: `${name.slice(0, 55)}-secret`, namespace: NS },
      } : {}),
    },
  };
}

export default function () {
  if (!SUBACCOUNT_ADMIN) throw new Error('XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN is required');
  if (!SECOND_DIRECTORY_ADMIN) throw new Error('XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN is required');
  if (SUBACCOUNT_ADMIN.toLowerCase() === SECOND_DIRECTORY_ADMIN.toLowerCase()) {
    throw new Error('Directory admins must be distinct');
  }
  if (!RUN_ID) throw new Error('Set XP_DIADROMOS_BTP_RUN_ID (or GITHUB_RUN_ID) to identify owned resources');

  const suffix = `${safePart(RUN_ID)}-${safePart(__ENV.GITHUB_RUN_ATTEMPT || '1')}-${Date.now().toString(36)}`;
  const subName = `xp-btp-bench-${suffix}`.slice(0, 63).replace(/-+$/g, '');
  const dirName = `xp-btp-bench-dir-${suffix}`.slice(0, 63).replace(/-+$/g, '');
  const subdomain = `xpbtpbench-${suffix}`.slice(0, 63).replace(/-+$/g, '');
  const names = {
    Subaccount: subName,
    Directory: dirName,
    Entitlement: `xp-btp-bench-ent-${suffix}`.slice(0, 63).replace(/-+$/g, ''),
    DirectoryEntitlement: `xp-btp-bench-dirent-${suffix}`.slice(0, 63).replace(/-+$/g, ''),
    SubaccountApiCredential: `xp-btp-bench-api-${suffix}`.slice(0, 63).replace(/-+$/g, ''),
  };
  const accountAPI = 'account.btp.sap.crossplane.io/v1alpha1';
  const securityAPI = 'security.btp.sap.crossplane.io/v1alpha1';
  const manifests = [
    resource('Subaccount', accountAPI, names.Subaccount, {
      displayName: names.Subaccount, region: REGION, subdomain, subaccountAdmins: [SUBACCOUNT_ADMIN],
    }),
    resource('Directory', accountAPI, names.Directory, {
      description: `xp-diadromos benchmark ${suffix}`,
      directoryAdmins: [SUBACCOUNT_ADMIN, SECOND_DIRECTORY_ADMIN],
      directoryFeatures: ['DEFAULT', 'ENTITLEMENTS'],
      displayName: names.Directory,
    }, false),
    resource('Entitlement', accountAPI, names.Entitlement, {
      serviceName: 'cis', servicePlanName: 'local', enable: true,
      subaccountRef: { name: names.Subaccount },
    }),
    resource('DirectoryEntitlement', accountAPI, names.DirectoryEntitlement, {
      directoryRef: { name: names.Directory }, serviceName: 'cis', planName: 'local',
    }, false),
    resource('SubaccountApiCredential', securityAPI, names.SubaccountApiCredential, {
      readOnly: true, subaccountRef: { name: names.Subaccount },
    }),
  ];

  const client = xp.k8sClient();
  const created = [];
  const errors = [];
  try {
    for (const manifest of manifests) {
      const kind = manifest.kind;
      const name = manifest.metadata.name;
      const started = Date.now();
      let phase = 'create_request';
      let phaseStarted = Date.now();
      try {
        xp.measureOperation('create', kind, () => {
          xp.recordMeasurementPhase('create_request', 'requested', kind);
          client.create(manifest);
          // This confirms Kubernetes accepted the managed-resource object;
          // it does not mean the corresponding BTP resource exists.
          created.push(manifest);
          xp.xpResourcesCreated.add(1);
          recordPhase(kind, 'create_request', phaseStarted, 'success', 'none', 'accepted');

          phase = 'readiness';
          phaseStarted = Date.now();
          xp.recordMeasurementPhase('readiness', 'started', kind);
          xp.waitForReady(kind, name, {
            namespace: manifest.metadata.namespace || NS,
            apiVersion: manifest.apiVersion,
            timeout: READY_TIMEOUT,
            requireReady: true,
            ...(kind === 'SubaccountApiCredential'
              ? { onObservation: recordCredentialReadinessObservation }
              : {}),
          });
          recordPhase(kind, 'readiness', phaseStarted, 'success', 'none', 'observed');
        });
        xpTimeToReady.add(Date.now() - started, { resource_kind: kind });
      } catch (error) {
        const failure = failureCategory(error);
        recordPhase(kind, phase, phaseStarted, failure.outcome, failure.reason, 'failed');
        throw error;
      }
    }
  } catch (error) {
    errors.push(`create/readiness failed (${failureCategory(error).reason})`);
  } finally {
    for (const manifest of created.reverse()) {
      const kind = manifest.kind;
      const name = manifest.metadata.name;
      let phase = 'delete_request';
      let phaseStarted = Date.now();
      xp.recordMeasurementPhase('delete_request', 'requested', kind);
      try {
        xp.deleteAndWait(kind, name, {
          namespace: manifest.metadata.namespace || NS,
          apiVersion: manifest.apiVersion,
          timeout: DELETE_TIMEOUT,
          trendMetric: xpTimeToDelete,
          trendTags: { resource_kind: kind },
          onDeleteAccepted: () => {
            recordPhase(kind, 'delete_request', phaseStarted, 'success', 'none', 'accepted');
            phase = 'kubernetes_absence_wait';
            phaseStarted = Date.now();
            xp.recordMeasurementPhase('kubernetes_absence_wait', 'started', kind);
          },
          onKubernetesObjectAbsent: () => {
            recordPhase(kind, 'kubernetes_absence_wait', phaseStarted, 'success', 'none', 'observed');
          },
        });
      } catch (error) {
        const failure = failureCategory(error);
        recordPhase(kind, phase, phaseStarted, failure.outcome, failure.reason, 'failed');
        xp.xpResourcesFailed.add(1);
        errors.push(`delete failed for owned ${kind} (${failure.reason})`);
      }
    }
  }
  if (errors.length) throw new Error(errors.join('; '));
}
