#!/usr/bin/env python3
"""Break the code on purpose and check something notices.

WHY THIS EXISTS. Six external reviews in three days found the same
defect repeatedly: tests that assert the shape of the source rather
than the behaviour of the system. Every one passed. Two examples, both
from tests written the same day they were caught - a deep-copy test
that did its own deepcopy, and a test that searched controller.py for
the word "try". Neither could fail. The suite going green had stopped
being evidence.

A mutation is a CLAIM: break this line, and that test module must
fail. If it does not, the test is decoration. Curated by hand rather
than generated, because the point is not coverage arithmetic - it is
that the things which actually matter are each guarded by
something that bites.

    ./venv/bin/python tools/mutate.py            # run them all
    ./venv/bin/python tools/mutate.py --list     # what is claimed
    ./venv/bin/python tools/mutate.py -k fault   # a subset

Each mutation runs in a COPY of the tree. The real one is never
touched, and the copy is deleted afterwards.

Only the named module is run, not the whole suite: a mutation whose
consequence shows up somewhere unrelated is usually a sign the claim
was written too loosely.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
MODULE_TIMEOUT_SECONDS = 120


# name, file, old, new, the module that must fail
#
# WHAT THIS SET COVERED UNTIL 2026-08-18, and did not.
#
# Reviewing the list by module showed all of it aimed at the log
# colouring, the secret scanner, the daily summaries, the chart ranges,
# the fault codes and the render canary. Not one entry named
# test_actuation, test_concurrency, test_durability, test_restart,
# test_plug_parse or test_control. The harness that exists because
# tests were passing without protecting anything was proving the
# colours can fail and saying nothing about the relay, the locks or the
# fault transaction.
#
# Worse, two separate documents asserted the opposite - one recorded
# actuation as mutation-tested against four deliberate defects, and an
# audit recommended RESTRICTING the set to safety-critical claims as
# though those were the entries present. The four were a manual
# exercise done by hand on 2026-08-10 and never written down here.
#
# The safety-core entries below are the correction. Each one is checked
# by hand for the right FAILURE, not merely a failure: a mutation
# caught by an unrelated crash is not evidence.
MUTATIONS = [
    ("emulator-batch-count-not-consumed", "tools/plug_emulator.py",
     '                state.failures_remaining -= 1', '                pass',
     "test_plug_emulator.py", "test_fail_next_batch_is_targeted_exact_and_recovers_without_rearming"),
    ("emulator-timeout-reply-restored", "tools/plug_emulator.py",
     '            if settings["failure"] == "timeout":', '            if False:',
     "test_plug_emulator.py", "test_timeout_and_connection_drop_leave_switch_unchanged_and_recover"),
    ("emulator-outage-never-recovers", "tools/plug_emulator.py",
     'self.clock() >= self.outage_until', 'False',
     "test_plug_emulator.py", "test_brief_outage_expires_without_changing_relay_or_extending_on_unrelated_save"),
    ("plug-boolean-watts-accepted", "controller.py",
     '            if isinstance(raw, bool):', '            if False:',
     "test_off_draw.py", "test_boolean_http_meter_values_cannot_confirm_fault_stop"),
    ("relay-mail-pending-stage-not-persisted", "controller.py",
     '                f["relay_notice"] = {"stage": stage, "delivered": False}',
     '                pass',
     "test_relay_notice_retry.py", "test_pending_notice_survives_cold_restart_without_replenishing_budget"),
    ("relay-retry-null-accepted", "controller.py",
     '    if "relay_retry" in value:', '    if retry is not None:',
     "test_relay_notice_retry.py", "test_null_retry_record_fails_closed_but_omitted_legacy_record_enforces_stop"),
    ("relay-mail-annotation-repair-skipped", "controller.py",
     '        saved = _save_annotation(current)', '        saved = False',
     "test_relay_notice_retry.py", "test_failed_notification_writes_repair_even_when_email_disabled_or_on_shutdown"),
    ("relay-mail-loop-retry-skipped", "controller.py",
     '            _retry_relay_notice()\n        except Exception as e:',
     '            pass\n        except Exception as e:',
     "test_relay_notice_retry.py", "test_real_meter_loop_retries_pending_mail_after_hardware_exhaustion"),
    ("relay-mail-retry-delay-dropped", "controller.py",
     'time.monotonic() < _relay_notice_due.get(token, 0)', 'False',
     "test_relay_notice_retry.py", "test_failed_terminal_email_retries_once_after_delay_without_hardware"),
    ("relay-mail-late-callback-accepted", "controller.py",
     '            if not notice or (f["at"], notice["stage"]) != token:',
     '            if not notice:',
     "test_relay_notice_retry.py", "test_inflight_and_old_callbacks_cannot_deliver_a_newer_stage_or_episode"),
    ("relay-mail-delivery-not-recorded", "controller.py",
     '            notice["delivered"] = True', '            notice["delivered"] = False',
     "test_relay_notice_retry.py", "test_failed_terminal_email_retries_once_after_delay_without_hardware"),
    ("stale-stop-confirmation-accepted", "controller.py",
     '            if (\n                    not cur or cur.get("at") != expected_at',
     '            if False and (\n                    not cur or cur.get("at") != expected_at',
     "test_concurrency.py", "test_old_stop_confirmation_cannot_override_new_observation_or_fault"),
    ("stop-observation-revision-ignored", "controller.py",
     '                    or STATE["relay_revision"] != relay_revision',
     '                    or False',
     "test_concurrency.py", "test_old_stop_confirmation_cannot_override_new_observation_or_fault"),
    ("stop-fault-episode-ignored", "controller.py",
     '                    not cur or cur.get("at") != expected_at',
     '                    not cur',
     "test_concurrency.py", "test_old_stop_confirmation_cannot_override_new_observation_or_fault"),
    ("relay-observation-delivery-race", "controller.py",
     '        with _fault_lock, _state_lock:\n            message = _adopt_relay_locked(actual, time.time())',
     '        with _state_lock:\n            message = _adopt_relay_locked(actual, time.time())',
     "test_concurrency.py", "test_relay_observation_serializes_with_delivery_persistence"),
    ("sensor-relay-observation-delivery-race", "controller.py",
     '        _daily_pending = None\n        with _fault_lock, _state_lock:',
     '        _daily_pending = None\n        with _state_lock:',
     "test_concurrency.py", "test_relay_observation_serializes_with_delivery_persistence"),
    ("email-header-validation-skipped", "controller.py",
     '        if name in ("email.sender", "email.recipients") and any(ch in v for ch in "\\r\\n"):',
     '        if False:',
     "test_config_boundaries.py", "test_email_header_line_breaks_rejected_at_api_and_startup"),
    ("email-message-build-errors-escape", "controller.py",
     '        return "Invalid email message headers or content (%s)" % type(exc).__name__',
     '        raise',
     "test_alert_delivery.py", "test_invalid_email_message_is_json_failure_without_smtp_contact"),
    ("auto-fault-scheduler-bypassed", "controller.py",
     '            if STATE["fault"] is not None:\n                return  # Fault-stop enforcement alone owns OFF commands and retry budgets.',
     '            if False:\n                return',
     "test_relay_release.py", "test_auto_polls_never_command_around_fault_scheduler"),
    ("external-on-stop-confirmation-retained", "controller.py",
     '        STATE["fault"].update(stopped=False)',
     '        STATE["fault"].update(stopped=True)',
     "test_relay_release.py", "test_external_on_revokes_recovered_stop_without_new_budget"),
    ("mail-unicode-serialization-broken", "controller.py",
     '        refused = s.sendmail(e["sender"], to, payload)',
     '        refused = s.sendmail(e["sender"], to, body)',
     "test_alert_delivery.py", "test_unicode_fault_notice_uses_real_smtp_serialization"),
    ("partial-sensor-outage-hidden", "controller.py",
     '        usable = all(vals.get(r["reading"]) is not None for r in live)',
     '        usable = any(vals.get(r["reading"]) is not None for r in live)',
     "test_partial_sensor.py", "test_each_missing_enabled_metric_preserves_safety_state"),
    ("failsafe-on-error-skips-outage-alert", "controller.py",
     '                switch_error = exc', '                raise',
     "test_failsafe_on_alert.py", "test_failed_on_still_alerts_truthfully_without_renewing_start_timing"),
    ("failsafe-on-error-claimed-success", "controller.py",
     '    _notify_stale(mins, already_on, fan_confirmed=switch_error is None)',
     '    _notify_stale(mins, already_on)',
     "test_failsafe_on_alert.py", "test_failed_on_still_alerts_truthfully_without_renewing_start_timing"),
    ("manual-stop-cold-wait-dropped", "controller.py",
     '        immediate = retry["attempts"] == 0 and f["code"] != FC_MANUAL_STOP',
     '        immediate = retry["attempts"] == 0',
     "test_relay_release.py",
     "test_initial_manual_retry_wait_survives_preserving_and_cold_restarts"),
    ("manual-stop-fault-not-latched", "controller.py",
     '''            _store_fault(dict(at=now, msg=msg, code=FC_MANUAL_STOP,
                stopped=False, attempted_at=None, delivered_at=None,
                relay_retry={"attempts": 0, "phase": "first"}))''',
     '            pass', "test_relay_release.py",
     "test_failed_manual_stop_latches_even_with_healthy_watts_and_monitor_disabled"),
    ("manual-stop-promise-bypasses-budget", "controller.py",
     '    if fault:\n        # Fault enforcement owns any outstanding stop.',
     '    if False:\n        # Fault enforcement owns any outstanding stop.',
     "test_relay_release.py",
     "test_failed_manual_stop_latches_even_with_healthy_watts_and_monitor_disabled"),
    ("manual-stop-initial-retry-not-spaced", "controller.py",
     '            _relay_retry_clock = (now, 0, time.monotonic()+30)',
     '            _relay_retry_clock = (now, 0, time.monotonic())',
     "test_relay_release.py",
     "test_failed_manual_stop_latches_even_with_healthy_watts_and_monitor_disabled"),
    ("manual-stop-persisted-recovery-not-finalized", "controller.py",
     '    if recovered:\n        with _state_lock:\n            _end_run_locked()',
     '    if False:\n        with _state_lock:\n            _end_run_locked()',
     "test_relay_release.py",
     "test_manual_recovery_persistence_retry_finishes_stop_before_acknowledgement"),
    ("manual-stop-persisted-recovery-keeps-marker", "controller.py",
     '            STATE["manual_off_pending"] = False\n        clear_manual_off_promise()',
     '            STATE["manual_off_pending"] = False\n        pass',
     "test_relay_release.py",
     "test_manual_recovery_persistence_retry_finishes_stop_before_acknowledgement"),
    ("manual-stop-shutdown-recovery-not-finalized", "controller.py",
     '    if cleanup:\n        _finish_saved_relay_recovery_locked(fault)',
     '    if False:\n        _finish_saved_relay_recovery_locked(fault)',
     "test_relay_release.py",
     "test_shutdown_saved_manual_recovery_is_not_owed_after_cold_start"),
    ("manual-stop-marker-created-outside-actuation", "controller.py",
     '    if not act_locked:\n        with _act_lock:\n            return _api_config_locked(act_locked=True)',
     '    if False:\n        with _act_lock:\n            return _api_config_locked(act_locked=True)',
     "test_relay_release.py",
     "test_manual_marker_is_created_inside_actuation_transaction"),
    ("manual-stop-new-promise-precedes-old-recovery", "controller.py",
     '            if (owed and old and old.get("code") in _RELAY_FAULT_CODES',
     '            if (False and old and old.get("code") in _RELAY_FAULT_CODES',
     "test_relay_release.py",
     "test_new_manual_save_finishes_older_recovery_before_creating_promise"),
    ("shutdown-busy-actuation-drops-fault", "controller.py",
     '    act_locked = _act_lock.acquire(blocking=False)\n    if not _fault_lock.acquire(timeout=2):',
     '    act_locked = _act_lock.acquire(blocking=False)\n    if not act_locked:\n        return False\n    if not _fault_lock.acquire(timeout=2):',
     "test_restart.py", "test_last_chance_write_persists_even_while_actuation_is_busy"),
    ("manual-stop-busy-shutdown-forgets-cleanup", "controller.py",
     '                if (_relay_recovered(pending) and pending.get("manual_stop_id")',
     '                if (False and pending.get("manual_stop_id")', "test_relay_release.py",
     "test_busy_shutdown_deferred_recovery_cleanup_retries_without_clearing_fault"),
    ("manual-stop-cold-recovery-identity-skipped", "controller.py",
     '    if "manual_stop_id" in fault and fault["manual_stop_id"] != _manual_stop_id():',
     '    if False:', "test_relay_release.py",
     "test_busy_shutdown_saves_recovery_then_reconciles_exact_promise_on_cold_start"),
    ("manual-stop-recovery-keeps-promise", "controller.py",
     '            if saved and f.get("stopped"):\n                _end_run_locked()\n                STATE["manual_off_pending"] = False',
     '            if saved and f.get("stopped"):\n                _end_run_locked()\n                STATE["manual_off_pending"] = True',
     "test_relay_release.py",
     "test_manual_recovery_requires_low_draw_and_discharges_promise_without_clearing_fault"),
    ("relay-release-spacing-skipped", "controller.py",
     '        elif time.monotonic() >= _relay_retry_deadline(f):', '        elif True:',
     "test_relay_release.py", "test_two_groups_exact_spacing_and_terminal_exhaustion"),
    ("relay-release-exhaustion-skipped", "controller.py",
     '        elif retry["phase"] in ("exhausted", "recovered"):',
     '        elif retry["phase"] == "recovered":',
     "test_relay_release.py", "test_two_groups_exact_spacing_and_terminal_exhaustion"),
    ("relay-release-clock-not-restored", "controller.py",
     '        _relay_retry_clock = tuple(retry_clock)', '        _relay_retry_clock = None',
     "test_relay_release.py", "test_preserving_and_cold_restarts_never_replenish_budget"),
    ("relay-release-budget-not-persisted", "controller.py",
     '            save_fault(f)\n            saved = True', '            saved = True',
     "test_relay_release.py", "test_attempt_is_saved_before_command_and_storage_failure_blocks_actuation"),
    ("relay-release-trusts-off-reply", "controller.py",
     '                confirmed = w is not None and _finite(w) and w <= CFG["plug"]["watts_off_max"]',
     '                confirmed = True',
     "test_relay_release.py", "test_two_groups_exact_spacing_and_terminal_exhaustion"),
    ("off-draw-check-skipped", "controller.py",
     '    if _off_draw_check(plug, gen):', '    if False:',
     "test_off_draw.py", "test_off_draw_latches_and_never_confirms_welded_stop"),
    ("off-stop-trusts-reply-despite-power", "controller.py",
     '                if w is None or not _finite(w) or w > CFG["plug"]["watts_off_max"]:',
     '                if False:', "test_off_draw.py",
     "test_fault_stop_requires_fresh_low_draw_and_invalidates_old_confirmation"),
    ("off-timer-not-restored", "controller.py",
     '    _off_draw_since = (off_since if off_since is not None and off_since <= mono\n                      and (allow_old_monotonic or off_since >= 0) else None)',
     '    _off_draw_since = None', "test_off_draw.py",
     "test_unrelated_saves_and_fresh_restart_keep_off_timer"),
    ("initial-spinup-grace-follows-wall-clock", "controller.py",
     '                remaining = SETTLE_SECONDS - age',
     '                remaining = SETTLE_SECONDS - (now - since)',
     "test_clock_rollback.py", "test_forward_step_before_first_draw_check_preserves_full_grace"),
    ("dns-deadline-follows-wall-clock", "controller.py",
     '    deadline = time.monotonic() + timeout',
     '    deadline = time.time() + timeout',
     "test_clock_rollback.py", "test_dns_startup_wait_is_bounded_after_clock_rollback"),
    ("dns-comparison-follows-wall-clock", "controller.py",
     '        if time.monotonic() >= deadline:',
     '        if time.time() >= deadline:',
     "test_clock_rollback.py", "test_dns_startup_wait_is_bounded_after_clock_rollback"),
    ("restart-elapsed-clocks-not-restored", "controller.py",
     '                _elapsed_clocks[key] = pair',
     '                pass', "test_clock_rollback.py",
     "test_failsafe_and_lease_use_elapsed_time_across_clock_steps"),
    ("force-auto-discards-physical-grace", "controller.py",
     '                expired = time.time() - CFG["control"]["min_run_minutes"]*60 - 1',
     '                STATE["changed_at"] = None\n                expired = time.time() - CFG["control"]["min_run_minutes"]*60 - 1',
     "test_clock_rollback.py", "test_force_to_auto_preserves_physical_spin_up_grace"),
    ("power-age-follows-wall-clock", "controller.py",
     '                watt_age = _elapsed_age_locked("watts_at")',
     '                watt_age = None if w_at is None else time.time()-w_at',
     "test_clock_rollback.py", "test_expired_power_is_unknown_after_clock_rollback"),
    ("sensor-age-follows-wall-clock", "controller.py",
     '        age = _elapsed_age_locked("reading_at", startup=True)\n    if age < mins * 60:',
     '        age = time.time() - (last if last is not None else START_TIME)\n    if age < mins * 60:',
     "test_clock_rollback.py", "test_failsafe_and_lease_use_elapsed_time_across_clock_steps"),
    ("minimum-run-follows-wall-clock", "controller.py",
     '            ran_for = _elapsed_age_locked(lease_key) if lease_at is not None else None',
     '            ran_for = time.time()-lease_at if lease_at is not None else None',
     "test_clock_rollback.py", "test_failsafe_and_lease_use_elapsed_time_across_clock_steps"),
    ("restart-freshness-follows-wall-clock", "controller.py",
     '    fresh = _restart_identity(st) != _startup_restart_identity',
     '    fresh = st.st_mtime >= _PROC_START',
     "test_clock_rollback.py", "test_new_restart_request_with_pre_start_timestamp_is_consumed"),
    ("draw-duration-follows-wall-clock", "controller.py",
     '            elapsed = mono - _oob_monotonic[1]',
     '            elapsed = now - _oob_since', "test_clock_rollback.py",
     "test_draw_fault_uses_elapsed_time_through_rollback_and_restart"),
    ("restart-drops-monotonic-draw-timer", "controller.py",
     '    _oob_monotonic = clock_pair(d.get("oob_monotonic"))',
     '    _oob_monotonic = None', "test_clock_rollback.py",
     "test_draw_fault_uses_elapsed_time_through_rollback_and_restart"),
    ("history-rollback-keeps-old-epoch", "controller.py",
     '                STATE["history_epoch"] = os.urandom(8).hex()',
     '                pass', "test_clock_rollback.py",
     "test_actual_poll_rollback_forces_full_sorted_history_reconciliation"),
    ("history-rollback-not-sorted", "controller.py",
     '    kept.sort(key=lambda p: p["t"])',
     '    pass', "test_clock_rollback.py",
     "test_actual_poll_rollback_forces_full_sorted_history_reconciliation"),
    ("failsafe-confirmed-restart-keeps-old-grace", "controller.py",
     '            if switch_error is None and (actual is False or not was_on):',
     '            if switch_error is None and not was_on:', "test_relay_monitoring.py",
     "test_failsafe_restart_renews_physical_spin_up_grace"),
    ("stop-fresh-relay-observation-discarded", "controller.py",
     '            STATE["fan"] = actual\n            fault = STATE["fault"]',
     '            pass\n            fault = STATE["fault"]', "test_actuation.py",
     "test_failed_stops_keep_fresh_on_and_invalidate_stop_confirmation"),
    ("stop-old-confirmation-retained", "controller.py",
     '                fault["stopped"] = False',
     '                fault["stopped"] = True', "test_actuation.py",
     "test_failed_stops_keep_fresh_on_and_invalidate_stop_confirmation"),
    ("daily-flush-writers-overlap", "controller.py",
     '    with _daily_write_lock:\n        return _save_daily_locked(force, report_error)',
     '    if True:\n        return _save_daily_locked(force, report_error)', "test_daily.py",
     "test_overlapping_flushes_cannot_overwrite_newer_daily_samples"),
    ("force-on-observed-off-not-adopted", "controller.py",
     '        _record_relay_observation(actual)\n        with _state_lock:\n            if actual is False:\n                STATE["fan"] = False',
     '        with _state_lock:\n            if actual is False:\n                pass',
     "test_actuation.py", "test_force_on_failed_reassert_keeps_confirmed_off_in_api"),
    ("escaped-secret-value-truncated", "tools/scan_secrets.py",
     'r"(?:\\\"((?:\\\\.|[^\\\"\\\\])*)\\\"',
     'r"(?:\\\"([^\\\"]*)\\\"',
     "test_secret_scanner.py", "test_everything_that_should_be_caught_is"),
    ("legacy-enable-type-unchecked", "controller.py",
     '        if not isinstance(ctl["enabled"], bool):',
     '        if False:', "test_config_boundaries.py",
     "test_legacy_enable_requires_boolean_before_migration"),
    ("quoted-credential-keys-missed", "tools/scan_secrets.py",
     'r"(?:\\\"%s\\\"|\'%s\'|%s)\\s*',
     'r"(?:%s|%s|%s)\\s*', "test_secret_scanner.py",
     "test_everything_that_should_be_caught_is"),
    ("late-stale-delivery-accepted", "controller.py",
     '            if _stale_episode is not episode:',
     '            if False:', "test_alert_delivery.py",
     "test_late_outage_delivery_cannot_suppress_next_episode"),
    ("backward-date-duplicates-row", "controller.py",
     '        row = next((r for r in rows if r.get("d") == day), None)',
     '        row = rows[-1] if rows and rows[-1].get("d") == day else None',
     "test_daily.py", "test_backward_date_updates_existing_row_and_survives_reload"),
    ("plug-failed-connect-hold-not-refreshed", "controller.py",
     '                _plug_ports[key] = time.monotonic()',
     '                pass', "test_plug_ports.py",
     "test_failed_connect_holds_port_for_full_interval_after_close"),
    ("restart-plug-request-not-frozen", "controller.py",
     '                (_plug_request_lock, "a plug request is in progress"),\n',
     '', "test_restart.py", "test_restart_handover_blocks_configuration_and_actuation"),
    ("plug-recent-source-port-reused", "controller.py",
     '        if key in _plug_ports:', '        if False:',
     "test_plug_ports.py", "test_recent_port_is_skipped_before_connecting_on_same_socket"),
    ("plug-port-guard-bypassed", "controller.py",
     "self.poolmanager.pool_classes_by_scheme['http'] = _PlugHTTPPool",
     "self.poolmanager.pool_classes_by_scheme['http'] = HTTPConnectionPool",
     "test_plug_ports.py", "test_transport_uses_guard_for_real_local_http_and_preserves_credentials"),
    ("numeric-reading-overflow-unhandled", "controller.py",
     '    if isinstance(v, (int, float)):\n        return float(v) if _finite(v) else None',
     '    if isinstance(v, (int, float)):\n        return float(v)',
     "test_numeric_parsing.py", "test_bad_disabled_co2_does_not_abort_temperature_control"),
    ("numeric-handover-overflow-unhandled", "controller.py",
     '    return float(v) if _finite(v) else None\n\n\ndef _resume_payload',
     '    return float(v) if math.isfinite(v) else None\n\n\ndef _resume_payload',
     "test_numeric_parsing.py", "test_oversized_handover_numbers_do_not_abort_resume"),
    ("events-loading-invalid-timestamp-retained", "controller.py",
     'if isinstance(x, dict) and _valid_fault_timestamp(x.get("at"))',
     'if isinstance(x, dict) and "at" in x', "test_events_loading.py",
     "test_invalid_event_timestamps_and_messages_are_discarded"),
    ("events-loading-unknown-fields-retained", "controller.py",
     'return [{"at": x["at"], "msg": x["msg"]} for x in e',
     'return [x for x in e', "test_events_loading.py",
     "test_only_supported_event_fields_survive_and_json_remains_valid"),
    ("plug-requests-unnecessarily-serialized", "controller.py",
     '        with _plug_request_lock.request():',
     '        with _plug_request_lock:', "test_plug_requests.py",
     "test_power_relay_and_switch_requests_may_overlap_across_client_instances"),
    ("plug-restart-registration-bypassed", "controller.py",
     '        with _plug_request_lock.request():',
     '        with nullcontext():', "test_plug_requests.py",
     "test_power_relay_and_switch_requests_may_overlap_across_client_instances"),
    ("force-on-five-second-check-cached", "controller.py",
     '                _force_on(plug, gen)\n',
     '                _force_on(plug, gen, poll=True)\n', "test_plug_polling.py",
     "test_force_on_checks_every_five_seconds_without_duplicate_start"),
    ("cold-start-hold-expired-early", "controller.py",
     '                elif time.monotonic() < deadline:',
     '                elif False:', "test_cold_start_hold.py",
     "test_already_on_cold_boot_holds_ten_minutes_then_stops_without_demand"),
    ("cold-start-demand-resets-physical-timer", "controller.py",
     '                    _stamp_elapsed_locked("min_run_at")',
     '                    _stamp_elapsed_locked("changed_at")', "test_cold_start_hold.py",
     "test_demand_handover_does_not_reset_existing_draw_fault_timer"),
    ("cold-start-demand-lease-ignored", "controller.py",
     '            lease_key = "min_run_at" if STATE["min_run_at"] is not None else "changed_at"',
     '            lease_key = "changed_at"', "test_cold_start_hold.py",
     "test_fresh_demand_takes_over_with_normal_minimum_run"),
    ("installer-percent-specifiers-unescaped", "install.sh",
     '  BEGIN { gsub(/%/, "%%", dir) }',
     '  BEGIN { }', "test_install_unit.py",
     "test_unit_generator_escapes_percent_specifiers_without_installing"),
    ("installer-apostrophe-path-accepted", "install.sh",
     '  *"\'"*)', '  *"__not_an_apostrophe__"*)', "test_install_unit.py",
     "test_apostrophe_path_is_rejected_by_initial_guard_without_installing"),
    ("restart-separate-lease-not-reported", "controller.py",
     '        lease = STATE["min_run_at"] is not None or STATE["changed_at"] is not None',
     '        lease = STATE["changed_at"] is not None', "test_cold_start_hold.py",
     "test_preserving_restart_keeps_separate_minimum_run_and_physical_times"),
    ("cold-start-hold-fixed-duration", "controller.py",
     '            minutes = CFG["control"]["cold_start_minutes"]',
     '            minutes = 10', "test_cold_start_hold.py",
     "test_configurable_duration_and_invalid_settings_are_checked"),
    ("cold-start-hold-handover-lost", "controller.py",
     '            "cold_start_hold_until": STATE["cold_start_hold_until"],',
     '            "cold_start_hold_until": None,', "test_cold_start_hold.py",
     "test_preserving_restart_keeps_existing_deadline_without_new_cold_hold"),
    ("cold-start-hold-fault-not-cancelled", "controller.py",
     '            if value is not None:\n                STATE["cold_start_pending"] = False',
     '            if False:\n                STATE["cold_start_pending"] = False',
     "test_cold_start_hold.py", "test_fault_stop_is_not_delayed_by_startup_hold"),
    ("config-sections-ignored", "controller.py",
     'if set(body) - {"protect", "plug", "control", "email"}:',
     'if False:', "test_config_boundaries.py",
     "test_unknown_sections_reject_the_whole_request_without_side_effects"),
    ("config-numeric-overflow-unhandled", "controller.py",
     '        except (TypeError, ValueError, OverflowError):\n            raise ValueError("%s must be a finite number" % name)',
     '        except (TypeError, ValueError):\n            raise ValueError("%s must be a finite number" % name)',
     "test_config_boundaries.py", "test_oversized_numbers_return_json_validation_errors_without_saving"),
    ("relay-fallback-always-enabled", "controller.py",
     "            relay_fallback = (CFG['control']['mode'] != 'on'\n                              and _relay_fallback_needed(plug, gen))",
     '            relay_fallback = True', "test_plug_polling.py",
     "test_healthy_normal_checks_do_not_trigger_background_relay_requests"),
    ("relay-fallback-never-enabled", "controller.py",
     "            relay_fallback = (CFG['control']['mode'] != 'on'\n                              and _relay_fallback_needed(plug, gen))",
     '            relay_fallback = False', "test_plug_polling.py",
     "test_fallback_resumes_when_normal_checks_stop"),
    ("saved-history-extra-fields-retained", "controller.py",
     '    kept = [{k: p[k] for k in fields if k in p}\n            for p in hist if _usable_point(p, cutoff)]',
     '    kept = [p for p in hist if _usable_point(p, cutoff)]',
     "test_history.py", "test_restored_history_keeps_only_supported_fields_and_browser_valid_json"),
    ("saved-history-metrics-unchecked", "controller.py",
     '            and all(point.get(key) is None or _finite(point[key])\n                    for key in ("v", "tc", "rh", "pm"))',
     '            and True', "test_history_and_fault_notices.py",
     "test_saved_history_validates_every_known_metric_and_preserves_good_rows"),
    ("fault-notice-assumes-stop-confirmed", "controller.py",
     '    if fault.get("stopped") is True and fan is False:',
     '    if True:', "test_history_and_fault_notices.py",
     "test_unsaved_fault_with_failed_stop_does_not_claim_success"),
    ("fault-notice-ignores-force-on-override", "controller.py",
     '    if mode == "on":\n        return ("Force on overrides fault-stop enforcement;',
     '    if False:\n        return ("Force on overrides fault-stop enforcement;',
     "test_history_and_fault_notices.py",
     "test_restart_notice_explains_force_on_instead_of_claiming_stopped"),
    ("midrun-trigger-activity-missing", "controller.py",
     '                    if joined:\n', '                    if False:\n',
     "test_trigger_activity.py",
     "test_humidity_joining_is_logged_once_without_command_or_lease_reset"),
    ("midrun-trigger-activity-repeated-every-poll", "controller.py",
     '                              and not before_latch.get(r["key"])',
     '                              and True', "test_trigger_activity.py",
     "test_humidity_joining_is_logged_once_without_command_or_lease_reset"),
    ("draw-fault-confirmation-skipped", "controller.py",
     '            actual = _fan_status(plug) if plug is not None else None',
     '            actual = None', "test_plug_polling.py",
     "test_external_stop_before_next_relay_poll_does_not_latch_draw_fault"),
    ("interlock-sensor-blocked-by-mode-actuation", "controller.py",
     '    if not PLUG_ACCESS_DISABLED and c["mode"] == "manual":',
     '    if c["mode"] == "manual":', "test_plug_polling.py",
     "test_interlocked_modes_still_read_sensor_and_preserve_stop_promise"),
    ("plug-polling-interlock-bypassed", "controller.py",
     '        if PLUG_ACCESS_DISABLED:\n            raise PlugError("Plug access is disabled for this installation",',
     '        if False:\n            raise PlugError("Plug access is disabled for this installation",',
     "test_plug_polling.py",
     "test_interlock_blocks_real_client_reads_tests_and_switches_before_http"),
    ("plug-polling-relay-cadence-five-seconds", "controller.py",
     'RELAY_SECONDS = 30', 'RELAY_SECONDS = 5', "test_plug_polling.py",
     "test_meter_five_seconds_relay_thirty_seconds_and_immediate_start"),
    ("plug-polling-relay-false-recovery", "controller.py",
     '        issue = issue or relay_issue', '        issue = None', "test_plug_polling.py",
     "test_relay_failure_recovers_only_after_a_successful_relay_read"),
    # ------------------------------------------------- the safety core
    ("auto-current-run-lease-ignored", "controller.py",
     '            ran_for = _elapsed_age_locked(lease_key) if lease_at is not None else None',
     '            ran_for = 3600', "test_relay_monitoring.py",
     "test_auto_honours_new_external_off_on_minimum_run_lease"),
    ("auto-current-fan-state-ignored", "controller.py",
     '            fan_on = STATE["fan"]\n            changed = STATE["changed_at"]',
     '            fan_on = True\n            changed = STATE["changed_at"]',
     "test_relay_monitoring.py", "test_auto_uses_current_off_state_without_a_redundant_stop"),
    ("relay-monitor-stale-meter-committed", "controller.py",
     '                if _stale_client(gen):\n                    continue',
     '                if False:\n                    continue',
     "test_relay_monitoring.py", "test_reconfigured_meter_read_is_discarded_before_state_commit"),
    ("relay-monitor-refresh-skipped", "controller.py",
     '            _refresh_relay(plug, gen, poll=True)\n',
     '            pass\n', "test_relay_monitoring.py",
     "test_running_fan_is_protected_during_disconnected_sensor_startup"),
    ("relay-monitor-generation-guard-skipped", "controller.py",
     '        if _stale_client(gen):\n            return\n        actual = _read_relay(plug, gen, poll=poll)',
     '        if False:\n            return\n        actual = _read_relay(plug, poll=poll)',
     "test_relay_monitoring.py", "test_relay_refresh_rejects_stale_clients_before_reading"),
    ("relay-cache-shared-cadence-disabled", "controller.py",
     '    if (poll and cached is not None and cached[0] is plug',
     '    if (False and cached is not None and cached[0] is plug',
     "test_plug_polling.py", "test_meter_checks_cache_expiry_not_a_second_independent_schedule"),
    ("relay-cache-command-confirmation-skipped", "controller.py",
     '        _remember_relay(plug, True)\n',
     '        pass\n', "test_plug_polling.py",
     "test_force_on_confirmation_is_available_to_fallback_cache"),
    ("discovery-connectivity-lost", "controller.py",
     '                        "isConnected": s.get("isConnected"),',
     '                        "isConnected": None,',
     "test_sensor_discovery.py", "test_discovery_preserves_connection_metadata_and_retained_co2"),
    ("sensor-health-plug-error-misclassified", "controller.py",
     '        if code == FC_SENSOR_DOWN:\n            STATE["sensor_error"] = msg',
     '        if True:\n            STATE["sensor_error"] = msg',
     "test_sensor_connectivity.py",
     "test_fresh_reading_remains_healthy_when_plug_command_fails"),
    ("sensor-health-recovery-not-cleared", "controller.py",
     '                         error=None, sensor_error=None,',
     '                         error=None,',
     "test_sensor_connectivity.py",
     "test_fresh_reading_remains_healthy_when_plug_command_fails"),
    ("sensor-disconnected-readings-accepted", "controller.py",
     '        if s.get("isConnected") is False:\n            # Protect can retain',
     '        if False:\n            # Protect can retain',
     "test_sensor_connectivity.py",
     "test_disconnected_retained_measurements_do_not_refresh_poll_state"),
    ("sensor-disconnected-co2-accepted", "controller.py",
     '        if s.get("isConnected") is False:\n            raise ProtectError("Selected sensor is disconnected")',
     '        if False:\n            raise ProtectError("Selected sensor is disconnected")',
     "test_sensor_connectivity.py",
     "test_disconnected_co2_helper_rejects_retained_measurement"),
    ("draw-timer-unrelated-save-reset", "controller.py",
     '            _oob_generation = _cfg_generation\n        now_mode',
     '            _oob_since = None\n            _oob_generation = _cfg_generation\n        now_mode',
     "test_draw_timer_saves.py",
     "test_repeated_email_and_noop_saves_cannot_postpone_fault"),
    ("draw-timer-relevant-save-not-reset", "controller.py",
     '        if draw_changed:\n            _oob_since = None',
     '        if draw_changed:\n            pass',
     "test_draw_timer_saves.py", "test_each_relevant_plug_setting_resets_timer"),
    ("force-confirmation-server-check-skipped", "controller.py",
     '                if fault:\n                    token = _fault_override_token(fault)',
     '                if False:\n                    token = _fault_override_token(fault)',
     "test_force_on_confirmation.py",
     "test_live_fault_requires_confirmation_before_any_save"),
    ("force-confirmation-token-check-skipped", "controller.py",
     '                    if request.headers.get("X-Fault-Override") != token:',
     '                    if False:', "test_force_on_confirmation.py",
     "test_confirmation_is_bound_to_current_fault_episode"),
    ("review-restart-config-not-frozen", "controller.py",
     '                (_cfg_lock, "settings are being changed"),\n',
     '', "test_restart.py",
     "test_restart_handover_blocks_configuration_and_actuation"),
    ("review-resumed-manual-promise-skipped", "controller.py",
     '    _restore_manual_stop_promise()\n    # A fault that outlived the restart.',
     '    # A fault that outlived the restart.', "test_restart.py",
     "test_resumed_manual_reconciles_durable_stop_promise"),
    ("review-plug-nonfinite-accepted", "controller.py",
     '            return watts if math.isfinite(watts) else None',
     '            return watts', "test_plug_parse.py",
     "test_nonfinite_power_is_missing_not_invalid_json"),
    ("review-email-recipient-snapshot-lost", "controller.py",
     '    return jsonify(ok=True, sent_to=recipients)',
     '    return jsonify(ok=True, sent_to=_recipients())', "test_alert_delivery.py",
     "test_email_test_recipient_report_uses_send_snapshot"),
    ("ack-origin-check-skipped", "controller.py",
     '    expected = _origin_key(request.host_url)',
     '    return None\n    expected = _origin_key(request.host_url)',
     "test_fault_acknowledgement.py",
     "test_cross_origin_fault_clear_preserves_the_safety_latch"),
    ("ack-json-check-skipped", "controller.py",
     '    err = _body_error(_json_body())\n    if err:\n        return err\n    # One transaction: clearing memory',
     '    err = None\n    if err:\n        return err\n    # One transaction: clearing memory',
     "test_fault_acknowledgement.py",
     "test_fault_clear_requires_a_valid_json_object_before_changing_state"),
    ("fault-timestamp-range-unchecked", "controller.py",
     '    try:\n        stamp = float(value)\n        if not math.isfinite(stamp):',
     '    return True\n    try:\n        stamp = float(value)\n        if not math.isfinite(stamp):',
     "test_fault_acknowledgement.py",
     "test_unrepresentable_fault_timestamps_fail_closed_and_clear_cleanly"),
    ("ack-notification-error-escapes", "controller.py",
     '        except Exception as exc:\n            try:\n                _log("%s Fault-clear notification could not be prepared: %s"',
     '        except Exception as exc:\n            raise\n            try:\n                _log("%s Fault-clear notification could not be prepared: %s"',
     "test_fault_acknowledgement.py",
     "test_fault_notification_failure_cannot_turn_a_committed_clear_into_500"),
    ("dangling-fault-forgotten", "controller.py",
     "    if not os.path.lexists(FAULT_PATH):",
     "    if not os.path.exists(FAULT_PATH):",
     "test_safety_boundaries.py", "test_dangling_fault_symlink_fails_closed"),
    ("startup-skips-range-checks", "controller.py",
     "        validate_config_relationships(cfg)",
     "        pass  # ranges not checked at startup",
     "test_safety_boundaries.py",
     "test_disk_config_obeys_every_cross_field_safety_rule"),
    ("sensor-accepts-nan", "controller.py",
     "        return float(v) if _finite(v) else None",
     "        return float(v)",
     "test_safety_boundaries.py", "test_non_finite_reading_never_reaches_history"),
    ("daily-state-leak", "controller.py",
     '                           if k not in ("history", "daily")})',
     '                           if k != "history"})',
     "test_api_boundaries.py",
     "test_daily_is_public_and_state_does_not_duplicate_it"),
    ("draw-loop-hides-check-errors", "controller.py",
     '            issue = issue or "draw check: %s" % _plug_issue(e)',
     '            pass',
     "test_api_boundaries.py",
     "test_draw_loop_reports_an_unexpected_check_failure"),
    ("plug-accepts-scalar-json", "controller.py",
     '        if not isinstance(data, dict):\n            raise PlugError("Plug returned a non-object JSON reply",',
     '        if False:\n            raise PlugError("Plug returned a non-object JSON reply",',
     "test_plug_parse.py", "test_a_json_reply_must_be_an_object"),
    ("smtp-allows-clear-auth", "controller.py",
     '                if "CRAM-MD5" not in mechs:',
     '                if False:',
     "test_alert_delivery.py",
     "test_refuses_when_only_clear_mechanisms_are_offered"),
    ("unsaved-fault-flag-lost", "controller.py",
     '                STATE["fault_unsaved"] = value is not None',
     '                STATE["fault_unsaved"] = False',
     "test_durability.py",
     "test_a_fault_is_not_reported_saved_if_the_directory_sync_fails"),
    ("cold-start-code-lost", "controller.py",
     '    msg = "%s Started cold" % FC_COLD_START',
     '    msg = "%s Started cold" % "FC-999"',
     "test_restart.py", "test_a_cold_start_has_a_short_explicit_message"),
    ("manual-stop-falsely-coded", "controller.py",
     '        _log("Fan off \\u2014 manual",',
     '        _log("FC-999 Fan off \\u2014 manual",',
     "test_fault_codes.py", "test_normal_operation_stays_uncoded"),
    ("owed-manual-stop-falsely-coded", "controller.py",
     '    _log("Fan off \\u2014 manual stop owed from before restart")',
     '    _log("FC-999 Fan off \\u2014 manual stop owed from before restart")',
     "test_fault_codes.py", "test_normal_operation_stays_uncoded"),
    ("missing-reading-clears-latch", "controller.py",
     '            pass\n        elif val >= c[r["on"]]:',
     '            latch[key] = False\n        elif val >= c[r["on"]]:',
     "test_control.py", "test_a_dropped_reading_holds_every_latch"),
    ("failed-stale-alert-counts-as-delivered", "controller.py",
     '            if ok:\n                _stale_delivered = True',
     '            if True:\n                _stale_delivered = True',
     "test_alert_delivery.py",
     "test_no_all_clear_when_the_warning_was_never_delivered"),
    ("late-callback-stamps-replacement-fault", "controller.py",
     '''        if not cur or cur.get("at") != expected_at:
            return                  # acknowledged, or replaced
        cur["delivered_at"] = time.time()
        with _state_lock:
            if not STATE["fault"] or STATE["fault"].get("at") != expected_at:''',
     '''        if not cur:
            return                  # acknowledged, or replaced
        cur["delivered_at"] = time.time()
        with _state_lock:
            if not STATE["fault"]:''',
     "test_alert_delivery.py",
     "test_a_replacement_fault_is_not_marked_delivered"),
    ("http-error-leaks-long-trace", "controller.py",
     '            return phrase',
     '            return str(e)',
     "test_failures.py",
     "test_an_unreachable_console_does_not_paste_the_retry_stack"),
    #
    # The run record behind the "Fan off" line. Four consecutive
    # reviews found the same defect in a different confirmed-stop path,
    # so each surviving path is claimed separately here rather than
    # trusting one entry to stand for the set. A stale record does not
    # move the relay; it makes the activity log name rules that never
    # ran, against readings from a different run.
    ("confirmed-off-does-not-end-the-run", "controller.py",
     '    if actual is False:\n        STATE["cold_start_hold_until"] = None\n        _end_run_locked()',
     '    if False:\n        STATE["cold_start_hold_until"] = None\n        _end_run_locked()',
     "test_actuation.py",
     "test_a_restart_that_finds_the_plug_off_ends_the_carried_run"),
    # Unknown is not off. Clearing on an unreadable plug discards a run
    # that may still be going, on no evidence at all.
    # Anchored on the shared relay adoption snapshot: the bare condition
    # also occurs in _force_on, and an anchor that matches
    # both proves nothing about either.
    ("unreadable-plug-treated-as-off", "controller.py",
     '    if actual is False:\n        STATE["cold_start_hold_until"] = None\n        _end_run_locked()',
     '    if actual is not True:\n        STATE["cold_start_hold_until"] = None\n        _end_run_locked()',
     "test_actuation.py",
     "test_an_unreadable_plug_does_not_end_the_run"),
    # Force on ends the physical run it interrupted. What follows is a
    # new run and is not driven by the rules at all.
    ("force-on-carries-the-interrupted-run", "controller.py",
     '            if actual is False:\n                STATE["fan"] = False\n                # The physical run ended',
     '            if False:\n                STATE["fan"] = False\n                # The physical run ended',
     "test_actuation.py",
     "test_force_on_ends_the_run_it_interrupted"),

    ("generation-guard-never-fires", "controller.py",
     "    return _stopping.is_set() or (gen is not None and gen != _cfg_generation)",
     "    return False",
     "test_concurrency.py",
     "test_the_actuating_helpers_refuse_a_superseded_plug"),
    ("container-lease-clocks-not-rebased", "controller.py",
     '            clocks[key] = [pair[0], shifted(pair[1])]',
     '            clocks[key] = pair', "test_container_state.py",
     "test_host_reboot_rebases_clocks_without_renewing_lease_or_draw_timer"),
    ("container-checkpoint-not-consumed", "controller.py",
     '        os.unlink(STOP_STATE_PATH)', '        pass',
     "test_container_state.py", "test_clean_stop_restores_latch_and_elapsed_timers_once"),
    ("container-stop-does-not-save-fault", "controller.py",
     '        if owed and not _persist_fault_locked():',
     '        if False:', "test_container_state.py",
     "test_essential_fault_save_precedes_checkpoint_and_shutdown_freezes_actuation"),
    ("container-late-sensor-ignores-shutdown", "controller.py",
     '    with _act_lock:\n        if _stale_client(gen):\n            _wake.set()\n            return\n        actual = _fan_status(h)',
     '    with _act_lock:\n        if gen != _cfg_generation:\n            _wake.set()\n            return\n        actual = _fan_status(h)',
     "test_container_state.py", "test_delayed_sensor_cannot_commit_after_shutdown_checkpoint"),
    ("container-auto-ignores-shutdown", "controller.py",
     '        if _stale_client(gen):\n            _wake.set()\n            return\n        # A COPY,',
     '        if gen != _cfg_generation:\n            _wake.set()\n            return\n        # A COPY,',
     "test_container_state.py", "test_auto_decision_cannot_actuate_after_shutdown_checkpoint"),
    ("container-failsafe-ignores-shutdown", "controller.py",
     '        if _stale_client(gen):\n            return\n        cur = CFG["control"]\n        mins = int(cur.get("stale_minutes") or 0)',
     '        if gen != _cfg_generation:\n            return\n        cur = CFG["control"]\n        mins = int(cur.get("stale_minutes") or 0)',
     "test_container_state.py", "test_queued_failsafe_cannot_actuate_after_shutdown_checkpoint"),
    ("container-publication-order-ignored", "tools/container_publication.py",
     "        if (run_id, attempt) <= (old['run'], old['attempt']):",
     "        if False:", "test_container_publication.py",
     "test_older_and_repeated_builds_cannot_replace_newer_publication"),
    ("container-workflow-latest-guard-ignored", ".github/workflows/container.yml",
     '            if [[ "$publish" == true ]]; then',
     '            if true; then', "test_container_publication.py",
     "test_actual_maintenance_script_skips_old_and_repeated_latest_moves"),
    ("container-platform-digests-reconstructed", ".github/workflows/container.yml",
     '            "${platforms[@]}"',
     '            "$IMAGE:build-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-amd64" "$IMAGE:build-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-arm64"',
     "test_container_publication.py", "test_actual_maintenance_script_skips_old_and_repeated_latest_moves"),
    ("container-platform-version-type-ignored", "tools/container_artifacts.py",
     "        if (row != checked or type(row['version']) is not int",
     "        if (False and row != checked or False",
     "test_container_publication.py", "test_platform_artifacts_keep_successful_attempt_identity_and_validate_provenance"),

    # ---------------------------------------------------- log classes
    ("prefix-not-stripped", "controller.py",
     '    m = _CODE_PREFIX.sub("", raw)',
     '    m = raw',
     "test_log_classes.py",
     "test_the_code_prefix_does_not_hide_the_text_rules"),
    ("3xx-ignores-mode", "controller.py",
     '            return "ok" if m.endswith("(Manual)") else "m-manual"',
     '            return "m-manual"',
     "test_log_classes.py",
     "test_every_line_gets_the_class_it_should"),
    ("2xx-not-red", "controller.py",
     '        if ser in ("0", "2"):',
     '        if ser in ("0",):',
     "test_log_classes.py",
     "test_every_line_gets_the_class_it_should"),
    ("class-not-served", "controller.py",
     '''    s["events"] = [dict(e, msg=_short_relay_activity(e["msg"]), cls=event_class(e.get("msg")))
                   for e in s["events"][:EVENTS_SHOWN]]''',
     '    s["events"] = s["events"][:EVENTS_SHOWN]',
     "test_log_classes.py",
     "test_the_endpoint_serves_the_class_with_each_event"),

    # ---------------------------------------------------- chart ranges
    # The bug that was on screen: a fan guide widening the reported
    # range, so the humidity row claimed a 70% high on a day the room
    # never passed 55.
    ("range-follows-the-scale", "controller.py",
     '        vals = [p[key] for p in (hist or [])\n                if isinstance(p, dict) and _finite(p.get(key))]',
     '        vals = [p[key] for p in (hist or [])\n                if isinstance(p, dict) and _finite(p.get(key))]\n        if key == "rh" and CFG["control"].get("humidity_enabled"):\n            vals = vals + [CFG["control"]["humidity_on"]]',
     "test_chart_ranges.py",
     "test_a_threshold_cannot_widen_the_range"),
    # A delta is what the page holds for most of its life.
    ("ranges-missing-from-deltas", "controller.py",
     '                        "ranges": history_ranges(hist),\n                        "first_t": first_t})',
     '                        "first_t": first_t})',
     "test_chart_ranges.py",
     "test_the_endpoint_sends_ranges_with_a_delta"),

    # ------------------------------------------------ the render canary
    # The same text from the two paths means different things and
    # carries different codes. Keyed on the message alone, whichever
    # arrived second was discarded - so an FC-208 could be eaten by an
    # ordinary script error saying the same thing.
    ("dedup-ignores-kind", "controller.py",
     '    if _ui_error_is_new((kind, msg)):',
     '    if _ui_error_is_new(msg):',
     "test_render_canary.py",
     "test_a_render_report_is_not_swallowed_by_a_script_error"),

    # ------------------------------------------------------ the fault
    ("stop-not-confirmed-claimed", "controller.py",
     '''                     ("Action taken",
                      "Plug switched off, fault latched" if confirmed else
                      "Stop commanded, NOT confirmed - fault latched"),''',
     '''                     ("Action taken",
                      "Plug switched off, fault latched"),''',
     "test_fault_codes.py",
     "test_an_unconfirmed_stop_is_not_reported_as_a_stop"),
    ("fault-code-not-stored", "controller.py",
     '''            f = {"at": now, "msg": msg, "stopped": False, "code": code,''',
     '''            f = {"at": now, "msg": msg, "stopped": False,''',
     "test_fault_codes.py",
     "test_a_silent_plug_raises_fc002_end_to_end"),
    ("external-change-drops-mode", "controller.py",
     '''            return "%s Fan %s — changed outside (%s)" % (
                FC_EXTERNAL_CHANGE, "on" if actual else "off", _mode_now())''',
     '''            return "%s Fan %s — changed outside" % (
                FC_EXTERNAL_CHANGE, "on" if actual else "off")''',
     "test_fault_codes.py",
     "test_an_external_change_records_the_mode"),

    # ------------------------------------------------------- the daily
    ("daily-fold-unguarded", "controller.py",
     '''        try:
            _daily_note(now, *_daily_pending)
        except Exception as e:      # pragma: no cover - must never escape
            reset_daily()
            _log("%s Daily summary reset after an error: %s: %s"
                 % (FC_DISK_FAILED, type(e).__name__, e))''',
     '        _daily_note(now, *_daily_pending)',
     "test_daily.py",
     "test_a_broken_summary_cannot_abort_a_poll"),
    ("daily-snapshot-shallow", "controller.py",
     '''        rows = copy.deepcopy(STATE.get("daily") or [])
        _daily_dirty = False''',
     '''        rows = STATE.get("daily") or []
        _daily_dirty = False''',
     "test_daily.py",
     "test_the_writer_snapshot_is_deep"),
    ("daily-accepts-nan", "controller.py",
     '        return math.isfinite(v)',
     '        return True',
     "test_daily.py",
     "test_a_non_finite_reading_never_reaches_the_file"),
    ("daily-logs-on-signal", "controller.py",
     '        if report_error:\n',
     '        if True:\n',
     "test_daily.py",
     "test_the_shutdown_path_does_not_log"),

    # ------------------------------------------------------ the scanner
    ("scanner-prints-the-value", "tools/scan_secrets.py",
     '''    print("%s:%s: credential-looking assignment (value redacted)"
          % (where, lineno))''',
     '''    print("%s:%s: credential-looking assignment: %s"
          % (where, lineno, line.strip()[:120]))''',
     "test_secret_scanner.py",
     "test_a_finding_never_prints_the_value"),
    # A FAITHFUL REGRESSION, not a token edit. The first version of
    # this mutation removed only the leading "^" - which leaves the
    # trailing "$" doing the anchoring, so it barely changed the
    # behaviour and "SURVIVED" said nothing about the tests. A
    # mutation has to be the bug you are actually afraid of: here,
    # the substring list that once contained "password" and excused
    # Password123!.
    ("scanner-placeholder-substring", "tools/scan_secrets.py",
     'PLACEHOLDER = re.compile(\n    r"^(?:"\n    r"changeme|change[-_]me|redacted|dummy|sample|fake|todo|tbd|"\n    r"xxx+|placeholder|example|test[-_]value|not[-_]a[-_]real"\n    r")(?:[-_.].*)?$"\n    r"|^your[-_].*$"\n    r"|^\\.\\.\\.$",\n    re.IGNORECASE)',
     'PLACEHOLDER = re.compile(\n    r"(placeholder|example|changeme|redacted|dummy|sample|fake|"\n    r"todo|tbd|password|secret)",\n    re.IGNORECASE)',
     "test_secret_scanner.py",
     "test_a_real_value_containing_the_word_password_is_still_caught"),
    ("scanner-yaml-gets-code-exemption", "tools/scan_secrets.py",
     '            if not yaml_style and CODE.search(v):',
     '            if CODE.search(v):',
     "test_secret_scanner.py",
     "test_a_yaml_value_that_merely_looks_like_code_is_caught"),
    ("scanner-ignores-git-failure", "tools/scan_secrets.py",
     '''    if r.returncode != 0:''',
     '''    if False:''',
     "test_secret_scanner.py",
     "test_a_failing_git_command_is_not_reported_as_clean"),
]


def _copy(dest):
    """A copy of the tree that is a REAL git repository.

    GIT DECIDES WHAT COMES, not a hand-maintained list. The list was
    wrong in both directions: it copied config.local.json,
    config.json.bak, the concrete systemd unit and a whole .venv if one
    existed - real credentials and machine paths, duplicated fifteen
    times per run - while an earlier version excluded "*.json"
    wholesale and took config.example.json with it, which made every
    mutation "caught" by a FileNotFoundError.

    ls-files --cached --others --exclude-standard is exactly "what git
    would take", so the copy matches what a clone gets and cannot drift
    from .gitignore. Reported by review of fdda51b.

    .git itself is not copied - large, and the history is irrelevant -
    but the scanner reads the tree THROUGH git, so the copy is
    initialised as its own repository. Without that the secret-scanner
    tests fail before anything is mutated, which the control run
    caught.
    """
    os.makedirs(dest, exist_ok=True)
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "-z"],
        capture_output=True, cwd=ROOT, check=True).stdout
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        src = os.path.join(ROOT, rel)
        # A path git knows but that is gone, or a submodule directory.
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    quiet = {"capture_output": True, "cwd": dest}
    subprocess.run(["git", "init", "-q", "."], **quiet)
    subprocess.run(["git", "config", "user.email", "mutate@invalid"], **quiet)
    subprocess.run(["git", "config", "user.name", "mutate"], **quiet)
    subprocess.run(["git", "add", "-A"], **quiet)
    subprocess.run(["git", "commit", "-qm", "control"], **quiet)
    return dest


def classify(returncode, out, expect):
    """The verdict, as a pure function of what the run produced.

    SEPARATED SO IT CAN BE TESTED. Everything else here copies trees
    and spawns processes; this is the part that decides, and it has
    been wrong twice - once accepting any non-zero exit, once accepting
    printed text without an exit code to match. Both were found by
    review rather than by this tool, which is the joke it deserves.

    BOTH SIGNALS ARE REQUIRED. A test module that prints a FAIL line
    and exits zero has not failed - its runner has swallowed the
    result, which is a broken harness rather than a caught mutation,
    and reporting CAUGHT there would hide exactly the defect this tool
    exists to find. Reported by review of 04ad1d7.
    """
    named = any(mark in out for mark in
                ("FAIL  %s:" % expect, "ERROR %s:" % expect,
                 "FAIL  %s " % expect, "ERROR %s " % expect))
    if named and returncode == 0:
        return "BROKEN-RUNNER", (
            "%s printed a failure but %s exited 0" % (expect, "the module"))
    if named:
        line = [ln.strip() for ln in out.splitlines()
                if (ln.startswith("FAIL") or ln.startswith("ERROR"))
                and expect in ln]
        return "CAUGHT", (line[0][:100] if line else expect)

    others = [ln.strip() for ln in out.splitlines()
              if ln.startswith("FAIL") or ln.startswith("ERROR")]
    if others:
        return "WRONG-TEST", "%s passed; %s" % (expect, others[0][:70])
    if returncode != 0:
        return "WRONG-TEST", ("%s passed; module exited %d with no named "
                              "failure" % (expect, returncode))
    return "SURVIVED", "%s still passes" % expect


def _apply(tree, old, new, path):
    p = os.path.join(tree, path)
    with io.open(p, encoding="utf-8") as f:
        s = f.read()
    if s.count(old) != 1:
        return s.count(old)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(s.replace(old, new))
    return 1


def run_one(name, path, old, new, module, expect, verbose=False):
    tmp = tempfile.mkdtemp(prefix="mutate-")
    try:
        tree = _copy(os.path.join(tmp, "t"))
        hits = _apply(tree, old, new, path)
        if hits != 1:
            # The anchor no longer matches: the code moved and the
            # claim was never re-checked. That is a finding in itself.
            return "STALE", "anchor matched %d times in %s" % (hits, path)

        # THE MUTATION MUST STILL BE VALID CODE. A syntax error makes
        # every test in the module blow up, which the old runner scored
        # as a catch - it accepted ANY non-zero exit. That proves
        # nothing about the behaviour under test, and it is the same
        # defect this tool exists to find, inside the tool. Reported by
        # review of fdda51b.
        mutated = os.path.join(tree, path)
        if path.endswith(".py"):
            try:
                with io.open(mutated, encoding="utf-8") as f:
                    compile(f.read(), mutated, "exec")
            except SyntaxError as e:
                return "INVALID", "mutation does not compile: %s" % e

        try:
            r = subprocess.run([PY, os.path.join(tree, "tests", module)],
                               capture_output=True, text=True, cwd=tree,
                               timeout=MODULE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return "HUNG", "%s exceeded %ds" % (module,
                                                    MODULE_TIMEOUT_SECONDS)
        return classify(r.returncode, r.stdout + r.stderr, expect)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def control_run(module):
    """The module must PASS before anything is broken.

    Without this a mutation looks caught when the harness itself is
    broken - a missing file, an import error, a stale path. Every
    "CAUGHT" below means "passed clean, failed mutated", not merely
    "failed".
    """
    tmp = tempfile.mkdtemp(prefix="control-")
    try:
        tree = _copy(os.path.join(tmp, "t"))
        try:
            r = subprocess.run([PY, os.path.join(tree, "tests", module)],
                               capture_output=True, text=True, cwd=tree,
                               timeout=MODULE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False, "%s exceeded %ds" % (module,
                                                 MODULE_TIMEOUT_SECONDS)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-k", metavar="SUBSTRING",
                    help="only mutations whose name contains this")
    a = ap.parse_args()

    picked = [m for m in MUTATIONS if not a.k or a.k in m[0]]
    if not picked:
        # A MISTYPED FILTER MUST NOT LOOK LIKE SUCCESS. "all 0 mutations
        # caught" and an exit of zero is a CI step that does no work and
        # goes green - the same failure mode as a test that cannot fail.
        # Reported by review of 04ad1d7.
        print("No mutation matches %r." % a.k)
        print("Nothing was run. %d mutations exist; --list shows them."
              % len(MUTATIONS))
        return 2
    if a.list:
        for name, path, _, _, module, expect in picked:
            print("%-34s %-22s %s" % (name, module, expect))
        return 0

    # One control run per module, before anything is broken.
    for module in sorted({m[4] for m in picked}):
        ok, tail = control_run(module)
        if not ok:
            print("CONTROL FAILED: %s does not pass unmutated" % module)
            print(tail)
            print("")
            print("Fix that first. Until it passes clean, a mutation "
                  "failing proves nothing.")
            return 2
        print("control  %-28s passes clean" % module)
    print("")

    bad = 0
    for name, path, old, new, module, expect in picked:
        verdict, detail = run_one(name, path, old, new, module, expect)
        print("%-34s %-9s %s" % (name, verdict, detail))
        sys.stdout.flush()
        if verdict != "CAUGHT":
            bad += 1

    print("")
    if bad:
        print("%d of %d mutations were not caught." % (bad, len(picked)))
        print("")
        print("SURVIVED   - the named test does not guard the behaviour "
              "it claims to.")
        print("             Fix the TEST, not this file.")
        print("WRONG-TEST - something failed, but not the test that "
              "claims this.")
        print("             The claim is mis-attributed.")
        print("INVALID    - the mutation does not compile, so it proves "
              "nothing.")
        print("             Fix the mutation.")
        print("BROKEN-RUNNER - a failure was printed and the module still "
              "exited 0.")
        print("             The harness is not propagating failure. Fix "
              "that first.")
        print("STALE      - the code moved and nobody re-checked the "
              "claim.")
        return 1
    print("all %d mutations caught" % len(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
