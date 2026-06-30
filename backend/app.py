import json
import os
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

app = FastAPI(title="配信カルテ API")

frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:4173,http://localhost:4173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if frontend_origins == ["*"] else frontend_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class AnalyzeRequest(BaseModel):
    youtubeUrl: str = Field(min_length=10, max_length=500)
    analysisMode: str = "standard"


class Highlight(BaseModel):
    start: str
    end: str
    title: str
    reason: str
    tag: str


class VideoInfo(BaseModel):
    videoId: str
    title: str
    thumbnailUrl: str
    watchUrl: str
    embedUrl: str
    durationSeconds: Optional[int] = None
    durationText: Optional[str] = None


class AnalysisResult(BaseModel):
    video: Optional[VideoInfo] = None
    streamType: str
    overallScore: int = Field(ge=0, le=100)
    summary: str
    mainTopicStartedAt: str
    titleMatchScore: int = Field(ge=0, le=100)
    talkDensityScore: int = Field(ge=0, le=100)
    silenceRate: Optional[float] = Field(default=None, ge=0, le=100)
    highlights: List[Highlight] = Field(min_length=3, max_length=3)
    advice: List[str] = Field(min_length=3, max_length=3)
    goodPoints: List[str] = Field(default_factory=list)
    dropOffPoints: List[str] = Field(default_factory=list)
    nextActions: List[str] = Field(default_factory=list)
    analysisMode: str = "standard"
    warnings: List[str]


class EmptyGeminiResponseError(ValueError):
    pass


class TranscriptUnavailableError(ValueError):
    pass


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "streamType": {"type": "string"},
        "overallScore": {"type": "integer"},
        "summary": {"type": "string"},
        "mainTopicStartedAt": {"type": "string"},
        "titleMatchScore": {"type": "integer"},
        "talkDensityScore": {"type": "integer"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["start", "end", "title", "reason", "tag"],
            },
        },
        "advice": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "streamType",
        "overallScore",
        "summary",
        "mainTopicStartedAt",
        "titleMatchScore",
        "talkDensityScore",
        "highlights",
        "advice",
        "warnings",
    ],
}


def is_public_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if host not in allowed_hosts:
        return False

    if host == "youtu.be":
        return bool(parsed.path.strip("/"))

    return parsed.path == "/watch" and "v=" in parsed.query


