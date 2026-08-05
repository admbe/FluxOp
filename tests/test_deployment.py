from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from api.deployment import (
    DeploymentQuiesced,
    deployment_lease,
    quiesce_path,
)


class DeploymentLeaseTests(unittest.TestCase):
    def test_quiesce_marker_prevents_new_job_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "flux.duckdb"
            quiesce_path(database).touch()

            with self.assertRaises(DeploymentQuiesced):
                with deployment_lease(database):
                    self.fail("A quiesced deployment must not admit new work.")

    def test_job_lease_is_available_without_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "flux.duckdb"

            with deployment_lease(database):
                self.assertFalse(quiesce_path(database).exists())


if __name__ == "__main__":
    unittest.main()
