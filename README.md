# Global Money Flow & Market Dashboard

APIキー不要の公開データを毎日集め、**最新値カード／自動分析／期間別パフォーマンス表／4段チャート**を1枚のHTMLにまとめるプロジェクトです。まずローカルで試し、その後 GitHub Pages で毎日自動更新できます。

> **位置づけ**: 個人向けの市場モニタリング用サンプルです。機関投資家向けシステムとしてのデータ保証、運用監視、障害対応、ライセンス適合性を備えるものではありません。投資判断の前に、必ず一次情報・各データ提供元の条件を確認してください。

## v2 で追加したもの

- **自動分析パネル**: VIX・ハイイールド債スプレッド・ドル指数・M2・MMF・ステーブルコイン・200日線乖離からリスクオン／オフを機械的にスコアリングし、判定理由を文章で表示
- **期間別パフォーマンス表**: 1週間／1ヶ月／3ヶ月／6ヶ月／1年／2年／全期間の騰落率を一覧表示（データ頻度が粗い系列は比較可能な場合のみ数値、無理な場合は「—」）
- **更新日時の明示**: ページ最上部に最終更新日時（JST）と観測期間、各カードに基準日を表示
- **表示の改善**: 日本語フォント優先のスタック、サブプロット見出しの左寄せ、軸ラベルの自動マージン、数値の等幅表示、モバイル対応、印刷用CSS
- **データ取得の堅牢化**: FRED は CSV → テキスト表 → 保存済みキャッシュ の3段フォールバック。失敗系列は「データ取得状況」に記録して折りたたみ表示

## データと単位

| 指標 | データ元 / シリーズ | 頻度・単位 |
|---|---|---|
| BTC、S&P 500、日経平均、EURO STOXX 50、金 | Yahoo Finance | 日次終値。期間初日を100として相対比較 |
| 米ドル指数、VIX | Yahoo Finance | 日次 |
| 米国M2 | FRED `WM2NS` | 週次、10億米ドル |
| 米国MMF資産 | FRED `MMMFFAQ027S` | 四半期、百万米ドル（1,000で割り10億米ドル表示） |
| ハイイールド債スプレッド | FRED `BAMLH0A0HYM2` | 日次、% |
| 米国債ボラティリティ（代理指標） | FRED `DGS10` | 20営業日の金利日次変化の標準偏差、bp |
| ステーブルコイン時価総額 | DefiLlama | 日次、10億米ドル |

**MOVEに関する注意**: Yahoo Finance の `^MOVE` は ICE BofA MOVE 指数として確認できないため使用しません。米10年債利回りから計算した代理指標を表示しており、正式な MOVE 指数ではありません。

## ローカル実行（Windows）

Python 3.12 を推奨します。

```powershell
cd D:\global-moneyflow-dashboard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

まずネット接続不要のデモで確認します。

```powershell
.\.venv\Scripts\python.exe generate_dashboard.py --demo
Start-Process .\public\index.html
```

実データを取得する場合:

```powershell
.\.venv\Scripts\python.exe generate_dashboard.py
Start-Process .\public\index.html
```

簡易バッチも用意しています。`setup_windows.bat` →（`run_demo_dashboard.bat` または `run_dashboard.bat`）の順に実行してください。

### よく使うオプション

```powershell
# 過去3年分
.\.venv\Scripts\python.exe generate_dashboard.py --lookback-days 1095

# 出力先を変更
.\.venv\Scripts\python.exe generate_dashboard.py --output out\index.html

# Plotly.js もHTMLに同梱（約4MB増。オフライン閲覧向け）
.\.venv\Scripts\python.exe generate_dashboard.py --inline-plotlyjs
```

標準ではグラフ描画ライブラリを CDN から読み込むため、HTML 表示時にインターネット接続が必要です。

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
├── data/cache/            # FRED 取得結果のキャッシュ（Actions で復元）
└── public/
    └── index.html         # 生成HTML（Git管理対象外）
```

## GitHub Pages で公開

1. リポジトリを作成し、プロジェクトファイルを push（例: `main` ブランチ）
2. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** に設定
3. **Actions** タブの `Build and Deploy Dashboard` を実行、または `main` に push
4. 成功後、deploy ジョブに表示される URL で確認。以降は毎日 UTC 23:00（日本時間 08:00）に自動更新

## 留意点

- 無料API・公開CSVは予告なく仕様変更、欠損、遅延、レート制限が起こり得ます。
- yfinance は Yahoo Finance の公式APIではありません。利用条件、再配布・公開範囲を確認してください。
- FRED がメンテナンス中の場合、`data/cache` の前回値で描画されます（「データ取得状況」に表示）。
- GitHub Actions の cron は混雑時に遅延することがあります。厳密な 08:00 更新を保証するものではありません。
- 自動分析はルールベースの機械判定であり、投資助言ではありません。