def extract_youtube_video_id(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host == "youtu.be":
        return parsed.path.strip("/")
    query = parse_qs(parsed.query)
    return (query.get("v") or [""])[0]


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def get_video_info(youtube_url: str) -> VideoInfo:
    video_id = extract_youtube_video_id(youtube_url)
    watch_url = canonical_youtube_url(video_id) if video_id else youtube_url
    thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
    title = "YouTube動画"
    duration_seconds = None

    try:
        import yt_dlp

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
        title = info.get("title") or title
        thumbnail_url = info.get("thumbnail") or thumbnail_url
        video_id = info.get("id") or video_id
        watch_url = canonical_youtube_url(video_id) if video_id else watch_url
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            duration_seconds = int(duration)
    except Exception:
        pass

    return VideoInfo(
        videoId=video_id,
        title=title,
        thumbnailUrl=thumbnail_url,
        watchUrl=watch_url,
        embedUrl=f"https://www.youtube.com/embed/{video_id}" if video_id else "",
        durationSeconds=duration_seconds,
        durationText=format_seconds(duration_seconds) if duration_seconds else None,
    )


MODE_SETTINGS = {
    "light": {
        "label": "軽量診断",
        "description": "冒頭10分を中心に、導入の分かりやすさと最初の見どころを素早く診断する",
    },
    "standard": {
        "label": "標準診断",
        "description": "冒頭30分を中心に、導入、話題の立ち上がり、盛り上がり候補を診断する",
    },
    "deep": {
        "label": "じっくり診断",
        "description": "冒頭30分に加えて、可能な範囲で中盤と終盤のサンプルも見て、配信全体の流れを診断する",
    },
}


def build_prompt(analysis_mode: str = "standard") -> str:
    mode = MODE_SETTINGS.get(analysis_mode, MODE_SETTINGS["standard"])
    return f"""
あなたはVTuber配信をやさしく改善する「配信カルテ」の分析エンジンです。
今回の分析モードは「{mode["label"]}」です。
分析範囲の考え方: {mode["description"]}

必ず指定されたJSON Schemaに一致するJSONだけを返してください。Markdownや説明文は不要です。
出力する文字列は、streamType、summary、highlights、advice、goodPoints、dropOffPoints、nextActions、warningsを含めて、すべて自然な日本語にしてください。
英語の見出しや英語の文章を返してはいけません。固有名詞以外は日本語に翻訳してください。
文章は大学生くらいの読者が読みやすい、自然で少しくだけた日本語にしてください。専門用語を詰め込みすぎず、でも内容は薄くしないでください。

分析方針:
- 冒頭30分の導入、OP/待機画面、話題の立ち上がり、会話量、タイトルとの一致を重視する
- 配信者を責めず、次回すぐ試せる前向きな提案にする
- mainTopicStartedAt は「OP終了の推定時刻」として、HH:MM:SS または MM:SS 形式にする
- OP終了の推定は、アニメーションループや待機画面が終わる瞬間、配信者の声が入り始める瞬間、最初の挨拶や画面切り替わりを根拠に判断する
- OPや待機がなく最初から配信が始まっている場合は 00:00 とする
- highlights は切り抜き候補を3件だけ返す
- summary は220〜360字程度で、何が起きた配信か、視聴者にどう見えるか、改善余地まで含めて読み応えを出す
- goodPoints は良かった点を3件。各項目は40〜90字程度で、なぜ良いかまで書く
- dropOffPoints は離脱されそうな点を3件。責める表現ではなく、視聴者目線で書く
- nextActions は次回の改善手順を3件。具体的に「何をどう変えるか」が分かる文章にする
- highlights の reason は、なぜ切り抜き候補になるかを40〜90字程度で具体的に書く
- コメント対応率は扱わない
- silenceRate は互換性のために残すが、値は推測しない
- warnings には「Gemini推定値」「公開YouTube動画のみ対応」「分析モード: {mode["label"]}」などの注意を含める

JSONキー:
streamType, overallScore, summary, mainTopicStartedAt, titleMatchScore, talkDensityScore, highlights, advice, goodPoints, dropOffPoints, nextActions, analysisMode, warnings
""".strip()


def build_translation_prompt(data: AnalysisResult) -> str:
    return f"""
次のJSONはVTuber配信分析結果です。数値、時刻、JSONキー、配列数、意味を保ったまま、文字列の値だけを自然な日本語にしてください。
英語の文章、英語の配信タイプ、英語の改善提案は必ず日本語へ翻訳してください。
summary、goodPoints、dropOffPoints、nextActions は大学生くらいの読者が読みやすい、自然で少しくだけた日本語にしてください。
JSONだけを返してください。Markdownは不要です。

{data.model_dump_json(ensure_ascii=False)}
""".strip()


def build_transcript_prompt(transcript: str) -> str:
    return f"""
あなたはVTuber配信をやさしく改善する「配信カルテ」の分析エンジンです。
Geminiの動画URL直読みが空応答だったため、公開動画の字幕/文字起こしから分かる範囲で診断してください。
冒頭30分相当の字幕を重点的に見て、日本語で建設的に診断してください。

必ずJSONだけを返してください。Markdownや説明文は不要です。
出力する文字列は、streamType、summary、highlights、advice、warningsを含めて、すべて自然な日本語にしてください。
mainTopicStartedAt は「OP終了の推定時刻」として、待機画面、アニメーションループ、配信者の声が入り始める瞬間、最初の挨拶を根拠に判断してください。
コメント対応率は扱いません。silenceRate は互換性のために残しますが、値は推測しないでください。

JSONキー:
streamType, overallScore, summary, mainTopicStartedAt, titleMatchScore, talkDensityScore, highlights, advice, warnings

字幕/文字起こし:
{transcript}
""".strip()


def parse_json_result(text: str) -> AnalysisResult:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    return AnalysisResult.model_validate(json.loads(cleaned))


def extract_response_text(response) -> str:
    try:
        value = response.text
        if value:
            return value
    except Exception:
        pass

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        chunks = [getattr(part, "text", "") for part in parts if getattr(part, "text", "")]
        if chunks:
            return "\n".join(chunks)

    finish_reasons = [
        str(getattr(candidate, "finish_reason", ""))
        for candidate in candidates
        if getattr(candidate, "finish_reason", None)
    ]
    if finish_reasons:
        raise EmptyGeminiResponseError(f"Geminiからテキスト応答が返りませんでした。終了理由: {', '.join(finish_reasons)}")
    raise EmptyGeminiResponseError("Geminiからテキスト応答が返りませんでした。別の公開動画で試してください。")


def contains_latin_sentence(data: AnalysisResult) -> bool:
    texts = [
        data.streamType,
        data.summary,
        *[item.title + " " + item.reason + " " + item.tag for item in data.highlights],
        *data.advice,
        *data.warnings,
    ]
    joined = " ".join(texts)
    latin_letters = len(re.findall(r"[A-Za-z]", joined))
    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", joined))
    return latin_letters > 40 and latin_letters > japanese_chars


def normalize_analysis_result(result: AnalysisResult, analysis_mode: str) -> AnalysisResult:
    result.analysisMode = analysis_mode
    if not result.goodPoints:
        result.goodPoints = [
            "配信の主題が見えたところでは、視聴者が追いやすい流れになっています。",
            "リアクションや説明が入る場面は、初見でも状況を理解しやすい強みがあります。",
            "タイトルと配信内容の方向性が近く、見に来た人の期待に応えやすい構成です。",
        ]
    if not result.dropOffPoints:
        result.dropOffPoints = [
            "冒頭で今日の見どころが伝わるまでに時間がかかると、初見の人は離れやすくなります。",
            "状況説明が少ない場面では、途中から来た視聴者が流れに乗りにくくなる可能性があります。",
            "盛り上がり前の準備時間が長いと、切り抜きや短時間視聴では魅力が伝わりにくくなります。",
        ]
    if not result.nextActions:
        result.nextActions = result.advice[:3]
    result.goodPoints = result.goodPoints[:3]
    result.dropOffPoints = result.dropOffPoints[:3]
    result.nextActions = result.nextActions[:3]
    while len(result.nextActions) < 3:
        result.nextActions.append("次回は冒頭で今日の目的と見どころを短く伝えると、初見の人が入りやすくなります。")
    return result


def request_gemini_analysis(
    client: genai.Client,
    youtube_url: str,
    analysis_mode: str = "standard",
    use_schema: bool = True,
    end_offset_seconds: int = 1800,
    fps: float = 0.2,
):
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=AnalysisResult if use_schema else None,
    )
    video_part = types.Part.from_uri(file_uri=youtube_url, mime_type="video/mp4")
    video_part.video_metadata = types.VideoMetadata(
        start_offset="0s",
        end_offset=f"{end_offset_seconds}s",
        fps=fps,
    )
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            video_part,
            build_prompt(analysis_mode),
        ],
        config=config,
    )


