# 配信カルテ

VTuberのYouTube配信アーカイブを診断するサービス「配信カルテ」のローカルプロトタイプです。
公開YouTube動画URLをGemini APIへ渡し、冒頭30分を重点的に分析したカルテを表示します。

## 今できること

- YouTube URL入力フォーム
- FastAPIバックエンド経由のGemini API分析
- 診断中の進捗アニメーション
- 診断した動画のタイトル・サムネイル表示
- 配信タイプ、総合点、各種指標の表示
- 冒頭30分の簡易音声解析による無言率
- 配信テンションマップ
- クリックでYouTubeの該当時間へ移動できる切り抜き候補、改善処方、共有カード
- PC・スマートフォン対応

Geminiの動画理解による推定値と、音量しきい値ベースの簡易無言率を表示します。無言率はBGMやゲーム音がある場合、実際の発話なし時間とはずれることがあります。

## Windowsで起動する

1. このフォルダで依存パッケージをインストールします。

```bash
pip install -r requirements.txt
```

2. `.env.example` をコピーして `.env` を作り、Gemini APIキーを設定します。

```bash
copy .env.example .env
```

`.env` の中身:

```env
GEMINI_API_KEY=あなたのAPIキー
GEMINI_MODEL=gemini-2.5-flash
```

3. FastAPIサーバーを起動します。

```bash
python -m uvicorn backend.app:app --reload --port 8000
```

4. ブラウザで `http://localhost:8000` を開きます。

フロントエンドもFastAPIから配信されるため、別の静的サーバーは不要です。

## 注意

- 対応するのは公開YouTube動画のみです。
- APIキーは `.env` から読み込みます。コードには書かないでください。
- APIキーや動画URLをアプリ側でログ出力しない実装にしています。
- Gemini APIの失敗時は、画面に日本語のエラーを表示します。

## Cloudflare Pages + Render に公開する

Cloudflare Pagesには静的フロントだけを置き、FastAPIはRenderなどのPythonサーバーに置きます。
`yt-dlp` と `ffmpeg` を使うため、Cloudflare Pages単体ではバックエンドまで動かせません。

### 1. RenderにAPIを作る

1. GitHubにこのフォルダをpushします。
2. Renderで `New Web Service` を作り、このリポジトリを選びます。
3. `render.yaml` を使うか、手動で次を設定します。

```bash
Build Command: pip install -r requirements.txt
Start Command: uvicorn backend.app:app --host 0.0.0.0 --port $PORT
```

4. RenderのEnvironmentに次を設定します。

```env
GEMINI_API_KEY=Google AI StudioのAPIキー
GEMINI_MODEL=gemini-2.5-flash
FRONTEND_ORIGINS=https://あなたの-cloudflare-pages.pages.dev
```

先にCloudflare PagesのURLが分からない場合は、一時的に `FRONTEND_ORIGINS=*` でも動作確認できます。公開運用ではPagesのURLに絞ってください。

APIが起動したら、次を開いて確認します。

```text
https://あなたの-render-url.onrender.com/api/health
```

`{"ok":true}` が出ればAPIは起動しています。

### 2. Cloudflare Pagesにフロントを置く

Cloudflare Pagesの設定:

```bash
Build command: python scripts/build_pages.py
Build output directory: dist
```

Environment variables:

```env
KARTE_API_BASE_URL=https://あなたの-render-url.onrender.com
```

これで `dist/config.js` にAPI URLが入り、Cloudflare Pages上の画面からRenderのAPIを呼びます。

### 3. ローカルとの違い

ローカルでは `config.js` の `window.KARTE_API_BASE_URL = "";` により、同じFastAPIサーバーの `/api/analyze` を呼びます。
Cloudflare Pagesでは `KARTE_API_BASE_URL` にRenderのURLを入れるため、別ドメインのAPIを呼びます。

## 次の実装候補

1. YouTube URLから動画IDを抽出
2. 進捗状況のポーリングまたはSSE
3. BGMやゲーム音を考慮した発話区間検出
4. Google OAuthとYouTube Analytics連携
5. PostgreSQLまたはSupabaseへの分析履歴保存

## 解析レスポンス

```json
{
  "video": {
    "videoId": "example",
    "title": "配信タイトル",
    "thumbnailUrl": "https://i.ytimg.com/vi/example/hqdefault.jpg",
    "watchUrl": "https://www.youtube.com/watch?v=example",
    "embedUrl": "https://www.youtube.com/embed/example"
  },
  "streamType": "深夜ラジオ型",
  "overallScore": 78,
  "summary": "落ち着いた雑談とゲーム進行のバランスが良い配信です。",
  "mainTopicStartedAt": "00:12:42",
  "titleMatchScore": 88,
  "talkDensityScore": 82,
  "silenceRate": 12.4,
  "highlights": [
    {
      "start": "00:08:14",
      "end": "00:09:02",
      "title": "初見コメントへの神対応",
      "reason": "短く意味が通り、リアクションも明快です",
      "tag": "有力"
    }
  ],
  "advice": [
    "冒頭3分で本題を宣言する"
  ],
  "warnings": [
    "Geminiによる推定診断です",
    "無言率は簡易音声解析による参考値です"
  ]
}
```
