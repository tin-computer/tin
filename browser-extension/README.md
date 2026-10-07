# Tin browser extension

This is the migration of the published Tin extension, with project-bound collection added
in version 0.3.0. It retains the published store name, legacy v2 protocol, browser-context
capture and deterministic package. The v3 bridge connects to Tin Lite. Explicit v3 pairing
confirms revocation of this device’s legacy Tin credential before clearing its refresh state; it does not modify LinkedIn cookies.

Load this directory unpacked for development. On an enabled project, open LinkedIn in the
same Chrome profile, then choose LinkedIn in Tin's Integrations page. Start **Collect
connections** in Tin and click **Continue collection** in the extension. Friend names and
filters come from that run. No profile is hardcoded.

The popup can close during local collection; Chrome must remain awake. Cloud transfer is
an explicit opt-in and requires a separately qualified deployment. See the [integration
contract](../docs/connection-collection.md) for setup, limits, recovery and verification.

```sh
npm test
# Run npm ci at the repository root first for the existing Playwright dependency.
npm run test:collection
npm run package:store
```

Updating the existing store item and its permission disclosures remains a separate release
operation. This source does not update an installed store extension. Do not upload diagnostic
extensions or private project configuration with the store archive.
