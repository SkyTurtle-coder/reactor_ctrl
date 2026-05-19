from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


_DEVICE_PRESETS = {
    "generic": {},
    "huber_unistat_430": {
        "baud_rate": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
        "flow_control": "none",
        "read_timeout_ms": 1500,
        "write_timeout_ms": 1500,
        "device_protocol": "huber_unistat_430",
        "device_type": "thermostat",
        "notes": "Huber Unistat/Pilot ONE PB over RS-232: 9600 baud, 8N1, no handshake.",
    },
    "huber_pilot_one": {
        "baud_rate": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
        "flow_control": "none",
        "read_timeout_ms": 1500,
        "write_timeout_ms": 1500,
        "device_protocol": "huber_pilot_one",
        "device_type": "thermostat",
        "notes": "Huber Unistat/Pilot ONE PB over RS-232: 9600 baud, 8N1, no handshake.",
    },
    "ika_eurostar_60": {
        "baud_rate": 9600,
        "data_bits": 7,
        "parity": "E",
        "stop_bits": 1,
        "flow_control": "none",
        "device_protocol": "ika_eurostar_60",
        "device_type": "stirrer",
        "notes": "IKA EUROSTAR 60 RS-232: 9600 baud, 7E1, no flow control.",
    },
}


def _request_json(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
):
    url = f"{base_url.rstrip('/')}{path}"
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body) if response_body else None
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            details = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            details = {"raw": response_body}
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def _normalize_server_code(host: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]+", "-", host.strip()).strip("-")
    compact = compact.upper() or "NPORT"
    return f"MOXA-{compact}"


def _connection_payload(args: argparse.Namespace, *, device_server_id: int, port_number: int) -> dict[str, Any]:
    return {
        "device_server_id": device_server_id,
        "port_number": port_number,
        "connection_label": f"{args.label_prefix} {port_number}",
        "transport_type": args.transport_type,
        "tcp_host": args.host,
        "tcp_port": args.tcp_port_base + port_number,
        "baud_rate": args.baud_rate,
        "data_bits": args.data_bits,
        "parity": args.parity,
        "stop_bits": args.stop_bits,
        "flow_control": args.flow_control,
        "read_timeout_ms": args.read_timeout_ms,
        "write_timeout_ms": args.write_timeout_ms,
        "reconnect_delay_ms": args.reconnect_delay_ms,
        "is_enabled": True,
    }


def _default_device_protocol(args: argparse.Namespace) -> str:
    preset = _DEVICE_PRESETS.get(args.device_preset) or {}
    return str(args.device_protocol or preset.get("device_protocol") or "").strip()


def _default_device_type(args: argparse.Namespace) -> str:
    preset = _DEVICE_PRESETS.get(args.device_preset) or {}
    return str(args.device_type or preset.get("device_type") or "actuator").strip()


def _default_device_asset_serial(args: argparse.Namespace, *, port_number: int, protocol: str) -> str:
    protocol_token = re.sub(r"[^A-Za-z0-9]+", "-", protocol.strip()).strip("-").upper() or "DEVICE"
    return f"{args.server_code}-P{port_number}-{protocol_token}"


def _default_device_display_name(args: argparse.Namespace, *, port_number: int, protocol: str) -> str:
    if args.device_display_name:
        return args.device_display_name
    labels = {
        "huber_unistat_430": "Huber Unistat 430",
        "huber_pilot_one": "Huber Pilot ONE",
        "ika_eurostar_60": "IKA EUROSTAR 60",
    }
    return f"{labels.get(protocol, protocol or 'Device')} Port {port_number}"


def _device_payload(args: argparse.Namespace, *, port_number: int) -> dict[str, Any]:
    protocol = _default_device_protocol(args)
    if not protocol:
        raise RuntimeError("--bind-device requires --device-protocol or a non-generic --device-preset.")

    payload: dict[str, Any] = {
        "asset_serial": args.device_asset_serial
        or _default_device_asset_serial(args, port_number=port_number, protocol=protocol),
        "display_name": _default_device_display_name(args, port_number=port_number, protocol=protocol),
        "device_type": _default_device_type(args),
        "protocol": protocol,
        "is_active": True,
    }
    if args.manufacturer_serial:
        payload["manufacturer_serial"] = args.manufacturer_serial
    if args.firmware_version:
        payload["firmware_version"] = args.firmware_version
    return payload


def _server_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "server_code": args.server_code,
        "display_name": args.display_name,
        "host": args.host,
        "vendor": "Moxa",
        "model": args.model,
        "serial_standard": args.serial_standard,
        "port_count": args.port_count,
        "notes": args.notes,
        "is_active": True,
    }
    if args.management_port is not None:
        payload["management_port"] = args.management_port
    return payload


