# Disposable guest coldboot acceptance

`rootless-coldboot.yml` adds one PR-only Ubuntu 24.04 amd64/private job, bounded to
45 minutes. It reuses the real installer, image receipt validator and
`tests/rootless-mcp.py` fixtures. It does not change or repeat the completed native
amd64/arm64 × private/shared upgrade and rollback matrix.

## What a passing receipt proves

1. Checkout is the actual PR head SHA, with no tracked changes. A `git archive`
   carries that commit in its header. The guest checks both that header and the
   archive SHA-256 before extraction, and checks that its controller script is
   byte-identical to the reviewed source. The installer consumes the existing
   `config/server-image.json` and immutable Quadlet pin. It builds the runner from
   this source, retaining Pi 0.85.1 and the existing runner base.
2. The installer creates the real rootless package and random secrets in a fresh
   guest. The fixture creates one active and one revoked native MCP profile,
   exercises the 15-tool contract, and saves their original URLs privately. It
   records hashes of all four Podman secrets and the cookie secret, and writes
   server-artifact and private-Pi markers. Initial smoke includes the health timer
   and runtime image/source receipt.
3. The controller requests an orderly **guest poweroff**, waits for QEMU to exit
   successfully, then starts a **new QEMU process on the same owned persistent
   disk**. A reboot request, snapshot reset or service restart is insufficient.
4. Before any package-user command on the second boot, root observes a changed
   kernel boot ID, unchanged machine ID, an already-active `user@1100.service`,
   `Linger=yes`, no package-owner login sessions, and HTTP `/health` returning
   `{"status":"ok"}` on guest loopback. Failure at any gate prevents subsequent
   package-user probes.
5. Queries through `setpriv` then confirm all three services and the health timer
   are active, Podman is rootless with cgroups v2, the original active MCP token
   works and the revoked token remains rejected, both profile records remain,
   all secret/cookie hashes and both volume marker hashes match, and the runtime image
   receipt remains correct. The fixture file itself must have its original hash.
   `setpriv` does not create a PAM/login session. No manual service start or
   package-owner login occurs after poweroff, including during verification.
6. Final guest poweroff succeeds, the owned QEMU process exits, the private disk,
   keys and logs are deleted, and the original KVM device ACL is restored.
   Cleanup failure fails the job even if retention passed.

This establishes **guest coldboot** for the tested Ubuntu/KVM environment. It does
not establish physical target firmware, real power-loss recovery, storage-device
flush behavior, host networking, shared Pi startup order, or arm64 guest startup.
The outer GitHub runner is never rebooted. Paid models are not called. A real
physical-host coldboot remains a separate deployment acceptance criterion.

## Ownership, isolation and prerequisites

- Only a `pull_request` event on a GitHub-hosted amd64 runner is accepted. There
  is no manual dispatch, publication, host-address or external-disk parameter.
- A 0700 directory beneath validated `RUNNER_TEMP` holds everything. Its UID,
  inode, parent and nonce are checked before VM use/deletion. QEMU is controlled
  through its own process handle; there is no global `pkill` or host inventory.
- The guest has a fresh NoCloud instance ID, matching QEMU UUID and root-owned
  nonce marker. Preparation rejects existing package state, units, container
  storage, linger state or runtime directory. The package account has a locked
  password, a `nologin` shell and no SSH key. Only the separate controller account
  is allowed SSH; it can use root inside this disposable guest.
- Fresh SSH client and host keys are generated per run. Strict checking against
  the injected host key is mandatory on both boots. No `ssh-keyscan`/trust on
  first use. QEMU exposes only an SSH forward bound to host `127.0.0.1`; application
  requests execute inside the guest against its own loopback.
- The dated Ubuntu image and real SHA-256 are in
  [`tests/fixtures/coldboot-image.json`](../tests/fixtures/coldboot-image.json).
  Both the official dated checksum entry and downloaded bytes must match that
  committed hash. Floating images, redirects, duplicate checksum entries and
  extra backing files are refused. Updating the pin requires reviewing another
  real dated official checksum, never substituting a guessed digest.
- Preflight grants only the current runner UID read/write access to `/dev/kvm`
  with an ACL, then requires KVM API version 12 and a successful `KVM_CREATE_VM`
  ioctl. Lack of group membership alone must not cause a false failure. Missing
  KVM, a rejected ioctl or unavailable resources fails closed: no TCG fallback.

## Resource and time bounds

After VM tooling installation the host must have **12.5 GiB free disk** and
**7 GiB available memory**. The VM receives 2 vCPUs, 6 GiB RAM and an 11 GiB virtual
qcow2 disk with a read-only verified base (download capped at 800 MiB). This keeps
the declared disk allocation plus base and reserve below a 13–14 GiB host disk
budget. The controller checks at least 512 MiB host reserve while booting.
These are fail-closed limits, not a claim that every hosted-runner image or future
container size fits. Guest disk exhaustion must fail; the job does not delete
unrelated host tools, enlarge onto another disk or hide insufficient resources.

Tooling operations, image transfer, SSH, guest installation, boot and shutdown
have individual timeouts. The controller has a 36-minute command deadline and a
37-minute alarm, leaving room within the 45-minute job for cleanup and receipt
upload. GitHub's nested virtualization availability can vary; a failed preflight
is evidence of an unmet prerequisite, never a coldboot pass.

## Evidence and local checks

The only uploaded file is `omnigent-coldboot-result.json`. It includes actual and
reviewed package SHAs, archive hash, pinned server source/digest, Ubuntu image pin,
completed stages, boot IDs plus hashed machine ID, explicit proof booleans,
static failure codes and cleanup outcome. Tokens, passwords, fixture JSON, keys,
serial output, command output and disk images are never uploaded or printed.
The generated boot identities belong only to this disposable guest.

Run offline contracts with Python 3.12 (the Ubuntu 24.04 guest runtime):

```sh
python3.12 -m unittest discover -s tests -p test_rootless_coldboot.py -v
```

They exercise checksum/source rejection, ownership guards, KVM ioctl handling,
strict SSH identity, and the ordering that prevents any package-user probe when
boot identity, user manager or health checks fail. They do not launch a VM.
A passing offline suite does not replace the real PR job's sanitized receipt.

Primary references: [Ubuntu dated image and checksums](https://cloud-images.ubuntu.com/noble/20261017/),
[NoCloud local images](https://ubuntu.com/docs/public-images/public-images-how-to/use-local-cloud-init-ds/),
[systemd linger](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html),
and [GitHub-hosted runner limitations](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners).
