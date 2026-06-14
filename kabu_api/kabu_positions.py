from typing import Any, Callable

import pandas as pd
import requests

from .exceptions import KabuApiException


class KabuApiPositions:
    """kabu.com API 保有銘柄一覧取得"""

    def __init__(self, base_url: str, token: str,
                 token_refresh_callback: Callable[[], str] | None = None):
        self.base_url = base_url
        self._token = token
        self._token_refresh = token_refresh_callback
        self.df_mypositions: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    # 内部: 生API呼び出し
    # ------------------------------------------------------------------
    def _fetch_raw(self, product: int = 0) -> list[dict[str, Any]]:
        """/positions エンドポイントから生レスポンスを取得する"""
        url = f"{self.base_url}/positions?product={product}"
        headers = {"X-API-KEY": self._token}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(f"Failed to connect to {url}: {e}") from e

        # ---- 401 検出時にトークンを再取得して1回リトライ ----
        if resp.status_code == 401 and self._token_refresh is not None:
            print(f"[WARN] 401 detected on /positions (product={product}). "
                  "Re-acquiring token and retrying...")
            self._token = self._token_refresh()
            headers["X-API-KEY"] = self._token
            try:
                resp = requests.get(url, headers=headers, timeout=10)
            except requests.RequestException as e:
                raise KabuApiException(f"Failed to connect to {url} (retry): {e}") from e

        if resp.status_code != 200:
            raise KabuApiException(
                "Positions request failed",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise KabuApiException(
                f"Invalid JSON response: {resp.text}",
                status_code=resp.status_code,
            ) from e

        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------
    def get_positions(self, product: int = 0) -> list[dict[str, Any]]:
        """保有ポジション一覧を dict のリストで取得する（従来互換）"""
        return self._fetch_raw(product)

    def build_df(self) -> pd.DataFrame:
        """現物・信用の全ポジションを取得し、DataFrame を構築して self.df_mypositions に格納する

        DataFrame の構造（Symbol を index とする）:

            Price               float64   買値
            CurrentPrice        float64   現在値
            CurrentHoldQuantity  int64    保有数量 (API: LeavesQty)
            change_pct          float64   変化率(％)
            p5                  bool      変化率 > +5%
            p10                 bool      変化率 > +10%
            m5                  bool      変化率 < -5%
            m10                 bool      変化率 < -10%

        Returns:
            構築された DataFrame（self.df_mypositions と同じオブジェクト）
        """
        chunks: list[pd.DataFrame] = []

        for product in (0, 1):
            positions = self._fetch_raw(product)
            if not positions:
                continue

            rows = []
            for p in positions:
                price = p.get("Price")
                curr = p.get("CurrentPrice")
                change_pct = None
                if price is not None and curr is not None and price != 0:
                    change_pct = (curr - price) / price * 100

                rows.append({
                    "Symbol": p.get("Symbol", ""),
                    "Price": price,
                    "CurrentPrice": curr,
                    "CurrentHoldQuantity": p.get("LeavesQty", 0),
                    "change_pct": change_pct,
                })

            if rows:
                chunks.append(pd.DataFrame(rows).set_index("Symbol"))

        if not chunks:
            self.df_mypositions = pd.DataFrame(
                columns=["Price", "CurrentPrice", "CurrentHoldQuantity",
                         "change_pct", "p5", "p10", "m5", "m10"]
            )
            return self.df_mypositions

        df = pd.concat(chunks)
        # 同一Symbolが現物・信用両方にある場合は信用(1)を優先
        df = df[~df.index.duplicated(keep="last")]

        df["p5"] = df["change_pct"] > 5.0
        df["p10"] = df["change_pct"] > 10.0
        df["m5"] = df["change_pct"] < -5.0
        df["m10"] = df["change_pct"] < -10.0

        self.df_mypositions = df
        return self.df_mypositions

    def list_stock_codes(self, product: int = 0) -> list[str]:
        """保有ポジションから銘柄コードのみのリストを取得する"""
        positions = self.get_positions(product)
        return [p.get("Symbol", "") for p in positions if p.get("Symbol")]