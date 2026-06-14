"""
kabu.com API クライアント メインエントリーポイント

事前に環境変数を設定しておくこと:
    export KABU_API_PW_PROD="<本番パスワード>"
    export KABU_API_PW_DEV="<検証パスワード>"

使用方法:
    # 1回だけ実行
    python kabu_com.py --mode prod

    # 60秒ごとに繰り返し（24時間）
    python kabu_com.py --mode dev --cycle 60

    # 10分ごとに市場時間(9:05-15:25)のみ繰り返し
    python kabu_com.py --mode prod --cycle 600 --market-hours

注意:
    ポジション情報（build_df）は常に本番環境から取得します。
    --mode は売り注文の発行有無のみを制御します。
    買い注文も同様に --mode に従います。
"""

import argparse
import sys
import time
from datetime import datetime, time as dtime

import pandas as pd

from kabu_api import (
    KabuApiBuy,
    KabuApiConfig,
    KabuApiPositions,
    KabuApiSell,
    KabuApiToken,
)

# 市場時間のデフォルト設定
MARKET_START = dtime(9, 5)    # 9:05
MARKET_END = dtime(15, 25)    # 15:25


# ===================================================================
# 売NGリスト
# ===================================================================
def load_sell_no_list(path: str) -> set[str]:
    """売NGリストファイルを読み込み、東証コードの set を返す。

    ファイルが存在しない場合は空の set を返す。
    先頭に空白があっても、# または // で始まる行はコメントとして無視する。
    有効な行は数字4桁のもののみ。それ以外の行は無視する。
    """
    try:
        codes = set()
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                # コメント行（先頭に空白があっても可）
                if s.startswith("#") or s.startswith("//"):
                    continue
                # 数字4桁のみ有効な東証コードとして扱う
                if s.isdigit() and len(s) == 4:
                    codes.add(s)
        print(f"売NGリストを読み込みました: {path} ({len(codes)} 銘柄)")
        return codes
    except FileNotFoundError:
        print(f"売NGリストが見つかりません（スキップ）: {path}")
        return set()


# ===================================================================
# 買指示リスト
# ===================================================================
def load_buy_list(path: str) -> pd.DataFrame:
    """買指示リスト（buy_list.txt）を読み込み、DataFrame を返す。

    ファイルフォーマット（CSVカンマ区切り、ヘッダーあり）:
        code,price,qty,trigger_price
        9602,0,100,2400

    Args:
        path: ファイルパス

    Returns:
        以下のカラムを持つ DataFrame。ファイルがない場合は空の DataFrame。
            code, price, qty, trigger_price, current_price, buy_status, order_id
    """
    default_columns = ["code", "price", "qty", "trigger_price",
                       "current_price", "buy_status", "order_id"]
    try:
        df = pd.read_csv(path, dtype={
            "code": str,
            "price": float,
            "qty": int,
            "trigger_price": float,
        })
        # 必須カラムの存在確認
        required = ["code", "price", "qty", "trigger_price"]
        for col in required:
            if col not in df.columns:
                print(f"エラー: {path} にカラム '{col}' がありません。", file=sys.stderr)
                return pd.DataFrame(columns=default_columns)

        # データ型変換と不正データの除去
        df["code"] = df["code"].astype(str).str.strip()
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("Int64")
        df["trigger_price"] = pd.to_numeric(df["trigger_price"], errors="coerce")

        # 不正行を除去
        df = df.dropna(subset=["price", "qty", "trigger_price"])
        df = df[df["code"].str.match(r"^\d{4}$")].copy()

        # 初期カラム追加
        df["current_price"] = None
        df["buy_status"] = False
        df["order_id"] = None

        print(f"買指示リストを読み込みました: {path} ({len(df)} 行)")
        return df

    except FileNotFoundError:
        print(f"買指示リストが見つかりません（スキップ）: {path}")
        return pd.DataFrame(columns=default_columns)
    except Exception as e:
        print(f"買指示リスト読み込みエラー: {e}", file=sys.stderr)
        return pd.DataFrame(columns=default_columns)


