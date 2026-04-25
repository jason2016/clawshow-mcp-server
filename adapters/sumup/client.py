"""SumUp HTTP 客户端 + 模式切换

使用 requests (sync) 保持与项目现有代码风格一致。
Mock 模式下不发出任何 HTTP 请求，完全离线。
"""

import os
import logging
from typing import Optional

import requests

from .types import SumUpMode
from .mock_responses import generate_mock_response

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sumup.com/v0.1"


class SumUpClient:
    """统一 SumUp 客户端，支持 mock / sandbox / live 三种模式"""

    def __init__(self, api_key: str = "", mode: Optional[SumUpMode] = None):
        self.mode: SumUpMode = mode or os.getenv("SUMUP_MODE", "mock")
        self.api_key = api_key

        if self.mode != "mock" and not api_key:
            raise ValueError(f"SumUp API key required for mode={self.mode}")

        logger.info(f"SumUpClient initialized in {self.mode} mode")

    def post(self, endpoint: str, data: dict) -> dict:
        if self.mode == "mock":
            logger.info(f"[SumUp MOCK] POST {endpoint} data={data}")
            return generate_mock_response(endpoint, data, "POST")

        resp = requests.post(
            f"{BASE_URL}{endpoint}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=data,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get(self, endpoint: str) -> dict:
        if self.mode == "mock":
            logger.info(f"[SumUp MOCK] GET {endpoint}")
            return generate_mock_response(endpoint, None, "GET")

        resp = requests.get(
            f"{BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"
