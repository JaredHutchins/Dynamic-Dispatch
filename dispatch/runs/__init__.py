"""The run queue.

Every dispatch is a durable row. Claims reserve one slot per agent account;
workers release their slots after all work and cleanup finish.

A run stays *active* until Plane reflects its outcome. After the work is done
it waits in a sync state - awaiting_review or awaiting_blocked - until the
board has actually been updated, and only then becomes terminal. The partial
unique index covers every active state, so a story whose board update failed
cannot be picked up and dispatched a second time.

The store is split into one module per area; callers use them all as runs.<name>.
"""

from .store import (ACTIVE, AWAITING, AWAITING_BLOCKED, AWAITING_REVIEW, CANCELLED, DETAIL_LIMIT, DONE, FAILED, HELD,
                    INTERRUPTED, LIMITED, MAIN_CHECK, MIN_SQLITE, QUEUED, RESTART_LIMIT, RUNNING, SCHEMA, StoreError,
                    check_sqlite, check_writable, connect, get, now)
from .timeline import clear_repeats, events, record_event
from .lifecycle import (INTERRUPTED_TOO_OFTEN, await_blocked, await_review, awaiting, close, combine_commit,
                        combined_note, counts_since, decision_after, defer_run, earlier_pull_request, enqueue,
                        forget_worktree, interrupt_running, is_active, last_agent, last_builder, last_committed,
                        last_pushed, limit_streak, mark_comment_posted, mark_committed, mark_pushed, old_failed,
                        record_combine, record_repair, repair_attempts, restarts, review_candidates, save_docs_line)
from .slots import (agent_work, busy_agents, claim_next, cooldowns, end_wait, has_running, mark_wait_announced,
                    release_slot, save_cooldown, set_agent, start_wait, wait_for, waits)
from .pull_requests import (approval_noticed, light, mark_approval_noticed, owed_lights, pr_settled, pull_request,
                            save_light, settle_pr, unapproved_lights)
from .health import (claim_main_alert, claim_main_check_alert, enqueue_main_check, held_for_main, hold_for_main,
                     main_check_due, main_check_failures, main_check_id, main_check_result, main_check_without_result,
                     main_health, main_is_red, mark_hold_announced, owe_main_alert, owe_main_check_alert,
                     resume_main_hold, save_main_fix, save_main_health)
from .updates import (claim_self_update_notice, claim_self_update_restart, rollback_state, save_rollback_state,
                      save_self_update_restart)
from .small_tables import (heartbeat, login_alert_keys, mark_login_alert, record_heartbeat, record_start_date,
                           record_usage, start_date_set, usage_records)
from .cycles import (PLAN_APPROVED, PLAN_ASSESSING, PLAN_COMPLETE, PLAN_NEW, PLAN_PLANNED, PLAN_WAITING,
                     claim_carried_over, claim_early_cycle_alert, complete_cycle, cycle_complete, cycle_members, cycle_plan,
                     cycle_work_active, drop_carry_over, early_cycle_end, save_early_cycle_end,
                     end_cycle_membership, finish_carry_over,
                     forget_cycle_member, is_plan_item, mark_plan_replied, plan_command, plan_issue_ids,
                     record_plan_command, remember_cycle_member, save_carry_over, save_cycle_plan, set_plan_status,
                     unfinished_carry_overs)

from .assessments import (assessment_cooldowns, assessment_cooldown_announced, claim_assessment_cooldown_notice,
                          assessment_for_run, assessment_requested, assessment_retry, assessment_running,
                          assessment_snapshot, assessments, finish_assessment, mark_assessment_alerted,
                          queue_assessments, running_assessments, save_assessment_snapshot, stories_fingerprint)
from .plan_reports import (done_durations, latest_plan_report, plan_report, record_done_story, replan_when_quiet,
                           save_plan_report)
from .plan_decisions import (guard_plan_deletion, mark_approval_step, plan_approval, reopen_approved_plan,
                             replace_plan_item, save_plan_approval)

from .sprint import (blocked_reminder, clear_blocked_reminders, mark_blocked_alert, mark_sprint_hold,
                     sprint_hold_announced, started_before_approval)