def merge_order_ids(df_new: pd.DataFrame, df_old: pd.DataFrame | None) -> pd.DataFrame:
    """新しく読み込んだ df_buylist に、前回の order_id をマージする。

    マージ条件: code, price, qty, trigger_price の4列が一致する行

    Args:
        df_new: 新しくファイルから読み込んだ DataFrame
        df_old: 前回の df_buylist（None の可能性あり）

    Returns:
        order_id が引き継がれた DataFrame
    """
    if df_old is None or df_old.empty:
        return df_new

    merge_keys = ["code", "price", "qty", "trigger_price"]
    # 新旧両方にキー列が存在することを確認
    missing = [k for k in merge_keys if k not in df_old.columns]
    if missing:
        return df_new

    # マージ用に一時的なキー列を作成
    df_new = df_new.copy()
    df_new["_merge_key"] = df_new[merge_keys].astype(str).agg("-".join, axis=1)
    df_old = df_old.copy()
    df_old["_merge_key"] = df_old[merge_keys].astype(str).agg("-".join, axis=1)

    # 旧データの order_id マップ
    old_order_map = df_old.set_index("_merge_key")["order_id"].to_dict()

    # 新しい行に order_id を設定
    df_new["order_id"] = df_new["_merge_key"].map(old_order_map)

    df_new = df_new.drop(columns=["_merge_key"])
    return df_new


# ===================================================================
# 市場時間ユーティリティ
# ===================================================================
def is_market_hours(now: datetime | None = None) -> bool:
    """現在時刻が市場時間内(9:05-15:25)かどうかを返す。

    Args:
        now: 判定対象の時刻。None の場合は現在時刻を使用。

    Returns:
        市場時間内なら True
    """
    if now is None:
        now = datetime.now()
    t = now.time()
    return MARKET_START <= t <= MARKET_END


def next_market_start(now: datetime | None = None) -> float:
    """次の市場開始時刻(9:05)までの待機時間(秒)を返す。

    現在時刻が市場時間外の場合のみ呼ばれることを想定。
    市場時間内の場合は 0.0 を返す。
    """
    if now is None:
        now = datetime.now()
    t = now.time()

    if is_market_hours(now):
        return 0.0

    # 今日の MARKET_START を生成
    today_start = datetime.combine(now.date(), MARKET_START)
    # 今日の MARKET_END を生成
    today_end = datetime.combine(now.date(), MARKET_END)

    if t < MARKET_START:
        # まだ今日の市場開始前 → 今日の開始時刻まで待つ
        return (today_start - now).total_seconds()
    else:
        # 今日の市場終了後 → 翌営業日の開始時刻まで待つ
        # （簡易的に24時間後を返す）
        next_day = today_start.replace(day=today_start.day + 1)
        return (next_day - now).total_seconds()


# ===================================================================
# 売りサイクル
# ===================================================================
def run_sell_cycle(config: KabuApiConfig, sell_mode: str,
                   sell_no_path: str) -> int:
    """1サイクル分の売り判定・注文処理を実行する。

    Args:
        config: API設定
        sell_mode: "prod" / "dev"
        sell_no_path: 売NGリストファイルのパス（各サイクルで再読み込み）

    Returns:
        売り注文を発行した銘柄数
    """
    # 毎サイクル sell_no.txt を再読み込み
    sell_no_list = load_sell_no_list(sell_no_path)

    token_mgr = KabuApiToken(config)

    # データ取得（ポジション・現在値）は常に本番トークン
    data_token = token_mgr.get_token("prod")
    # 注文用トークンは mode に応じて切り替え（同じなら再利用してトークン無効化を防止）
    order_token = data_token if sell_mode == "prod" else token_mgr.get_token(sell_mode)
    print(f"[prod] Token: {data_token[:8]}...")
    if sell_mode != "prod":
        print(f"[{sell_mode}] Order Token: {order_token[:8]}...")

    base_url = config.base_url("prod")

    # 保有ポジション取得
    positions_api = KabuApiPositions(
        base_url, data_token,
        token_refresh_callback=lambda: token_mgr.get_token("prod"),
    )
    print("\n[prod] 保有ポジションを取得中...")
    try:
        df = positions_api.build_df()
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 0

    if df.empty:
        print("保有銘柄はありません。")
        return 0

    print(f"\n=== 保有銘柄一覧 ({len(df)} 銘柄) ===\n")
    print(df.to_string())
    print()

    # 注文用ベースURL（mode によって切り替え）
    order_base_url = config.base_url(sell_mode)
    # 売り条件判定
    sell_api = KabuApiSell(
        data_base_url=base_url,
        order_base_url=order_base_url,
        data_token=data_token,
        order_token=order_token,
        mode=sell_mode,
        sell_no_list=sell_no_list,
    )
    df_eval = sell_api.evaluate_and_sort(df)
    targets = df_eval[df_eval["sell_signal"]]

    if targets.empty:
        print("売り条件に合う銘柄はありません。")
        return 0

    print(f"\n=== 売り条件適合銘柄 ({len(targets)} 銘柄) ===\n")
    ordered_count = 0
    for symbol, row in targets.iterrows():
        result = sell_api.process_sell(row)
        print(f"  [{result['status']}] {result['symbol']}")
        print(f"    保有数量: {result.get('hold_qty', '?')}")
        print(f"    売り数量: {result.get('sell_qty', '?')}")
        print(f"    買値: {row['Price']:.0f}")
        print(f"    最新現在値: {result.get('latest_price', 0):.0f}")
        print(f"    トリガー価格: {result.get('trigger_price', 0):.0f}")
        print(f"    売り指値: {result.get('sell_price', 0):.0f}")
        print(f"    理由: {result.get('sell_reason', '?')}")
        print(f"    収益額: {result.get('profit', 0):.0f}")
        if result["status"] == "error":
            print(f"    エラー理由: {result.get('reason', '?')}")
        elif result["status"] == "skipped":
            print(f"    スキップ理由: {result.get('reason', '?')}")
        elif result["status"] == "ordered":
            print(f"    注文結果: {result.get('order_result', '?')}")
            ordered_count += 1
        print()

    return ordered_count


