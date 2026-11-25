@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Python 3.12 の仮想環境を作成します...
py -3.12 -m venv .venv
if errorlevel 1 (
  echo エラー: Python 3.12 が見つかりません。Python 3.12をインストールしてください。
  pause
  exit /b 1
)

echo [2/3] pipを更新します...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/3] 必要なライブラリをインストールします...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo セットアップが完了しました。
echo run_demo_dashboard.bat でダミーデータ、run_dashboard.bat で実データを生成できます。
pause
exit /b 0

:error
echo.
echo セットアップに失敗しました。インターネット接続とエラーメッセージを確認してください。
pause
exit /b 1
