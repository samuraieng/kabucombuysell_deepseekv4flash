from typing import Any

import pandas as pd
import requests

from .exceptions import KabuApiException


class KabuApiSell:
    """kabu.com API 売り判定・売り注文"""

    def __init__(self, data_base_url: str, order_base_url: str,
                 data_token: str, order_token: str, mode: str = "dev",
                 sell_no_list: set[str] | None = None):
        """
        Args:
            data_base_url: データ取得用ベースURL（常に本番環境 / ポート18082）
            order_base_url: 注文用ベースURL（mode=prod → 18082, mode=dev → 18083）
            data_token: データ取得用トークン（常に prod）
            order_token: 注文用トークン（prod環境→prod用, dev環境→dev用）
            mode: "prod" / "dev"
            sell_no_list: 売NGリスト（東証コードのset）。含まれる銘柄は売却対象外
        """
        self.data_base_url = data_base_url
        self.order_base_url = order_base_url
        self._data_token = data_token
        self._order_token = order_token
        self.mode = mode
        self.sell_no_list = sell_no_list if sell_no_list is not None else set()

    # ------------------------------------------------------------------
    # 売り条件の評価とソート
    # ------------------------------------------------------------------
    def evaluate_and_sort(self, df: pd.DataFrame) -> pd.DataFrame:
        """売り条件を評価し、収益額降順でソートした DataFrame を返す

        DataFrame に以下のカラムを追加:
            sell_signal (bool):   売り条件を満たすか
            trigger_price (float): 条件のトリガー価格
            sell_reason (str):    "p5+10000" / "p10" / ""
            profit (float):       (CurrentPrice - Price) * CurrentHoldQuantity

        条件A (p5+10000):
            p5 == True かつ (CurrentPrice - Price) * 100 >= 10000
            → trigger_price = Price + 100

        条件B (p10):
            条件Aに該当せず、かつ p10 == True
            → trigger_price = Price * 1.1
        """
        df = df.copy()

        profit = (df["CurrentPrice"] - df["Price"]) * df["CurrentHoldQuantity"]
        df["profit"] = profit

        # 条件A: p5 かつ 100株あたり収支 >= 10000円
        cond_a = df["p5"] & ((df["CurrentPrice"] - df["Price"]) * 100 >= 10000)
        df["sell_signal"] = cond_a
        df["trigger_price"] = df["Price"] + 100.0
        df["sell_reason"] = "p5+10000"

        # 条件B: 条件Aに該当しない かつ p10
        cond_b = ~cond_a & df["p10"]
        df["sell_signal"] = df["sell_signal"] | cond_b
        df.loc[cond_b, "trigger_price"] = df.loc[cond_b, "Price"] * 1.1
        df.loc[cond_b, "sell_reason"] = "p10"

        # 売り対象外は trigger_price / sell_reason をクリア
        df.loc[~df["sell_signal"], "trigger_price"] = None
        df.loc[~df["sell_signal"], "sell_reason"] = ""

        # 収益額降順でソート
        df = df.sort_values("profit", ascending=False)

        # 売NGリストに含まれる銘柄は売却対象外とする
        mask_no_sell = df.index.isin(self.sell_no_list)
        df.loc[mask_no_sell, "sell_signal"] = False
        df.loc[mask_no_sell, "sell_reason"] = "sell_no_list"
        df.loc[mask_no_sell, "trigger_price"] = None

        return df

    # ------------------------------------------------------------------
    # 現在値の再取得
    # ------------------------------------------------------------------
    def get_current_price(self, symbol: str) -> float:
        """GET /board/{symbol}@1 から最新の現在値を取得する（常に data_base_url / prod, 東証固定）

        Args:
            symbol: 銘柄コード（例: "9602"）

        Returns:
            現在値（CurrentPrice）
        """
        url = f"{self.data_base_url}/board/{symbol}@1"
        headers = {"X-API-KEY": self._data_token}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(
                f"Failed to get current price for {symbol}: {e}"
            ) from e

        if resp.status_code != 200:
            raise KabuApiException(
                f"get_current_price failed for {symbol}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise KabuApiException(
                f"Invalid JSON for {symbol}: {resp.text}",
                status_code=resp.status_code,
            ) from e

        try:
            return float(data["CurrentPrice"])
        except KeyError:
            raise KabuApiException(
                f"get_current_price: 'CurrentPrice' not in response for {symbol}. "
                f"Response keys: {list(data.keys())}, body: {resp.text[:500]}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

    # ------------------------------------------------------------------
    # 発注済みチェック
    # ------------------------------------------------------------------
    def has_pending_sell_order(self, symbol: str) -> bool:
        """GET /orders で未約定の売り注文が存在するかを確認する

        Args:
            symbol: 銘柄コード（例: "9602"）

        Returns:
            未約定の売り注文が存在すれば True
        """
        url = f"{self.order_base_url}/orders"
        headers = {"X-API-KEY": self._order_token}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            print(f"  [WARN] GET /orders 失敗 ({symbol}): {e}")
            return False

        if resp.status_code != 200:
            print(f"  [WARN] GET /orders 応答 {resp.status_code} ({symbol})")
            return False

        try:
            orders = resp.json()
        except ValueError:
            return False

        if not isinstance(orders, list):
            return False

        for order in orders:
            if order.get("Side") != "1":
                continue
            if order.get("Symbol") != symbol:
                continue
            state = order.get("State")
            if state in (1, 2):
                return True

        return False

    # ------------------------------------------------------------------
    # 売り注文の実行
    # ------------------------------------------------------------------
    def _send_order(self, symbol: str, price: float, qty: int) -> dict[str, Any]:
        """POST /sendorder で売り注文を発行する（order_base_url を使用）"""
        url = f"{self.order_base_url}/sendorder"
        headers = {"X-API-KEY": self._order_token}
        body = {
            "Symbol": symbol,
            "Side": "1",           # 1=売り
            "CashMargin": "1",     # 1=現物
            "DelivType": "0",      # 0=普通
            "FundType": "  ",      # 空白＝なし
            "AccountType": "4",    # 4=特定口座
            "Qty": qty,
            "FrontOrderType": 20,  # 20=指値
            "Price": price,
            "ExpireDay": 0,        # 0=当日中
            "MarketCode": 0,       # 0=SOR
            "SecurityType": "1",   # 1=現物
            "Exchange": 9,         # 9=SOR
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(
                f"Failed to send sell order for {symbol}: {e}"
            ) from e

        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.text else {},
        }

    def process_sell(self, row: pd.Series) -> dict[str, Any]:
        """売り条件に合う銘柄1件に対して売り処理を行う

        1. 現在値を最新取得
        2. 指値を決定: max(最新現在値, trigger_price)
        3. 数量を100株単位に丸める（0株ならスキップ）
        4. modeに応じて注文発行 or 表示

        Args:
            row: evaluate_and_sort() の結果の1行

        Returns:
            処理結果を表す dict
        """
        symbol = row.name  # index = Symbol
        trigger_price = float(row["trigger_price"])
        hold_qty = int(row["CurrentHoldQuantity"])

        # 未約定の売り注文があればスキップ
        if self.has_pending_sell_order(symbol):
            return {
                "symbol": symbol,
                "status": "skipped",
                "hold_qty": hold_qty,
                "sell_qty": 0,
                "trigger_price": trigger_price,
                "sell_reason": str(row["sell_reason"]),
                "profit": float(row["profit"]),
                "reason": "未約定の売り注文が既に存在します",
            }

        # 100株単位に切り捨て
        qty = (hold_qty // 100) * 100
        if qty <= 0:
            return {
                "symbol": symbol,
                "status": "skipped",
                "hold_qty": hold_qty,
                "sell_qty": qty,
                "reason": f"保有数量 {hold_qty} は100株未満",
            }

        # 最新現在値を再取得
        try:
            latest_price = self.get_current_price(symbol)
        except KabuApiException as e:
            return {
                "symbol": symbol,
                "status": "error",
                "hold_qty": hold_qty,
                "sell_qty": qty,
                "trigger_price": trigger_price,
                "sell_reason": str(row["sell_reason"]),
                "profit": float(row["profit"]),
                "reason": f"現在値取得エラー: {e}",
            }

        # 指値 = max(最新現在値, trigger_price)
        sell_price = max(latest_price, trigger_price)

        result: dict[str, Any] = {
            "symbol": symbol,
            "hold_qty": hold_qty,
            "sell_qty": qty,
            "latest_price": latest_price,
            "trigger_price": trigger_price,
            "sell_price": sell_price,
            "sell_reason": str(row["sell_reason"]),
            "profit": float(row["profit"]),
        }

        # 注文発行（dev モードでも order_base_url に送信する）
        order_result = self._send_order(symbol, sell_price, qty)
        result["status"] = "ordered"
        result["order_result"] = order_result

        return result