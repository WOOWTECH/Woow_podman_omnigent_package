# Native MCP package upgrade and full rollback

Package **0.21.0-3** targets the WOOW native MCP server on upstream **0.21.0**. The host stays
0.21.0, Pi stays 0.85.1, and the existing runner base stays unchanged. Native MCP uses the
existing server port and PostgreSQL/artifact volumes. Run host commands as the normal user
who owns the rootless installation.

The package pins the generic server artifact from source
`459340b0d9516fd3ecb4c35a95eb6e17cc3a5340`, CI run `44896274348` attempt `1`.
`config/server-image.json` is the unchanged producer receipt for both architectures; its
SHA-256 is `c8e3fabb5b70ea4405edf2265324abb9fd9723a97e634cbefc6a0826a32d10c0`.
The pinned index digest is
`sha256:7627b7a4dff2c16cbec16e8b0198b04078cbc0c4f102e44fcdd1b6e77d919f8e`.
Complete the rootless acceptance procedure below before rollout. Merge/release/deployment
remain separately coordinated actions.

## Verify the image pin

The generic server `tag@sha256` is recorded in `quadlet/omnigent-server.container`.
Keep the previous pin in the release evidence. For
0.21.0-2 the repository pin was:

```text
ghcr.io/omnigent-ai/omnigent-server:v0.21.0@sha256:9bba2964f015ddbb61da0924267effd1801c0db8f44ecec95f401f24d9a3f90c
```

For future artifact updates, copy the producer's **generic server** `candidate-image.json`
unchanged to `config/server-image.json` and set Quadlet `Image=` to the receipt's
`tag` + `@` + `imageDigest`. The `image` field is digest-only; `tag` separately identifies
source and CI run. Use an actual producer receipt for each update.

The verifier consumes the producer's schema: `schemaVersion`, `publishable`, `component`, `variant`,
`coreVersion`, `sourceCommit`, `baseImage`, `wheelSha256`, `uiSha256`, `contentSha256`,
`contentManifestSha256`, `architecture`, `platforms`, `tag`, `image`, `imageDigest`, `runId`,
`runAttempt` and `ci`. It rejects local snapshot receipts and Kubernetes server receipts.
Both architecture digests must be present, with a matching source/run identity.
The generic and Kubernetes variants share `ghcr.io/woowtech/omnigent-server-kubernetes`;
Podman requires `component=server`, `variant=server`, the `-server-` tag and the exact generic
`baseImage`. A repository name containing `kubernetes` does not select the runtime variant.
The actual image's base-image label must exactly match the receipt, including its digest.

Runtime verification uses `/opt/venv/bin/python` and the producer's
`/opt/woow-artifact/verify.py runtime --artifact /opt/woow-artifact --source-commit ... --debian-security --image-variant server`.
That verifier checks wheel/manifest/UI hashes, installed file contents, imports and security
fixes. The package then compares `/opt/woow-artifact/WOOW_BUILD_RECEIPT.json` with its checked-in
receipt and verifies the actual image digest and OCI revision. No production config or
receipt is synthesized inside a running container.

```bash
python3 scripts/verify-server-image.py
bash tests/dryrun.sh
bash tests/runner-login.sh
bash tests/mcp-render.sh
python3 -m unittest discover -s tests -p 'test_package.py' -v
# Disposable image-only probe: no mounted data, no network, no entrypoint migrations.
python3 scripts/verify-server-image.py --image --platform linux/amd64
python3 scripts/verify-server-image.py --image --platform linux/arm64
```

The image probes need the corresponding architecture or emulation. CI runs both with QEMU.
The static receipt check does not prove a running image; retain the separate runtime result.
The install gate rejects an absent or inconsistent receipt before changing any host state.

## Before the maintenance window

Record the exact authorized host and rootless user, installed package/checkout commit,
installed units, server/runner/PostgreSQL image IDs and registry digests, Pi CLI version,
PostgreSQL migration head, public URL/proxy configuration, and private/shared Pi mode.
Inspect only selected image fields: full container inspection can contain secrets.

```bash
id -u
podman info --format '{{.Host.Security.Rootless}}'
systemctl --user is-active omnigent-postgres omnigent-server omnigent-runner
podman exec omnigent-runner pi --version
podman exec omnigent-postgres psql -U omnigent -d omnigent -Atc 'SELECT version_num FROM alembic_version'
```

