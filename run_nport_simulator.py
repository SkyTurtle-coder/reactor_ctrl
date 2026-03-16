from __future__ import annotations

import argparse

from reactor_app.services import build_default_nport_simulator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Starts a local Moxa NPort-like TCP simulator for reactor_ctrl development."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host/IP to bind the simulator to.")
    parser.add_argument(
        "--base-tcp-port",
        type=int,
        default=4000,
        help="Base TCP port. Port 1 listens on base+1, port 2 on base+2, ...",
    )
    parser.add_argument(
        "--port-count",
        type=int,
        default=8,
        help="Number of simulated NPort serial channels.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    simulator = build_default_nport_simulator(
        host=args.host,
        base_tcp_port=args.base_tcp_port,
        port_count=args.port_count,
    )
    simulator.start()

    print("Local NPort simulator started.")
    for port in simulator.describe_ports():
        print(
            f"Port {port['port_number']}: tcp://{args.host}:{port['tcp_port']} -> "
            f"{port['device_name']} ({port['asset_serial']})"
        )
    print("Example command: STATUS?")
    print("Stop with Ctrl+C.")

    simulator.wait_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
