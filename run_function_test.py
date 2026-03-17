from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _request_json(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
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
        with urllib.request.urlopen(request, timeout=20) as response:
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


def _find_existing(items: list[dict], *, key: str, value):
    for item in items:
        if item.get(key) == value:
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end function test for RS-232 over Ethernet via the Flask API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Base URL of the Flask app.")
    parser.add_argument("--api-token", default=os.getenv("API_AUTH_TOKEN"), help="API token for write endpoints.")
    parser.add_argument("--server-host", default="127.0.0.1", help="Host of the Moxa NPort or local simulator.")
    parser.add_argument("--port-number", type=int, default=1, help="Moxa port number / TCP port mapping.")
    parser.add_argument("--device-protocol", default="generic_text", help="Device protocol name.")
    parser.add_argument("--command-text", default="TEMP?", help="RS-232 command for the test.")
    parser.add_argument("--channel-code", default="temp_c", help="Measurement channel code.")
    parser.add_argument("--measurement-key", default="TEMP_C", help="Response key to parse from semicolon-separated replies.")
    parser.add_argument("--measurement-unit", default="C", help="Measurement unit.")
    args = parser.parse_args()

    if not args.api_token:
        raise RuntimeError("API token is required. Pass --api-token or set API_AUTH_TOKEN in the environment.")

    suffix = int(time.time())
    device_asset_serial = f"FT-{suffix}"
    device_name = f"Function Test {suffix}"
    server_code = f"TEST-{args.server_host.replace('.', '-')}"

    print("1. Lade bestehende Device-Server ...")
    _, servers_payload = _request_json(base_url=args.base_url, path="/api/device-servers")
    servers = servers_payload["items"]
    server = _find_existing(servers, key="host", value=args.server_host)
    if server is None:
        print("   Lege Device-Server an.")
        _, server = _request_json(
            base_url=args.base_url,
            path="/api/device-servers",
            method="POST",
            token=args.api_token,
            payload={
                "server_code": server_code,
                "display_name": f"Function Test Server {args.server_host}",
                "vendor": "Moxa",
                "model": "NPort 5610-8-DT",
                "host": args.server_host,
                "serial_standard": "rs232",
                "port_count": 8,
            },
        )
    print(f"   Device-Server: {server['device_server_id']} ({server['host']})")

    print("2. Lade bestehende Verbindungen ...")
    _, connections_payload = _request_json(base_url=args.base_url, path="/api/device-connections")
    connections = connections_payload["items"]
    connection = next(
        (
            item
            for item in connections
            if item["device_server_id"] == server["device_server_id"] and item["port_number"] == args.port_number
        ),
        None,
    )
    if connection is None:
        print("   Lege Device-Connection an.")
        _, connection = _request_json(
            base_url=args.base_url,
            path="/api/device-connections",
            method="POST",
            token=args.api_token,
            payload={
                "device_server_id": server["device_server_id"],
                "port_number": args.port_number,
                "connection_label": f"Port {args.port_number}",
                "baud_rate": 9600,
                "parity": "N",
            },
        )
    print(f"   Connection: {connection['connection_id']} -> {connection['tcp_host']}:{connection['tcp_port']}")

    print("3. Lege Test-Geraet an ...")
    _, device = _request_json(
        base_url=args.base_url,
        path="/api/devices",
        method="POST",
        token=args.api_token,
        payload={
            "asset_serial": device_asset_serial,
            "display_name": device_name,
            "device_type": "reactor_component",
            "protocol": args.device_protocol,
        },
    )
    print(f"   Device: {device['device_id']} ({device['asset_serial']})")

    print("4. Port an das Test-Geraet binden ...")
    bound_device = connection.get("bound_device")
    if bound_device is not None:
        _request_json(
            base_url=args.base_url,
            path=f"/api/devices/{bound_device['device_id']}/binding",
            method="DELETE",
            token=args.api_token,
        )
        print(f"   Vorheriges Binding geloest: Device {bound_device['device_id']}")

    _, device = _request_json(
        base_url=args.base_url,
        path=f"/api/devices/{device['device_id']}/binding",
        method="PUT",
        token=args.api_token,
        payload={
            "connection_id": connection["connection_id"],
            "quality_state": "configured",
            "is_online": False,
            "reason": "function_test",
        },
    )

    print("5. TCP-Probe ausfuehren ...")
    _, probe_result = _request_json(
        base_url=args.base_url,
        path=f"/api/device-connections/{connection['connection_id']}/probe",
        method="POST",
        token=args.api_token,
    )
    print(f"   Probe reachable={probe_result['probe']['reachable']} latency_ms={probe_result['probe']['latency_ms']}")

    print("6. RS-232-Command senden und Measurement speichern ...")
    _, command_result = _request_json(
        base_url=args.base_url,
        path=f"/api/devices/{device['device_id']}/commands",
        method="POST",
        token=args.api_token,
        payload={
            "command_name": "query_text",
            "requested_by": "function_test",
            "payload": {
                "text": args.command_text,
                "line_ending": "crlf",
                "expect_response": True,
                "measurement": {
                    "channel_code": args.channel_code,
                    "display_name": "Temperature",
                    "unit": args.measurement_unit,
                    "parser": "float",
                    "key": args.measurement_key,
                    "source": "poller",
                },
            },
        },
    )
    print(f"   Response: {command_result['result']['response_text']}")
    measurement = command_result.get("measurement")
    if measurement is None:
        raise RuntimeError("Command succeeded but no measurement was returned.")
    print(
        "   Measurement:",
        f"id={measurement['measurement_id']}",
        f"channel={measurement['channel_code']}",
        f"value={measurement['numeric_value']}",
        f"unit={measurement['unit']}",
    )

    print("7. Measurement-Liste laden ...")
    _, measurements_payload = _request_json(
        base_url=args.base_url,
        path=f"/api/devices/{device['device_id']}/measurements?limit=5",
    )
    items = measurements_payload["items"]
    print(f"   Gespeicherte Messungen: {len(items)}")
    if items:
        latest = items[0]
        latest_value = latest["numeric_value"] if latest["numeric_value"] is not None else latest["text_value"]
        print(
            "   Letzte Messung:",
            f"{latest['channel_code']}={latest_value} {latest['unit'] or ''}".strip(),
            f"at {latest['measured_at']}",
        )

    print("Funktionstest erfolgreich.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
