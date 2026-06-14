"""
kabu.com API 買い判定・買い注文

buy_list.txt のフォーマット（CSVカンマ区切り、ヘッダーあり）:
    code,price,qty,trigger_price
    9602,0,100,2400

    - code: 東証コード（4桁）
    - price: 指値（0=成り行き）
    - qty: 買い数量
    - trigger_price: 指示出し値

df_buylist カラム:
    code            str     東証コード
    price           float   指値（0=成り行き）
    qty             int     買い数量
    trigger_price   float   指示出し値
    current_price   float   最新現在値
    buy_status      bool    True=現在値<=指示出し値(買いシグナル)
    order_id        object  None=未発注 / 文字列=発行済み(未約定) / code値=約定済み
"""

from typing import Any

import pandas as pd
import requests

from .exceptions import KabuApiException


class KabuApiBuy:
    """kabu.com API 買い判定・買い注文"""

    def __init__(self, data_base_url: str, order_base_url: str,
                 data_token: str, order_token: str, mode: str = "dev"):
        """
        Args:
            data_base_url: データ取得用ベースURL（常に本番環境 / ポート18082）
            order_base_url: 注文用ベースURL（mode=prod → 18082, mode=dev → 18083）
            data_token: データ取得用トークン（常に prod）
            order_token: 注文用トークン（prod環境→prod用, dev環境→dev用）
            mode: "prod" / "dev"
        """
        self.data_base_url = data_base_url
        self.order_base_url = order_base_url
        self._data_token = data_token
        self._order_token = order_token
        self.mode = mode

    # ------------------------------------------------------------------
    # order_id の状態判定（ヘルパー）
    # ------------------------------------------------------------------
    @staticmethod
    def is_ordered(order_id: Any, code: str) -> bool:
        """発行済み（未約定）かどうか。order_id が None でも code でもなければ発行済み"""
        if order_id is None:
            return False
        return str(order_id) != code

    @staticmethod
    def is_settled(order_id: Any, code: str) -> bool:
        """約定済みかどうか。order_id が code と等しければ約定済み"""
        if order_id is None:
            return False
        return str(order_id) == code

    # ------------------------------------------------------------------
    # 現在値の取得
    # ------------------------------------------------------------------
    def get_current_price(self, symbol: str) -> float:
        """GET /board/{symbol}@1 から最新の現在値を取得する"""
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
    # 買い条件の評価
    # ------------------------------------------------------------------
    def evaluate_buy(self, df: pd.DataFrame) -> pd.DataFrame:
        """買い条件を評価し、buy_status を更新した DataFrame を返す

        Args:
            df: 少なくとも code, trigger_price カラムを含む DataFrame

        Returns:
            current_price, buy_status が更新された DataFrame
        """
        df = df.copy()

        current_prices = []
        for code in df["code"]:
            try:
                price = self.get_current_price(code)
            except KabuApiException:
                # 現在値取得エラーの場合は buy_status=False とする
                price = None
            current_prices.append(price)

        df["current_price"] = current_prices

        # buy_status: 現在値 <= 指示出し値 なら True（底値拾い）
        df["buy_status"] = df.apply(
            lambda row: (
                row["current_price"] is not None
                and row["current_price"] <= row["trigger_price"]
            ),
            axis=1,
        )

        return df

    # ------------------------------------------------------------------
    # 発注済みチェック（GET /orders）
    # ------------------------------------------------------------------
    def _fetch_orders(self) -> list[dict[str, Any]]:
        """GET /orders で全注文一覧を取得する"""
        url = f"{self.order_base_url}/orders"
        headers = {"X-API-KEY": self._order_token}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            print(f"  [WARN] GET /orders 失敗: {e}")
            return []

        if resp.status_code != 200:
            print(f"  [WARN] GET /orders 応答 {resp.status_code}")
            return []

        try:
            orders = resp.json()
        except ValueError:
            return []

        return orders if isinstance(orders, list) else []

    def has_pending_buy_order(self, symbol: str) -> bool:
        """未約定の買い注文が存在するかを確認する（発注前チェック用）"""
        orders = self._fetch_orders()
        for order in orders:
            if order.get("Side") != "2":
                continue
            if order.get("Symbol") != symbol:
                continue
            state = order.get("State")
            # State="1"(待機), "2"(処理中), "3"(処理済) は有効な注文
            # State="4"(訂正取消送信中)も一応有効とみなす
            # State="5"(終了)は無効
            if state in ("1", "2", "3", "4"):
                return True
        return False

    # ------------------------------------------------------------------
    # 約定チェック（発行済み注文の状態確認）
    # ------------------------------------------------------------------
    def check_order_settlement(self, df: pd.DataFrame) -> pd.DataFrame:
        """df_buylist の発行済み注文（order_id が発行済み状態）の約定を確認する

        GET /orders で全注文一覧を取得し、発行済みの order_id が
        リストに含まれているか、State/CummQty で判定する。

        ロジック:
            - リストに order_id が含まれていない → 完全約定（レスポンス対象外）
              → order_id = code（約定済みマーク）
            - 含まれている場合：
                State="5":
                    CummQty >= qty → 約定完了 → order_id = code
                    CummQty <  qty → 未達（エラー/取消/失効）→ order_id = None
                State="3"（処理済）:
                    有効な注文 → 何もしない
                State="1"/"2"/"4":
                    処理中 → 何もしない

        Returns:
            order_id が更新された DataFrame
        """
        df = df.copy()
        orders = self._fetch_orders()

        # 買い注文 (Side="2") の order_id → order_info マップ
        buy_orders: dict[str, dict[str, Any]] = {}
        for o in orders:
            if o.get("Side") != "2":
                continue
            oid = o.get("OrderId")
            if oid:
                buy_orders[oid] = o

        for idx, row in df.iterrows():
            oid = row.get("order_id")
            code = str(row["code"])

            # 発行済み（未約定）でなければスキップ
            if not self.is_ordered(oid, code):
                continue

            oid_str = str(oid)

            if oid_str not in buy_orders:
                # レスポンスに存在しない → 完全約定済み
                df.at[idx, "order_id"] = code
                print(f"  [買約定] {code}: 注文 {oid_str} 完全約定")
                continue

            order = buy_orders[oid_str]
            state = str(order.get("State", ""))
            cummqty = int(order.get("CummQty", 0))
            req_qty = int(row["qty"])

            if state == "5":
                if cummqty >= req_qty:
                    # 約定完了
                    df.at[idx, "order_id"] = code
                    print(f"  [買約定] {code}: 注文 {oid_str} State=5, "
                          f"約定数量={cummqty}/{req_qty} → 約定完了")
                else:
                    # 未達（エラー/取消/失効）
                    df.at[idx, "order_id"] = None
                    print(f"  [買未達] {code}: 注文 {oid_str} State=5, "
                          f"約定数量={cummqty}/{req_qty} → 未達、再発注可能に")
            elif state in ("1", "2", "3", "4"):
                # 有効な注文 → 何もしない
                pass
            else:
                # 想定外のState
                pass

        return df

    # ------------------------------------------------------------------
    # 買い注文の発行
    # ------------------------------------------------------------------
    def _send_order(self, symbol: str, price: float, qty: int) -> dict[str, Any]:
        """POST /sendorder で買い注文を発行する

        Args:
            symbol: 銘柄コード
            price: 指値（0=成り行き）
            qty: 買い数量

        Returns:
            レスポンス情報を含む dict
        """
        url = f"{self.order_base_url}/sendorder"
        headers = {"X-API-KEY": self._order_token}

        if price == 0:
            # 成り行き
            front_order_type = 10
            order_price = 0
        else:
            # 指値
            front_order_type = 20
            order_price = price

        body = {
            "Symbol": symbol,
            "Side": "2",           # 2=買い
            "CashMargin": "1",     # 1=現物
            "DelivType": 2,        # 2=お預り金
            "FundType": "AA",      # AA:信用代用
            "AccountType": "4",    # 4=特定口座
            "Qty": qty,
            "FrontOrderType": front_order_type,
            "Price": order_price,
            "ExpireDay": 0,        # 0=当日中
            "MarketCode": 0,       # 0=SOR
            "SecurityType": "1",   # 1=現物
            "Exchange": 9,         # 9=SOR
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(
                f"Failed to send buy order for {symbol}: {e}"
            ) from e

        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.text else {},
        }

    def _cancel_order(self, order_id: str) -> dict[str, Any]:
        """DELETE /cancelorder で注文をキャンセルする

        Args:
            order_id: キャンセルする注文の OrderId

        Returns:
            レスポンス情報を含む dict
        """
        url = f"{self.order_base_url}/cancelorder"
        headers = {"X-API-KEY": self._order_token}
        body = {"OrderId": order_id}

        try:
            resp = requests.delete(url, headers=headers, json=body, timeout=10)
        except requests.RequestException as e:
            raise KabuApiException(
                f"Failed to cancel order {order_id}: {e}"
            ) from e

        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.text else {},
        }

    # ------------------------------------------------------------------
    # 買い処理（1行単位）
    # ------------------------------------------------------------------
    def process_buy(self, row: pd.Series) -> dict[str, Any]:
        """買い条件に合う銘柄1件に対して買い処理を行う

        注文発行のみ。呼び出し元で buy_status に応じて振り分けること。

        Args:
            row: evaluate_buy() の結果の1行

        Returns:
            処理結果を表す dict
        """
        symbol = row["code"]
        limit_price = float(row["price"])
        qty = int(row["qty"])
        trigger_price = float(row["trigger_price"])
        current_price = row.get("current_price")
        oid = row.get("order_id")

        result: dict[str, Any] = {
            "symbol": symbol,
            "limit_price": limit_price,
            "qty": qty,
            "trigger_price": trigger_price,
            "current_price": current_price,
            "order_id": oid,
        }

        # 注文発行
        order_result = self._send_order(symbol, limit_price, qty)
        result["status"] = "ordered"
        result["order_result"] = order_result

        # 発行成功時は OrderId を返す
        if order_result.get("status_code") == 200:
            body = order_result.get("body", {})
            new_order_id = body.get("OrderId")
            if new_order_id:
                result["new_order_id"] = new_order_id
                print(f"  [買注文] {symbol}: OrderId={new_order_id}")
        else:
            result["new_order_id"] = None
            print(f"  [買注文エラー] {symbol}: "
                  f"status={order_result.get('status_code')}, "
                  f"body={order_result.get('body')}")

        return result

    def process_cancel(self, row: pd.Series) -> dict[str, Any]:
        """発行済みの買い注文をキャンセルする

        Args:
            row: df_buylist の1行（order_id が発行済みであることを前提）

        Returns:
            処理結果を表す dict
        """
        symbol = row["code"]
        oid = str(row["order_id"])

        result: dict[str, Any] = {
            "symbol": symbol,
            "order_id": oid,
        }

        cancel_result = self._cancel_order(oid)
        result["status"] = "cancelled"
        result["cancel_result"] = cancel_result

        print(f"  [買取消] {symbol}: OrderId={oid} "
              f"status={cancel_result.get('status_code')}")

        return result