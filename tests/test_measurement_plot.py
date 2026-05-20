import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from reactor_app.services import measurement_plot


class MeasurementPlotServiceTests(unittest.TestCase):
    def test_mysql_plot_loader_falls_back_to_python_when_optimized_query_returns_no_points(self):
        mysql_empty = {"ika_actual_rpm": {"channel_code": "ika_actual_rpm", "unit": "rpm", "items": []}}
        python_series = {
            "ika_actual_rpm": {
                "channel_code": "ika_actual_rpm",
                "unit": "rpm",
                "items": [{"measured_at": "2026-04-14T10:00:00+00:00", "numeric_value": 200.0, "unit": "rpm"}],
            }
        }

        with patch.object(measurement_plot, "_db_dialect_name", return_value="mysql"), patch.object(
            measurement_plot,
            "_load_plot_series_mysql",
            return_value=mysql_empty,
        ) as mysql_loader, patch.object(
            measurement_plot,
            "_load_plot_series_python",
            return_value=python_series,
        ) as python_loader:
            result = measurement_plot.load_device_plot_series(
                device_id=1,
                channel_codes=["ika_actual_rpm"],
                since_minutes=60,
                max_points=240,
            )

        mysql_loader.assert_called_once()
        python_loader.assert_called_once()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["channel_code"], "ika_actual_rpm")
        self.assertEqual(len(result[0]["items"]), 1)

    def test_plot_window_loader_returns_shared_window_metadata(self):
        window_end = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        python_series = {
            "temp": {
                "channel_code": "temp",
                "unit": "C",
                "latest_measurement_at": None,
                "items": [],
            }
        }

        with patch.object(measurement_plot, "_db_dialect_name", return_value="sqlite"), patch.object(
            measurement_plot,
            "_load_plot_series_python",
            return_value=python_series,
        ) as python_loader:
            result = measurement_plot.load_device_plot_series_window(
                device_id=1,
                channel_codes=["temp"],
                since_minutes=60,
                max_points=120,
                window_end=window_end,
            )

        python_loader.assert_called_once()
        self.assertEqual(python_loader.call_args.kwargs["window_start"], window_end - timedelta(minutes=60))
        self.assertEqual(python_loader.call_args.kwargs["window_end"], window_end)
        self.assertEqual(result["window_start"], (window_end - timedelta(minutes=60)).isoformat())
        self.assertEqual(result["window_end"], window_end.isoformat())
        self.assertEqual(result["bucket_seconds"], 30)
        self.assertEqual(result["series"], [python_series["temp"]])

    def test_batched_plot_window_loader_preserves_requested_order(self):
        window_end = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        batched_series = {
            (2, "temp"): {
                "device_id": 2,
                "channel_code": "temp",
                "unit": "C",
                "latest_measurement_at": "2026-05-20T11:59:00+00:00",
                "items": [{"measured_at": "2026-05-20T11:59:00+00:00", "numeric_value": 20.0, "unit": "C"}],
            },
            (1, "rpm"): {
                "device_id": 1,
                "channel_code": "rpm",
                "unit": "rpm",
                "latest_measurement_at": "2026-05-20T11:59:30+00:00",
                "items": [{"measured_at": "2026-05-20T11:59:30+00:00", "numeric_value": 100.0, "unit": "rpm"}],
            },
        }

        with patch.object(measurement_plot, "_db_dialect_name", return_value="sqlite"), patch.object(
            measurement_plot,
            "_load_batched_plot_series_python",
            return_value=batched_series,
        ) as python_loader:
            result = measurement_plot.load_batched_device_plot_series_window(
                series_specs=[
                    {"device_id": 2, "channel_code": "temp"},
                    {"device_id": 1, "channel_code": "rpm"},
                ],
                since_minutes=10,
                max_points=60,
                window_end=window_end,
            )

        python_loader.assert_called_once()
        self.assertEqual(result["window_start"], (window_end - timedelta(minutes=10)).isoformat())
        self.assertEqual(result["window_end"], window_end.isoformat())
        self.assertEqual([item["device_id"] for item in result["series"]], [2, 1])
        self.assertEqual([item["channel_code"] for item in result["series"]], ["temp", "rpm"])

    def test_batched_plot_window_loader_uses_cache_for_repeated_window(self):
        measurement_plot._LIVE_PLOT_CACHE.clear()
        window_end = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        batched_series = {
            (1, "rpm"): {
                "device_id": 1,
                "channel_code": "rpm",
                "unit": "rpm",
                "latest_measurement_at": None,
                "items": [],
            }
        }

        with patch.object(measurement_plot, "_db_dialect_name", return_value="sqlite"), patch.object(
            measurement_plot,
            "_load_batched_plot_series_python",
            return_value=batched_series,
        ) as python_loader:
            first = measurement_plot.load_batched_device_plot_series_window(
                series_specs=[{"device_id": 1, "channel_code": "rpm"}],
                since_minutes=10,
                max_points=60,
                window_end=window_end,
                cache_seconds=1,
            )
            second = measurement_plot.load_batched_device_plot_series_window(
                series_specs=[{"device_id": 1, "channel_code": "rpm"}],
                since_minutes=10,
                max_points=60,
                window_end=window_end,
                cache_seconds=1,
            )

        # First call: main query + fallback (both call the same loader); second
        # call hits the cache so no further DB calls.
        self.assertEqual(python_loader.call_count, 2)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])


if __name__ == "__main__":
    unittest.main()
