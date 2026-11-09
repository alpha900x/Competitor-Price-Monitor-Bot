"""Shared Pydantic data contracts (see ARCHITECTURE.md section 6)."""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BACKORDER = "backorder"
    UNKNOWN = "unknown"


class TargetConfig(BaseModel):
    site_id: str
    product_url: str
    requires_js: bool


class ScrapedProduct(BaseModel):
    site_id: str
    product_url: str
    title: str
    price: float | None = None
    currency: str = "USD"
    stock_status: StockStatus = StockStatus.UNKNOWN
    pdf_url: str | None = None
    scraped_at: datetime
    success: bool
    error_message: str | None = None


class SpecSheetData(BaseModel):
    warranty_years: int | None = None
    power_consumption_watts: int | None = None
    key_features: list[str] = Field(default_factory=list)


class DiffResult(BaseModel):
    product_id: int
    price_changed: bool
    price_delta_pct: float | None = None
    stock_changed: bool
    old_stock: str | None = None
    new_stock: str | None = None
    should_alert: bool
