import json

import requests

from .config import KabuApiConfig
from .exceptions import KabuApiException


class KabuApiToken:
    """kabu.com API トークン管理"""

    def __init__(self, config: KabuApiConfig | None = None):
        self.config = config or KabuApiConfig()
        self._tokens: dict[str, str | None] = {"prod": None, "dev": None}

    def get_token(self, env: str = "prod") -> str:
        """指定環境の API トークンを取得して返す"""
        url = f"{self.config.base_url(env)}/token"
        pw = self.config.password(env)

        if not pw:
            raise KabuApiException(
                f"Password for environment '{env}' is not set. "
                f"Check environment variable KABU_API_PW_{env.upper()}."
            )

        payload = {"APIPassword": pw}

        try:
            resp = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(f"Failed to connect to {url}: {e}") from e

        if resp.status_code != 200:
            raise KabuApiException(
                f"Token request failed",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise KabuApiException(
                f"Invalid JSON response: {resp.text}",
                status_code=resp.status_code,
            ) from e

        token: str = data.get("Token", "")
        self._tokens[env] = token
        return token

    def get_tokens(self) -> dict[str, str]:
        """本番・検証両方のトークンを取得し dict で返す"""
        return {
            "prod": self.get_token("prod"),
            "dev": self.get_token("dev"),
        }

    def get_cached_token(self, env: str = "prod") -> str | None:
        """キャッシュ済みのトークンを返す（未取得なら None）"""
        return self._tokens.get(env)