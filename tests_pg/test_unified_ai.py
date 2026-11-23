"""Policy-safe local model adapter: citation and post-inference ACL regression."""
import unittest
from core06.hybrid_retrieval import Hit
from core06.unified_ai import (
    evidence_from_hits,source_cards,unchanged_and_allowed,generated_response,search_only_response
)
def hit(cid="c1",did="d1",text="allowed text",revision="A"):
    return Hit(cid,did,"Allowed title",text,1,revision,1.0)

class UnifiedAITests(unittest.TestCase):
    def test_keeps_only_hydrated_evidence(self):
        a=[hit("c1","d1","A"*1500),hit("c2","d2","B"*1300)]
        evidence=evidence_from_hits(a)
        self.assertEqual(len(evidence),2)
        self.assertEqual(len(evidence[0]["body"]),700)
        self.assertEqual(len(source_cards(a)[0]["excerpt"]),400)
    def test_post_generation_policy_denies_changes_and_revocation(self):
        a=[hit("c1"),hit("c2","d2")]
        self.assertTrue(unchanged_and_allowed(a,list(reversed(a))))
        self.assertFalse(unchanged_and_allowed(a,[a[0]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1",text="edited"),a[1]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1",revision="B"),a[1]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1"),hit("c1")]))
    def test_rejects_unverifiable_model_output(self):
        a=[hit()]
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":[99]}))
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":["1"]}))
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":[]}))
    def test_source_numbering_is_authoritative(self):
        a=[hit("c1"),hit("c2","d2")]
        res=generated_response(a,{"answer":"Answer [2]","citations":[2,9],"filtered_lines":1})
        self.assertEqual(res["citations"],[2])
        self.assertEqual([x["cited"] for x in res["sources"]],[False,True])
        self.assertTrue(res["verification_required"])
        self.assertEqual(res["mode"],"local_llm_advisory")
    def test_fallback_never_claims_generation(self):
        res=search_only_response([hit()],"No model")
        self.assertEqual(res["mode"],"retrieval_only")
        self.assertTrue(res["citation_warning"])

if __name__=="__main__":unittest.main()
