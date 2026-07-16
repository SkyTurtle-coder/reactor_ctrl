-- Migration v11: allow Ethernet device servers
--
-- Purpose:
--   Permit direct Ethernet devices such as Mettler Toledo ICS435 COM2 Ethernet
--   to be represented without labelling the device server as RS-232.
--
-- Usage on MariaDB:
--   mysql -u reactor_user -p reactor_ctrl < sql/migrate_v11_add_ethernet_device_server_standard.sql
--
-- If your MySQL version uses DROP CHECK instead of DROP CONSTRAINT, run the
-- equivalent DROP CHECK statement manually and then the ADD CONSTRAINT below.

USE reactor_ctrl;

ALTER TABLE device_server
    DROP CONSTRAINT chk_device_server_serial_standard;

ALTER TABLE device_server
    ADD CONSTRAINT chk_device_server_serial_standard
        CHECK (serial_standard IN ('rs232', 'rs422', 'rs485', 'ethernet'));

