import unittest

from app.models.schema import MaterialInfo
from app.services.material import _material_source_record


class TestMaterialRightsProvenance(unittest.TestCase):
    def test_source_record_preserves_reported_license_and_sanitizes_public_urls(self):
        item = MaterialInfo(provider="pexels", duration=5, source_info={
            "asset_id": "pexels-123", "source_page": "https://pexels.com/video/example?tracking=1",
            "license": "Pexels License", "license_url": "https://pexels.com/license?token=private",
            "attribution": "Creator name", "commercial_use": "provider-reported",
            "download_url": "https://private.invalid/file?api_key=secret",
        })
        record = _material_source_record(item, "/private/path/clip.mp4")
        self.assertEqual(record["asset_id"], "pexels-123")
        self.assertEqual(record["source_page"], "https://pexels.com/video/example")
        self.assertEqual(record["license_url"], "https://pexels.com/license")
        self.assertEqual(record["license"], "Pexels License")
        self.assertNotIn("download_url", record)
        self.assertNotIn("secret", str(record))
        self.assertEqual(record["local_file"], "clip.mp4")


if __name__ == "__main__":
    unittest.main()
