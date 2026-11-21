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
   keys and logs are deleted, pending device events settle, and the original KVM
   device ACL is restored.
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
  The owner SSH guard permits only absence, an owner-owned mode-0700 directory
  with no entries, or that directory containing solely an owner-owned mode-0600,
  regular, single-link, zero-byte `authorized_keys`. It rejects every nonzero byte,
  unexpected entry, symlink, unsafe mode or wrong owner and never deletes content.
  Root also verifies the account password is locked and global `sshd -T` output
  permits only `coldboot-controller`, with password, keyboard-interactive and root
  login disabled. This global check does not evaluate connection-specific `Match`
  blocks. These queries do not log in or start services as the owner.
  Empty files can be created by cloud-init's
  [fingerprint module](https://github.com/canonical/cloud-init/blob/24.1/cloudinit/config/cc_ssh_authkey_fingerprints.py)
  through its [SSH path helper](https://github.com/canonical/cloud-init/blob/24.1/cloudinit/ssh_util.py),
  even when the user has no authorized keys.
- The guest keeps APT recommendations disabled and explicitly installs `netavark`
  and `aardvark-dns`. Noble lists the DNS helper as a
  [recommendation](https://packages.ubuntu.com/noble/netavark), and it is absent
  from the [dated base-image manifest](https://cloud-images.ubuntu.com/noble/20261017/noble-server-cloudimg-amd64.manifest).
  The stack uses container names for server/database and runner/server traffic.
  Netavark 1.4 [skips DNS startup when the helper is absent](https://github.com/containers/netavark/blob/v1.4.0/src/commands/setup.rs#L113-L148).
  Both Ubuntu helper binaries must execute `--version` before source extraction,
  linger setup or the installer; a missing/failing helper stops at
  `guest-network-tools`. This catches the dependency omission early. A passing
  helper check alone does not prove name resolution or explain a historical
  health timeout; the actual installer and two-boot acceptance must still pass.
- Fresh SSH client and host keys are generated per run. Strict checking against
  the injected host key is mandatory on both boots. No `ssh-keyscan`/trust on
  first use. QEMU exposes only an SSH forward bound to host `127.0.0.1`; application
  requests execute inside the guest against its own loopback. The virtio NIC uses
  `romfile=` because this guest boots from disk and does not use PXE.
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
- Immediately before **each** QEMU launch (probe, first guest, second guest), a
  bounded `udevadm settle --timeout=5` drains pending device events; the command
  itself has a six-second timeout. The controller then reapplies only the current
  UID's read/write ACL and snapshots effective access after that final grant.
  Device inode, device number, owner and group must still match. A failed settle,
  grant, identity or access check prevents launch. The original ACL is saved
  once, with the original device identity captured before the first grant, and
  never overwritten by these refreshes. Cleanup settles pending events after
  stopping the owned processes, then revalidates that original device identity
  before restoring its ACL. A missing, replaced or symlinked device skips the ACL
  write and fails cleanup with `cleanup-kvm-identity`; owned-file cleanup still
  runs. A settle failure still attempts restoration when identity matches and
  fails the job. No udev rules, persistent group membership or device-wide
  permissions are changed. There is no retry of a failed QEMU process.
- Immediately after that ioctl, before downloading the image, an owned QEMU
  process must remain alive for three seconds with the same binary/default
  machine, KVM accelerator, host CPU, two vCPUs and 6 GiB RAM as the real boot.
  This probe uses `-S`, no default devices, no images and no network. Its separate
  process handle is terminated and reaped before preparation continues (five
  seconds for termination, then five seconds after a forced kill if needed).
  Early exit uses the same safe operation/errno diagnostics. A passing
  `qemuKvmProbeVerified` proves only this startup probe, never guest coldboot or
  retention. The full guest poweroff/new-process/retention checks remain required.
- If that QEMU probe reports `open` / `EACCES`, a failure-only comparison repeats
  the ioctl probe in the controller and a fresh Python child, checks whether the
  device identity/permissions and granted ACL changed, and records effective
  read/write access before and after the failure. It checks whether PATH selected
  the system QEMU binary and hashes its resolved regular file. A bounded kernel
  journal query checks for an AppArmor denial of `/dev/kvm` tied to that owned
  QEMU PID. Only fixed statuses, booleans and the binary SHA-256 enter
  `qemuAccessDifferential`; raw ACLs, profile names and kernel logs stay private.
  Missing diagnostics are `unavailable`/null; `not-observed`
  is not proof that confinement is absent. The job stays failed, without changing
  permissions, disabling confinement or launching QEMU with extra privileges.

## ACL lifetime evidence

A failed hosted-runner receipt showed successful initial Python `KVM_CREATE_VM`,
then QEMU `open` / `EACCES`. Both subsequent Python probes also returned `EACCES`.
The device identity/mode/ownership stayed unchanged, its ACL changed, and effective
access went from true to false. This establishes ordinary access loss, not a
QEMU-specific failure. The exact actor changing the ACL remains unproven.

A source-supported explanation is that the harness's own create/close probe
queues device-change events: [Linux KVM emits change events on VM creation and
destruction](https://github.com/torvalds/linux/blob/v6.11/virt/kvm/kvm_main.c).
On systems that tag KVM for `uaccess`, [systemd's uaccess handler resets named-user
ACLs when there is no active seat](https://github.com/systemd/systemd/blob/v255/src/udev/udev-builtin-uaccess.c).
This explains why a grant checked before the ioctl need not survive until the
next launch; it is an inference from source and the receipt, not a captured
runner event trace. The bounded settle/grant sequence addresses that lifetime.

`kvmAccessRefreshes` records only the fixed phase, effective-access booleans and
an ACL change category (`unchanged`, `named-user-removed`, `named-user-changed`,
`mask-changed`, or `other-change`). These compare the previous grant with the
post-settle state, before refreshing it. Raw ACLs never leave the private logs.
A successful refresh or offline test is not a coldboot pass: the actual startup
probe and full two-boot guest acceptance must still succeed.

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
static failure codes and cleanup outcome. An early QEMU exit records its numeric
exit status and an allowlisted fixed diagnostic summary from QEMU stderr. Unknown
stderr stays withheld; the guest serial/cloud-init stream is never a diagnostic
source. KVM failures additionally record fixed operation names and errno symbols
matched against complete known C-locale messages. This distinguishes opening the
device, creating a VM, IRQ-chip setup and final initialization without copying
stderr text. An unknown reason is `withheld`; a successful preflight ioctl alone
does not prove that QEMU initialized successfully. Tokens, passwords, fixture JSON,
keys, serial output, command output and disk images are never uploaded or printed.
The generated boot identities belong only to this disposable guest.

Guest preflight failures identify the failing guard through fixed
`guest-guard-*` codes: runtime, arguments, marker file, marker nonce, DMI UUID,
cloud instance ID, release, owner account, owner home, owner SSH state, controller
account, locked password or global SSH configuration. The checks remain
mandatory before preparation or any package-owner command. Values read by those guards and raw exception text are never reported.
The receipt includes only a fixed `ownerSshState` category (such as `absent`,
`empty-directory`, `empty-authorized-keys`, or a rejection category) and
`ownerSshIsolationVerified`. A successful guest response requires an allowed empty
state and verified isolation. No key bytes, unexpected filenames or raw SSH
configuration enter the receipt. An earlier receipt identified only path
presence; real acceptance must establish the actual safe state.

Run offline contracts with Python 3.12 (the Ubuntu 24.04 guest runtime):

```sh
python3.12 -m unittest discover -s tests -p test_rootless_coldboot.py -v
```

They exercise checksum/source rejection, ownership guards, KVM ioctl handling,
ACL loss after ioctl/close, bounded refresh before all three QEMU launches,
original-ACL restoration including failure paths, strict SSH identity, and the
ordering that prevents any package-user probe when boot identity, user manager or
health checks fail. They do not launch a VM.
A passing offline suite does not replace the real PR job's sanitized receipt.

Primary references: [Ubuntu dated image and checksums](https://cloud-images.ubuntu.com/noble/20261017/),
[NoCloud local images](https://ubuntu.com/docs/public-images/public-images-how-to/use-local-cloud-init-ds/),
[systemd linger](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html),
and [GitHub-hosted runner limitations](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners).

### Installer failure evidence

If the guest installer exits unsuccessfully, its known fatal messages map to fixed
`install-*` failure codes (render, runner build, image pull, service start/reload,
HTTP/container health, admin bootstrap, operator provisioning or smoke). Unknown
messages remain `install`; the 840-second installer deadline reports
`install-timeout`. `installerFailure` includes only that fixed code, the integer
exit status (null for timeout), and a fixed error signature: `storage-full`,
`permission-denied`, `dns`, `registry-rate-limit`, `network-timeout`, `multiple`, or
`unclassified`. Signatures mean those strings occurred in private output; they do
not establish a root cause. Unknown fields are discarded at the host boundary.
Raw stdout/stderr, commands, paths, URLs and exceptions remain private and are
removed with the owned guest disk. Failure is never retried or converted to a
coldboot pass. The installer and the two-boot acceptance sequence are unchanged.


Only a first-boot `prepare` failure at `install-http-health` also gathers
`installRuntime`, before cleanup: fixed service/container states, exit codes and
OOM flags for the three owned components; PostgreSQL readiness; network backend
and DNS-enabled flag; helper-executable presence; and database-name resolution
inside the owned server container. Ten read-only commands share a 20-second
budget, with at most two seconds per command. No owner login, service start,
restart, journal read, credential inspection, raw log upload or repair is performed.
The resolver prints no addresses. Missing containers, timeout or malformed output
remain `unavailable`/null. The host reconstructs the fixed schema and discards
unknown fields. Snapshot failure preserves the original acceptance failure and
full cleanup. This snapshot never runs during second-boot verification, so it
cannot activate or probe the owner before the boot/manager/health gates.
