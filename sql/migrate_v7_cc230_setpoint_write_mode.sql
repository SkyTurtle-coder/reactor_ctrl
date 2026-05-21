-- Migration v7: Add cc230_setpoint_write_mode to device_connection
-- Stores the setpoint write variant (0=SETPOINT!, 1=SET decimal, 2=SET integer)
-- that last succeeded for a CC230 device, so the driver tries it first on the
-- next set_setpoint command.  NULL = no preference (try A→B→C in order).

USE reactor_ctrl;

ALTER TABLE device_connection
    ADD COLUMN cc230_setpoint_write_mode SMALLINT NULL
    COMMENT '0=SETPOINT!, 1=SET decimal, 2=SET integer; NULL=no preference';

-- End of migration