def format_seconds(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def fetch_transcript_text(youtube_url: str, max_seconds: int = 1800) -> str:
    video_id = extract_youtube_video_id(youtube_url)
    if not video_id:
        raise TranscriptUnavailableError("YouTube動画IDを取得できませんでした。")

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:
        raise TranscriptUnavailableError("字幕取得ライブラリが未インストールです。start_windows.batを再起動してください。") from exc

    languages = ["ja", "ja-JP", "en", "en-US"]
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
        rows = [
            {"start": item.start, "duration": item.duration, "text": item.text}
            for item in fetched
        ]
    except AttributeError:
        rows = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
    except Exception as exc:
        try:
            rows = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
        except Exception as fallback_exc:
            raise TranscriptUnavailableError("この動画の公開字幕/文字起こしを取得できませんでした。") from fallback_exc

    lines = []
    for row in rows:
        start = float(row.get("start", 0))
        if start > max_seconds:
            break
        text = re.sub(r"\s+", " ", str(row.get("text", ""))).strip()
        if text:
            lines.append(f"[{format_seconds(start)}] {text}")

    if not lines:
        raise TranscriptUnavailableError("冒頭部分の字幕/文字起こしが空でした。")
    return "\n".join(lines)[:60000]


def request_transcript_analysis(client: genai.Client, transcript: str):
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[build_transcript_prompt(transcript)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AnalysisResult,
        ),
    )


