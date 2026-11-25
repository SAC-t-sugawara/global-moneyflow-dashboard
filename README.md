# Global Money Flow & Market Dashboard

無料で取得できる公開データを毎日集め、Plotlyのインタラクティブなダッシュボードを単一HTMLとして生成するプロジェクトです。まずローカルで試し、その後GitHub Pagesへ公開できます。

> **位置づけ**: 個人向けの市場モニタリング用サンプルです。機関投資家向けシステムとしてのデータ保証、運用監視、障害対応、ライセンス適合性を備えるものではありません。投資判断の前に、必ず一次情報・各データ提供元の条件を確認してください。

## データと単位

| 指標 | データ元 / シリーズ | 頻度・単位（提供元の系列定義に従う） |
|---|---|---|
| BTC、S&P 500、日経平均、EURO STOXX 50、金 | Yahoo Finance | 日次終値。各系列の取得初日の値を100として相対比較 |
| 米ドル指数、VIX | Yahoo Finance | 日次 |
| 米国M2 | FRED `WM2NS` | 週次、10億米ドル |
| 米国MMF資産 | FRED `MMMFFAQ027S` | 系列のCSV値を1,000で割り、10億米ドル表示 |
| ハイイールド債スプレッド | FRED `BAMLH0A0HYM2` | 日次、% |
| 米10年債金利変動ボラティリティ（代理指標） | FRED `DGS10` | 20営業日の金利日次変化の標準偏差、bp |
| ステーブルコイン時価総額 | DefiLlama | 日次、10億米ドル |

**MOVEに関する注意**: Yahoo Financeの `^MOVE` はICE BofA MOVE指数として確認できないため使用しません。このプロジェクトでは米10年債利回りから計算した代理指標を表示します。これは正式なMOVE指数ではありません。

## ローカル実行（Windows）

Python 3.12を推奨します。PowerShellでプロジェクトフォルダへ移動して実行します。

```powershell
cd D:\global-moneyflow-dashboard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

まずネットワークアクセス不要のデモデータで生成を確認します。

```powershell
.\.venv\Scripts\python.exe generate_dashboard.py --demo
Start-Process .\public\index.html
```

実データを取得して生成する場合:

```powershell
.\.venv\Scripts\python.exe generate_dashboard.py
Start-Process .\public\index.html
```

初回の実データ取得にはネットワーク接続が必要です。データ元の応答遅延・レート制限等により、一部系列が欠けることがあります。取得できなかった系列は警告を表示し、他のデータの生成は続行します。

### よく使うオプション

```powershell
# 過去3年分
.\.venv\Scripts\python.exe generate_dashboard.py --lookback-days 1095

# 出力先を変更
.\.venv\Scripts\python.exe generate_dashboard.py --output out\index.html

# Plotly.jsもHTML内に埋め込み（約4MB増。オフライン閲覧向け）
.\.venv\Scripts\python.exe generate_dashboard.py --inline-plotlyjs
```

標準ではHTML内のグラフライブラリをCDNから読み込むため、ブラウザでグラフを表示する際にインターネット接続が必要です。`--inline-plotlyjs` を使うとネット接続なしでもグラフを表示できます。

## Windowsの簡易起動ファイル

プロジェクト内の `setup_windows.bat` を一度実行すると、仮想環境を作成して依存関係をインストールします。その後は `run_demo_dashboard.bat`（ダミーデータ）または `run_dashboard.bat`（実データ）を実行できます。

## ローカル実行（macOS / Linux）

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python generate_dashboard.py --demo
```

生成された `public/index.html` をブラウザで開いてください。

## ディレクトリ構成

```text
global-moneyflow-dashboard/
├── .github/workflows/deploy.yml
├── generate_dashboard.py
├── requirements.txt
├── setup_windows.bat
├── run_dashboard.bat
├── run_demo_dashboard.bat
├── README.md
└── public/                 # 実行時に自動作成
    └── index.html          # 生成HTML（Git管理対象外）
```

## GitHub Pagesで公開

1. GitHubでリポジトリを作成し、プロジェクトファイルをPushします（例: `main` ブランチ）。
2. リポジトリの **Settings → Pages → Build and deployment → Source** を **GitHub Actions** に設定します。
3. **Actions** タブから `Build and Deploy Dashboard` を実行するか、`main` にPushします。
4. 成功後、Actionsのdeployジョブに表示されるURLで確認します。以降は毎日UTC 23:00（日本時間08:00）に更新されます。

GitHub Pagesの公開URLは通常 `https://<ユーザー名>.github.io/<リポジトリ名>/` です。ユーザー/組織の特別リポジトリではパスが異なります。無料プランのPages公開条件・リポジトリ公開範囲はGitHubの最新仕様を確認してください。

## 留意点

- 無料API・公開CSVは予告なく仕様変更、欠損、遅延、レート制限が起こり得ます。
- yfinanceはYahoo Financeの公式APIではありません。利用条件、データの再配布・公開範囲を確認してください。
- DefiLlamaの系列定義やFREDの系列単位が変わった場合は、グラフの単位換算を再確認してください。
- GitHub Actionsのcronは混雑時に遅延する場合があります。厳密な08:00更新を保証するものではありません。
- 投資助言ではありません。