# ===================================================================
# 買いサイクル
# ===================================================================
def run_buy_cycle(config: KabuApiConfig, buy_mode: str,
                  df_buylist: pd.DataFrame) -> pd.DataFrame:
    """1サイクル分の買い判定・注文処理を実行する。

    Args:
        config: API設定
        buy_mode: "prod" / "dev"
        df_buylist: 買指示リスト（load_buy_list の戻り値）

    Returns:
        更新後の df_buylist（order_id の状態が反映されている）
    """
    if df_buylist.empty:
        print("買指示リストは空です。")
        return df_buylist

    token_mgr = KabuApiToken(config)
    data_token = token_mgr.get_token("prod")
    # 同じ環境ならトークンを再利用（トークン無効化を防止）
    order_token = data_token if buy_mode == "prod" else token_mgr.get_token(buy_mode)

    base_url = config.base_url("prod")
    order_base_url = config.base_url(buy_mode)

    buy_api = KabuApiBuy(
        data_base_url=base_url,
        order_base_url=order_base_url,
        data_token=data_token,
        order_token=order_token,
        mode=buy_mode,
    )

    df = df_buylist.copy()

    print(f"\n=== 買い処理 ({len(df)} 行) ===\n")

    # ---- STEP 4: 発行済み注文の約定チェック ----
    print("【STEP 4】発行済み注文の約定チェック...")
    df = buy_api.check_order_settlement(df)

    # ---- STEP 2: 現在値取得 + STEP 3: buy_status 再計算 ----
    print("【STEP 2/3】現在値取得・buy_status 再計算...")
    df = buy_api.evaluate_buy(df)

    # ---- 結果表示 ----
    print(f"\n買指示ステータス:")
    for _, row in df.iterrows():
        status_str = "買い" if row["buy_status"] else "様子見"
        oid_str = str(row.get("order_id", ""))
        cp_str = f"{row['current_price']:.0f}" if row["current_price"] is not None else "N/A"
        print(f"  {row['code']}: 現在値={cp_str}, "
              f"指示値={row['trigger_price']:.0f}, "
              f"status={status_str}, "
              f"order_id={oid_str}")

    # ---- STEP 5: buy_status=True の行 → 発注 ----
    print("\n【STEP 5】買いシグナル処理...")
    for idx, row in df.iterrows():
        code = row["code"]
        oid = row.get("order_id")

        if not row["buy_status"]:
            continue

        # 約定済みスキップ
        if buy_api.is_settled(oid, code):
            print(f"  {code}: 既に約定済み、スキップ")
            continue

        # 発行済みスキップ
        if buy_api.is_ordered(oid, code):
            print(f"  {code}: 既に注文済み (order_id={oid})、スキップ")
            continue

        # 発注
        result = buy_api.process_buy(row)
        new_order_id = result.get("new_order_id")
        if new_order_id:
            df.at[idx, "order_id"] = new_order_id
            print(f"  → order_id 設定: {new_order_id}")
        else:
            print(f"  → 発注失敗")

    # ---- STEP 6: buy_status=False の行 → キャンセル ----
    print("\n【STEP 6】買いシグナルOFF → キャンセル処理...")
    for idx, row in df.iterrows():
        code = row["code"]
        oid = row.get("order_id")

        if row["buy_status"]:
            continue

        # 発行済みのみキャンセル
        if buy_api.is_ordered(oid, code):
            buy_api.process_cancel(row)
            df.at[idx, "order_id"] = None
            print(f"  → order_id クリア")
        else:
            # None か約定済み → 何もしない
            pass

    return df


