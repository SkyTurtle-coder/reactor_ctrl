from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_HOST = "192.168.55.29"
DEFAULT_TCP_PORT = 4305
DEFAULT_SERVER_CODE = "ICS435-01"
DEFAULT_CONNECTION_LABEL = "COM2 Ethernet"
DEFAULT_ASSET_SERIAL = "ICS435-01"


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


def _diff_keys(existing: dict[str, Any], desired: dict[str, Any], *, keys: list[str]) -> list[str]:
    changed: list[str] = []
    for key in keys:
        if existing.get(key) != desired.get(key):
            changed.append(key)
    return changed


def _server_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "server_code": args.server_code,
        "display_name": args.display_name,
        "vendor": "Mettler Toledo",
        "model": "ICS435",
        "host": args.host,
        "serial_standard": "ethernet",
        "port_count": 1,
        "notes": args.notes,
        "is_active": True,
    }


def _connection_payload(args: argparse.Namespace, *, device_server_id: int) -> dict[str, Any]:
    return {
        "device_server_id": device_server_id,
        "port_number": 1,
        "connection_label": args.connection_label,
        "transport_type": "tcp_socket",
        "tcp_host": args.host,
        "tcp_port": args.tcp_port,
        "baud_rate": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
        "flow_control": "none",
        "read_timeout_ms": args.read_timeout_ms,
        "write_timeout_ms": args.write_timeout_ms,
        "reconnect_delay_ms": args.reconnect_delay_ms,
        "is_enabled": True,
    }


def _device_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "asset_serial": args.asset_serial,
        "display_name": args.device_display_name,
        "device_type": "scale",
        "protocol": "mettler_toledo_ics435",
        "is_active": True,
    }
    if args.manufacturer_serial:
        payload["manufacturer_serial"] = args.manufacturer_serial
    if args.firmware_version:
        payload["firmware_version"] = args.firmware_version
    return payload


def _find_existing_server(servers: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any] | None:
    server_code = str(args.server_code or "").strip()
    host = str(args.host or "").strip()
    return next(
        (
            item
            for item in servers
            if str(item.get("server_code") or "").strip() == server_code
            or str(item.get("host") or "").strip() == host
        ),
        None,
    )


