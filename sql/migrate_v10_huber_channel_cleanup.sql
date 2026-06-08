-- Migration v10: Huber telemetry channel cleanup
--
-- Purpose:
--   1. Add the Unistat/Pilot ONE external/process temperature channel.
--   2. Ensure CC230 active telemetry channels exist (setpoint_C, internal_temp_C,
--      external_temp_C).  The runtime upserts these automatically on the first
--      poll cycle, but running this migration before the first poll avoids a
--      window where values are silently discarded due to missing channel rows.
--   3. Deactivate CC230 telemetry channels that are no longer logged.
--
-- Idempotent: can be applied repeatedly.
--
-- Usage:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v10_huber_channel_cleanup.sql

USE reactor_ctrl;

-- 1. Unistat / Pilot ONE: ensure external temperature channel exists.
INSERT INTO measurement_channel (
    device_id,
    channel_code,
    display_name,
    unit,
    value_type,
    is_active
)
SELECT
    d.device_id,
    'external_temp_C',
    'External Temperature',
    'degC',
    'float',
    1
FROM device d
WHERE d.protocol IN ('huber_unistat_430', 'huber_pilot_one')
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    unit = VALUES(unit),
    value_type = VALUES(value_type),
    is_active = VALUES(is_active);

-- 2. CC230: ensure active channels exist and are activated.
INSERT INTO measurement_channel (
    device_id,
    channel_code,
    display_name,
    unit,
    value_type,
    is_active
)
SELECT d.device_id, 'setpoint_C', 'Setpoint', 'degC', 'float', 1
FROM device d
WHERE d.protocol = 'huber_cc230'
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    unit = VALUES(unit),
    value_type = VALUES(value_type),
    is_active = 1;

INSERT INTO measurement_channel (
    device_id,
    channel_code,
    display_name,
    unit,
    value_type,
    is_active
)
SELECT d.device_id, 'internal_temp_C', 'Internal Temperature', 'degC', 'float', 1
FROM device d
WHERE d.protocol = 'huber_cc230'
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    unit = VALUES(unit),
    value_type = VALUES(value_type),
    is_active = 1;

INSERT INTO measurement_channel (
    device_id,
    channel_code,
    display_name,
    unit,
    value_type,
    is_active
)
SELECT d.device_id, 'external_temp_C', 'External Temperature', 'degC', 'float', 1
FROM device d
WHERE d.protocol = 'huber_cc230'
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    unit = VALUES(unit),
    value_type = VALUES(value_type),
    is_active = 1;

-- 3. CC230: deactivate channels that are no longer logged.
UPDATE measurement_channel mc
JOIN device d ON d.device_id = mc.device_id
SET mc.is_active = 0
WHERE d.protocol = 'huber_cc230'
  AND mc.channel_code IN (
      'actual_temp_C',
      'bath_temp_C',
      'cc230_status',
      'cc230_error',
      'cc230_warning'
  );

-- End of migration
