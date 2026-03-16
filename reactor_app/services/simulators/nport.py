from __future__ import annotations

import socketserver
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SimulatedTextDevice:
    device_name: str
    asset_serial: str
    manufacturer_serial: str
    protocol: str = "generic_text"
    encoding: str = "ascii"
    request_terminator: bytes = b"\r\n"
    response_terminator: bytes = b"\r\n"
    response_delay_s: float = 0.0
    running: bool = False
    temperature_c: float = 24.0
    pressure_bar: float = 1.05
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def extract_messages(self, buffer: bytearray, *, final: bool = False) -> tuple[list[bytes], bytearray]:
        terminator = self.request_terminator
        if not terminator:
            if final and buffer:
                return [bytes(buffer)], bytearray()
            return [], buffer

        messages: list[bytes] = []
        search_from = 0
        while True:
            position = buffer.find(terminator, search_from)
            if position < 0:
                break
            messages.append(bytes(buffer[:position]))
            del buffer[: position + len(terminator)]
            search_from = 0

        if final and buffer:
            messages.append(bytes(buffer))
            buffer.clear()

        return messages, buffer

    def handle_request(self, request_bytes: bytes, *, port_number: int) -> bytes:
        try:
            text = request_bytes.decode(self.encoding, errors="replace").strip()
        except LookupError as exc:
            raise ValueError(f"Unsupported encoding '{self.encoding}'.") from exc

        try:
            response = self._handle_command(text, port_number=port_number)
        except ValueError as exc:
            response = f"ERR;{exc}"
        if self.response_delay_s > 0:
            time.sleep(self.response_delay_s)
        return response.encode(self.encoding) + self.response_terminator

    def _handle_command(self, command: str, *, port_number: int) -> str:
        normalized = command.strip()
        command_upper = normalized.upper()

        if not normalized:
            return "ERR;EMPTY_COMMAND"

        with self._lock:
            if command_upper == "PING":
                return "PONG"
            if command_upper == "HELP?":
                return "OK;COMMANDS=PING,IDENT?,STATUS?,TEMP?,PRESSURE?,START,STOP,TEMP=<value>,PRESSURE=<value>"
            if command_upper == "IDENT?":
                return (
                    f"OK;DEVICE={self.device_name};ASSET={self.asset_serial};MFG={self.manufacturer_serial};"
                    f"PORT={port_number};PROTOCOL={self.protocol}"
                )
            if command_upper == "STATUS?":
                return (
                    f"OK;DEVICE={self.device_name};RUNNING={int(self.running)};"
                    f"TEMP_C={self.temperature_c:.1f};PRESSURE_BAR={self.pressure_bar:.2f}"
                )
            if command_upper == "TEMP?":
                return f"OK;TEMP_C={self.temperature_c:.1f}"
            if command_upper == "PRESSURE?":
                return f"OK;PRESSURE_BAR={self.pressure_bar:.2f}"
            if command_upper == "START":
                self.running = True
                return "OK;STATE=RUNNING"
            if command_upper == "STOP":
                self.running = False
                return "OK;STATE=STOPPED"
            if command_upper.startswith("TEMP="):
                value = self._parse_float(command_upper[5:], field_name="TEMP")
                self.temperature_c = value
                return f"OK;TEMP_C={self.temperature_c:.1f}"
            if command_upper.startswith("PRESSURE="):
                value = self._parse_float(command_upper[9:], field_name="PRESSURE")
                self.pressure_bar = value
                return f"OK;PRESSURE_BAR={self.pressure_bar:.2f}"
            return f"ERR;UNKNOWN_COMMAND={normalized}"

    @staticmethod
    def _parse_float(raw_value: str, *, field_name: str) -> float:
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ValueError(f"Invalid numeric value for {field_name}.") from exc


@dataclass(frozen=True)
class SimulatedNPortPort:
    port_number: int
    tcp_port: int
    device: SimulatedTextDevice


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _PortRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        port_definition: SimulatedNPortPort = server.port_definition  # type: ignore[attr-defined]
        device = port_definition.device
        buffer = bytearray()

        while True:
            payload = self.request.recv(4096)
            if not payload:
                messages, _ = device.extract_messages(buffer, final=True)
                for message in messages:
                    self._respond(device, message, port_definition.port_number)
                break

            buffer.extend(payload)
            messages, buffer = device.extract_messages(buffer)
            for message in messages:
                self._respond(device, message, port_definition.port_number)

    def _respond(self, device: SimulatedTextDevice, message: bytes, port_number: int) -> None:
        response = device.handle_request(message, port_number=port_number)
        self.request.sendall(response)


class NPortSimulator:
    def __init__(self, *, host: str, ports: list[SimulatedNPortPort]):
        self.host = host
        self.ports = ports
        self._servers: list[_ThreadedTCPServer] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._servers:
            return

        for port_definition in self.ports:
            server = _ThreadedTCPServer((self.host, port_definition.tcp_port), _PortRequestHandler)
            server.port_definition = port_definition  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._servers.append(server)
            self._threads.append(thread)

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._servers.clear()
        self._threads.clear()

    def wait_forever(self) -> None:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def describe_ports(self) -> list[dict[str, object]]:
        return [
            {
                "port_number": port_definition.port_number,
                "tcp_port": port_definition.tcp_port,
                "device_name": port_definition.device.device_name,
                "asset_serial": port_definition.device.asset_serial,
                "manufacturer_serial": port_definition.device.manufacturer_serial,
            }
            for port_definition in self.ports
        ]


def build_default_nport_simulator(
    *,
    host: str = "127.0.0.1",
    port_count: int = 8,
    base_tcp_port: int = 4000,
) -> NPortSimulator:
    ports: list[SimulatedNPortPort] = []
    for port_number in range(1, port_count + 1):
        ports.append(
            SimulatedNPortPort(
                port_number=port_number,
                tcp_port=base_tcp_port + port_number,
                device=SimulatedTextDevice(
                    device_name=f"Reactor-Sim-{port_number:02d}",
                    asset_serial=f"SIM-R-{port_number:03d}",
                    manufacturer_serial=f"SIM-MFG-{port_number:03d}",
                    running=False,
                    temperature_c=23.0 + port_number,
                    pressure_bar=1.00 + (port_number * 0.03),
                    response_delay_s=0.02,
                ),
            )
        )
    return NPortSimulator(host=host, ports=ports)
