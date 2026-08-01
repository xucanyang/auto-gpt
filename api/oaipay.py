from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from core.config_store import config_store
from services.chatgpt_core.oaipay_upload import fetch_oaipay_categories

router = APIRouter(prefix="/integrations", tags=["oaipay"])


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


@router.get("/oaipay-categories")
def get_oaipay_categories():
    """Return OAIPay upload categories for the manual upload dialog.

    The retired ``api.integrations`` router is intentionally not mounted
    anymore. Keep this narrow compatibility endpoint mounted at the old
    frontend path and delegate category fetching to the same helper used by
    automatic OAIPay upload so auth headers, fallback endpoints and cache
    parsing stay identical.
    """

    api_url = _safe_str(config_store.get("oaipay_api_url", ""))
    api_key = _safe_str(config_store.get("oaipay_api_key", ""))
    if not api_url or not api_key:
        return {
            "success": False,
            "error": "OAIPay URL/Key not configured",
            "categories": [],
        }

    categories = fetch_oaipay_categories(api_url, api_key, force_refresh=True)
    if not categories:
        return {
            "success": False,
            "error": "未从 OAIPay 拉取到可用分组，请检查 API URL、上传密钥或远端分类接口",
            "categories": [],
        }
    return {"success": True, "categories": categories}