Keep local copies of the old immutable images. For locally built runner images, preserve
the old local tag and image ID; export the old image if rollback must work offline. Keep
custom proxy configuration and any externally managed cookie secret in the same private
checkpoint. Automatic capture refuses package unit drop-ins because they need a separately
verified capture/restore procedure; reconcile those before this maintenance window. The standard package's generated cookie secret is
`/data/accounts-cookie-secret` on `omnigent-server-data`.

Use synthetic accounts and sessions for the rehearsal. Record counts/identities of accounts,
private/shared sessions, items, paused automations, agents, artifacts and profiles without
putting passwords or token URLs in evidence. For an MCP A→B upgrade, record one valid and one
revoked profile; the original upstream 0.21.0-2 has no MCP profiles to preserve.

## Take the checkpoint and upgrade

Block new external writes in the existing proxy/maintenance mechanism, let active work
finish, and record paused automation state. Do not run secret rotation concurrently. Stop
writers before taking the database dump and artifact snapshot; PostgreSQL remains running.
Stopping them explicitly keeps them stopped after `backup.sh --stop`. The script also stops
both writer units unconditionally, including activating/auto-restart states, verifies no
active unit, process, queued job or running writer container remains, and records the prior
states in `writer-states.tsv`. Only previously running/starting units are resumed, including
on a dump/export failure. The package lock serializes backup, restore, install and rotation.

The env must describe the currently installed units when taking this checkpoint. Apply any
new MCP public URL after the old checkpoint is secured; do not edit the old env and leave its
rendered units inconsistent before running upgrade.

```bash
systemctl --user stop omnigent-server-health.timer omnigent-runner.service omnigent-server.service
scripts/upgrade.sh
```

`upgrade.sh` saves installed package units and the install manifest under
`~/.local/state/woow-quadlet/omnigent/rollback/<timestamp>/`, then runs
`backup.sh --stop --include-secrets`. Record the exact snapshot and backup directory printed
by the command; do not select a rollback point merely by sorting timestamps. If the host
uses `QL_STATE_ROOT`, its rollback directory is beneath that location instead.

The backup includes the transactional PostgreSQL custom dump, server-data archive
(artifacts, settings, cookie secret), env file, installed package units under `units/`, their
hashes and Pi mode in `checkpoint.json`, private Pi archive when in private mode, and the
matching database/admin/operator Podman secrets in `secrets.json`. JSON preserves whitespace,
` #`, quotes, backslashes, Unicode and embedded/trailing newlines; it is not sourced as env
syntax. Files are 0600 inside a 0700 directory. Check the dump, archive and secrets `.sha256`
files and preserve the full directory privately. `pi-agent-data` is owned by the sibling Pi package and is never exported
or restored here; coordinate its own checkpoint with its owner if needed.

After a successful upgrade, start the health timer if it was not already started by install,
then complete acceptance before reopening external writes. On failure, upgrade restores the
old units and **leaves application writers stopped**, printing the matching data-restore
command. The restored units are not evidence that the old application can use the new schema.

## Full rollback

Migration `za3b4c5d6e7f` adds the MCP profile schema after `d0a1f879a01d`. The old server image
does not know that revision. Do not manually edit `alembic_version`, attempt an untested
schema downgrade, or run `converge.sh --rollback` as a database rollback. That command only
restores units and immediately restarts services.

1. Keep maintenance blocking external writes. Stop the runner, server and health timer.
2. Select the recorded pre-upgrade backup and verify its checksums. Preserve the current
   failed state separately if needed. Restoration discards writes after that checkpoint.

   ```bash
   read -r -p 'Recorded pre-upgrade data backup directory: ' rollback_backup
   test -f "$rollback_backup/checkpoint.json"
   ```
3. Restore the exact saved old unit files and install manifest while writers remain stopped.
   If `upgrade.sh` already failed and restored its snapshot, verify those files rather than
   replacing them again. For a rollback after a successful upgrade, use its recorded snapshot:

   ```bash
   set -euo pipefail
   read -r -p 'Recorded upgrade unit snapshot directory: ' rollback_snapshot
   state_dir=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/omnigent
   test -f "$rollback_snapshot/manifest" && test -d "$rollback_snapshot/files"
   systemctl --user stop omnigent-server-health.timer omnigent-runner.service omnigent-server.service
   # Remove only package files introduced after the snapshot, using both package manifests.
   while read -r checksum path; do
     grep -qF -- "  $path" "$rollback_snapshot/manifest" || rm -f -- "$path"
   done <"$state_dir/manifest"
   while IFS= read -r -d '' saved; do
     target=${saved#"$rollback_snapshot/files"}
     mkdir -p -- "${target%/*}"
     cp -p -- "$saved" "$target"
   done < <(find "$rollback_snapshot/files" -type f -print0)
   cp -p -- "$rollback_snapshot/manifest" "$state_dir/manifest"
   rm -f -- "$state_dir/pending-restart"
   systemctl --user daemon-reload
   ```

