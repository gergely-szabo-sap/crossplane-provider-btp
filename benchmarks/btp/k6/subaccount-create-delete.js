import * as xp from './crossplane-helpers.js';

export const options = {
  scenarios: {
    create_delete: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '110m',
    },
  },
  thresholds: {
    xp_lifecycle_success: ['rate==1'],
  },
};

const SETTINGS = {
  readyTimeout: Number(__ENV.XP_DIADROMOS_BTP_READY_TIMEOUT || '600'),
  deleteTimeout: Number(__ENV.XP_DIADROMOS_BTP_DELETE_TIMEOUT || '600'),
  createBudget: Number(__ENV.XP_DIADROMOS_BTP_CREATE_BUDGET || '2700'),
  cleanupBudget: Number(__ENV.XP_DIADROMOS_BTP_CLEANUP_BUDGET || '3000'),
  region: __ENV.XP_DIADROMOS_BTP_REGION || 'eu10',
  subaccountAdmin: __ENV.XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN,
  secondDirectoryAdmin: __ENV.XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN,
  runId: __ENV.XP_DIADROMOS_BTP_RUN_ID || __ENV.GITHUB_RUN_ID,
  attempt: __ENV.XP_DIADROMOS_BTP_RUN_ATTEMPT || '1',
};

const ACCOUNT_API = 'account.btp.sap.crossplane.io/v1alpha1';
const SECURITY_API = 'security.btp.sap.crossplane.io/v1alpha1';

// Five independent parent/child sets are retained until every resource is Ready.
const INSTANCES_PER_KIND = 5;

function buildResourceSet(settings) {
  const {
    subaccountAdmin,
    secondDirectoryAdmin,
    runId,
    attempt,
    region,
  } = settings;
  if (!subaccountAdmin) throw new Error('XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN is required');
  if (!secondDirectoryAdmin) throw new Error('XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN is required');
  if (subaccountAdmin.toLowerCase() === secondDirectoryAdmin.toLowerCase()) {
    throw new Error('Directory admins must be distinct');
  }

  const sets = Array.from({ length: INSTANCES_PER_KIND }, (_, index) => {
    const names = xp.buildResourceNames(runId, attempt, index + 1);
    return {
      subaccount: xp.managedResource('Subaccount', ACCOUNT_API, names.Subaccount, {
        displayName: names.Subaccount,
        region,
        subdomain: names.subdomain,
        subaccountAdmins: [subaccountAdmin],
      }),
      directory: xp.managedResource('Directory', ACCOUNT_API, names.Directory, {
        description: `xp-diadromos benchmark ${names.suffix}`,
        directoryAdmins: [subaccountAdmin, secondDirectoryAdmin],
        directoryFeatures: ['DEFAULT', 'ENTITLEMENTS'],
        displayName: names.Directory,
      }),
      entitlement: xp.managedResource('Entitlement', ACCOUNT_API, names.Entitlement, {
        serviceName: 'cis',
        servicePlanName: 'local',
        enable: true,
        subaccountRef: { name: names.Subaccount },
      }),
      directoryEntitlement: xp.managedResource('DirectoryEntitlement', ACCOUNT_API, names.DirectoryEntitlement, {
        directoryRef: { name: names.Directory },
        serviceName: 'cis',
        planName: 'local',
      }),
      apiCredential: xp.managedResource('SubaccountApiCredential', SECURITY_API, names.SubaccountApiCredential, {
        readOnly: true,
        subaccountRef: { name: names.Subaccount },
      }, { connectionSecret: names.connectionSecret }),
    };
  });

  // Kind-major order makes every parent Ready before any dependent child request.
  return [
    ...sets.map((set) => set.subaccount),
    ...sets.map((set) => set.directory),
    ...sets.map((set) => set.entitlement),
    ...sets.map((set) => set.directoryEntitlement),
    ...sets.map((set) => set.apiCredential),
  ];
}

export default function () {
  xp.runOwnedResourceLifecycle({ buildManifests: buildResourceSet, settings: SETTINGS });
}
