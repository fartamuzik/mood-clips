#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот, который собирает клипы в GitHub Actions.

Пишешь боту:
  • трек (mp3/m4a) с текстом припева в подписи  → получаешь клип
  • видео                                     → добавляется в фоны
  • первая строка подписи «3»                 → три клипа с разными фонами
  • /bg — сколько фонов, /clearbg — удалить фоны, /help — подсказка

Запуск (делает workflow):
  python bot.py poll   лёгкие сообщения + проверка, есть ли треки
  python bot.py run    собрать клипы и ответить

Секреты: TG_TOKEN (обязательно), TG_ALLOWED (твой ник в Telegram),
GROQ_API_KEY (необязательно; без него распознаёт локальный Whisper).
"""

import os
import sys
import re
import json
import time
import random
import shutil
import hashlib
import traceback
import urllib.request
import urllib.error
import uuid
from pathlib import Path

import clip

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TG_TOKEN", "").strip()
API = os.environ.get("TG_API", "https://api.telegram.org").rstrip("/")
ALLOWED = [x.strip().lstrip("@").lower()
           for x in os.environ.get("TG_ALLOWED", "").split(",") if x.strip()]
BG_PATH = os.path.join(HERE, "backgrounds.json")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
JOBS = os.path.join(HERE, "jobs")
DEFER_SEC = 180            # ждём текст к треку без подписи (и трек к тексту)
PAIR_SEC = 300             # трек и отдельный текст считаем парой в этом окне
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TG_UPLOAD_LIMIT = 49 * 1024 * 1024
MAX_VARIANTS = 5

HELP = (
    "Привет! Я собираю клипы для TikTok.\n\n"
    "1) Пришли 2–5 видео для фона — сохраню их.\n"
    "2) Пришли трек (mp3) и в подписи к нему — текст припева.\n"
    "Через несколько минут пришлю готовый клип и подпись.\n\n"
    "• Хочешь несколько клипов с разными фонами — первой строкой подписи напиши число, например 3.\n"
    "• Тот же трек ещё раз — просто перешли своё сообщение с треком.\n"
    "• Припев я нахожу в треке сам. Взял не тот — первой строкой подписи напиши, "
    "откуда резать: 1:05 (или 1:05-1:25).\n"
    "• Точные времена строк, если нужно: [0:12] строка\n\n"
    "/bg — сколько фонов, /clearbg — удалить все фоны"
)


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------- Telegram API

class TgError(Exception):
    pass


def _multipart(fields, files):
    boundary = "----bot" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode("utf-8"))
    for k, path in files.items():
        with open(path, "rb") as f:
            data = f.read()
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n"
                    % (boundary, k, os.path.basename(path))).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return boundary, b"".join(out)


def tg(method, params=None, files=None, timeout=60):
    url = "%s/bot%s/%s" % (API, TOKEN, method)
    if files:
        boundary, body = _multipart(params or {}, files)
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    else:
        body = json.dumps(params or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise TgError("HTTP %s" % e.code)
    except Exception as e:
        raise TgError(str(e))
    if not res.get("ok"):
        raise TgError(res.get("description", "ошибка Telegram"))
    return res["result"]


def say(chat_id, text, html=False):
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if html:
        p["parse_mode"] = "HTML"
    try:
        tg("sendMessage", p)
    except TgError as e:
        log("! sendMessage: %s" % e)


def download(file_id, dest):
    info = tg("getFile", {"file_id": file_id})
    url = "%s/file/bot%s/%s" % (API, TOKEN, info["file_path"])
    with urllib.request.urlopen(url, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def confirm(update_id):
    """Telegram больше не пришлёт это и все более ранние обновления."""
    try:
        tg("getUpdates", {"offset": update_id + 1, "limit": 1, "timeout": 0})
    except TgError as e:
        log("! confirm: %s" % e)


# ------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_backgrounds(bgs):
    with open(BG_PATH, "w", encoding="utf-8") as f:
        json.dump(bgs, f, ensure_ascii=False, indent=1)


def settings():
    cfg = dict(clip.DEFAULTS)
    cfg["video_bitrate"] = "5M"      # чтобы клип влез в лимит Telegram (50 МБ)
    cfg["whisper_model"] = "large-v3-turbo"
    cfg["max_len"] = 30              # по умолчанию: один припев, не больше 30 с
    cfg.update(load_json(SETTINGS_PATH, {}))
    if os.environ.get("GROQ_API_KEY"):
        cfg["groq_api_key"] = os.environ["GROQ_API_KEY"]
    return cfg


# ------------------------------------------------------------- messages

class Item:
    def __init__(self, u):
        self.uid = u["update_id"]
        m = u.get("message") or u.get("edited_message") or {}
        self.msg = m
        self.chat = (m.get("chat") or {}).get("id")
        frm = m.get("from") or {}
        self.user = (frm.get("username") or "").lower()
        self.user_id = str(frm.get("id", ""))
        self.date = m.get("date", 0)
        self.text = m.get("text") or ""
        self.caption = m.get("caption") or ""
        self.kind, self.file = self._classify(m)

    @staticmethod
    def _classify(m):
        if not m:
            return "skip", None
        doc = m.get("document")
        if m.get("audio"):
            return "audio", m["audio"]
        if m.get("voice"):
            return "audio", m["voice"]
        if m.get("video"):
            return "video", m["video"]
        if doc:
            name = (doc.get("file_name") or "").lower()
            mime = (doc.get("mime_type") or "").lower()
            if mime.startswith("audio/") or name.endswith(clip.AUDIO_EXT):
                return "audio", doc
            if mime.startswith("video/") or name.endswith(clip.VIDEO_EXT):
                return "video", doc
        if m.get("text", "").startswith("/"):
            return "cmd", None
        if m.get("text"):
            return "text", None
        return "other", None

    def allowed(self):
        return not ALLOWED or self.user in ALLOWED or self.user_id in ALLOWED


COUNT_RE = re.compile(r"^\s*[xх×]?\s*([1-9])\s*[xх×]?\s*(клип\w*)?\s*$", re.I)


def parse_count(caption):
    """«3\\nтекст» → (3, «текст»). Число может стоять в первой или второй строке
    (например, после строки со временем «1:05»)."""
    lines = (caption or "").replace("\r", "\n").split("\n")
    seen = 0
    for i, l in enumerate(lines):
        if not l.strip():
            continue
        m = COUNT_RE.match(l)
        if m:
            return min(int(m.group(1)), MAX_VARIANTS), "\n".join(lines[:i] + lines[i + 1:])
        seen += 1
        if seen >= 2:
            break
    return 1, caption or ""


def nearby(items, i, kind, consumed):
    """Ближайшее сообщение нужного типа из того же чата: сначала после, потом до."""
    it = items[i]
    order = list(range(i + 1, len(items))) + list(range(i - 1, -1, -1))
    for j in order:
        o = items[j]
        if o.uid in consumed or o.chat != it.chat or o.kind != kind:
            continue
        if abs(o.date - it.date) <= PAIR_SEC:
            return o
    return None


# ------------------------------------------------------------- speech

_MODEL = None


def local_whisper(audio_path, cfg, prompt):
    """Распознавание прямо на сервере GitHub (faster-whisper), без ключей."""
    global _MODEL
    try:
        from faster_whisper import WhisperModel
        if _MODEL is None:
            name = cfg.get("whisper_model") or "large-v3-turbo"
            log("  загружаю модель Whisper %s…" % name)
            try:
                _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
            except Exception as e:
                log("  ! %s — беру medium" % e)
                _MODEL = WhisperModel("medium", device="cpu", compute_type="int8")
        vad = bool(cfg.get("_vad"))       # только для выделенного голоса: там тишина — правда тишина
        segments, _info = _MODEL.transcribe(
            audio_path, language=cfg.get("language") or None, beam_size=5,
            word_timestamps=True, initial_prompt=prompt or None,
            condition_on_previous_text=False, vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200} if vad else None)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append((w.word, float(w.start), float(w.end)))
        return words
    except Exception as e:
        raise clip.Fail("локальный Whisper: %s" % e)


def make_stt(cfg):
    base = clip.transcribe if cfg.get("groq_api_key") else local_whisper
    memo = {}

    def stt(audio_path, c, prompt):
        with open(audio_path, "rb") as f:
            key = (hashlib.md5(f.read()).hexdigest(), prompt, bool(c.get("_vad")))
        if key not in memo:
            memo[key] = base(audio_path, c, prompt)
        return memo[key]
    return stt


# ------------------------------------------------------------- vocals

_SEP = None
_SEP_FAILED = False


def separate_vocals(audio_path, out_path):
    """Выделяет голос из музыки (Demucs). Если не получилось — None, и всё работает по миксу."""
    global _SEP, _SEP_FAILED
    if _SEP_FAILED:
        return None
    try:
        import torch
        torch.set_num_threads(max(1, os.cpu_count() or 2))
        from demucs.api import Separator
        from demucs.audio import save_audio
        if _SEP is None:
            log("  загружаю модель выделения голоса…")
            _SEP = Separator(model="htdemucs", device="cpu", progress=False)
        t0 = time.time()
        _, stems = _SEP.separate_audio_file(Path(audio_path))
        save_audio(stems["vocals"], out_path, samplerate=_SEP.samplerate)
        log("  голос выделен за %.0f c" % (time.time() - t0))
        return out_path
    except Exception as e:
        _SEP_FAILED = True
        log("  ! выделение голоса не сработало: %s" % e)
        return None


# ------------------------------------------------------------- jobs

def gradient_bg(path):
    clip.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i",
              "gradients=s=1080x1920:c0=0x1a1033:c1=0x5b2a86:c2=0x0f3d5c:speed=0.015:d=20:r=30",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "20", path])
    if not os.path.exists(path):
        clip.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                  "-f", "lavfi", "-i", "color=c=0x24123d:s=1080x1920:d=20:r=30",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", path])
    return path


def do_job(it, lyrics, count, cfg, stt):
    chat = it.chat
    f = it.file
    if (f.get("file_size") or 0) > TG_DOWNLOAD_LIMIT:
        say(chat, "Трек больше 20 МБ — Telegram не даёт боту его скачать. Пришли mp3 поменьше.")
        return
    jdir = os.path.join(JOBS, str(it.uid))
    shutil.rmtree(jdir, ignore_errors=True)
    os.makedirs(jdir)
    try:
        tg("sendChatAction", {"chat_id": chat, "action": "upload_video"})
    except TgError:
        pass
    say(chat, "🎬 Собираю %s…" % ("клип" if count == 1 else "%d клипа" % count
                                  if count < 5 else "%d клипов" % count))
    try:
        name = f.get("file_name") or ("track" + (".ogg" if it.msg.get("voice") else ".mp3"))
        ext = os.path.splitext(name)[1].lower() or ".mp3"
        audio = download(f["file_id"], os.path.join(jdir, "audio" + ext))
        t0 = time.time()
        timing = clip.analyze(audio, lyrics, cfg, stt=stt, separate=separate_vocals)
        log("тайминг за %.0f c: %s" % (time.time() - t0, timing["mode"]))
        where = "%s–%s" % (clip.fmt_time(timing["start"]), clip.fmt_time(timing["end"]))

        bgs = load_json(BG_PATH, [])
        picks = []
        if bgs:
            pool = bgs[:]
            random.shuffle(pool)
            while len(picks) < count:
                picks.append(pool[len(picks) % len(pool)])
        local_bg = {}
        hook = None
        for k in range(count):
            if picks:
                b = picks[k]
                if b["file_id"] not in local_bg:
                    p = os.path.join(jdir, "bg%d%s" % (len(local_bg), b.get("ext", ".mp4")))
                    try:
                        local_bg[b["file_id"]] = download(b["file_id"], p)
                    except TgError as e:
                        say(chat, "Не смог скачать один из фонов (%s) — беру градиент." % e)
                        local_bg[b["file_id"]] = gradient_bg(os.path.join(jdir, "grad.mp4"))
                video = local_bg[b["file_id"]]
            else:
                video = gradient_bg(os.path.join(jdir, "grad.mp4"))
            out = os.path.join(jdir, "clip%d.mp4" % (k + 1))
            res = clip.make_clip(cfg, audio, video, lyrics, out, timing=timing)
            hook = res["hook"]
            if os.path.getsize(out) > TG_UPLOAD_LIMIT:
                say(chat, "Клип вышел больше 50 МБ — уменьши max_len или video_bitrate в settings.json.")
                continue
            cap = "Отрывок %s · %s" % (where, timing["mode"])
            if count > 1:
                cap = "Клип %d/%d · %s" % (k + 1, count, cap)
            tg("sendVideo", {"chat_id": chat, "caption": cap, "supports_streaming": "true",
                             "width": cfg["width"], "height": cfg["height"]},
               files={"video": out}, timeout=300)
        if hook:
            caption = hook + ((" " + cfg["hashtags"].strip()) if cfg.get("hashtags") else "")
            say(chat, "Подпись для TikTok (нажми, чтобы скопировать):\n<code>%s</code>"
                % (caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")),
                html=True)
        if not picks:
            say(chat, "Фонов пока нет, поэтому фон — градиент. Пришли мне несколько видео.")
        if timing["mode"].startswith("равномерно"):
            say(chat, "⚠️ Не смог найти этот текст в треке, поэтому строки стоят примерно. "
                      "Проверь, что текст совпадает с тем, что поётся, или пришли трек ещё раз "
                      "и первой строкой подписи напиши, где начинается припев, например: 0:45")
    except clip.Fail as e:
        say(chat, "Не получилось: %s" % e)
    except Exception as e:
        traceback.print_exc()
        say(chat, "Ошибка: %s" % str(e)[:300])
    finally:
        shutil.rmtree(jdir, ignore_errors=True)


def add_background(it):
    f = it.file
    size = f.get("file_size") or 0
    if size > TG_DOWNLOAD_LIMIT:
        say(it.chat, "Видео больше 20 МБ — бот не сможет его скачать. "
                     "Отправь его как обычное видео (не файлом) или покороче.")
        return
    bgs = load_json(BG_PATH, [])
    if any(b.get("file_unique_id") == f.get("file_unique_id") for b in bgs):
        say(it.chat, "Этот фон уже есть (всего %d)." % len(bgs))
        return
    name = (f.get("file_name") or "bg.mp4").lower()
    ext = os.path.splitext(name)[1] or ".mp4"
    bgs.append({"file_id": f["file_id"], "file_unique_id": f.get("file_unique_id"),
                "ext": ext, "added": it.date})
    save_backgrounds(bgs)
    say(it.chat, "Фон добавлен ✅ (всего %d)" % len(bgs))


def command(it):
    cmd = it.text.split()[0].split("@")[0].lower()
    if cmd in ("/bg", "/фоны"):
        say(it.chat, "Фонов: %d" % len(load_json(BG_PATH, [])))
    elif cmd in ("/clearbg",):
        save_backgrounds([])
        say(it.chat, "Все фоны удалены. Пришли новые видео.")
    else:
        say(it.chat, HELP)


# ------------------------------------------------------------- main loop

def process(mode):
    """mode='poll': обработать лёгкое, вернуть True при первом треке.
    mode='run': обработать всё."""
    try:
        updates = tg("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    except TgError as e:
        log("! Telegram недоступен: %s" % e)
        return False
    items = [Item(u) for u in updates]
    log("обновлений: %d" % len(items))
    now = time.time()
    consumed = set()
    cfg = stt = None
    for i, it in enumerate(items):
        if it.uid in consumed or it.kind == "skip":
            confirm(it.uid)
            continue
        if not it.allowed():
            if it.chat:
                say(it.chat, "Это личный бот.")
            confirm(it.uid)
            continue
        if it.kind == "audio":
            count, lyrics = parse_count(it.caption)
            pair = None
            if not lyrics.strip():
                pair = nearby(items, i, "text", consumed)
                if pair is None and now - it.date < DEFER_SEC:
                    log("трек без текста — подождём следующего запуска")
                    return False
                if pair is not None:
                    c2, lyrics = parse_count(pair.text)
                    count = max(count, c2)
            if mode == "poll":
                return True
            if pair is not None:
                consumed.add(pair.uid)
            if not lyrics.strip():
                say(it.chat, "К треку нет текста. Пришли трек ещё раз, а текст припева — в подписи.")
                confirm(it.uid)
                continue
            if cfg is None:
                cfg = settings()
                stt = make_stt(cfg)
            log("трек от %s, клипов: %d" % (it.user or it.user_id, count))
            do_job(it, lyrics, count, cfg, stt)
            confirm(it.uid)
        elif it.kind == "text":
            if nearby(items, i, "audio", consumed) is not None:
                continue            # это текст к треку — его заберёт трек
            if now - it.date < DEFER_SEC:
                return False        # вдруг трек ещё в пути
            say(it.chat, "Это похоже на текст. Пришли трек (mp3), а текст припева — в подписи к нему. /help")
            confirm(it.uid)
        elif it.kind == "video":
            add_background(it)
            confirm(it.uid)
        elif it.kind == "cmd":
            command(it)
            confirm(it.uid)
        else:
            confirm(it.uid)
    return False


def main(argv):
    mode = argv[0] if argv else "run"
    if not TOKEN:
        log("TG_TOKEN не задан — добавь секрет в Settings → Secrets and variables → Actions")
        return 0
    try:
        has_jobs = process(mode)
    except Exception:
        traceback.print_exc()
        has_jobs = False
    if mode == "poll":
        out = os.environ.get("GITHUB_OUTPUT")
        line = "jobs=%s\n" % ("true" if has_jobs else "false")
        if out:
            with open(out, "a") as f:
                f.write(line)
        log(line.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
