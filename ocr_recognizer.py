from paddleocr import PaddleOCR
from PIL import Image
import numpy as np
import re
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Optional


class OCRRecognizer:
    def __init__(self, lang: str = "ch", model_dir: str = None):
        if model_dir is None:
            model_dir = str(Path(__file__).parent.parent.parent.parent / "modelfile" / "PaddleOCR")
        self.ocr = PaddleOCR(
            use_angle_cls=True,
            lang=lang,
            use_doc_unwarping=False,
            use_doc_orientation_classify=False,
            enable_mkldnn=False,
            det_limit_side_len=1920,
            det_model_dir=f"{model_dir}/PP-OCRv6_medium_det",
            rec_model_dir=f"{model_dir}/PP-OCRv6_medium_rec",
            cls_model_dir=f"{model_dir}/PP-LCNet_x1_0_textline_ori"
        )

    def recognize(self, image_bytes: bytes) -> List[Dict]:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img_array = np.array(img)
        result = self.ocr.ocr(img_array)
        if not result:
            return []
        ocr_result = result[0]
        rec_texts = ocr_result["rec_texts"]
        rec_scores = ocr_result["rec_scores"]
        rec_polys = ocr_result["rec_polys"]
        texts = []
        for text, score, bbox in zip(rec_texts, rec_scores, rec_polys):
            texts.append({
                "text": text,
                "confidence": score,
                "bbox": bbox
            })
        print(f"[OCR调试] 共识别 {len(texts)} 条文本:")
        for i, t in enumerate(texts):
            bbox = t["bbox"]
            x0, y0 = bbox[0][0], bbox[0][1]
            x1, y1 = bbox[1][0], bbox[1][1]
            print(f"  [{i}] '{t['text']}' 置信度={t['confidence']:.2f} 位置=({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})")
        return texts

    def find_market_name(self, ocr_results: List[Dict], known_markets: List[str]) -> Optional[str]:
        for result in ocr_results:
            text = result["text"]
            for market in known_markets:
                if market in text or text in market:
                    return market
        return None

    def extract_prices(self, ocr_results: List[Dict]) -> List[Dict]:
        if not ocr_results:
            return []

        skip_keywords = ["潮汐集市", "今日价格", "价格随波", "只待慧眼", "今日剩余",
                         "浪花币", "小时", "分钟", "/"]
        data_items = []
        for r in ocr_results:
            text = r["text"].strip()
            if not text:
                continue
            if any(kw in text for kw in skip_keywords):
                continue
            data_items.append(r)

        items_with_y = []
        for r in data_items:
            bbox = r["bbox"]
            avg_y = (bbox[0][1] + bbox[2][1]) / 2
            avg_x = (bbox[0][0] + bbox[2][0]) / 2
            items_with_y.append({"text": r["text"], "avg_y": avg_y, "avg_x": avg_x})

        items_with_y.sort(key=lambda x: x["avg_y"])

        rows = []
        current_row = []
        for item in items_with_y:
            if not current_row:
                current_row.append(item)
            else:
                if abs(item["avg_y"] - current_row[0]["avg_y"]) < 40:
                    current_row.append(item)
                else:
                    rows.append(current_row)
                    current_row = [item]
        if current_row:
            rows.append(current_row)

        products = []
        for row in rows:
            row_sorted_x = sorted(row, key=lambda x: x["avg_x"])
            name = None
            price = None
            for item in row_sorted_x:
                if "物资" in item["text"] and name is None:
                    name = item["text"]
            for item in reversed(row_sorted_x):
                m = re.search(r"(\d+)", item["text"])
                if m:
                    val = int(m.group(1))
                    if val > 0:
                        price = val
                        break
            if name and price:
                products.append({"name": name, "price": price})

        print(f"[OCR调试] 过滤后剩余 {len(data_items)} 条数据项:")
        for i, d in enumerate(data_items):
            print(f"  [{i}] '{d['text']}'")
        print(f"[OCR调试] 提取到 {len(products)} 个商品:")
        for p in products:
            print(f"  {p['name']}: {p['price']}元")
        return products
