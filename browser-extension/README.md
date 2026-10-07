# Tin for Chrome

## Connect for the first time

1. In Tin, open **Integrations → LinkedIn → Connect**.
2. Choose **Install Tin for Chrome**, then **Add to Chrome → Add extension**. Open LinkedIn
   and sign in using the same Chrome profile. Return to Tin and refresh the tab.
3. Check the account shown, choose how Tin should collect, and click **Connect**.

Choose **Cloud with browser backup** to let Tin collect while Chrome is closed and use
Chrome when needed. **Cloud only** never collects results in Chrome. **This browser only**
keeps your login in Chrome and needs the browser open and awake during collection.

Once setup says cloud access is ready, start collections from Tin with **Manual run**.
You do not need to open the extension or approve each run. If Tin needs you to sign in
again, it will say so. Change the choice or disconnect under **Integrations → LinkedIn**.

## Development and release

This is version 0.4.1 of the existing published Tin extension, not a separate store item.
The store link requires a 0.4.1 release; merging this source does not update installed
extensions. See [store release](STORE_RELEASE.md).

For an operator pilot before the store update, open `chrome://extensions`, enable
**Developer mode**, choose **Load unpacked**, and select this `browser-extension` directory.
If it is already loaded, click its reload button instead. Refresh Tin, then follow step 3
above. The deployment must allowlist this installation's extension ID. Keep older diagnostic
copies disabled so you use one Tin connection.

The v3 bridge supports the new setup protocol while retaining the legacy v2 transport.
Pairing confirms retirement of this device's legacy Tin credential before clearing its
refresh state. It does not modify LinkedIn cookies. Older connections need to finish setup
once to grant continuing permission; existing single-run consent is not upgraded silently.
See the [integration contract](../docs/connection-collection.md) for limits and recovery.

```sh
npm test
# Run npm ci at the repository root first for Playwright.
npm run test:collection
npm run package:store
```
