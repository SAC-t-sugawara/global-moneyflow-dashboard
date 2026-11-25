# Global Money Flow — ライブボード（v4）

APIキー不要の公開データを毎日集め、**スクロールせずに一画面で読めるダッシュボード**を1枚のHTMLとして出力するプロジェクトです。まずローカルで確認し、その後 GitHub Pages で毎日自動更新できます。

> **位置づけ**: 個人向けの市場モニタリング用サンプルです。機関投資家向けシステムとしてのデータ保証、運用監視、障害対応、ライセンス適合性を備えるものではありません。投資判断の前に、必ず一次情報・各データ提供元の条件を確認してください。

## v4 で変わったこと（v3との違い）

v3は「リスク資産／ストレス／暗号資産・信用／動性」の4タブでした。v4では**「マネーフロー」タブを追加**し、世界中の主要資産を網羅して**「マネーがどこに流れているか」を視覚的に表現**します。

### 新機能：マネーフロー可視化（第5タブ）

- **Sankey図（サンキー図）**: 資金の「源泉（M2拡大・MMF流出・ドル安・ステーブル流入・VIX低下）」→「資産クラス（株式・債券・商品・暗号資産・不動産）」→「地域（北米・欧州・日本・アジア・ラテン・大洋州・新興国・先進国）」の3段階フローを矢印の太さで表現
- **世界ヒートマップ**: 地域×資産クラスのマトリクスで、ホットマネーの集中度を色の濃淡で表示
- **資産ローテーションホイール**: 資産クラス別の配分比率をドーナツチャートで表示

### v3から継承の機能

- **スクロール不要の固定レイアウト**: 画面の高さに合わせて自動調整
- **タブ切替式チャート**: 5タブ（リスク資産／ストレス／暗号資産・信用／流動性／マネーフロー）
- **自動ローテーション**: 9秒ごとにタブが自動で切り替わる
- **地合いゲージ**: リスクオンスコアを半円ゲージ＋カウントアップで表示
- **相場テープ**: ヘッダー下を指標が流れるティッカー
- **指標スナップショット**: 右カラムに最新値・1ヶ月比チップ・ミニスパークラインを一覧表示
- **期間別パフォーマンスのヒート表**: 騰落率の大きさをセル背景色の濃淡で表現
- **期間ボタン**: 1M／3M／6M／1Y／全 をワンクリックで切替
- **キーボード操作**: ←→でタブ移動、Fで全画面
- **データ取得状況の常時表示**: ドット（緑＝取得OK／赤＝失敗）で右下に一覧表示

小さな画面（幅1180px以下、または高さ660px以下）では自動的に縦積みレイアウトへ切り替わります。

## データと単位（v4追加あり）

### 主要リスク資産（v3から継続）

| 指標 | データ元 / シリーズ | 頻度・単位 |
|---|---|---|
| BTC、S&P 500、日経平均、EURO STOXX 50、金 | Yahoo Finance | 日次終値。期間初日100として相対比較 |
| 米ドル指数、VIX | Yahoo Finance | 日次 |
| 米国M2 | FRED `WM2NS` | 週次、10億米ドル |
| 米国MMF資産 | FRED `MMMFFAQ027S` | 四半期、百万米ドル（1,000で割り10億米ドル表示） |
| ハイイールド債スプレッド | FRED `BAMLH0A0HYM2` | 日次、% |
| 米国債ボラティティ（代理指標） | FRED `DGS10` | 20営業日の金利日次変化の標準偏差、bp |
| ステーブルコイン時価総額 | DefiLlama | 日次、10億米ドル |

### 世界主要指数（v4追加）

| 指標 | データ元 | 地域 |
|---|---|---|
| FTSE 100 | Yahoo Finance `^FTSE` | 英国（欧州） |
| DAX | Yahoo Finance `^GDAXI` | ドイツ（欧州） |
| 上海総合 | Yahoo Finance `000001.SS` | 中国（アジア） |
| ハンセン | Yahoo Finance `^HSI` | 香港（アジア） |
| Nifty 50 | Yahoo Finance `^NSEI` | インド（アジア） |
| BOVESPA | Yahoo Finance `^BVSP` | ブラジル（ラテン） |
| ASX | Yahoo Finance `^AORD` | オーストラリア（大洋州） |
| TSX | Yahoo Finance `^GSPTSE` | カナダ（北米） |

### 資産クラETF（v4追加）

| 指標 | データ元 | 資産クラス |
|---|---|---|
| TLT | Yahoo Finance `TLT` | 米長期債 |
| IEF | Yahoo Finance `IEF` | 米中期債 |
| HYG | Yahoo Finance `HYG` | ハイールド債 |
| LQD | Yahoo Finance `LQD` | 投資適格債 |
| VNQ | Yahoo Finance `VNQ` | 不動産 |
| EEM | Yahoo Finance `EEM` | 新興国株式 |
| EFA | Yahoo Finance `EFA` | 先進国株式 ex-US |
| 原油WTI | Yahoo Finance `CL=F` | 商品 |

**マネーフローの計算方法**: 各資産の1ヶ月リターンを `tanh(リターン/15%)` で[-1, 1]に正規化した「フロースコア」を使用。M2増加・MMF減少・ドル安・ステーブルコイン増加・VIX低下を「資金の源泉」とみなし、資産クラスと地域への配分を推定しています。価格変動を資金流入の代理指標として使用しており、実際の資金フロー（取引高・資金調達額等）ではありません。

## セットアップ

1. Python 3.11以上をインストール
2. 必要なパッケージをインストール:
   ```bash
   pip install pandas plotly requests yfinance
   ```

## 使い方

### ローカルで実行（ネットワークあり）

```bash
python generate_dashboard.py
```

`index.html` が生成されます。

### デモモード（ネットワークなし、ダミーデータ）

```bash
python generate_dashboard.py --demo
```

### オフライン版（Plotly.jsを同梱）

```bash
python generate_dashboard.py --inline-plotlyjs --output ライブボード見本_v4.html
```

### 出力先を変更

```bash
python generate_dashboard.py --output docs/index.html
```

## GitHub Actions で自動更新

`.github/workflows/update-dashboard.yml` をリポジトリに配置すると、毎日JST 8:00に自動実行されます。

```yaml
name: Update Dashboard
on:
  schedule:
    - cron: '0 23 * * *'  # UTC 23:00 = JST 8:00
  workflow_dispatch:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install pandas plotly requests yfinance
      - run: python generate_dashboard.py --output docs/index.html
      - uses: actions/upload-pages-artifact@v3
        with:
          path: docs/
  deploy:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/deploy-pages@v4
        id: deployment
```

## ファイル構成

```
global-moneyflow-dashboard/
├── generate_dashboard.py      # メインスクリプト（v4）
├── README.md                  # このファイル
├── docs/
│   └── index.html            # 生成されたダッシュボード
└── .github/
    └── workflows/
        └── update-dashboard.yml
```

## 注意事項

- yfinance は非公式の Yahoo Finance API ラッパーす。IT部門のセキュリティ審査で指摘される可能性があります。社内導入の際は一次データソース（Bloomberg、Refinitiv等）への切り替えを検討してください。
- 会社ネットワークではプロキシ設定が必要な場合があります。
- 本ダッシュボードは情報提供のみを目的とした定点観測であり、投資助言・勧誘ではありません。
