import json
import math
import os
import re
import subprocess
import tempfile
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
    warnings: List[str]


class EmptyGeminiResponseError(ValueError):
    pass


class TranscriptUnavailableError(ValueError):
    pass


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
    try:
        import yt_dlp
        options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
        title = info.get("title") or title
        thumbnail_url = info.get("thumbnail") or thumbnail_url
        video_id = info.get("id") or video_id
        watch_url = canonical_youtube_url(video_id) if video_id else watch_url
    except Exception:
        pass
    return VideoInfo(
        videoId=video_id,
        title=title,
        thumbnailUrl=thumbnail_url,
        watchUrl=watch_url,
        embedUrl=f"https://www.youtube.com/embed/{video_id}" if video_id else "",
    )


def build_prompt() -> str:
    return """
あなたはVTuber配信をやさしく改善する「配信カルテ」の分析エンジンです。
公開YouTube動画の冒頭30分を重点的に見て、日本語で建設的に診断してください。

必ず指定されたJSON Schemaに一致するJSONだけを返してください。Markdownや説明文は不要です。
出力する文字列は、streamType、summary、highlights、advice、warningsを含めて、すべて自然な日本語にしてください。
英語の見出しや英語の文章を返してはいけません。固有名詞以外は日本語に翻訳してください。

分析方針:
- 冒頭30分の導入、話題の立ち上がり、会話量、タイトルとの一致を重視する
- 配信者を責めず、次回すぐ試せる前向きな提案にする
- mainTopicStartedAt は HH:MM:SS または MM:SS 形式にする
- highlights は切り抜き候補を3件だけ返す
- コメント対応率は扱わない
- silenceRate はバックエンドの音声解析で後から入れるため、推測しない
- warnings には「Gemini推定値」「公開YouTube動画のみ対応」などの注意を含める

JSONキー:
streamType, overallScore, summary, mainTopicStartedAt, titleMatchScore, talkDensityScore, highlights, advice, warnings
""".strip()


