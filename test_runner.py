from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_app
import test_ai_extension
import test_engineering_retrieval
import test_engineering_evidence
import test_engineering_corpus
import test_engineering_inspection
import test_dimension_trace
import test_feature_revision
import test_visual_review
import test_audit_witness
import test_cad_visual
import test_drawing_registration
suite=unittest.TestSuite([
    unittest.defaultTestLoader.loadTestsFromModule(test_app),
    unittest.defaultTestLoader.loadTestsFromModule(test_ai_extension),
    unittest.defaultTestLoader.loadTestsFromModule(test_engineering_retrieval),
    unittest.defaultTestLoader.loadTestsFromModule(test_engineering_evidence),
    unittest.defaultTestLoader.loadTestsFromModule(test_engineering_corpus),
    unittest.defaultTestLoader.loadTestsFromModule(test_engineering_inspection),
    unittest.defaultTestLoader.loadTestsFromModule(test_dimension_trace),
    unittest.defaultTestLoader.loadTestsFromModule(test_feature_revision),
    unittest.defaultTestLoader.loadTestsFromModule(test_visual_review),
    unittest.defaultTestLoader.loadTestsFromModule(test_audit_witness),
    unittest.defaultTestLoader.loadTestsFromModule(test_cad_visual),
    unittest.defaultTestLoader.loadTestsFromModule(test_drawing_registration),
])
if __name__ == "__main__":
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
