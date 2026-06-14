class KabuApiException(Exception):
    """kabu.com API 呼び出しに関する例外の基底クラス"""

    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.response_body is not None:
            parts.append(f"body={self.response_body}")
        return " | ".join(parts)