def _diff_keys(existing: dict[str, Any], desired: dict[str, Any], *, keys: list[str]) -> list[str]:
    changed: list[str] = []
    for key in keys:
        if existing.get(key) != desired.get(key):
            changed.append(key)
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update a Moxa NPort device server and provision all serial TCP ports in reactor_ctrl."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Base URL of the Flask app.")
    parser.add_argument(
        "--api-token",
        default=os.getenv("API_AUTH_TOKEN"),
        help="API token for authenticated write endpoints. Defaults to API_AUTH_TOKEN.",
    )
    parser.add_argument("--host", required=True, help="IP address or host name of the Moxa NPort device.")
    parser.add_argument("--server-code", help="Internal server code. Defaults to MOXA-<host>.")
    parser.add_argument("--display-name", default="Moxa NPort 5610-8-DT", help="Human-readable device server name.")
    parser.add_argument("--model", default="NPort 5610-8-DT", help="Moxa model name stored in reactor_ctrl.")
    parser.add_argument("--management-port", type=int, help="Optional management web port of the Moxa device.")
    parser.add_argument("--serial-standard", default="rs232", choices=("rs232", "rs422", "rs485"))
    parser.add_argument("--port-count", type=int, default=8, help="Number of serial channels to provision.")
    parser.add_argument(
        "--only-port",
        action="append",
        type=int,
        help="Provision only this serial port number. Can be passed multiple times.",
    )
    parser.add_argument(
        "--device-preset",
        default="generic",
        choices=tuple(sorted(_DEVICE_PRESETS)),
        help="Apply known serial settings before explicit CLI overrides.",
    )
    parser.add_argument(
        "--transport-type",
        default="tcp_socket",
        choices=("tcp_socket",),
        help="Transport used by reactor_ctrl for NPort communication. The current runtime supports tcp_socket.",
    )
    parser.add_argument(
        "--tcp-port-base",
        type=int,
        default=4000,
        help="Base TCP port. Port 1 becomes base+1, port 2 becomes base+2, ...",
    )
    parser.add_argument("--baud-rate", type=int, default=115200, help="Serial baud rate for every provisioned port.")
    parser.add_argument("--data-bits", type=int, default=8, choices=(5, 6, 7, 8))
    parser.add_argument("--parity", default="N", choices=("N", "E", "O"))
    parser.add_argument("--stop-bits", type=int, default=1, choices=(1, 2))
    parser.add_argument("--flow-control", default="none", choices=("none", "rtscts", "xonxoff"))
    parser.add_argument("--read-timeout-ms", type=int, default=1200)
    parser.add_argument("--write-timeout-ms", type=int, default=1200)
    parser.add_argument("--reconnect-delay-ms", type=int, default=1000)
    parser.add_argument("--label-prefix", default="Port", help="Connection label prefix, for example 'Port'.")
    parser.add_argument("--notes", help="Optional notes stored on the device server record.")
    parser.add_argument(
        "--bind-device",
        action="store_true",
        help="Create/update one device and bind it to the selected MOXA port. Requires exactly one --only-port.",
    )
    parser.add_argument(
        "--device-protocol",
        choices=("huber_unistat_430", "huber_pilot_one", "ika_eurostar_60"),
        help="Protocol for --bind-device. Defaults to the selected device preset when available.",
    )
    parser.add_argument("--device-asset-serial", help="Stable internal asset serial for --bind-device.")
    parser.add_argument("--device-display-name", help="Display name for --bind-device.")
    parser.add_argument("--device-type", help="Device type for --bind-device, defaults to preset device type.")
    parser.add_argument("--manufacturer-serial", help="Optional manufacturer serial for --bind-device.")
    parser.add_argument("--firmware-version", help="Optional firmware version for --bind-device.")
    parser.add_argument("--binding-quality-state", default="configured", help="Quality state stored on the binding.")
    parser.add_argument("--binding-online", action="store_true", help="Mark the initial binding online.")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Probe every provisioned TCP endpoint after create or update.",
    )
    return parser


def _apply_device_preset(args: argparse.Namespace, argv: list[str]) -> None:
    preset = _DEVICE_PRESETS[args.device_preset]
    explicit_options = set()
    for item in argv:
        if item.startswith("--"):
            explicit_options.add(item.split("=", 1)[0])

    option_to_attr = {
        "--baud-rate": "baud_rate",
        "--data-bits": "data_bits",
        "--parity": "parity",
        "--stop-bits": "stop_bits",
        "--flow-control": "flow_control",
        "--read-timeout-ms": "read_timeout_ms",
        "--write-timeout-ms": "write_timeout_ms",
        "--notes": "notes",
        "--device-protocol": "device_protocol",
        "--device-type": "device_type",
    }
    for option, attr in option_to_attr.items():
        if option not in explicit_options and attr in preset:
            setattr(args, attr, preset[attr])


