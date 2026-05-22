import unittest
from pathlib import Path


class NginxDeployConfigTests(unittest.TestCase):
    def test_static_assets_are_proxied_instead_of_aliasing_home_directory(self):
        config_path = Path(__file__).resolve().parents[1] / "deploy" / "nginx_reactor_ctrl.conf"
        config = config_path.read_text(encoding="utf-8")

        self.assertIn("location /static/", config)
        self.assertIn("proxy_pass http://127.0.0.1:5000;", config)
        self.assertNotIn("alias /home/", config)

    def test_old_host_redirects_to_canonical_process_control_domain(self):
        config_path = Path(__file__).resolve().parents[1] / "deploy" / "nginx_reactor_ctrl.conf"
        config = config_path.read_text(encoding="utf-8")

        self.assertIn("server_name v002020.edu.ds.fhnw.ch;", config)
        self.assertIn("server_name u1-process-control.lifesciences.fhnw.ch;", config)
        self.assertIn("listen 443 ssl;", config)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/u1-process-control.lifesciences.fhnw.ch/fullchain.pem;", config)
        self.assertIn("return 301 https://u1-process-control.lifesciences.fhnw.ch$request_uri;", config)


if __name__ == "__main__":
    unittest.main()
