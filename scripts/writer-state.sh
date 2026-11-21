# shellcheck shell=bash
# Shared synchronous writer-stop verification for backup and destructive restore.
OMNIGENT_WRITER_UNITS=(omnigent-runner.service omnigent-server.service)

omnigent_assert_writers_stopped() {
  local unit state pid job container rc running field
  for unit in "${OMNIGENT_WRITER_UNITS[@]}"; do
    state=$(systemctl --user show -p ActiveState --value "$unit") || ql_die "cannot inspect $unit"
    [[ $state == inactive || $state == failed ]] || ql_die "$unit has not stopped (ActiveState=$state)"
    for field in MainPID ControlPID; do
      pid=$(systemctl --user show -p "$field" --value "$unit") || ql_die "cannot inspect $unit $field"
      [[ $pid == 0 ]] || ql_die "$unit still has a $field process"
    done
    job=$(systemctl --user show -p Job --value "$unit") || ql_die "cannot inspect $unit jobs"
    [[ -z $job || $job == 0 ]] || ql_die "$unit still has a pending systemd job"
    container=${unit%.service}
    rc=0
    podman container exists "$container" >/dev/null 2>&1 || rc=$?
    case $rc in
      0)
        running=$(podman container inspect --format '{{.State.Running}}' "$container") || ql_die "cannot inspect $container"
        [[ $running == false ]] || ql_die "$container is still running after its unit stopped" ;;
      1) ;; # Quadlet removed the stopped container.
      *) ql_die "cannot determine whether $container is stopped" ;;
    esac
  done
}

omnigent_stop_writers() {
  # A synchronous stop also cancels an activating unit or a queued auto-restart.
  systemctl --user stop "${OMNIGENT_WRITER_UNITS[@]}" || ql_die "could not stop all application writers"
  omnigent_assert_writers_stopped
}
