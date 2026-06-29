import json
import math
import os
import re
import subprocess
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
origins = [x.strip() for x in os.getenv("FRONTEND_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if origins == ["*"] else origins,
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


def is_public_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    if host == "youtu.be":
        return bool(parsed.path.strip("/"))
    return host in {"youtube.com", "www.youtube.com", "m.youtube.com"} and parsed.path == "/watch" and "v=" in parsed.query


def extract_video_id(value: str) -> str:
    parsed = urlparse(value)
    if parsed.netloc.lower() == "youtu.be":
        return parsed.path.strip("/")
    return (parse_qs(parsed.query).get("v") or [""])[0]


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def get_video_info(youtube_url: str) -> VideoInfo:
    video_id = extract_video_id(youtube_url)
    title = "YouTube動画"
    thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
        title = info.get("title") or title
        thumbnail = info.get("thumbnail") or thumbnail
        video_id = info.get("id") or video_id
    except Exception:
        pass
    watch = canonical_url(video_id) if video_id else youtube_url
    return VideoInfo(videoId=video_id, title=title, thumbnailUrl=thumbnail, watchUrl=watch, embedUrl=f"https://www.youtube.com/embed/{video_id}" if video_id else "")


def build_prompt() -> str:
    return """
あなたはVTuber配信をやさしく改善する「配信カルテ」の分析エンジンです。
公開YouTube動画の冒頭30分を重点的に見て、日本語で建設的に診断してください。
必ずJSONだけを返してください。Markdownや説明文は禁止です。
英語の説明文は返さず、固有名詞以外は自然な日本語にしてください。

JSONキー:
streamType, overallScore, summary, mainTopicStartedAt, titleMatchScore, talkDensityScore, highlights, advice, warnings

条件:
- highlights は切り抜き候補を3件だけ返す。start/end/title/reason/tag を含める
- advice は改善提案を3件だけ返す
- mainTopicStartedAt は HH:MM:SS または MM:SS
- コメント対応率は扱わない
- silenceRate は推測しない
- warnings には「Gemini推定値」「公開YouTube動画のみ対応」を含める
""".strip()


def parse_json_result(text: str) -> AnalysisResult:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    return AnalysisResult.model_validate(json.loads(cleaned))


def response_text(response) -> str:
    if getattr(response, "parsed", None):
        return response.parsed
    try:
        if response.text:
            return response.text
    except Exception:
        pass
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []
        chunks = [getattr(part, "text", "") for part in parts if getattr(part, "text", "")]
        if chunks:
            return "\n".join(chunks)
    raise ValueError("Geminiから分析本文が返りませんでした。別の公開動画で試してください。")


def request_analysis(client: genai.Client, youtube_url: str, seconds: int, fps: float):
    part = types.Part.from_uri(file_uri=youtube_url, mime_type="video/mp4")
    part.video_metadata = types.VideoMetadata(start_offset="0s", end_offset=f"{seconds}s", fps=fps)
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[part, build_prompt()],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=AnalysisResult),
    )


def analyze_with_retries(client: genai.Client, youtube_url: str) -> AnalysisResult:
    attempts = [(1800, 0.2, "冒頭30分"), (1200, 0.15, "冒頭20分"), (600, 0.1, "冒頭10分"), (300, 0.1, "冒頭5分")]
    last_error: Exception | None = None
    for seconds, fps, label in attempts:
        try:
            result = response_text(request_analysis(client, youtube_url, seconds, fps))
            data = result if isinstance(result, AnalysisResult) else parse_json_result(result)
            if seconds < 1800:
                data.warnings.append(f"今回はGeminiの応答安定化のため、{label}に範囲を短縮して分析しました。")
            return data
        except Exception as exc:
            last_error = exc
            continue
    raise last_error or ValueError("Geminiから分析本文が返りませんでした。")


def sanitize(value: str) -> str:
    value = re.sub(r"AQ\.[A-Za-z0-9._-]+", "[API_KEY]", value)
    value = re.sub(r"AIza[0-9A-Za-z_-]+", "[API_KEY]", value)
    value = re.sub(r"https://www\.youtube\.com/watch\?v=[0-9A-Za-z_-]+", "[YOUTUBE_URL]", value)
    value = re.sub(r"https://youtu\.be/[0-9A-Za-z_-]+", "[YOUTUBE_URL]", value)
    return value[:900]