def request_japanese_rewrite(client: genai.Client, data: AnalysisResult):
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[build_translation_prompt(data)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AnalysisResult,
        ),
    )


def parse_analysis_response(response) -> AnalysisResult:
    if getattr(response, "parsed", None):
        return response.parsed
    return parse_json_result(extract_response_text(response))


def get_mode_attempts(analysis_mode: str):
    if analysis_mode == "light":
        return [
            {"end": 600, "fps": 0.12, "schema": True, "label": "冒頭10分"},
            {"end": 300, "fps": 0.1, "schema": False, "label": "冒頭5分"},
        ]
    if analysis_mode == "deep":
        return [
            {"end": 1800, "fps": 0.12, "schema": True, "label": "冒頭30分"},
            {"end": 1200, "fps": 0.1, "schema": True, "label": "冒頭20分"},
            {"end": 600, "fps": 0.1, "schema": True, "label": "冒頭10分"},
        ]
    return [
        {"end": 1800, "fps": 0.18, "schema": True, "label": "冒頭30分"},
        {"end": 1200, "fps": 0.12, "schema": True, "label": "冒頭20分"},
        {"end": 600, "fps": 0.1, "schema": True, "label": "冒頭10分"},
        {"end": 300, "fps": 0.1, "schema": False, "label": "冒頭5分"},
    ]


def analyze_with_retries(client: genai.Client, youtube_url: str, analysis_mode: str) -> AnalysisResult:
    attempts = get_mode_attempts(analysis_mode)
    last_error: Exception | None = None

    for attempt in attempts:
        try:
            response = request_gemini_analysis(
                client,
                youtube_url,
                analysis_mode=analysis_mode,
                use_schema=attempt["schema"],
                end_offset_seconds=attempt["end"],
                fps=attempt["fps"],
            )
            result = parse_analysis_response(response)
            result.analysisMode = analysis_mode
            if attempt["label"] != attempts[0]["label"]:
                result.warnings.append(f"今回は安定して分析するため、対象範囲を{attempt['label']}に短縮しました。")
            return result
        except EmptyGeminiResponseError as exc:
            last_error = exc
            continue
        except Exception as exc:
            if getattr(exc, "status_code", None) == 400 and attempt["end"] > 300:
                last_error = exc
                continue
            raise

    try:
        transcript = fetch_transcript_text(youtube_url)
        response = request_transcript_analysis(client, transcript)
        result = parse_analysis_response(response)
        result.warnings.append("Geminiの動画URL直読みが空応答だったため、字幕/文字起こしから分かる範囲で分析しました。")
        result.warnings.append("映像の詳細は未確認です。OP終了時刻は字幕/文字起こしから分かる範囲で推定しています。")
        return result
    except TranscriptUnavailableError as exc:
        last_error = exc

    if last_error:
        raise last_error
    raise EmptyGeminiResponseError("Geminiから分析本文が返りませんでした。別の公開動画で試してください。")


def sanitize_error_message(value: str) -> str:
    value = re.sub(r"AQ\.[A-Za-z0-9._-]+", "[API_KEY]", value)
    value = re.sub(r"AIza[0-9A-Za-z_-]+", "[API_KEY]", value)
    value = re.sub(r"https://www\.youtube\.com/watch\?v=[0-9A-Za-z_-]+", "[YOUTUBE_URL]", value)
    value = re.sub(r"https://youtu\.be/[0-9A-Za-z_-]+", "[YOUTUBE_URL]", value)
    return value[:900]


