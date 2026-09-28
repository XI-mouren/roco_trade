from typing import List, Dict, Tuple
from .ocr_recognizer import OCRRecognizer


class PriceParser:
    def __init__(self, ocr: OCRRecognizer, known_markets: List[str]):
        self.ocr = ocr
        self.known_markets = known_markets

    def parse_screenshot(self, image_bytes: bytes) -> Tuple[str, List[Dict]]:
        ocr_results = self.ocr.recognize(image_bytes)
        market_name = self.ocr.find_market_name(ocr_results, self.known_markets)
        if not market_name:
            market_name = "未知市场"
        products = self.ocr.extract_prices(ocr_results)
        return market_name, products

    def merge_products(self, existing: List[Dict], new: List[Dict]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        existing_names = {p["name"] for p in existing}
        added = []
        skipped = []
        for product in new:
            if product["name"] in existing_names:
                skipped.append(product)
            else:
                added.append(product)
        return existing + added, added, skipped
