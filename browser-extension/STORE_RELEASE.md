# Release the Tin extension

Update the [existing Chrome Web Store item](https://chromewebstore.google.com/detail/tin-computer-for-linkedin/eanmnipacaadfahphbcijncgpfkdcbec).
Do not create a second item or upload an unpacked diagnostic build. The source version is
0.4.0; the installation screen requires its setup protocol.

1. Run the extension and collection tests, then `npm run package:store`. The packager emits
   a deterministic archive containing only the allowlisted runtime files and store manifest.
2. Update the existing listing and linked privacy disclosure to include visible connection
   collection and the optional encrypted session transfer described below. Older outreach-only
   wording does not describe this release.
3. Upload the archive to the existing item and submit it for review. Verify the published
   version and store ID before inviting new users. Store submission and approval are separate
   from merging or deploying Tin.
4. Allowlist that store ID on the designated deployment. Roll out the compatible backend,
   migration and dedicated collection image together. Existing installations stay on their
   previous protocol until the user finishes setup in Tin.

## What the release does

Users connect from Tin's Integrations page, confirm the LinkedIn account signed in to Chrome,
and choose cloud collection with browser backup, cloud only, or browser only. Starting a run
in Tin then authorizes collection of the selected profiles' visible second-degree results.
The extension automatically picks up that run; it does not send messages or request intros.

Local collection reads rendered LinkedIn pages and uploads the selected results to the
user's project. Cloud collection sends an allowlisted set of LinkedIn cookies, user agent,
request language and client context directly to Tin over HTTPS after that user enables it.
Tin encrypts the session, keeps it for at most seven days (less when the login or device
expires), and refreshes it while the extension is available. Temporary compute is deleted
independently of this saved session. Neither path signs the user out or writes cloud cookies
back to Chrome. Disconnecting removes saved credentials and invalidates collection access.

The update uses the existing cookies, storage, webRequest, scripting and alarms permissions
and existing Tin/LinkedIn host access. There are no additional Chrome permissions. The Tin
setup confirmation records project, account and collection mode; it is separate from Chrome's
installation permission prompt. Raw login material never enters project files, chat or MCP.
Collected results are project artifacts and remain until the user deletes them.

Use synthetic accounts and results for listing screenshots. Keep customer names, private
workflow recipes, session values, test credentials and acceptance logs out of the archive,
listing and source tree.