def build_translation_prompt(data: AnalysisResult) -> str:
    return f"""
次のJSONはVTuber配信分析結果です。数値、時刻、JSONキー、配列数、意味を保ったまま、文字列の値だけを自然な日本語にしてください。
英語の文章、英語の配信タイプ、英語の改善提案は必ず日本語へ翻訳してください。
JSONだけを返してください。Markdownは不要です。

{data.model_dump_json(ensure_ascii=False)}
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


def request_gemini_analysis(client: genai.Client, youtube_url: str, use_schema: bool = True, end_offset_seconds: int = 1800, fps: float = 0.2):
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=AnalysisResult if use_schema else None)
    video_part = types.Part.from_uri(file_uri=youtube_url, mime_type="video/mp4")
    video_part.video_metadata = types.VideoMetadata(start_offset="0s", end_offset=f"{end_offset_seconds}s", fps=fps)
    return client.models.generate_content(model=GEMINI_MODEL, contents=[video_part, build_prompt()], config=config)


def request_japanese_rewrite(client: genai.Client, data: AnalysisResult):
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[build_translation_prompt(data)],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=AnalysisResult),
    )


def parse_analysis_response(response) -> AnalysisResult:
    if getattr(response, "parsed", None):
        return response.parsed
    return parse_json_result(extract_response_text(response))


def analyze_with_retries(client: genai.Client, youtube_url: str) -> AnalysisResult:
    attempts = [
        {"end": 1800, "fps": 0.2, "schema": True, "label": "冒頭30分"},
        {"end": 1200, "fps": 0.15, "schema": True, "label": "冒頭20分"},
        {"end": 600, "fps": 0.1, "schema": True, "label": "冒頭10分"},
        {"end": 300, "fps": 0.1, "schema": False, "label": "冒頭5分"},
    ]
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            response = request_gemini_analysis(client, youtube_url, use_schema=attempt["schema"], end_offset_seconds=attempt["end"], fps=attempt["fps"])
            result = parse_analysis_response(response)
            if attempt["end"] < 1800:
                result.warnings.append(f"Geminiの空応答を避けるため、今回は{attempt['label']}に範囲を短縮して分析しました。")
            return result
        except EmptyGeminiResponseError as exc:
            last_error = exc
            continue
        except Exception as exc:
            if getattr(exc, "status_code", None) == 400 and attempt["end"] > 300:
                last_error = exc
                continue
            raise
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
    if status_code in {401, 403} or "API_KEY_INVALID" in raw_message:
        return "Gemini APIキーが認証されませんでした。Google AI Studioでキーが有効か確認してください。"
    if status_code == 404 or "not found" in raw_message.lower():
        return "指定したGeminiモデルが見つかりませんでした。.env の GEMINI_MODEL を確認してください。"
    if status_code == 429 or "quota" in raw_message.lower() or "rate" in raw_message.lower():
        return "Gemini APIの利用上限に達した可能性があります。しばらく待ってから再試行してください。"
    if "fewer than" in raw_message.lower() and "images" in raw_message.lower():
        return "動画が長すぎてGeminiの処理上限を超えました。短い公開動画で試すか、分析範囲を短くしてください。"
    return f"Gemini APIで分析できませんでした。詳細: {label}"


def get_audio_stream_url(youtube_url: str) -> str:
    import yt_dlp
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "format": "bestaudio/best", "extract_flat": False}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
    formats = info.get("formats") or []
    audio_formats = [item for item in formats if item.get("url") and item.get("acodec") not in {None, "none"} and item.get("vcodec") in {None, "none"}]
    if not audio_formats and info.get("url"):
        return info["url"]
    if not audio_formats:
        raise RuntimeError("音声ストリームを取得できませんでした。")
    audio_formats.sort(key=lambda item: (float(item.get("abr") or 0), float(item.get("tbr") or 0)), reverse=True)
    return audio_formats[0]["url"]


def analyze_silence_rate(youtube_url: str, max_seconds: int = 300) -> float:
    import imageio_ffmpeg
    audio_url = get_audio_stream_url(youtube_url)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    sample_rate = 16000
    bytes_per_second = sample_rate * 2
    silent_seconds = 0
    measured_seconds = 0
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0", "-t", str(max_seconds), "-i", audio_url, "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        while measured_seconds < max_seconds:
            chunk = process.stdout.read(bytes_per_second)
            if not chunk or len(chunk) < bytes_per_second // 2:
                break
            sample_count = len(chunk) // 2
            total_square = 0
            for index in range(0, len(chunk) - 1, 2):
                sample = int.from_bytes(chunk[index : index + 2], "little", signed=True)
                total_square += sample * sample
            rms = math.sqrt(total_square / max(1, sample_count))
            db = 20 * math.log10(max(rms, 1) / 32768)
            if db <= -45:
                silent_seconds += 1
            measured_seconds += 1
    finally:
        process.kill()
        process.communicate(timeout=5)
    if measured_seconds == 0:
        raise RuntimeError("音声解析用のデータを取得できませんでした。")
    return round((silent_seconds / measured_seconds) * 100, 1)


def add_silence_rate(result: AnalysisResult, youtube_url: str) -> AnalysisResult:
    try:
        result.silenceRate = analyze_silence_rate(youtube_url)
        result.warnings.append("無言率は冒頭5分の音量しきい値から計算した簡易値です。BGMやゲーム音がある場合、実際の発話なし時間とはずれることがあります。")
    except Exception as exc:
        safe_message = sanitize_error_message(str(exc))
        with open(os.path.join(ROOT_DIR, "silence_error.log"), "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now().isoformat(timespec='seconds')}] {exc.__class__.__name__}: {safe_message}\n")
        result.silenceRate = None
        result.warnings.append("無言率の簡易音声解析に失敗しました。yt-dlp/ffmpegの準備、動画の公開状態、またはネットワーク状態を確認してください。")
    return result


def enrich_result(result: AnalysisResult, youtube_url: str) -> AnalysisResult:
    result.video = get_video_info(youtube_url)
    return add_silence_rate(result, youtube_url)


@app.post("/api/analyze", response_model=AnalysisResult)
def analyze(request: AnalyzeRequest) -> AnalysisResult:
    youtube_url = request.youtubeUrl.strip()
    if not is_public_youtube_url(youtube_url):
        raise HTTPException(status_code=400, detail="公開YouTube動画のURLを入力してください。")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail=".env に GEMINI_API_KEY を設定してください。")
    client = genai.Client(api_key=api_key)
    try:
        result = analyze_with_retries(client, youtube_url)
        if contains_latin_sentence(result):
            rewrite = request_japanese_rewrite(client, result)
            if getattr(rewrite, "parsed", None):
                result = rewrite.parsed
            else:
                result = parse_json_result(extract_response_text(rewrite))
        result = enrich_result(result, youtube_url)
        if contains_latin_sentence(result):
            rewrite = request_japanese_rewrite(client, result)
            if getattr(rewrite, "parsed", None):
                rewritten = rewrite.parsed
            else:
                rewritten = parse_json_result(extract_response_text(rewrite))
            rewritten.video = result.video
            rewritten.silenceRate = result.silenceRate
            result = rewritten
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=record_gemini_error(exc)) from exc


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(ROOT_DIR, "index.html"))


app.mount("/", StaticFiles(directory=ROOT_DIR), name="static")
