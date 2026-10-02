import * as xp from './crossplane-helpers.js';

export const options = {
  scenarios: {
    create_delete: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '1h',
    },
  },
};

const SETTINGS = {
  readyTimeout: Number(__ENV.XP_DIADROMOS_BTP_READY_TIMEOUT || '600'),
  deleteTimeout: Number(__ENV.XP_DIADROMOS_BTP_DELETE_TIMEOUT || '600'),
  region: __ENV.XP_DIADROMOS_BTP_REGION || 'eu10',
  subaccountAdmin: __ENV.XP_DIADROMOS_BTP_SUBACCOUNT_ADMIN,
  secondDirectoryAdmin: __ENV.XP_DIADROMOS_BTP_SECOND_DIRECTORY_ADMIN,
  runId: __ENV.XP_DIADROMOS_BTP_RUN_ID || __ENV.GITHUB_RUN_ID,
  attempt: __ENV.GITHUB_RUN_ATTEMPT || '1',
};

const ACCOUNT_API = 'account.btp.sap.crossplane.io/v1alpha1';
const SECURITY_API = 'security.btp.sap.crossplane.io/v1alpha1';

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

  const names = xp.buildResourceNames(runId, attempt);
  const subaccount = xp.managedResource('Subaccount', ACCOUNT_API, names.Subaccount, {
    displayName: names.Subaccount,
    region,
    subdomain: names.subdomain,
    subaccountAdmins: [subaccountAdmin],
  });
  const directory = xp.managedResource('Directory', ACCOUNT_API, names.Directory, {
    description: `xp-diadromos benchmark ${names.suffix}`,
    directoryAdmins: [subaccountAdmin, secondDirectoryAdmin],
    directoryFeatures: ['DEFAULT', 'ENTITLEMENTS'],
    displayName: names.Directory,
  });
  const entitlement = xp.managedResource('Entitlement', ACCOUNT_API, names.Entitlement, {
    serviceName: 'cis',
    servicePlanName: 'local',
    enable: true,
    subaccountRef: { name: names.Subaccount },
  });
  const directoryEntitlement = xp.managedResource('DirectoryEntitlement', ACCOUNT_API, names.DirectoryEntitlement, {
    directoryRef: { name: names.Directory },
    serviceName: 'cis',
    planName: 'local',
  });
  const apiCredential = xp.managedResource('SubaccountApiCredential', SECURITY_API, names.SubaccountApiCredential, {
    readOnly: true,
    subaccountRef: { name: names.Subaccount },
  }, { connectionSecret: names.connectionSecret });

  // Parent resources are Ready before their dependent allocations/credential.
  return [subaccount, directory, entitlement, directoryEntitlement, apiCredential];
}

export default function () {
  xp.runOwnedResourceLifecycle({ buildManifests: buildResourceSet, settings: SETTINGS });
}
