-- Migration v12: cache non-IKA manual-state telemetry
--
-- Purpose:
--   device_manual_state only ever had IKA-shaped reported columns
--   (reported_setpoint_rpm, actual_rpm, torque_ncm). Devices without those
--   columns (e.g. the ICS435 scale) had no cached telemetry to serve, so the
--   Process view's manual panel had to issue a live device command on every
--   poll tick just to show a value. That live command competes with the
--   background reconciler for the same per-device lock and blocks an HTTP
--   worker thread for the round trip, which under frequent polling (the
--   ICS435 reconciler runs every 500-1000 ms) can exhaust the small Gunicorn
--   thread pool and stall unrelated page navigation app-wide.
--
--   reported_extra is a generic JSON cache column populated by the existing
--   background poll cycle (no new device traffic) so GET /manual-state can
--   serve the last known value without touching the device.
--
-- Usage on MariaDB:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v12_device_manual_state_reported_extra.sql

USE reactor_ctrl;

ALTER TABLE device_manual_state
    ADD COLUMN reported_extra JSON NULL AFTER torque_ncm;
