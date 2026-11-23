from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core06 import hybrid_retrieval
from core06 import readiness
from core06.ingest_index_worker import IndexOutcome
from p2_gateway import app as gateway
from tools import runtime_diagnostics
from unified_app import unified_ready


class ReliabilityTests(unittest.TestCase):
    def test_cold_search_checks_vector_service_before_loading_encoder(self):
        client = Mock()
        client.get_collection.side_effect = ConnectionError
        with (patch.object(gateway, "_HYBRID_RETRIEVER", None),
              patch.object(hybrid_retrieval, "qdrant_client", return_value=client),
              patch.object(hybrid_retrieval, "LocalE5") as encoder):
            with self.assertRaises(ConnectionError):
                gateway._p3_retriever()
            client.get_collection.side_effect = None
            result = gateway._p3_retriever()
            self.assertIs(gateway._p3_retriever(), result)
            encoder.assert_called_once()
        client.close.assert_called_once()

    def test_readiness_probes_all_services_and_keeps_exception_private(self):
        with (patch.object(readiness, "postgres_ready", return_value=True),
              patch.object(readiness, "vector_ready", side_effect=ConnectionError("secret URL")),
              patch.object(readiness, "oidc_ready", return_value=True)):
            response = unified_ready()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(json.loads(response.body), {
            "ready": False, "dependencies": {
                "postgres_policy": True, "qdrant_collection": False, "keycloak_oidc": True,
            },
        })

    def test_diagnostics_reports_stale_worker_and_handles_invalid_state(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.assertEqual(runtime_diagnostics.indexer_state(path)["state"], "unavailable")
            path.write_text('{"status":"running","timestamp":100}', encoding="utf-8")
            self.assertEqual(runtime_diagnostics.indexer_state(path, now=280)["state"], "running")
            self.assertEqual(runtime_diagnostics.indexer_state(path, now=281)["state"], "stale")
            self.assertEqual(runtime_diagnostics.indexer_state(path, now=99)["state"], "unavailable")

    def test_host_recovers_to_running_when_queue_is_empty_after_an_outage(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "storage_index_worker_host.pyw"
        loader = SourceFileLoader("storage_worker_fixture", str(script))
        spec = spec_from_loader(loader.name, loader)
        host = module_from_spec(spec)
        loader.exec_module(host)
        stop = SimpleNamespace(exists=lambda: next(polls))
        polls = iter((False, False, True))
        outcomes = iter((ConnectionError("fixture unavailable"), IndexOutcome(None, "empty", 0)))

        def process():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (patch.object(host, "STOPFILE", stop), patch.object(host, "state") as states,
              patch.object(host.time, "sleep"), self.assertLogs(level="WARNING") as logs):
            self.assertEqual(host.run_loop(process), 0)
        self.assertEqual([call.args[0] for call in states.call_args_list],
                         ["running", "degraded", "running", "stopped"])
        self.assertNotIn("fixture unavailable", " ".join(logs.output))