def friendly_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    raw = sanitize(str(exc))
    label = f"{exc.__class__.__name__} {status}" if status else exc.__class__.__name__
    with open(os.path.join(ROOT_DIR, "gemini_error.log"), "a", encoding="utf-8") as log:
        log.write(f"[{datetime.now().isoformat(timespec='seconds')}] {label}: {raw}\n")
    if status in {401, 403} or "API_KEY_INVALID" in raw:
        return "Gemini APIキーが認証されませんでした。Google AI Studioでキーが有効か確認してください。"
    if status == 429 or "quota" in raw.lower():
        return "Gemini APIの利用上限に達した可能性があります。しばらく待ってから再試行してください。"
    if "fewer than" in raw.lower() and "images" in raw.lower():
        return "動画が長すぎてGeminiの処理上限を超えました。短い公開動画で試してください。"
    return f"Gemini APIで分析できませんでした。詳細: {label}"


def get_audio_stream_url(youtube_url: str) -> str:
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "format": "bestaudio/best"}) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
    formats = info.get("formats") or []
    audio = [x for x in formats if x.get("url") and x.get("acodec") not in {None, "none"} and x.get("vcodec") in {None, "none"}]
    if audio:
        audio.sort(key=lambda x: float(x.get("abr") or x.get("tbr") or 0), reverse=True)
        return audio[0]["url"]
    if info.get("url"):
        return info["url"]
    raise RuntimeError("音声ストリームを取得できませんでした。")


def analyze_silence_rate(youtube_url: str, max_seconds: int = 1800) -> float:
    import imageio_ffmpeg

    audio_url = get_audio_stream_url(youtube_url)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    sample_rate = 16000
    chunk_size = sample_rate * 2
    silent = 0
    measured = 0
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0", "-t", str(max_seconds), "-i", audio_url, "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        while measured < max_seconds:
            chunk = process.stdout.read(chunk_size)
            if len(chunk) < chunk_size // 2:
                break
            total = 0
            count = len(chunk) // 2
            for i in range(0, len(chunk) - 1, 2):
                sample = int.from_bytes(chunk[i : i + 2], "little", signed=True)
                total += sample * sample
            rms = math.sqrt(total / max(1, count))
            db = 20 * math.log10(max(rms, 1) / 32768)
            if db <= -45:
                silent += 1
            measured += 1
    finally:
        process.kill()
        process.communicate(timeout=5)
    if measured == 0:
        raise RuntimeError("音声解析用のデータを取得できませんでした。")
    return round(silent / measured * 100, 1)


def enrich(result: AnalysisResult, youtube_url: str) -> AnalysisResult:
    result.video = get_video_info(youtube_url)
    try:
        result.silenceRate = analyze_silence_rate(youtube_url)
        result.warnings.append("無言率は冒頭30分の音量しきい値から計算した簡易値です。BGMやゲーム音がある場合、実際の発話なし時間とはずれることがあります。")
    except Exception:
        result.silenceRate = None
        result.warnings.append("無言率の簡易音声解析に失敗しました。yt-dlp/ffmpegの準備、動画の公開状態、またはネットワーク状態を確認してください。")
    return result


@app.post("/api/analyze", response_model=AnalysisResult)
def analyze(request: AnalyzeRequest) -> AnalysisResult:
    youtube_url = request.youtubeUrl.strip()
    if not is_public_youtube_url(youtube_url):
        raise HTTPException(status_code=400, detail="公開YouTube動画のURLを入力してください。")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail=".env に GEMINI_API_KEY を設定してください。")
    try:
        client = genai.Client(api_key=api_key)
        return enrich(analyze_with_retries(client, youtube_url), youtube_url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=friendly_error(exc)) from exc


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(ROOT_DIR, "index.html"))


app.mount("/", StaticFiles(directory=ROOT_DIR), name="static")
