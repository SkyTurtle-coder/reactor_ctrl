import unittest
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


if __name__ == "__main__":
    unittest.main()
