# Shared Fieldwork service

The shared mode gives each ordinary Firebase Google or email/password account
one private workspace. Existing owner data stays in the original tables; this
change does not move records, rewrite IDs, reset revisions or reset change feeds.
There is no team sharing or user-selected workspace impersonation in this release.

## Boundaries

The edge verifies signed Firebase identity against the configured project. It
then signs a short-lived assertion bound to the final request method, path,
query and body. The backend verifies this with a dedicated first-party bridge
key, resolves the UID through a persisted workspace registry, and uses separate
PostgreSQL schemas. The bridge key is distinct from the legacy owner credential.
It never appears in an OAuth grant, client tool input or result.

Request headers, document IDs and metadata cannot choose a workspace. Missing
authenticated store context fails closed. Lists, counts, nested references,
updates, tombstones, change feeds and exports all use the same private store.
R2 keys use a private workspace prefix resolved from the authenticated backend;
knowing another user's image digest gives no access or existence information.

The legacy workspace token keeps access only to the original workspace. The
configured, verified Google owner can claim that workspace once. A password
account using the same email cannot claim it, and another UID cannot claim it
again. Other accounts start with empty workspaces.

Public OAuth uses `/oauth/authorize`, normal service sign-in, browser-bound
single-use consent, PKCE and `fieldwork:read` only. Each exchange and renewal
checks the signed service identity and matching refresh credential. The existing
Access-owner `/authorize` route and its grants remain compatible. New grants
forward their own user identity; they never borrow the owner's workspace token.
Read connections cannot expose editing or human-approval authority.

Browser drafts use the authenticated workspace receipt. Switching identity
reloads the editor, dropping old state and pending requests; a new user's draft
lookup never falls back to an old unscoped owner draft. Original owner/local
draft keys remain available only in the original workspace mode.

## Staged release

1. Preserve the existing database, R2 bucket, OAuth KV and Worker secrets. Take
   an access-restricted database backup before enabling shared mode. Never put
   backups, tokens or reviewer passwords in the repository or public upload.
2. Deploy the tested backend, editor and gateway with shared mode disabled.
   Recheck the original owner's editor, images, MCP and existing OAuth renewal.
3. Configure a new random `FIELDWORK_IDENTITY_BRIDGE_KEY` of at least 32 bytes
   on the editor Worker/container. It must differ from `ANNOTATION_TOKEN`.
   Keep `ANNOTATION_TOKEN` and existing OAuth KV unchanged.
4. Set `FIELDWORK_LEGACY_OWNER_EMAIL` to the existing verified Google owner on
   the editor. Enable normal email/password in the existing Firebase project;
   retain its existing Google provider and authorized domains.
5. Configure the gateway's public `FIREBASE_PROJECT_ID` and its Firebase web
   `FIREBASE_API_KEY`. The key is web configuration, not a user/admin credential.
6. Enable `FIELDWORK_SHARED_WORKSPACES=true` on both editor and gateway only
   after the deployed backend and new frontend are present. A running Container keeps its startup environment: activate with a new backend image rollout, then verify the live GetInfo workspace receipt before approving grants. Do not broaden the
   old owner-only Access policy: the new public authorization path is separate.
7. Use two new ordinary accounts and synthetic records to verify lists/counts,
   known foreign IDs, writes, exports, change feeds and image GET/HEAD/PUT. Verify
   the owner still sees the original workspace, then test a fresh native read
   grant, renewal and revocation before recording the public review demo.

Core document schema version remains 1. The additive registry is created at
shared-mode startup; new workspace tables are created transactionally. Do not
disable shared mode after users are onboarded without a maintenance plan: users
would lose access to their private workspaces. Never remap their identities onto
the old workspace as a fallback. Workspace data and private blobs need explicit
retention/deletion handling when servicing account-deletion requests; deleting a
Firebase login alone does not remove these records.

## Verification

Run `go test -race ./internal/annotation/...`, the annotation browser session
tests, `npm run build:annotations`, and `npm test`/`npm run typecheck` in this
directory. Set `ANNOTATION_TEST_DATABASE_URL` only to a disposable direct
PostgreSQL database. The PostgreSQL integration tests isolate and clean their
test schemas. The hosted boundary test additionally needs
`FIELDWORK_TEST_BACKEND_URL` pointing to a disposable loopback backend with the
documented synthetic fixture keys. CI starts that backend and PostgreSQL 18.

The runtime tests use locally signed Firebase fixtures and a real PostgreSQL
backend/R2 runtime, not a production Google or Cloudflare account. Successful
source tests do not establish production deployment, native tool exposure or
public submission.
