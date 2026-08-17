"""
Token 刷新模块

当前主链路采用纯 RT 方案：
- 主流程 / 状态探测 / 刷新动作只认 OAuth Refresh Token
- Session Token 不再作为主判定链的一部分

文件内保留 session 刷新 helper 仅为兼容历史排障代码，默认业务流程不会调用。
"""

from __future__ import annotations

import logging
import json
import time
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from curl_cffi import requests as cffi_requests

# from ..config.settings import get_settings  # removed: external dep
# from ..database.session import get_db  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.models import Account  # removed: external dep

logger = logging.getLogger(__name__)


@dataclass
class TokenRefreshResult:
    """Token 刷新结果"""
    success: bool
    access_token: str = ""
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    error_message: str = ""
    http_status: int = 0
    error_code: str = ""
    expires_in: int = 0
    expiry_source: str = ""


class TokenRefreshManager:
    """
    Token 刷新管理器。

    现行业务口径：纯 RT。
    Session Token helper 保留但不参与主业务判定。
    """

    # OpenAI OAuth 端点
    SESSION_URL = "https://chatgpt.com/api/auth/session"
    TOKEN_URL = "https://auth.openai.com/oauth/token"

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        *,
        browser_fingerprint: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化 Token 刷新管理器

        Args:
            proxy_url: 代理 URL
        """
        self.proxy_url = proxy_url
        # Status refreshes must not silently manufacture a new browser
        # identity.  Only an already persisted account fingerprint is used.
        self.browser_fingerprint = (
            dict(browser_fingerprint)
            if isinstance(browser_fingerprint, dict) and browser_fingerprint
            else {}
        )
        from .constants import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI
        self._oauth_client_id = OAUTH_CLIENT_ID
        self._oauth_redirect_uri = OAUTH_REDIRECT_URI

    def _browser_fingerprint_object(self):
        """Build a curl-cffi-compatible identity without filling missing data."""
        payload = self.browser_fingerprint
        if not payload:
            return None

        device_id = str(payload.get("device_id") or "").strip()
        user_agent = str(payload.get("user_agent") or "").strip()
        impersonate = str(payload.get("impersonate") or "").strip()
        if not (device_id and user_agent and impersonate):
            return None

        try:
            chrome_major = int(payload.get("chrome_major") or 0)
        except (TypeError, ValueError):
            chrome_major = 0
        chrome_full_version = str(payload.get("chrome_full_version") or "").strip()
        if not chrome_major and chrome_full_version:
            try:
                chrome_major = int(chrome_full_version.split(".", 1)[0])
            except (TypeError, ValueError):
                chrome_major = 0

        try:
            from .utils import coerce_browser_fingerprint

            return coerce_browser_fingerprint(payload)
        except Exception:
            return None

    def _create_session(self) -> cffi_requests.Session:
        """创建 HTTP 会话"""
        fingerprint = self._browser_fingerprint_object()
        impersonate = fingerprint.impersonate if fingerprint else "chrome146"
        try:
            session = cffi_requests.Session(impersonate=impersonate, proxy=self.proxy_url)
        except Exception:
            # A malformed legacy value must not make a token refresh unusable.
            session = cffi_requests.Session(impersonate="chrome146", proxy=self.proxy_url)
            fingerprint = None
        if fingerprint is not None:
            try:
                from .utils import apply_browser_fingerprint

                apply_browser_fingerprint(session, fingerprint)
            except Exception:
                pass
        return session

    def refresh_by_session_token(self, session_token: str) -> TokenRefreshResult:
        """
        使用 Session Token 刷新（仅历史兼容 helper）。

        注意：纯 RT 主链路不会调用此方法。
        """
        result = TokenRefreshResult(success=False)

        try:
            session = self._create_session()

            # 设置会话 Cookie
            session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
                path="/"
            )

            # 请求会话端点
            response = session.get(
                self.SESSION_URL,
                headers={
                    "accept": "application/json",
                    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                },
                timeout=30
            )

            if response.status_code != 200:
                result.error_message = f"Session token 刷新失败: HTTP {response.status_code}"
                logger.warning(result.error_message)
                return result

            data = response.json()

            # 提取 access_token
            access_token = data.get("accessToken")
            if not access_token:
                result.error_message = "Session token 刷新失败: 未找到 accessToken"
                logger.warning(result.error_message)
                return result

            # 提取过期时间
            expires_at = None
            expires_str = data.get("expires")
            if expires_str:
                try:
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                except:
                    pass

            result.success = True
            result.access_token = access_token
            result.expires_at = expires_at

            logger.info(f"Session token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"Session token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_by_oauth_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None
    ) -> TokenRefreshResult:
        """
        使用 OAuth Refresh Token 刷新

        Args:
            refresh_token: OAuth 刷新令牌
            client_id: OAuth Client ID

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False)

        try:
            session = self._create_session()

            # 使用配置的 client_id 或默认值
            client_id = client_id or self._oauth_client_id

            # 构建请求体
            token_data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": self._oauth_redirect_uri
            }

            response = session.post(
                self.TOKEN_URL,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json"
                },
                data=token_data,
                timeout=30
            )

            if response.status_code != 200:
                result.http_status = int(response.status_code or 0)
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}
                if isinstance(error_data, dict):
                    raw_error = error_data.get("error")
                    error_obj = raw_error if isinstance(raw_error, dict) else {}
                    for candidate in (
                        error_obj.get("code"),
                        raw_error if isinstance(raw_error, str) else "",
                        error_data.get("error_code"),
                        error_data.get("code"),
                    ):
                        if str(candidate or "").strip():
                            result.error_code = str(candidate).strip()
                            break
                result.error_message = f"OAuth token 刷新失败: HTTP {response.status_code}"
                logger.warning(f"{result.error_message}, 响应: {response.text[:200]}")
                return result

            data = response.json()
            result.http_status = int(response.status_code or 0)

            # 提取令牌
            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 0)
            try:
                expires_in = max(0, int(float(expires_in)))
            except (TypeError, ValueError):
                expires_in = 0

            if not access_token:
                raw_error = data.get("error")
                error_obj = raw_error if isinstance(raw_error, dict) else {}
                for candidate in (
                    error_obj.get("code"),
                    raw_error if isinstance(raw_error, str) else "",
                    data.get("error_code"),
                    data.get("code"),
                ):
                    if str(candidate or "").strip():
                        result.error_code = str(candidate).strip()
                        break
                result.error_message = "OAuth token 刷新失败: 未找到 access_token"
                logger.warning(result.error_message)
                return result

            # 计算过期时间
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                if expires_in > 0
                else None
            )

            result.success = True
            result.access_token = access_token
            result.refresh_token = new_refresh_token
            result.expires_at = expires_at
            result.expires_in = expires_in
            result.expiry_source = "oauth_expires_in" if expires_in > 0 else ""

            logger.info(f"OAuth token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"OAuth token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_account(self, account: Account) -> TokenRefreshResult:
        """
        刷新账号的 Token（纯 RT 模式）

        仅使用 OAuth Refresh Token 刷新。

        Args:
            account: 账号对象

        Returns:
            TokenRefreshResult: 刷新结果
        """
        if account.refresh_token:
            logger.info(f"尝试使用 OAuth Refresh Token 刷新账号 {account.email}")
            result = self.refresh_by_oauth_token(
                refresh_token=account.refresh_token,
                client_id=account.client_id
            )
            return result

        return TokenRefreshResult(
            success=False,
            error_message="账号没有可用的刷新方式（缺少 refresh_token）"
        )

    def validate_token(self, access_token: str) -> Tuple[bool, Optional[str]]:
        """
        验证 Access Token 是否有效

        Args:
            access_token: 访问令牌

        Returns:
            Tuple[bool, Optional[str]]: (是否有效, 错误信息)
        """
        try:
            session = self._create_session()

            # 调用 OpenAI API 验证 token
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json"
                },
                timeout=30
            )

            if response.status_code == 200:
                return True, None
            elif response.status_code == 401:
                return False, "Token 无效或已过期"
            elif response.status_code == 403:
                return False, "账号可能被封禁"
            else:
                return False, f"验证失败: HTTP {response.status_code}"

        except Exception as e:
            return False, f"验证异常: {str(e)}"


def refresh_account_token(account_id: int, proxy_url: Optional[str] = None) -> TokenRefreshResult:
    """
    刷新指定账号的 Token 并更新数据库

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL

    Returns:
        TokenRefreshResult: 刷新结果
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return TokenRefreshResult(success=False, error_message="账号不存在")

        manager = TokenRefreshManager(proxy_url=proxy_url)
        result = manager.refresh_account(account)

        if result.success:
            # 更新数据库
            update_data = {
                "access_token": result.access_token,
                "last_refresh": datetime.utcnow()
            }

            if result.refresh_token:
                update_data["refresh_token"] = result.refresh_token

            if result.expires_at:
                update_data["expires_at"] = result.expires_at

            crud.update_account(db, account_id, **update_data)

        return result


def validate_account_token(account_id: int, proxy_url: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    验证指定账号的 Token 是否有效

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL

    Returns:
        Tuple[bool, Optional[str]]: (是否有效, 错误信息)
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return False, "账号不存在"

        if not account.access_token:
            return False, "账号没有 access_token"

        manager = TokenRefreshManager(proxy_url=proxy_url)
        return manager.validate_token(account.access_token)
