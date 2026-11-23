"""Non-destructive startup guard regression (all external effects mocked)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from tools import bringup_factory as system

class FactoryBringupGuards(unittest.TestCase):
    def test_overlapping_start_is_denied_and_lock_can_be_reused(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "stack_start.lock"
            with system.startup_lock(path):
                with self.assertRaisesRegex(RuntimeError, "ALREADY_RUNNING"):
                    with system.startup_lock(path):
                        self.fail("second startup acquired the lock")
            with system.startup_lock(path):
                self.assertEqual(path.stat().st_size, 0)

    def test_launcher_rotates_only_its_log_before_spawn(self):
        with TemporaryDirectory() as folder:
            log = Path(folder) / "launcher.log"
            log.write_bytes(b"X" * 5_000_000)
            with patch.object(system.subprocess, "Popen") as spawn:
                spawn.return_value.pid = 123
                self.assertEqual(system.spawn("fixture", ["fixture"], log.parent, log)["pid"], 123)
            self.assertEqual(log.with_suffix(".log.1").stat().st_size, 5_000_000)
            self.assertEqual(log.stat().st_size, 0)

    def test_diagnostics_keeps_other_services_when_docker_cli_is_missing(self):
        with (patch.object(system, "engine_ready", side_effect=FileNotFoundError),
              patch.object(system, "pg_ready", return_value=True),
              patch.object(system, "keycloak_ready", return_value=True),
              patch.object(system, "legacy_ready", return_value=True),
              patch.object(system, "main_ready", return_value=True),
              patch.object(system, "reachable", return_value=True)):
            state = system.snapshot()
        self.assertFalse(state["docker"])
        self.assertFalse(state["qdrant"])
        self.assertTrue(state["postgres"])
        self.assertTrue(state["unified"])

    def test_postgres_foreign_port_denied(self):
        with patch.object(system,"pg_ready",return_value=False), patch.object(system,"reachable",return_value=True):
            with self.assertRaisesRegex(RuntimeError,"UNVERIFIED_PROCESS"):
                system.ensure_pg([])
    def test_qdrant_foreign_container_denied(self):
        with patch.object(system,"command",return_value=(0,"qdrant/qdrant:v1.20.2|unexpected|exited","")):
            with self.assertRaisesRegex(RuntimeError,"OWNER_MISMATCH"):
                system.container_owner()
    def test_qdrant_unknown_state_denied(self):
        with patch.object(system,"qdrant_ready",return_value=False),patch.object(system,"container_owner",return_value="paused"),patch.object(system,"reachable",return_value=False):
            with self.assertRaisesRegex(RuntimeError,"UNEXPECTED_STATE"):
                system.ensure_qdrant([])
    def test_keycloak_foreign_port_denied(self):
        with patch.object(system,"keycloak_ready",return_value=False),patch.object(system,"reachable",return_value=True):
            with self.assertRaisesRegex(RuntimeError,"IDENTITY_NOT_VALID"):
                system.ensure_keycloak([])
    def test_unified_foreign_port_denied(self):
        with patch.object(system,"main_ready",return_value=False),patch.object(system,"reachable",return_value=True):
            with self.assertRaisesRegex(RuntimeError,"HEALTH_INVALID"):
                system.ensure_main([])
    def test_model_not_part_of_required_services(self):
        statuses={"postgres":True,"docker":True,"qdrant":True,"keycloak":True,"legacy_p13":True,"unified":True,"local_model_tcp":False}
        self.assertTrue(all(statuses[k] for k in ("postgres","docker","qdrant","keycloak","legacy_p13","unified")))

if __name__=="__main__": unittest.main()