def record_gemini_error(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    raw_message = sanitize_error_message(str(exc))
    label = exc.__class__.__name__
    if status_code:
        label = f"{label} {status_code}"

    with open(os.path.join(ROOT_DIR, "gemini_error.log"), "a", encoding="utf-8") as log:
        log.write(f"[{datetime.now().isoformat(timespec='seconds')}] {label}: {raw_message}\n")

    if "WinError 10013" in raw_message or exc.__class__.__name__ == "APIConnectionError":
        return "Gemini APIへ接続できませんでした。Windowsのファイアウォール、プロキシ、またはCodex内の通信制限を確認してください。"
    if status_code in {401, 403} or "API_KEY_INVALID" in raw_message:
        return "Gemini APIキーが認証されませんでした。Google AI Studioでキーが有効か確認してください。"
    if status_code == 404 or "not found" in raw_message.lower():
        return "指定したGeminiモデルが見つかりませんでした。.env の GEMINI_MODEL を確認してください。"
    if status_code == 429 or "quota" in raw_message.lower() or "rate" in raw_message.lower():
        return "Gemini APIの利用上限に達した可能性があります。しばらく待ってから再試行してください。"
    if "fewer than" in raw_message.lower() and "images" in raw_message.lower():
        return "動画が長すぎてGeminiの処理上限を超えました。短い公開動画で試すか、分析範囲を短くしてください。"
    if "テキスト応答が返りません" in raw_message or "finish_reason" in raw_message:
        return "Geminiから分析本文が返りませんでした。動画の地域制限、年齢制限、安全フィルタ、または一時的な空応答の可能性があります。別の公開動画でも試してください。"
    if exc.__class__.__name__ == "TranscriptUnavailableError" or "字幕" in raw_message or "文字起こし" in raw_message:
        return "Geminiの動画直読みが空応答で、字幕/文字起こしも取得できませんでした。字幕付きの公開動画、または別の公開動画で試してください。"
    if "public" in raw_message.lower() or "youtube" in raw_message.lower():
        return "YouTube動画を読み込めませんでした。公開動画か、地域制限や年齢制限がないか確認してください。"
    return f"Gemini APIで分析できませんでした。詳細: {label}"


def enrich_result(result: AnalysisResult, youtube_url: str) -> AnalysisResult:
    result.video = get_video_info(youtube_url)
    result.silenceRate = None
    return result


@app.post("/api/analyze", response_model=AnalysisResult)
def analyze(request: AnalyzeRequest) -> AnalysisResult:
    youtube_url = request.youtubeUrl.strip()
    analysis_mode = request.analysisMode if request.analysisMode in MODE_SETTINGS else "standard"
    if not is_public_youtube_url(youtube_url):
        raise HTTPException(status_code=400, detail="公開YouTube動画のURLを入力してください。")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail=".env に GEMINI_API_KEY を設定してください。")

    client = genai.Client(api_key=api_key)

    try:
        result = analyze_with_retries(client, youtube_url, analysis_mode)
        result = normalize_analysis_result(result, analysis_mode)
        if contains_latin_sentence(result):
            rewrite = request_japanese_rewrite(client, result)
            if getattr(rewrite, "parsed", None):
                result = rewrite.parsed
            else:
                result = parse_json_result(extract_response_text(rewrite))
            result = normalize_analysis_result(result, analysis_mode)
        result = enrich_result(result, youtube_url)
        if contains_latin_sentence(result):
            rewrite = request_japanese_rewrite(client, result)
            if getattr(rewrite, "parsed", None):
                rewritten = rewrite.parsed
            else:
                rewritten = parse_json_result(extract_response_text(rewrite))
            rewritten.video = result.video
            rewritten.silenceRate = None
            result = normalize_analysis_result(rewritten, analysis_mode)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=record_gemini_error(exc),
        ) from exc


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(ROOT_DIR, "index.html"))


app.mount("/", StaticFiles(directory=ROOT_DIR), name="static")
