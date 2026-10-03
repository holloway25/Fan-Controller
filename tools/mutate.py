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
    ("config-sections-ignored", "controller.py",
     'if set(body) - {"protect", "plug", "control", "email"}:',
     'if False:', "test_config_boundaries.py",
     "test_unknown_sections_reject_the_whole_request_without_side_effects"),
    ("config-numeric-overflow-unhandled", "controller.py",
     '        except (TypeError, ValueError, OverflowError):\n            raise ValueError("%s must be a finite number" % name)',
     '        except (TypeError, ValueError):\n            raise ValueError("%s must be a finite number" % name)',
     "test_config_boundaries.py", "test_oversized_numbers_return_json_validation_errors_without_saving"),
    ("relay-fallback-always-enabled", "controller.py",
     '            relay_fallback = _relay_fallback_needed(plug, gen)',
     '            relay_fallback = True', "test_plug_polling.py",
     "test_healthy_normal_checks_do_not_trigger_background_relay_requests"),
    ("relay-fallback-never-enabled", "controller.py",
     '            relay_fallback = _relay_fallback_needed(plug, gen)',
     '            relay_fallback = False', "test_plug_polling.py",
     "test_fallback_resumes_when_normal_checks_stop"),
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
     '            ran_for = (time.time() - changed) if changed is not None else None',
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
     "test_plug_polling.py", "test_all_modes_share_relay_reads_between_sensor_and_meter_loops"),
    ("relay-cache-command-confirmation-skipped", "controller.py",
     '        _remember_relay(plug, True)\n',
     '        pass\n', "test_plug_polling.py",
     "test_force_on_command_reply_prevents_duplicate_poll_and_start"),
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
     "        return n if math.isfinite(n) else None",
     "        return n",
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
     '           % (FC_COLD_START,',
     '           % ("FC-999",',
     "test_restart.py", "test_a_cold_start_says_what_it_did_not_carry"),
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
     '        if ok:\n            _stale_delivered = True',
     '        if True:\n            _stale_delivered = True',
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
     '    prev = STATE["fan"]\n    if actual is False:\n        _end_run_locked()',
     '    prev = STATE["fan"]\n    if False:\n        _end_run_locked()',
     "test_actuation.py",
     "test_a_restart_that_finds_the_plug_off_ends_the_carried_run"),
    # Unknown is not off. Clearing on an unreadable plug discards a run
    # that may still be going, on no evidence at all.
    # Anchored on the shared relay adoption snapshot: the bare condition
    # also occurs in _force_on, and an anchor that matches
    # both proves nothing about either.
    ("unreadable-plug-treated-as-off", "controller.py",
     '    prev = STATE["fan"]\n    if actual is False:\n        _end_run_locked()',
     '    prev = STATE["fan"]\n    if actual is not True:\n        _end_run_locked()',
     "test_actuation.py",
     "test_an_unreadable_plug_does_not_end_the_run"),
    # Force on ends the physical run it interrupted. What follows is a
    # new run and is not driven by the rules at all.
    ("force-on-carries-the-interrupted-run", "controller.py",
     '            if actual is False:\n                # The physical run ended',
     '            if False:\n                # The physical run ended',
     "test_actuation.py",
     "test_force_on_ends_the_run_it_interrupted"),

    ("generation-guard-never-fires", "controller.py",
     "    return gen is not None and gen != _cfg_generation",
     "    return False",
     "test_concurrency.py",
     "test_the_actuating_helpers_refuse_a_superseded_plug"),

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
     '''    s["events"] = [dict(e, cls=event_class(e.get("msg")))
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
