import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from PIL import Image
from starlette.testclient import TestClient

os.environ["PANTRY_TEST_MODE"] = "1"

import main
from main import app, init_db, DetectedItem, ReceiptLineItem, ReceiptScanResult


class TestPhotoDiscard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_discard.db")
        os.environ["PANTRY_DB_FILE"] = self.db_path
        if hasattr(main, "DB_FILE"):
            main.DB_FILE = self.db_path
        init_db()

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _create_test_image_bytes(self):
        buf = io.BytesIO()
        img = Image.new("RGB", (200, 200), color="green")
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def _get_temp_files_set(self):
        tmp_dir = tempfile.gettempdir()
        try:
            return set(os.listdir(tmp_dir))
        except Exception:
            return set()

    @patch("main.analyze_storage_photo")
    def test_scan_photo_success_leaves_no_temp_and_no_image_in_db(self, mock_analyze):
        mock_analyze.return_value = (
            [DetectedItem(item_name="Honeycrisp Apples", quantity=4.0, unit="count", location="Fridge", category="Produce/Veggies")],
            "gemini"
        )
        before_temp = self._get_temp_files_set()
        img_bytes = self._create_test_image_bytes()

        res = self.client.post(
            "/api/scan-photo",
            data={"location": "Fridge"},
            files={"file": ("shelf.jpg", img_bytes, "image/jpeg")},
            headers={"x-gemini-key": "test-key"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["added"], 1)

        # Check temp directory: no new orphaned files
        after_temp = self._get_temp_files_set()
        new_temps = after_temp - before_temp
        self.assertEqual(len(new_temps), 0, f"Leaked temp files: {new_temps}")

        # Check DB: items exist, but zero image/blob/base64 data anywhere
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT item_name, quantity, unit, location FROM inventory")
        rows = c.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Honeycrisp Apples")

        # Verify no column in schema holds blobs
        c.execute("PRAGMA table_info(inventory)")
        columns = c.fetchall()
        col_types = [col[2].upper() for col in columns]
        self.assertNotIn("BLOB", col_types)

        conn.close()

    @patch("main.analyze_storage_photo")
    def test_scan_photo_ai_failure_cleans_up_temp_files(self, mock_analyze):
        mock_analyze.side_effect = RuntimeError("503 Service Unavailable: Model Overloaded")
        before_temp = self._get_temp_files_set()
        img_bytes = self._create_test_image_bytes()

        res = self.client.post(
            "/api/scan-photo",
            data={"location": "Fridge"},
            files={"file": ("shelf_err.jpg", img_bytes, "image/jpeg")},
            headers={"x-gemini-key": "test-key"}
        )
        self.assertEqual(res.status_code, 502)

        after_temp = self._get_temp_files_set()
        new_temps = after_temp - before_temp
        self.assertEqual(len(new_temps), 0, f"Leaked temp files on error: {new_temps}")

    def test_scan_photo_invalid_file_cleans_up_temp_files(self):
        before_temp = self._get_temp_files_set()
        corrupt_bytes = b"not a valid image byte stream"

        res = self.client.post(
            "/api/scan-photo",
            data={"location": "Fridge"},
            files={"file": ("corrupt.jpg", corrupt_bytes, "image/jpeg")},
            headers={"x-gemini-key": "test-key"}
        )
        self.assertEqual(res.status_code, 400)

        after_temp = self._get_temp_files_set()
        new_temps = after_temp - before_temp
        self.assertEqual(len(new_temps), 0, f"Leaked temp files on corrupt input: {new_temps}")

    @patch("main.analyze_grocery_receipt")
    def test_scan_receipt_photos_cleaned_up_unconditionally(self, mock_analyze):
        mock_res = ReceiptScanResult(
            store_name="Trader Joe's",
            items=[ReceiptLineItem(
                item_name="Almond Milk",
                quantity=1.0,
                unit="carton",
                default_location="Fridge",
                inventory_category="Dairy",
                matched_shopping_list_index=-1
            )]
        )
        mock_analyze.return_value = (mock_res, "gemini")
        before_temp = self._get_temp_files_set()
        img_bytes = self._create_test_image_bytes()

        res = self.client.post(
            "/api/receipt/scan",
            files=[
                ("files", ("receipt_1.jpg", img_bytes, "image/jpeg")),
                ("files", ("receipt_2.jpg", img_bytes, "image/jpeg")),
            ],
            headers={"x-gemini-key": "test-key"}
        )
        self.assertEqual(res.status_code, 200)

        after_temp = self._get_temp_files_set()
        new_temps = after_temp - before_temp
        self.assertEqual(len(new_temps), 0, f"Leaked temp files on receipt scan: {new_temps}")

    @patch("main.analyze_receipt_pdf")
    def test_scan_receipt_pdf_cleaned_up_unconditionally(self, mock_analyze):
        mock_res = ReceiptScanResult(
            store_name="Whole Foods",
            items=[ReceiptLineItem(
                item_name="Organic Bananas",
                quantity=2.0,
                unit="lbs",
                default_location="Cupboard / Pantry",
                inventory_category="Produce/Veggies",
                matched_shopping_list_index=-1
            )]
        )
        mock_analyze.return_value = (mock_res, "gemini")
        before_temp = self._get_temp_files_set()
        pdf_bytes = b"%PDF-1.4 simulated order pdf content"

        res = self.client.post(
            "/api/receipt/scan-pdf",
            files={"file": ("order.pdf", pdf_bytes, "application/pdf")},
            headers={"x-gemini-key": "test-key"}
        )
        self.assertEqual(res.status_code, 200)

        after_temp = self._get_temp_files_set()
        new_temps = after_temp - before_temp
        self.assertEqual(len(new_temps), 0, f"Leaked temp files on receipt pdf: {new_temps}")


if __name__ == "__main__":
    unittest.main()