4. Restore the saved env while writers remain stopped. Confirm the restored units reference
   the recorded old image IDs/digests. Restore custom proxy configuration and any external
   cookie secret from the same checkpoint. Do not run candidate `install.sh`, which would
   reinstall new units. Validate that the old unit snapshot and data checkpoint match:

   ```bash
   install -m 600 -- "$rollback_backup/omnigent.env" "$HOME/.config/omnigent/omnigent.env"
   python3 scripts/backup-checkpoint.py check --backup "$rollback_backup" \
     --quadlet-dir "${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}" \
     --systemd-dir "${QL_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}" \
     --env "$HOME/.config/omnigent/omnigent.env"
   ```

   This check requires byte-for-byte matching units/env and the same private/shared Pi mode.
   A mismatched snapshot, missing private Pi unit/archive, or an added unit drop-in must be
   resolved before restoration. Do not bypass the check or render units from the new checkout.
5. Run the data restore against the recorded checkpoint:

   ```bash
   scripts/restore.sh "$rollback_backup" --with-secrets
   ```

   Before stopping services or deleting data, restore validates the checkpoint unit/env
   hashes, all required secrets, archive checksums and tar contents, and the PostgreSQL dump
   catalog. Missing, malformed or incomplete passwords abort before any data mutation. It
   can read legacy raw `secrets.env` without treating ` #` as a comment, but backups lacking
   `checkpoint.json` and the recorded units are refused; use the previously verified restore
   procedure for such historical backups, with their independently recorded matching units.

   Restore then stops/verifies both writers, reloads the verified old units, restores the
   database and server/private Pi data, restores the three passwords and derived database
   URL, and starts the old services. The full server archive retains the cookie secret and
   artifact/config checkpoint. PostgreSQL stays on its old pinned image. The health timer is
   started **before** the final smoke test, including when maintenance stopped it earlier.
   No units or env are rendered or overwritten by restore. The command asks for confirmation
   before replacing current data; do not add `--yes` without restoration authorization.
6. Verify the old image identity and migration head, original account passwords, sessions,
   items, artifacts, settings, paused automation state and runner registration. Confirm
   private Pi still works and the shared Pi owner's container ID/start time are unchanged.
   An upstream 0.21.0-2 rollback removes MCP functionality and any profiles created later.
   A rollback between MCP builds must preserve the checkpoint's active/revoked states.
7. Confirm `omnigent-server-health.timer` is active, reopen external writes, and record the backup ID,
   image identity and restore result. Keep the release coordinator's receipt private where
   it contains deployment identifiers; publish only sanitized build/test evidence.

## Rootless acceptance evidence

Run these on the explicitly authorized candidate host, for both private and shared Pi modes:

- Upgrade an old data copy; compare accounts/passwords, sessions/shares/items, paused
  automations, agents, artifacts, cookie secret and database head with the baseline.
- Run `tests/smoke-container.sh`, `tests/smoke-pi-integration.sh`,
  `tests/smoke-runner-dialin.sh`, and `tests/e2e/` against the candidate's real endpoint.
  The MCP spec takes `OMNIGENT_MCP_PUBLIC_BASE_URL` and checks the API-generated canonical
  URL from a client with no login cookie, then revokes its own profile.
- Verify all tools from the live schema, including `omnigent_session_history`; read the
  100-round session as 200 messages with pagination and check account/share revocation ACLs.
- Keep one active and one revoked MCP profile privately. Restart the server/runner and then
  cold boot the rootless host in its approved window; confirm valid still succeeds and
  revoked still fails. Repeat across an MCP A→B upgrade. Never attach token URLs to reports.
- Check Pi reports **0.85.1**, host reports **0.21.0**, private state remains usable, and shared
  Pi credentials/files and its owner's container ID/start time remain unchanged. No Pi SDK
  or shared data-format migration is included in this server update.
- Rehearse the full rollback on the candidate copy and verify the restored baseline. Record
  actual receipts and failures. Mock tests, static rendering and unavailable hosts do not
  count as container, rootless boot, upgrade, profile-persistence or restore acceptance.
