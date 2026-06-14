# kabu.com API クライアント

[kabu.com](https://kabu.com/) の API を利用して株式の売買注文を自動化する Python ツールです。

## ディレクトリ構成

```
.
├── kabu_com.py                 # メインエントリーポイント
├── buy_list.txt                # 買指示リスト（ユーザー作成）
├── sell_no.txt                 # 売NGリスト（ユーザー作成）
├── kabu_api/
│   ├── __init__.py
│   ├── config.py               # API 接続設定
│   ├── exceptions.py           # 例外定義
│   ├── kabu_buy.py             # 買い注文処理
│   ├── kabu_positions.py       # ポジション情報取得
│   ├── kabu_sell.py            # 売り注文処理
│   ├── kabu_token.py           # トークン管理
│   └── requirements.txt        # 依存パッケージ一覧
```

## セットアップ

### 1. 環境変数の設定

以下の4つの環境変数を設定してください。

```bash
# API サーバーのホスト（デフォルト: localhost）
export KABU_API_HOST="localhost"

# API サーバーのベースポート（デフォルト: 18080）
# 本番環境はこのポート、検証環境は +1 したポートを使用します
export KABU_API_PORT="18080"

# 本番環境のパスワード
export KABU_API_PW_PROD="<本番パスワード>"

# 検証環境のパスワード
export KABU_API_PW_DEV="<検証パスワード>"
```

### 2. 依存パッケージのインストール

```bash
pip install -r kabu_api/requirements.txt
```

## 使用方法

### 1回だけ実行

```bash
python kabu_com.py --mode dev
```

### 60秒ごとに繰り返し（24時間）

```bash
python kabu_com.py --mode dev --cycle 60
```

### 10分ごとに市場時間(9:05〜15:25)のみ繰り返し

```bash
python kabu_com.py --mode prod --cycle 600 --market-hours
```

## 設定ファイル

### 買指示リスト (`buy_list.txt`)

買いたい銘柄を CSV 形式で記述します。

```csv
code,price,qty,trigger_price
9602,0,100,2400
```

| カラム | 型 | 説明 |
|---|---|---|
| `code` | 文字列(4桁) | 東証コード |
| `price` | 数値 | 買い指値（0 の場合は成行） |
| `qty` | 整数 | 買い数量 |
| `trigger_price` | 数値 | この価格を下回ったら買いシグナル |

### 売NGリスト (`sell_no.txt`)

売却したくない銘柄の東証コードを1行に1つずつ記述します。`#` または `//` で始まる行はコメントとして扱われます。

```
# 売りたくない銘柄
6501
# もう一声
7203
```

## コマンドラインオプション

| オプション | 説明 |
|---|---|
| `--mode {prod,dev}` | 売買注文の発行有無（`prod`=実際に発行, `dev`=表示のみ、デフォルト: `dev`） |
| `--cycle <秒>` | 判定ループ間隔（0=1回のみ, 1〜3600=指定秒間隔でループ、デフォルト: 0） |
| `--market-hours` | 市場時間(9:05〜15:25)のみ稼働。`--cycle` と併用時に有効 |
| `--sellnolist <path>` | 売NGリストのパス（デフォルト: `./sell_no.txt`） |
| `--buylist <path>` | 買指示リストのパス（デフォルト: `./buy_list.txt`） |

## 注意事項

- ポジション情報（現在の保有銘柄）は常に本番環境から取得します。
- `--mode` は売買注文の発行有無のみを制御します。データ取得は常に本番環境を使用します。
- 買い注文・売り注文ともに `--mode` に従います。
- 売り処理は毎サイクルごとに `sell_no.txt` を再読み込みします。