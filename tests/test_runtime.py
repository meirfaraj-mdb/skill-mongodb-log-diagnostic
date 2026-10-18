import importlib.util
from pathlib import Path
import unittest

RUNTIME = Path(__file__).resolve().parents[1] / "src" / "mongodb_log_diagnostic_atlas" / "runtime.py"
spec = importlib.util.spec_from_file_location("atlas_runtime", RUNTIME)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class SelectPipelineCloudTests(unittest.TestCase):
    def test_explicit_pipeline_cloud_wins(self):
        self.assertEqual(runtime.select_pipeline_cloud({
            "MONGODB_LOG_DIAG_PIPELINE_CLOUD": "gcp",
            "MONGODB_LOG_DIAG_STORAGE_PROVIDER": "s3",
        }), "gcp")

    def test_maps_storage_provider(self):
        self.assertEqual(runtime.select_pipeline_cloud({
            "MONGODB_LOG_DIAG_STORAGE_PROVIDER": "s3",
        }), "aws")
        self.assertEqual(runtime.select_pipeline_cloud({
            "MONGODB_LOG_DIAG_STORAGE_PROVIDER": "gcs",
        }), "gcp")

    def test_infers_bucket_scheme(self):
        self.assertEqual(runtime.select_pipeline_cloud({"MONGODB_LOG_DIAG_BUCKET": "gs://bucket"}), "gcp")
        self.assertEqual(runtime.select_pipeline_cloud({"MONGODB_LOG_DIAG_BUCKET": "s3://bucket"}), "aws")

    def test_does_not_treat_atlas_runtime_as_pipeline_cloud(self):
        with self.assertRaisesRegex(ValueError, "MONGODB_LOG_DIAG_PIPELINE_CLOUD"):
            runtime.select_pipeline_cloud({"CLOUD_PROVIDER": "atlas"})

    def test_requires_explicit_cloud_for_ambiguous_bucket(self):
        with self.assertRaises(ValueError):
            runtime.select_pipeline_cloud({"MONGODB_LOG_DIAG_BUCKET": "plain-bucket"})


if __name__ == "__main__":
    unittest.main()
