-- Migration v6: Expand last_error / error_message to TEXT and cleanup orphaned control_command_event rows
-- WARNING: Make a DB backup before running this script. Example:
-- mysqldump -u reactor_user -p reactor_ctrl > reactor_ctrl.$(date +%F).sql

USE reactor_ctrl;

-- 1) Expand columns to TEXT to avoid DataError 1406 for long error messages
ALTER TABLE device_connection MODIFY COLUMN last_error TEXT NULL;
ALTER TABLE device_manual_state MODIFY COLUMN last_error TEXT NULL;
ALTER TABLE recipe_program_state MODIFY COLUMN last_error TEXT NULL;
ALTER TABLE recipe_program_run MODIFY COLUMN last_error TEXT NULL;
ALTER TABLE control_command MODIFY COLUMN error_message TEXT NULL;

-- 2) Remove control_command_event rows that reference non-existent control_command parents
--    (these cause FK 1452 when application tries to insert related rows later)
-- Count orphans before deletion:
SELECT COUNT(*) AS orphaned_events FROM control_command_event e WHERE NOT EXISTS (SELECT 1 FROM control_command c WHERE c.command_id = e.command_id);

-- Delete orphaned events (uncomment to execute)
-- DELETE FROM control_command_event WHERE NOT EXISTS (SELECT 1 FROM control_command c WHERE c.command_id = control_command_event.command_id);

-- 3) Optional: show rows in control_command_event that would be deleted for manual review
SELECT e.* FROM control_command_event e WHERE NOT EXISTS (SELECT 1 FROM control_command c WHERE c.command_id = e.command_id) LIMIT 100;

-- End of migration
