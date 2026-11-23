"""P3 acceptance must allow additional protected upload points in the collection."""
import unittest
import uuid
from types import SimpleNamespace

from core06.hybrid_retrieval import NAMESPACE
from tools.p3_live_acceptance import verify_demo_point_subset


def points(chunks):
    return [SimpleNamespace(id=str(uuid.uuid5(NAMESPACE, chunk))) for chunk in chunks]


class TestDemoCollection(unittest.TestCase):
    def setUp(self):
        self.demo=[f"demo-chunk-{i:02}" for i in range(13)]

    def test_exact_demo_points(self):
        self.assertEqual(verify_demo_point_subset(self.demo,points(self.demo)),13)

    def test_extra_protected_upload_is_valid(self):
        actual=points(self.demo+["upload-document-01-chunk-1"])
        self.assertEqual(verify_demo_point_subset(self.demo,actual),13)

    def test_missing_demo_point_cannot_be_masked_by_upload(self):
        actual=points(self.demo[:-1]+["upload-document-01-chunk-1"])
        with self.assertRaisesRegex(RuntimeError,"DEMO_POINTS_MISSING"):
            verify_demo_point_subset(self.demo,actual)

    def test_duplicate_demo_chunks_fail(self):
        duplicate=self.demo[:-1]+[self.demo[0]]
        with self.assertRaisesRegex(RuntimeError,"DEMO_POINTS_MISSING"):
            verify_demo_point_subset(duplicate,points(self.demo))

    def test_wrong_expected_count_fails(self):
        with self.assertRaisesRegex(RuntimeError,"DEMO_POINTS_MISSING"):
            verify_demo_point_subset(self.demo,points(self.demo),expected_count=12)


if __name__=="__main__":
    unittest.main()
