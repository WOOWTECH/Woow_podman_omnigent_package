# shellcheck shell=bash
# scripts/render-args.sh: values computed from the env file, sourced by scripts/install.sh
# and tests/dryrun.sh so CI renders exactly what a host gets.
#
# render_args <envfile>: QL_ENV is loaded; sets RENDER_ARGS=(KEY=VALUE...).
render_args() {
  local mode
  mode=$(ql_env_get OMNIGENT_PI_STATE private)
  # shellcheck disable=SC2034 # RENDER_ARGS is read by the caller
  case $mode in
    # the runner mounts this package's own volume unit
    private) RENDER_ARGS=(OMNIGENT_PI_VOLUME=omnigent-pi.volume) ;;
    # a reference to Woow_podman_pi_agent_package's unit; the dry-run needs it installed
    # (install.sh: --ref-dir ~/.config/containers/systemd; CI: tests/fixtures/refs/)
    shared) RENDER_ARGS=(OMNIGENT_PI_VOLUME=pi-agent-data.volume) ;;
    *) ql_die "OMNIGENT_PI_STATE must be private or shared, not '$mode'" ;;
  esac
}