def _find_existing_connection(
    connections: list[dict[str, Any]],
    *,
    device_server_id: int,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in connections
            if int(item.get("device_server_id") or 0) == device_server_id
            and int(item.get("port_number") or 0) == 1
        ),
        None,
    )


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
                item
                for item in devices
                if str(item.get("manufacturer_serial") or "").strip() == manufacturer_serial
                and str(item.get("protocol") or "").strip() == protocol
            ),
            None,
        )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update the Mettler Toledo ICS435 Ethernet scale setup in reactor_ctrl."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Base URL of the Flask app.")
    parser.add_argument(
        "--api-token",
        default=os.getenv("API_AUTH_TOKEN"),
        help="API token for authenticated write endpoints. Defaults to API_AUTH_TOKEN.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="IP address or host name of the ICS435 scale.")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="MT-SICS TCP server port.")
    parser.add_argument("--server-code", default=DEFAULT_SERVER_CODE, help="Stable builder mapping code.")
    parser.add_argument("--display-name", default="Mettler Toledo ICS435", help="Device server display name.")
    parser.add_argument("--connection-label", default=DEFAULT_CONNECTION_LABEL, help="Stable builder connection label.")
    parser.add_argument("--asset-serial", default=DEFAULT_ASSET_SERIAL, help="Stable internal asset serial.")
    parser.add_argument("--device-display-name", default="ICS435 Balance", help="Device display name.")
    parser.add_argument("--manufacturer-serial", help="Optional manufacturer serial.")
    parser.add_argument("--firmware-version", help="Optional firmware version.")
    parser.add_argument("--read-timeout-ms", type=int, default=1200)
    parser.add_argument("--write-timeout-ms", type=int, default=1200)
    parser.add_argument("--reconnect-delay-ms", type=int, default=1000)
    parser.add_argument(
        "--notes",
        default="Mettler Toledo ICS435 direct COM2 Ethernet MT-SICS endpoint.",
        help="Notes stored on the device server record.",
    )
    parser.add_argument("--binding-quality-state", default="configured")
    parser.add_argument("--binding-online", action="store_true", help="Mark the initial binding online.")
    parser.add_argument("--probe", action="store_true", help="Probe the TCP endpoint after create or update.")
    return parser


def _upsert_server(*, args: argparse.Namespace) -> dict[str, Any]:
    _, servers_payload = _request_json(base_url=args.base_url, path="/api/device-servers", token=args.api_token)
    servers = servers_payload["items"]
    server = _find_existing_server(servers, args)
    desired_server = _server_payload(args)
    server_fields = [
        "server_code",
        "display_name",
        "host",
        "vendor",
        "model",
        "serial_standard",
        "port_count",
        "notes",
        "is_active",
    ]

    if server is None:
        print(f"1. Lege Device Server {args.server_code} fuer {args.host} an.")
        _, server = _request_json(
            base_url=args.base_url,
            path="/api/device-servers",
            method="POST",
            token=args.api_token,
            payload=desired_server,
        )
        return server

    changed_fields = _diff_keys(server, desired_server, keys=server_fields)
    if changed_fields:
        print(f"1. Aktualisiere Device Server {server['device_server_id']} ({', '.join(changed_fields)}).")
        _, server = _request_json(
            base_url=args.base_url,
            path=f"/api/device-servers/{server['device_server_id']}",
            method="PATCH",
            token=args.api_token,
            payload=desired_server,
        )
    else:
        print(f"1. Device Server {server['device_server_id']} ist bereits passend konfiguriert.")
    return server


def _upsert_connection(*, args: argparse.Namespace, server: dict[str, Any]) -> dict[str, Any]:
    _, connections_payload = _request_json(base_url=args.base_url, path="/api/device-connections", token=args.api_token)
    connections = connections_payload["items"]
    connection = _find_existing_connection(
        connections,
        device_server_id=int(server["device_server_id"]),
        args=args,
    )
    desired_connection = _connection_payload(args, device_server_id=int(server["device_server_id"]))
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

    if connection is None:
        print(f"2. Lege Connection {args.connection_label} -> {args.host}:{args.tcp_port} an.")
        _, connection = _request_json(
            base_url=args.base_url,
            path="/api/device-connections",
            method="POST",
            token=args.api_token,
            payload=desired_connection,
        )
        return connection

    changed_fields = _diff_keys(connection, desired_connection, keys=connection_fields)
    if changed_fields:
        print(f"2. Aktualisiere Connection {connection['connection_id']} ({', '.join(changed_fields)}).")
        _, connection = _request_json(
            base_url=args.base_url,
            path=f"/api/device-connections/{connection['connection_id']}",
            method="PATCH",
            token=args.api_token,
            payload=desired_connection,
        )
    else:
        print(f"2. Connection {connection['connection_id']} ist bereits passend konfiguriert.")
    return connection


def _upsert_device(*, args: argparse.Namespace, connection: dict[str, Any]) -> dict[str, Any]:
    _, devices_payload = _request_json(base_url=args.base_url, path="/api/devices", token=args.api_token)
    devices = devices_payload["items"]
    desired_device = _device_payload(args)
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
        print(f"3. Lege Device {args.device_display_name} ({desired_device['protocol']}) an.")
        _, device = _request_json(
            base_url=args.base_url,
            path="/api/devices",
            method="POST",
            token=args.api_token,
            payload=desired_device,
        )
    else:
        changed_fields = _diff_keys(device, desired_device, keys=device_fields)
        if changed_fields:
            print(f"3. Aktualisiere Device {device['device_id']} ({', '.join(changed_fields)}).")
            _, device = _request_json(
                base_url=args.base_url,
                path=f"/api/devices/{device['device_id']}",
                method="PATCH",
                token=args.api_token,
                payload=desired_device,
            )
        else:
            print(f"3. Device {device['device_id']} ist bereits passend konfiguriert.")

    connection_id = int(connection["connection_id"])
    current_binding = device.get("current_binding") if isinstance(device.get("current_binding"), dict) else None
    if not current_binding or int(current_binding.get("connection_id") or 0) != connection_id:
        print(f"4. Binde Device {device['device_id']} an Connection {connection_id}.")
        _, device = _request_json(
            base_url=args.base_url,
            path=f"/api/devices/{device['device_id']}/binding",
            method="PUT",
            token=args.api_token,
            payload={
                "connection_id": connection_id,
                "quality_state": args.binding_quality_state,
                "is_online": bool(args.binding_online),
                "reason": "ics435_provisioning",
            },
        )
    else:
        print(f"4. Binding Device {device['device_id']} -> Connection {connection_id} existiert bereits.")

    return device


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.api_token:
        raise RuntimeError("API token is required. Pass --api-token or set API_AUTH_TOKEN in the environment.")

    server = _upsert_server(args=args)
    connection = _upsert_connection(args=args, server=server)
    device = _upsert_device(args=args, connection=connection)

    if args.probe:
        _, probe_payload = _request_json(
            base_url=args.base_url,
            path=f"/api/device-connections/{connection['connection_id']}/probe",
            method="POST",
            token=args.api_token,
        )
        probe = probe_payload["probe"]
        status = "reachable" if probe["reachable"] else "unreachable"
        print(
            "5. Probe: "
            + status
            + (f", latency_ms={probe['latency_ms']}" if probe["latency_ms"] is not None else "")
            + (f", error={probe['error']}" if probe["error"] else "")
        )

    print("Provisionierung abgeschlossen.")
    print("Reactor Builder Mapping:")
    print(f"   Moxa / Device Server = {server['server_code']}")
    print(f"   Port / Connection    = {connection['connection_label']}")
    print(f"   Protocol             = {device['protocol']}")
    print(f"   TCP endpoint         = {connection['tcp_host']}:{connection['tcp_port']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
