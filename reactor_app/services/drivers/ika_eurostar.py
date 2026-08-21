from __future__ import annotations

from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverValidationError
from .capabilities import DeviceCapability
from ..transports.interface import ITransport

_LINE_ENDINGS = {
    "none": b"",
    "cr": b"\r",
    "lf": b"\n",
    "crlf": b"\r\n",
    "space_crlf": b" \r\n",
}
_DRAIN_IDLE_TIMEOUT_S = 0.08
_DEFAULT_MAX_STALE_RESPONSES = 3
_STALE_WRITE_RESPONSE_PREFIXES = ("OUT_SP_4",)
_STALE_WRITE_RESPONSE_TOKENS = {"START_4", "STOP_4"}


def _coerce_bool(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    raise DriverValidationError(f"Field '{field_name}' must be a boolean.")


def _coerce_int(value: Any, *, field_name: str, default: int, min_value: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be an integer.") from exc

    if parsed < min_value:
        raise DriverValidationError(f"Field '{field_name}' must be >= {min_value}.")
    return parsed


def _coerce_line_ending(value: Any, *, field_name: str, default: str) -> str:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized not in _LINE_ENDINGS:
        allowed = ", ".join(sorted(_LINE_ENDINGS))
        raise DriverValidationError(f"Field '{field_name}' must be one of: {allowed}.")
    return normalized


def _default_expect_response(command_text: str) -> bool:
    normalized = str(command_text or "").strip().upper()
    return normalized.startswith("IN_")


def _drain_stale_input(transport: ITransport, *, max_bytes: int) -> bytes:
    drain = getattr(transport, "drain_input", None)
    if not callable(drain):
        return b""
    try:
        return drain(max_bytes=max_bytes, idle_timeout_s=_DRAIN_IDLE_TIMEOUT_S)
    except Exception:
        return b""


def _decoded_response(response_bytes: bytes, *, encoding: str, strip_response: bool) -> str | None:
    if not response_bytes:
        return None
    response_text = response_bytes.decode(encoding, errors="replace")
    if strip_response:
        response_text = response_text.rstrip(" \r\n")
    return response_text


def _is_stale_ika_response(response_text: str | None, *, command_text: str) -> bool:
    normalized = str(response_text or "").strip().upper()
    if not normalized:
        return False
    sent_command = str(command_text or "").strip().upper()
    sent_command_name = sent_command.split()[0] if sent_command else ""
    if normalized in {sent_command, sent_command_name}:
        return True
    first_token = normalized.split()[0]
    if first_token in _STALE_WRITE_RESPONSE_TOKENS:
        return True
    return any(normalized.startswith(prefix) for prefix in _STALE_WRITE_RESPONSE_PREFIXES)


class IkaEurostarDriver(DeviceDriver):
    protocol_names = ("ika_eurostar_60",)

    def get_capabilities(self) -> frozenset[str]:
        return frozenset({
            DeviceCapability.CAN_STIR,
            DeviceCapability.HAS_FEEDBACK,
            DeviceCapability.CAN_EMERGENCY_STOP,
            DeviceCapability.SUPPORTS_MANUAL_MODE,
            DeviceCapability.SUPPORTS_RECIPE_MODE,
        })

    def execute(self, *, transport: ITransport, request: DeviceCommandRequest) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.ika_eurostar.start")
        payload = request.payload
        text = payload.get("text", payload.get("command_text"))
        if text is None or not str(text).strip():
            raise DriverValidationError("Field 'payload.text' is required.")

        command_text = str(text).strip().upper()
        encoding = str(payload.get("encoding", "ascii")).strip() or "ascii"
        line_ending_name = _coerce_line_ending(payload.get("line_ending"), field_name="line_ending", default="space_crlf")
        expect_response = _coerce_bool(
            payload.get("expect_response"),
            field_name="expect_response",
            default=_default_expect_response(command_text),
        )
        # Field devices have been observed to answer with CRLF, sometimes with an extra
        # carriage return before LF, even though commands themselves use "blank CRLF".
        response_terminator_name = _coerce_line_ending(
            payload.get("response_terminator"),
            field_name="response_terminator",
            default="crlf" if expect_response else "none",
        )
        max_response_bytes = _coerce_int(
            payload.get("max_response_bytes"),
            field_name="max_response_bytes",
            default=max(transport.recv_size, 4096),
        )
        strip_response = _coerce_bool(payload.get("strip_response"), field_name="strip_response", default=True)
        drain_before_send = _coerce_bool(
            payload.get("drain_before_send"),
            field_name="drain_before_send",
            default=True,
        )
        drain_after_write = _coerce_bool(
            payload.get("drain_after_write"),
            field_name="drain_after_write",
            default=True,
        )
        max_stale_responses = _coerce_int(
            payload.get("max_stale_responses"),
            field_name="max_stale_responses",
            default=_DEFAULT_MAX_STALE_RESPONSES,
            min_value=0,
        )

        try:
            request_bytes = command_text.encode(encoding) + _LINE_ENDINGS[line_ending_name]
        except LookupError as exc:
            raise DriverValidationError(f"Encoding '{encoding}' is not supported.") from exc

        request.throw_if_interrupted(location="driver.ika_eurostar.pre_send")
        drained_bytes = _drain_stale_input(transport, max_bytes=max_response_bytes) if drain_before_send else b""
        transport.send(request_bytes)

        response_bytes = b""
        response_text = None
        skipped_stale_responses: list[str] = []
        if expect_response:
            request.throw_if_interrupted(location="driver.ika_eurostar.pre_receive")
            response_terminator = _LINE_ENDINGS[response_terminator_name]
            while True:
                response_bytes = (
                    transport.receive_until(response_terminator, max_bytes=max_response_bytes)
                    if response_terminator
                    else transport.receive(recv_size=max_response_bytes)
                )
                response_text = _decoded_response(response_bytes, encoding=encoding, strip_response=strip_response)
                if (
                    len(skipped_stale_responses) < max_stale_responses
                    and _is_stale_ika_response(response_text, command_text=command_text)
                ):
                    skipped_stale_responses.append(str(response_text or ""))
                    continue
                break
            request.throw_if_interrupted(location="driver.ika_eurostar.post_receive")
        elif drain_after_write:
            drained_after_write_bytes = _drain_stale_input(transport, max_bytes=max_response_bytes)
            if drained_after_write_bytes:
                drained_bytes += drained_after_write_bytes

        return DeviceCommandResult(
            acknowledged=True,
            response_text=response_text,
            response_hex=response_bytes.hex() if response_bytes else None,
            metadata={
                "driver": "ika_eurostar_60",
                "encoding": encoding,
                "line_ending": line_ending_name,
                "expect_response": expect_response,
                "request_hex": request_bytes.hex(),
                "drained_hex": drained_bytes.hex() if drained_bytes else None,
                "skipped_stale_responses": skipped_stale_responses,
            },
        )
