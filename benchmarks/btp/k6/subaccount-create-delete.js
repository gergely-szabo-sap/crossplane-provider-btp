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