# ===================================================================
# メイン
# ===================================================================
def main():
    parser = argparse.ArgumentParser(description="kabu.com API 操作")
    parser.add_argument(
        "--mode",
        choices=["prod", "dev"],
        default="dev",
        help="売り/買い注文の発行有無 (prod=実際に発行, dev=表示のみ, デフォルト: dev)",
    )
    parser.add_argument(
        "--sellnolist",
        default="./sell_no.txt",
        help="売NGリストファイルのパス (デフォルト: ./sell_no.txt)",
    )
    parser.add_argument(
        "--buylist",
        default="./buy_list.txt",
        help="買指示リストファイルのパス (デフォルト: ./buy_list.txt)",
    )
    parser.add_argument(
        "--cycle",
        type=int,
        default=0,
        help="判定ループ間隔(秒)。0=1回のみ実行, 1..3600=指定秒間隔でループ",
    )
    parser.add_argument(
        "--market-hours",
        action="store_true",
        help="市場時間(9:05-15:25)のみ稼働。--cycle と併用時に有効",
    )
    args = parser.parse_args()
    sell_mode = args.mode
    cycle_sec = args.cycle
    market_hours_only = args.market_hours

    if cycle_sec < 0 or cycle_sec > 3600:
        print("エラー: --cycle は 0〜3600 の範囲で指定してください。", file=sys.stderr)
        sys.exit(1)

    if cycle_sec == 0 and market_hours_only:
        print("--market-hours は --cycle と併用してください。（--cycle 0 の場合は意味がありません）")
        sys.exit(1)

    # 設定（トークン取得のための config）
    config = KabuApiConfig()

    # 買い指示リスト（ループ間で保持）
    df_buylist_prev: pd.DataFrame | None = None

    if cycle_sec <= 0:
        # ---- 1回だけ実行 ----
        run_sell_cycle(config, sell_mode, args.sellnolist)

        # 買い処理（1回）
        df_buylist = load_buy_list(args.buylist)
        if not df_buylist.empty:
            df_buylist = merge_order_ids(df_buylist, None)
            run_buy_cycle(config, sell_mode, df_buylist)
    else:
        # ---- ループ実行 ----
        print(f"ループモード: {cycle_sec}秒間隔"
              f"{'（市場時間のみ）' if market_hours_only else '（24時間）'}")
        print(f"市場時間: {MARKET_START.strftime('%H:%M')}〜{MARKET_END.strftime('%H:%M')}")
        print("Ctrl+C で停止します。\n")

        while True:
            try:
                # 市場時間チェック
                if market_hours_only and not is_market_hours():
                    wait_sec = next_market_start()
                    if wait_sec > 0:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"市場時間外です。{wait_sec:.0f}秒後に再開します。")
                        time.sleep(min(wait_sec, 60))
                        continue
                    else:
                        # 市場時間内になった
                        pass

                # 1サイクル実行
                print(f"\n{'='*60}")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] サイクル開始")
                print(f"{'='*60}")

                # ---- 売り処理（sell_no.txt 毎サイクル再読み込み） ----
                run_sell_cycle(config, sell_mode, args.sellnolist)

                # ---- 買い処理 ----
                print(f"\n{'─'*40}")
                print("買い処理開始")
                print(f"{'─'*40}")
                df_buylist_new = load_buy_list(args.buylist)
                if not df_buylist_new.empty:
                    df_buylist_new = merge_order_ids(df_buylist_new, df_buylist_prev)
                    df_buylist_updated = run_buy_cycle(config, sell_mode, df_buylist_new)
                    df_buylist_prev = df_buylist_updated
                else:
                    print("買指示リストが空のためスキップ")
                    df_buylist_prev = None

                # 次のサイクルまで待機
                print(f"\n次のサイクルまで {cycle_sec}秒 待機します...")
                # 市場時間モードの場合、待機中に市場終了を跨ぐ可能性があるので
                # 細かく sleep してチェックできるようにする
                if market_hours_only:
                    remaining = cycle_sec
                    while remaining > 0:
                        if not is_market_hours():
                            print("市場時間外になりました。待機を中断します。")
                            break
                        wait = min(remaining, 5)
                        time.sleep(wait)
                        remaining -= wait
                else:
                    time.sleep(cycle_sec)

            except KeyboardInterrupt:
                print("\n\nユーザーにより中断されました。")
                break
            except Exception as e:
                print(f"\n予期せぬエラー: {e}", file=sys.stderr)
                print(f"次のサイクルまで {cycle_sec}秒 待機して継続します。")
                time.sleep(cycle_sec)


if __name__ == "__main__":
    main()