def _selected_ports(args: argparse.Namespace) -> list[int]:
    if not args.only_port:
        return list(range(1, args.port_count + 1))
    ports = sorted(set(args.only_port))
    invalid = [port for port in ports if port < 1 or port > args.port_count]
    if invalid:
        raise RuntimeError(
            f"--only-port must be between 1 and --port-count ({args.port_count}); got {invalid}."
        )
    return ports


def _find_existing_device(devices: list[dict[str, Any]], desired_device: dict[str, Any]) -> dict[str, Any] | None:
    asset_serial = str(desired_device.get("asset_serial") or "").strip()
    if asset_serial:
        match = next((item for item in devices if str(item.get("asset_serial") or "").strip() == asset_serial), None)
        if match is not None:
            return match

    manufacturer_serial = str(desired_device.get("manufacturer_serial") or "").strip()
    protocol = str(desired_device.get("protocol") or "").strip()
    if manufacturer_serial and protocol:
        return next(
            (
                item for item in devices
                if str(item.get("manufacturer_serial") or "").strip() == manufacturer_serial
                and str(item.get("protocol") or "").strip() == protocol
            ),
            None,
        )
    return None


def _provision_bound_device(
    *,
    base_url: str,
    token: str,
    args: argparse.Namespace,
    port_number: int,
    connection: dict[str, Any],
) -> dict[str, Any]:
    desired_device = _device_payload(args, port_number=port_number)
    _, devices_payload = _request_json(base_url=base_url, path="/api/devices", token=token)
    devices = devices_payload["items"]
    device = _find_existing_device(devices, desired_device)
    device_fields = [
        "asset_serial",
        "display_name",
        "device_type",
        "protocol",
        "manufacturer_serial",
        "firmware_version",
        "is_active",
    ]

    if device is None:
        print(
            f"   Lege Device {desired_device['display_name']} "
            f"({desired_device['protocol']}, asset_serial={desired_device['asset_serial']}) an."
        )
        _, device = _request_json(
            base_url=base_url,
            path="/api/devices",
            method="POST",
            token=token,
            payload=desired_device,
        )
    else:
        changed_fields = _diff_keys(device, desired_device, keys=device_fields)
        if changed_fields:
            print(f"   Aktualisiere Device {device['device_id']} ({', '.join(changed_fields)}).")
            _, device = _request_json(
                base_url=base_url,
                path=f"/api/devices/{device['device_id']}",
                method="PATCH",
                token=token,
                payload=desired_device,
            )
        else:
            print(f"   Device {device['device_id']} ist bereits passend konfiguriert.")

    current_binding = device.get("current_binding") if isinstance(device.get("current_binding"), dict) else None
    connection_id = int(connection["connection_id"])
    if not current_binding or int(current_binding.get("connection_id") or 0) != connection_id:
        print(f"   Binde Device {device['device_id']} an Connection {connection_id}.")
        _, device = _request_json(
            base_url=base_url,
            path=f"/api/devices/{device['device_id']}/binding",
            method="PUT",
            token=token,
            payload={
                "connection_id": connection_id,
                "quality_state": args.binding_quality_state,
                "is_online": bool(args.binding_online),
                "reason": "moxa_provisioning",
            },
        )
    else:
        print(f"   Binding Device {device['device_id']} -> Connection {connection_id} existiert bereits.")

    return device


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _apply_device_preset(args, sys.argv[1:])

    if not args.api_token:
        raise RuntimeError("API token is required. Pass --api-token or set API_AUTH_TOKEN in the environment.")

    args.server_code = args.server_code or _normalize_server_code(args.host)
    args.parity = args.parity.upper()
    selected_ports = _selected_ports(args)
    if args.bind_device and len(selected_ports) != 1:
        raise RuntimeError("--bind-device requires exactly one selected port. Pass exactly one --only-port.")

    print("1. Lade Device-Server ...")
    _, servers_payload = _request_json(base_url=args.base_url, path="/api/device-servers", token=args.api_token)
    servers = servers_payload["items"]
    server = next((item for item in servers if item.get("host") == args.host), None)
    if server is None:
        server = next((item for item in servers if item.get("server_code") == args.server_code), None)

    desired_server = _server_payload(args)
    server_fields = [
        "server_code",
        "display_name",
        "host",
        "vendor",
        "model",
        "management_port",
        "serial_standard",
        "port_count",
        "notes",
        "is_active",
    ]

    if server is None:
        print(f"   Lege Device-Server {args.server_code} an.")
        _, server = _request_json(
            base_url=args.base_url,
            path="/api/device-servers",
            method="POST",
            token=args.api_token,
            payload=desired_server,
        )
    else:
        changed_fields = _diff_keys(server, desired_server, keys=server_fields)
        if changed_fields:
            print(
                f"   Aktualisiere Device-Server {server['device_server_id']} ({', '.join(changed_fields)})."
            )
            _, server = _request_json(
                base_url=args.base_url,
                path=f"/api/device-servers/{server['device_server_id']}",
                method="PATCH",
                token=args.api_token,
                payload=desired_server,
            )
        else:
            print(f"   Device-Server {server['device_server_id']} ist bereits passend konfiguriert.")

    print(
        f"   Device-Server: id={server['device_server_id']} code={server['server_code']} host={server['host']}"
    )

    print("2. Lade vorhandene Device-Connections ...")
    _, connections_payload = _request_json(base_url=args.base_url, path="/api/device-connections", token=args.api_token)
    existing_connections = {
        (item["device_server_id"], item["port_number"]): item for item in connections_payload["items"]
    }

    created = 0
    updated = 0
    unchanged = 0
    probed = 0
    bound_device = None

    connection_fields = [
        "connection_label",
        "transport_type",
        "tcp_host",
        "tcp_port",
        "baud_rate",
        "data_bits",
        "parity",
        "stop_bits",
        "flow_control",
        "read_timeout_ms",
        "write_timeout_ms",
        "reconnect_delay_ms",
        "is_enabled",
    ]

    for port_number in selected_ports:
        desired_connection = _connection_payload(
            args,
            device_server_id=server["device_server_id"],
            port_number=port_number,
        )
        current = existing_connections.get((server["device_server_id"], port_number))

        if current is None:
            print(
                f"   Lege Port {port_number} an -> {desired_connection['tcp_host']}:{desired_connection['tcp_port']}"
            )
            _, current = _request_json(
                base_url=args.base_url,
                path="/api/device-connections",
                method="POST",
                token=args.api_token,
                payload=desired_connection,
            )
            created += 1
        else:
            changed_fields = _diff_keys(current, desired_connection, keys=connection_fields)
            if changed_fields:
                print(
                    f"   Aktualisiere Port {port_number} ({', '.join(changed_fields)}) -> "
                    f"{desired_connection['tcp_host']}:{desired_connection['tcp_port']}"
                )
                _, current = _request_json(
                    base_url=args.base_url,
                    path=f"/api/device-connections/{current['connection_id']}",
                    method="PATCH",
                    token=args.api_token,
                    payload=desired_connection,
                )
                updated += 1
            else:
                print(
                    f"   Port {port_number} unveraendert -> {current['tcp_host']}:{current['tcp_port']}"
                )
                unchanged += 1

        if args.probe:
            _, probe_payload = _request_json(
                base_url=args.base_url,
                path=f"/api/device-connections/{current['connection_id']}/probe",
                method="POST",
                token=args.api_token,
            )
            probe = probe_payload["probe"]
            status = "reachable" if probe["reachable"] else "unreachable"
            latency = probe["latency_ms"]
            error = probe["error"]
            print(
                f"      Probe: {status}"
                + (f", latency_ms={latency}" if latency is not None else "")
                + (f", error={error}" if error else "")
            )
            probed += 1

        if args.bind_device:
            bound_device = _provision_bound_device(
                base_url=args.base_url,
                token=args.api_token,
                args=args,
                port_number=port_number,
                connection=current,
            )

    print("3. Zusammenfassung")
    print(f"   Server-Code: {server['server_code']}")
    print(f"   Host: {server['host']}")
    print(f"   Ports provisioniert: {', '.join(str(port) for port in selected_ports)}")
    print(f"   Connections erstellt: {created}")
    print(f"   Connections aktualisiert: {updated}")
    print(f"   Connections unveraendert: {unchanged}")
    if bound_device is not None:
        current_binding = (
            bound_device.get("current_binding") if isinstance(bound_device.get("current_binding"), dict) else {}
        )
        binding_connection = (
            current_binding.get("connection") if isinstance(current_binding.get("connection"), dict) else {}
        )
        binding_label = binding_connection.get("connection_label") or f"{args.label_prefix} {selected_ports[0]}"
        print(
            f"   Device gebunden: id={bound_device['device_id']} "
            f"name={bound_device['display_name']} protocol={bound_device['protocol']}"
        )
        print("   Flowsheet-Zuordnung fuer Reactor Builder:")
        print(f"      device_server_code = {server['server_code']}")
        print(f"      connection_label   = {binding_label}")
        print(f"      protocol           = {bound_device['protocol']}")
    if args.probe:
        print(f"   Connections geprueft: {probed}")
    print("Provisionierung abgeschlossen.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
