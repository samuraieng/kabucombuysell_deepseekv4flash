import os


class KabuApiConfig:
    """kabu.com API 接続設定"""

    BASE_PATH = "/kabusapi"

    def __init__(self):
        # 環境変数からホストを取得（デフォルト: localhost）
        self.server_address = os.environ.get("KABU_API_HOST", "localhost")
        # 環境変数からベースポートを取得（デフォルト: 18080）
        base_port = int(os.environ.get("KABU_API_PORT", "18080"))
        self.port_prod = base_port
        self.port_dev = base_port + 1

        self.password_prod = os.environ.get("KABU_API_PW_PROD", "")
        self.password_dev = os.environ.get("KABU_API_PW_DEV", "")

    @property
    def base_url_prod(self) -> str:
        return f"http://{self.server_address}:{self.port_prod}{self.BASE_PATH}"

    @property
    def base_url_dev(self) -> str:
        return f"http://{self.server_address}:{self.port_dev}{self.BASE_PATH}"

    def base_url(self, env: str = "prod") -> str:
        if env == "prod":
            return self.base_url_prod
        elif env == "dev":
            return self.base_url_dev
        else:
            raise ValueError(f"Invalid environment: {env}. Use 'prod' or 'dev'.")

    def password(self, env: str = "prod") -> str:
        if env == "prod":
            return self.password_prod
        elif env == "dev":
            return self.password_dev
        else:
            raise ValueError(f"Invalid environment: {env}. Use 'prod' or 'dev'.")