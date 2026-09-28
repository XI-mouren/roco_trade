from pydantic import BaseModel
from typing import List


class Config(BaseModel):
    """倒货助手插件配置"""
    db_path: str = "roco_trade.db"
    ocr_lang: str = "ch"
    day_boundary_hour: int = 4
    markets: List[str] = ["潮汐集市1号摊", "潮汐集市2号摊", "潮汐集市3号摊"]
