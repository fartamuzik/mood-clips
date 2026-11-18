#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот, который собирает клипы в GitHub Actions.

Пишешь боту:
  • трек (mp3/m4a) с текстом припева в подписи  → получаешь клип
  • видео                                     → добавляется в фоны
  • первая строка подписи «3»                 → три клипа с разными фонами
  • в подписи только таймкод «1:05»          → клип без текста
  • фраза в кавычках «…» + эффект             → фраза красивым шрифтом поверх клипа
  • /bg — сколько фонов, /clearbg — удалить фоны, /help — подсказка
  • каждый клип сам встаёт в расписание: Instagram выходит сам (Zernio),
    для TikTok вечером приходит готовое видео с подписью (plan.py, queue.json)
  • «отмена» в ответ на клип — убрать из плана; /plan — расписание

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
import datetime as dt
from collections import OrderedDict
from types import SimpleNamespace
from pathlib import Path

import clip
import plan
import publish

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
    "Привет! Я собираю клипы и веду недельное расписание.\n\n"
    "1) Пришли 2–5 видео для фона — сохраню их.\n"
    "2) Присылай треки (mp3), в подписи к каждому — текст припева. Трек встаёт в очередь недели: "
    "3 трека в день, по 3 клипа с каждого. Клипы на день соберу накануне днём и пришлю на проверку.\n"
    "3) Instagram выходит сам. Вечером пришлю подписи для черновиков TikTok и видео, "
    "которые не влезли во «Входящие».\n"
    "• Клип нужен прямо сейчас — добавь в подпись строку: сразу\n"
    "• /week — неделя: какие треки на какой день и где не хватает\n\n"
    "• Хочешь несколько клипов с разными фонами — первой строкой подписи напиши число, например 3.\n"
    "• Тот же трек ещё раз — просто перешли своё сообщение с треком.\n"
    "• Припев я нахожу в треке сам. Взял не тот — первой строкой подписи напиши, "
    "откуда резать: 1:05 (или 1:05-1:25).\n"
    "• Точные времена строк, если нужно: [0:12] строка\n"
    "• Клип без текста — в подписи только таймкод: 1:05 (или 1:05-1:25).\n\n"
    "✨ Фраза поверх клипа — напиши её в кавычках «», рядом можно эффект и шрифт:\n"
    "1:05\n«не звони мне больше» глитч\n\n"
    "Эффекты: глитч, мерцание, пульс (качает под бит), печать, плавно, без эффекта, "
    "случайно (с числом клипов — у каждого свой).\n"
    "Шрифты: рукописный, каллиграфия, элегантный, жирный (если не написать — подберу под эффект).\n\n"
    "🗓 Расписание.\n"
    "• Убрать трек из очереди или клип из плана — ответь на него (смахни влево) словом: отмена\n"
    "• Своё готовое видео в план — пришли его с подписью: пост текст подписи\n"
    "• Только в одну сеть: пост тт / пост инст\n"
    "• /plan — что и когда выйдет, /week — неделя, /clearplan — очистить расписание\n\n"
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
    cfg["bg_pool"] = 4               # сколько разных фонов чередовать в одном клипе
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
        self.post = None            # (сети, подпись) для команды «пост»
        self.reply_id = (m.get("reply_to_message") or {}).get("message_id")
        if self.kind == "text" and CANCEL_RE.match(self.text):
            self.kind = "cancel"
        elif self.kind == "text" and PLAN_RE.match(self.text):
            self.kind, self.text = "cmd", "/plan"
        own = message_video(m)
        cmd = parse_post(self.caption if own else self.text)
        if cmd is not None:
            reply = m.get("reply_to_message") or {}
            src = own or message_video(reply)
            only, text = cmd
            if own:
                self.kind, self.file = "post", own
                self.post = (only, text)
            elif src or "\n" not in (self.text or "").strip():
                # ответ на клип; без ответа файла нет — бот подскажет, как надо.
                # Многострочный текст без ответа — это текст песни со строкой «пост», его заберёт трек.
                self.kind, self.file = "post", src
                self.post = (only, text or stored_hook(reply.get("caption")))

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


POST_RE = re.compile(r"^\s*/?(пост|выложи|опубликуй|post)(?![\w])[\s:,.!—-]*(.*)$", re.I | re.S)
NET_RE = re.compile(r"^(тт|тикток|тик-ток|tiktok|tt|инст|инста|инстаграм|instagram|ig|reels|рилс)(?![\w])[\s:,.!—-]*(.*)$",
                    re.I | re.S)
HOOK_MARK = "📝 "
CANCEL_RE = re.compile(r"^\s*/?(отмена|отменить|отмени|убрать|убери|не надо|не выкладывай|cancel)\s*[.!]*\s*$", re.I)
PLAN_RE = re.compile(r"^\s*(план|расписание)\s*[?.!]*\s*$", re.I)


def message_video(m):
    """Видео из сообщения (обычное или файлом) либо None."""
    if not m:
        return None
    if m.get("video"):
        return m["video"]
    doc = m.get("document")
    if doc:
        name = (doc.get("file_name") or "").lower()
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("video/") or name.endswith(clip.VIDEO_EXT):
            return doc
    return None


def parse_post(text):
    """«пост инст моя подпись» → (["instagram"], "моя подпись"); не команда → None."""
    m = POST_RE.match(text or "")
    if not m:
        return None
    rest = m.group(2).strip()
    only = None
    n = NET_RE.match(rest)
    if n:
        word = n.group(1).lower()
        only = ["tiktok"] if word in ("тт", "тикток", "тик-ток", "tiktok", "tt") else ["instagram"]
        rest = n.group(2).strip()
    return only, rest


FLAG_RE = re.compile(r"^\s*/?(пост|выложи|опубликуй|post)(?:\s+(тт|тикток|тик-ток|tiktok|tt|инст|инста|инстаграм|"
                     r"instagram|ig|reels|рилс))?\s*[.!]*\s*$", re.I)


def split_post_flag(text):
    """Ищет в подписи к треку отдельную строку «пост» (или «пост тт» / «пост инст»).
    -> (нужна ли публикация, сети или None, подпись без этой строки)"""
    lines = (text or "").replace("\r", "\n").split("\n")
    for i, l in enumerate(lines):
        m = FLAG_RE.match(l)
        if m:
            only = None
            if m.group(2):
                word = m.group(2).lower()
                only = ["tiktok"] if word in ("тт", "тикток", "тик-ток", "tiktok", "tt") else ["instagram"]
            return True, only, "\n".join(lines[:i] + lines[i + 1:])
    return False, None, text or ""


NOW_RE = re.compile(r"^\s*(сразу|сейчас|срочно|now)\s*[.!]*\s*$", re.I)


def split_now_flag(text):
    """Строка «сразу» в подписи к треку: собрать клипы сейчас, а не по недельному плану."""
    lines = (text or "").replace("\r", "\n").split("\n")
    for i, l in enumerate(lines):
        if NOW_RE.match(l):
            return True, "\n".join(lines[:i] + lines[i + 1:])
    return False, text or ""


def track_title(lyrics, phrase, file):
    for line in (lyrics or "").split("\n"):
        line = re.sub(r"\[[^\]]*\]", "", line)
        line = re.sub(r"^\s*\d{1,2}:\d{2}(\s*[-–]\s*\d{1,2}:\d{2})?\s*", "", line).strip()
        if re.search(r"[A-Za-zА-Яа-яЁё]", line):
            return line[:40]
    if phrase and phrase.get("text"):
        return phrase["text"][:40]
    return os.path.splitext((file or {}).get("file_name") or "трек")[0][:40]


def stored_hook(caption):
    """Подпись, которую бот сам приписал к клипу строкой «📝 …»."""
    for line in (caption or "").split("\n"):
        if line.startswith(HOOK_MARK):
            return line[len(HOOK_MARK):].strip()
    return ""


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


def parse_caption(text):
    """-> (сколько клипов, фраза «…» или None, текст/таймкод)"""
    phrase, rest = clip.split_phrase(text or "")
    count, rest = parse_count(rest)
    return count, phrase, rest


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


def load_audio_16k(path):
    """16 кГц моно float32 — ровно то, что ждёт Whisper."""
    import numpy as np
    raw = path + ".f32"
    clip.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
              "-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", raw])
    if not os.path.exists(raw):
        return None
    try:
        return np.fromfile(raw, dtype="<f4")
    finally:
        os.remove(raw)


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
        # звук раскодируем сами (ffmpeg -> numpy): чтение файла внутри faster-whisper
        # ломается на новых версиях PyAV (TypeError: ... 'metadata_errors')
        audio = load_audio_16k(audio_path)
        if audio is None or len(audio) < 1600:
            raise RuntimeError("ffmpeg не смог прочитать звук для распознавания")

        def run_once(vad):
            segments, _info = _MODEL.transcribe(
                audio, language=cfg.get("language") or None, beam_size=5,
                word_timestamps=True, initial_prompt=prompt or None,
                condition_on_previous_text=False, vad_filter=vad,
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200} if vad else None)
            out = []
            for seg in segments:
                for w in (seg.words or []):
                    out.append((w.word, float(w.start), float(w.end)))
            return out

        # фильтр тишины только для выделенного голоса; если он «съел» пение — пробуем без него
        words = run_once(bool(cfg.get("_vad")))
        if cfg.get("_vad") and len(words) < 3:
            log("  мало слов с фильтром тишины — пробую без него")
            words = run_once(False)
        return words
    except Exception as e:
        raise clip.Fail("Whisper: %s: %s" % (type(e).__name__, str(e)[:200]))


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
        clip.note("выделение голоса не сработало: %s: %s" % (type(e).__name__, str(e)[:200]))
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


def phrase_variants(phrase, count, cfg):
    """Фраза для каждого клипа: эффект из подписи, иначе из настроек; «случайно» — разные."""
    if not phrase:
        return [None] * count
    eff = clip.norm_effect(phrase.get("effect")) or clip.norm_effect(cfg.get("phrase_effect")) or "glitch"
    font = phrase.get("font") or clip.norm_font(cfg.get("phrase_font"))
    if eff == "random":
        pool = list(clip.FX_ALL)
        random.shuffle(pool)
        effs = [pool[k % len(pool)] for k in range(count)]
    else:
        effs = [eff] * count
    return [dict(phrase, effect=e, font=font, seed=k) for k, e in enumerate(effs)]


def do_job(it, lyrics, count, cfg, stt, phrase=None, autopost=None, slot=None):
    """Собирает клипы. slot = {day, index, per_day} — место трека в недельном плане.
    Возвращает True, если клипы получились."""
    chat = it.chat
    f = it.file
    made = []
    if phrase:
        hint, rest = clip.split_window_hint(lyrics)
        if not hint and not clip.parse_lyrics(rest):
            say(chat, "Фразу вижу, но не знаю, откуда резать трек. Пришли трек ещё раз и добавь "
                      "в подпись таймкод, например:\n1:05\n«%s»" % phrase["text"])
            return False
    if (f.get("file_size") or 0) > TG_DOWNLOAD_LIMIT:
        say(chat, "Трек больше 20 МБ — Telegram не даёт боту его скачать. Пришли mp3 поменьше.")
        return False
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

        # несколько фонов на клип: кадры меняются под бит, чередуя ролики
        bgs = load_json(BG_PATH, [])
        random.shuffle(bgs)
        want = max(1, int(cfg.get("bg_pool", 4)))
        pool, failed = [], 0
        for b in bgs:
            if len(pool) >= max(want, count):
                break
            p = os.path.join(jdir, "bg%d%s" % (len(pool), b.get("ext", ".mp4")))
            try:
                pool.append(download(b["file_id"], p))
            except TgError as e:
                failed += 1
                log("! фон не скачался: %s" % e)
        if failed:
            say(chat, "Не смог скачать %d фон(а) — использую остальные." % failed)
        if not pool:
            pool = [gradient_bg(os.path.join(jdir, "grad.mp4"))]
        hook = None
        phrases = phrase_variants(phrase, count, cfg)
        for k in range(count):
            first = pool[k % len(pool)]                  # каждый вариант начинается с другого фона
            rest = [v for v in pool if v != first]
            random.shuffle(rest)
            videos = [first] + rest[:want - 1]
            out = os.path.join(jdir, "clip%d.mp4" % (k + 1))
            res = clip.make_clip(cfg, audio, videos, lyrics, out, timing=timing, phrase=phrases[k])
            hook = res["hook"]
            if os.path.getsize(out) > TG_UPLOAD_LIMIT:
                say(chat, "Клип вышел больше 50 МБ — уменьши max_len или video_bitrate в settings.json.")
                continue
            cap = "Отрывок %s · %s · смена кадров %s" % (where, timing["mode"], res.get("cuts", ""))
            if res.get("phrase_fx"):
                cap += " · фраза: %s" % res["phrase_fx"]
            if count > 1:
                cap = "Клип %d/%d · %s" % (k + 1, count, cap)
            if hook:
                cap += "\n" + HOOK_MARK + hook
            sent = tg("sendVideo", {"chat_id": chat, "caption": cap, "supports_streaming": "true",
                                    "width": cfg["width"], "height": cfg["height"]},
                      files={"video": out}, timeout=300)
            made.append({"n": k + 1, "path": out, "hook": hook, "msg_id": (sent or {}).get("message_id"),
                         "file_id": sent_file_id(sent)})
        if made and (cfg.get("auto_schedule", True) or autopost is not None or slot):
            schedule_clips(chat, it.uid, made, cfg, only=autopost or None, total=count, slot=slot)
        elif hook:
            say(chat, "Подпись для TikTok (нажми, чтобы скопировать):\n<code>%s</code>"
                % esc(plan.caption(hook, cfg, "tiktok")), html=True)
        if not bgs:
            say(chat, "Фонов пока нет, поэтому фон — градиент. Пришли мне несколько видео.")
        weak = timing["mode"].startswith("равномерно") or timing["mode"].startswith("строки по паузам")
        if timing["mode"].startswith("равномерно"):
            say(chat, "⚠️ Не смог найти этот текст в треке, поэтому строки стоят примерно. "
                      "Проверь, что текст совпадает с тем, что поётся, или пришли трек ещё раз "
                      "и первой строкой подписи напиши, где начинается припев, например: 0:45")
        if weak and timing.get("notes"):
            say(chat, "🔧 Подробности (перешли их в чат с Claude, если клип не тот):\n"
                + "\n".join("• " + n for n in timing["notes"])[:3500])
    except clip.Fail as e:
        say(chat, "Не получилось: %s" % e)
        return False
    except Exception as e:
        traceback.print_exc()
        say(chat, "Ошибка: %s" % str(e)[:300])
        return False
    finally:
        shutil.rmtree(jdir, ignore_errors=True)
    return bool(made)


def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def sent_file_id(msg):
    msg = msg or {}
    for k in ("video", "document", "animation"):
        if isinstance(msg.get(k), dict) and msg[k].get("file_id"):
            return msg[k]["file_id"]
    return None


def schedule_clips(chat, uid, made, cfg, only=None, total=1, slot=None):
    """Ставит готовые клипы в расписание и пишет, когда что выйдет.
    slot = {day, index, per_day}: клипы недельного трека встают в слоты своего дня."""
    q = plan.load()
    now = utcnow()
    lines, problems, placed = [], [], []
    for c in made:
        label = ("Клип %d" % c["n"]) if total > 1 else "Клип"
        if not c["hook"]:
            lines.append("%s — без подписи, в план не поставил. Ответь на него: пост текст подписи" % label)
            continue
        day = slot["day"] if slot else None
        idx = (c["n"] - 1) * slot["per_day"] + slot["index"] if slot else None
        item, probs = plan.add_clip(q, cfg, now, cid="%s-%d" % (uid, c["n"]), hook=c["hook"], path=c["path"],
                                    file_id=c["file_id"], msg_id=c["msg_id"], chat=chat, only=only,
                                    day=day, slot=idx)
        problems += probs
        lines.append("%s: %s" % (label, plan.describe(item, cfg)))
        if item in q["items"]:
            placed.append(item)
    head = ("🗓 Клипы на %s — в расписании:\n" % plan.day_label(slot["day"])) if slot else "🗓 В расписании:\n"
    text = head + esc("\n".join(lines))
    if problems:
        text += "\n\n⚠️ " + esc("\n⚠️ ".join(problems))
    hooks = [c["hook"] for c in made if c["hook"]]
    if hooks:
        text += "\n\nПодпись для TikTok:\n<code>%s</code>" % esc(plan.caption(hooks[0], cfg, "tiktok"))
    text += "\n\nУбрать клип — ответь на него словом: отмена. Всё расписание — /plan"
    try:
        msg = tg("sendMessage", {"chat_id": chat, "text": text,
                                 "parse_mode": "HTML", "disable_web_page_preview": True})
        for item in placed:
            item["plan_msg_id"] = msg.get("message_id")
    except TgError as e:
        log("! план: %s" % e)
    plan.save(q, now)


def do_post(it):
    """«пост» в ответ на клип (или видео с подписью «пост …») — ставит его в расписание."""
    chat = it.chat
    f = it.file
    only, caption = it.post
    if not f:
        say(chat, "Не вижу, какой клип поставить в план — я понимаю это только по ответу на сообщение.\n\n"
                  "Смахни сообщение с клипом влево (или нажми на него → «Ответить») и напиши: пост")
        return
    own = message_video(it.msg)
    msg_id = it.msg.get("message_id") if own else it.reply_id
    q = plan.load()
    if any(x.get("msg_id") == msg_id for x in q["items"]):
        say(chat, "Этот клип уже в расписании. Посмотреть — /plan")
        return
    if not caption:
        say(chat, "У этого видео нет подписи. Ответь на него так: пост текст подписи")
        return
    if (f.get("file_size") or 0) > TG_DOWNLOAD_LIMIT:
        say(chat, "Видео больше 20 МБ — Telegram не даёт боту его скачать. Пришли ролик поменьше.")
        return
    jdir = os.path.join(JOBS, "post%s" % it.uid)
    shutil.rmtree(jdir, ignore_errors=True)
    os.makedirs(jdir)
    try:
        path = download(f["file_id"], os.path.join(jdir, "clip.mp4"))
        made = [{"n": 1, "path": path, "hook": caption, "msg_id": msg_id, "file_id": f["file_id"]}]
        schedule_clips(chat, "p%s" % it.uid, made, settings(), only=only)
    except TgError as e:
        say(chat, "Не смог скачать видео из Telegram: %s" % e)
    except Exception as e:
        traceback.print_exc()
        say(chat, "Ошибка при постановке в план: %s" % str(e)[:300])
    finally:
        shutil.rmtree(jdir, ignore_errors=True)


def do_cancel(it):
    if not it.reply_id:
        say(it.chat, "Чтобы убрать клип из расписания, ответь словом «отмена» прямо на сообщение с клипом "
                     "(смахни его влево). Всё расписание — /plan")
        return
    q = plan.load()
    t = plan.cancel_track(q, it.reply_id)
    if t:
        plan.save(q)
        say(it.chat, "Убрал из очереди трек «%s» (%s)." % (t["title"], plan.day_label(dt.date.fromisoformat(t["day"]))))
        return
    n, notes = plan.cancel(q, it.reply_id)
    plan.save(q)
    if n:
        text = "Убрал из расписания: %d %s." % (n, plural(n, "клип", "клипа", "клипов"))
    else:
        text = "Этого клипа нет в расписании. Всё расписание — /plan"
    if notes:
        text += "\n" + "\n".join(notes)
    say(it.chat, text)


def queue_track(it, lyrics, count, phrase, cfg):
    """Трек встаёт в очередь недели."""
    q = plan.load()
    now = utcnow()
    cnt = count if count > 1 else int(cfg.get("clips_per_track", 3))
    title = track_title(lyrics, phrase, it.file)
    t = plan.add_track(q, cfg, now, tid="t%s" % it.uid, file=it.file, voice=bool(it.msg.get("voice")),
                       lyrics=lyrics, phrase=phrase, count=cnt, title=title,
                       msg_id=it.msg.get("message_id"), chat=it.chat)
    day = dt.date.fromisoformat(t["day"])
    b = plan.build_at(cfg, day)
    text = "🎵 В очереди: «%s» → %s, трек %d из %d, клипов: %d.\n" % (
        title, plan.day_label(day), t["index"] + 1, plan.per_day(cfg), cnt)
    if now >= b:
        text += "Клипы соберу в ближайшие минуты и пришлю на проверку.\n"
    else:
        text += "Клипы соберу %s после %s и пришлю на проверку.\n" % (plan.day_label(b.date()), b.strftime("%H:%M"))
    text += "Убрать из очереди — ответь на трек словом: отмена. Неделя — /week"
    try:
        msg = tg("sendMessage", {"chat_id": it.chat, "text": text, "disable_web_page_preview": True})
        t["note_id"] = msg.get("message_id")
    except TgError as e:
        log("! очередь: %s" % e)
    plan.save(q, now)


def build_due(deadline):
    """Собирает клипы для треков, чей день сборки настал (не дольше deadline)."""
    cfg = settings()
    q = plan.load()
    now = utcnow()
    due = plan.due_builds(q, cfg, now)
    plan.save(q, now)
    if not due:
        return
    stt = make_stt(cfg)
    for t in due:
        if time.time() > deadline:
            log("время вышло — остальные треки соберу в следующий запуск")
            break
        q = plan.load()
        t = plan.find_track(q, t["id"])
        if not t or t.get("status") != "waiting":
            continue
        t["attempts"] = t.get("attempts", 0) + 1
        plan.save(q)
        chat = q.get("chat")
        day = dt.date.fromisoformat(t["day"])
        log("сборка: %s на %s" % (t["title"], t["day"]))
        say(chat, "🎬 Клипы на %s — трек %d из %d: «%s»" % (plan.day_label(day), t["index"] + 1,
                                                          plan.per_day(cfg), t["title"]))
        it = SimpleNamespace(uid=t["id"], chat=chat, date=0, msg={"voice": True} if t.get("voice") else {},
                             file={"file_id": t["file_id"], "file_name": t.get("file_name"),
                                   "file_size": t.get("file_size")})
        ok = do_job(it, t["lyrics"], t["count"], cfg, stt, t.get("phrase"),
                    slot={"day": day, "index": t["index"], "per_day": plan.per_day(cfg)})
        q = plan.load()
        t2 = plan.find_track(q, t["id"])
        if t2:
            if ok:
                t2["status"] = "built"
            elif t2.get("attempts", 0) >= 2:
                t2["status"] = "failed"
                say(chat, "Не смог собрать клипы для «%s» — убрал трек из плана. Пришли его ещё раз." % t2["title"])
        plan.save(q)


def check_instagram(now=None):
    """Сразу сообщает, если ролик в Instagram не вышел по плану."""
    if not publish.enabled():
        return
    q = plan.load()
    now = now or utcnow()
    chat = q.get("chat")
    bad, changed = [], False
    for item in q["items"]:
        if not item.get("instagram_post") or item.get("ig_checked"):
            continue
        at = dt.datetime.fromisoformat(item["instagram_at"])
        if not (now - dt.timedelta(days=1) < at <= now - dt.timedelta(minutes=15)):
            continue
        st = publish.status(item["instagram_post"])
        if st == "published":
            item["ig_checked"] = changed = True
        elif st in ("failed", "partial", "deleted") or (at <= now - dt.timedelta(hours=3) and st):
            item["ig_checked"] = item["ig_failed"] = changed = True
            bad.append(item)
    if bad and chat:
        cfg = settings()
        say(chat, "⚠️ Instagram: не вышел%s по плану — %s. Проверь в Zernio."
            % ("и" if len(bad) > 1 else "", ", ".join(
                "«%s» (%s)" % (b.get("hook", ""), dt.datetime.fromisoformat(b["instagram_at"])
                               .astimezone(plan.ig_tz(cfg)).strftime("%H:%M %d.%m")) for b in bad)))
    if changed:
        plan.save(q, now)


def alert_short_day(now=None):
    """Накануне, когда пора собирать: если на завтра треков меньше нормы — сказать один раз."""
    cfg = settings()
    if not plan.weekly(cfg):
        return
    q = plan.load()
    now = now or utcnow()
    res = plan.short_day(q, cfg, now)
    if not res or not q.get("chat"):
        return
    day, have = res
    need = plan.per_day(cfg) - have
    if have:
        text = "⚠️ На завтра (%s) в очереди %d трек(а) из %d. Пришли ещё %d — соберу сразу." % (
            plan.day_label(day), have, plan.per_day(cfg), need)
    else:
        text = "⚠️ На завтра (%s) треков нет. Пришли %d — соберу сразу, и всё выйдет по плану." % (
            plan.day_label(day), need)
    say(q["chat"], text)
    q["short_alert"] = day.isoformat()
    plan.save(q, now)


def remind(now=None):
    """Вечером: что вышло в Instagram, подписи для черновиков TikTok и видео сверх лимита «Входящих»."""
    cfg = settings()
    q = plan.load()
    now = now or utcnow()
    res = plan.due(q, cfg, now)
    if res is None:
        return
    today, items = res
    chat = q.get("chat")
    ig = plan.instagram_since(q, now)
    if chat and (items or ig):
        parts = ["🌙 Вечер, %s" % plan.day_label(dt.date.fromisoformat(today))]
        if ig:
            bad = []
            for item in ig:
                st = item.get("ig_failed") and "failed" or (
                    publish.status(item["instagram_post"]) if item.get("instagram_post") and publish.enabled() else None)
                if st in ("failed", "partial", "deleted"):
                    bad.append(item)
            if bad:
                parts.append("Instagram: вышло %d из %d. ⚠️ Не вышли: %s — проверь в Zernio." % (
                    len(ig) - len(bad), len(ig), ", ".join("«%s»" % esc(b.get("hook", "")) for b in bad)))
            else:
                parts.append("Instagram: вышло %d %s по плану ✅" % (len(ig), plural(len(ig), "ролик", "ролика", "роликов")))
        inbox, manual = [], []
        for item in items:
            ok = False
            if item.get("tiktok_post"):
                st = publish.status(item["tiktok_post"]) if publish.enabled() else None
                ok = st not in ("failed", "partial", "deleted")
            (inbox if ok else manual).append(item)
            item["tiktok_done"] = True
        if inbox:
            groups = OrderedDict()
            for item in inbox:
                cap = plan.caption(item.get("hook"), cfg, "tiktok")
                groups[cap] = groups.get(cap, 0) + 1
            parts.append("📥 TikTok: во «Входящих» %d %s. Открой уведомление → вставь подпись "
                         "(нажми на неё — скопируется) → «Опубликовать»:" % (
                             len(inbox), plural(len(inbox), "черновик", "черновика", "черновиков")))
            parts.append("\n".join("%s<code>%s</code>" % (("×%d " % n) if n > 1 else "", esc(cap))
                                    for cap, n in groups.items()))
        if manual:
            parts.append("📲 Ещё %d — из галереи (TikTok держит во «Входящих» не больше %d): сохрани видео ниже → "
                         "TikTok «+» → вставь подпись → «Опубликовать»." % (len(manual), int(cfg.get("tiktok_inbox_max", 5))))
        text = "\n\n".join(parts)
        try:
            tg("sendMessage", {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                               "disable_web_page_preview": True})
        except TgError as e:
            log("! сводка: %s" % e)
            say(chat, re.sub(r"</?code>", "", text)[:4000])
        for k, item in enumerate(manual, 1):
            cap = "<code>%s</code>" % esc(plan.caption(item.get("hook"), cfg, "tiktok"))
            sent = False
            if item.get("file_id"):
                for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
                    try:
                        tg(method, {"chat_id": chat, field: item["file_id"], "caption": cap, "parse_mode": "HTML"})
                        sent = True
                        break
                    except TgError as e:
                        log("! %s: %s" % (method, e))
            if not sent:
                say(chat, "%d) %s" % (k, cap), html=True)
    else:
        for item in items:
            item["tiktok_done"] = True
    q["reminded"] = today
    q["reminded_at"] = now.isoformat()
    plan.save(q, now)


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
    elif cmd in ("/week", "/неделя"):
        say(it.chat, plan.week_overview(plan.load(), settings(), utcnow()))
    elif cmd in ("/plan", "/план", "/schedule"):
        say(it.chat, plan.overview(plan.load(), settings(), utcnow()))
    elif cmd in ("/clearplan",):
        q = plan.load()
        n, _ = plan.cancel_items(q, list(q["items"]))
        plan.save(q)
        say(it.chat, "Расписание очищено (%d %s). То, что уже вышло в Instagram, остаётся."
            % (n, plural(n, "клип", "клипа", "клипов")))
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
            flag, only, cap_text = split_post_flag(it.caption)
            now_flag, cap_text = split_now_flag(cap_text)
            count, phrase, lyrics = parse_caption(cap_text)
            pair = None
            if not lyrics.strip():
                pair = nearby(items, i, "text", consumed)
                if pair is None and phrase is None and now - it.date < DEFER_SEC:
                    log("трек без текста — подождём следующего запуска")
                    return False
                if pair is not None:
                    f2, o2, pair_text = split_post_flag(pair.text)
                    flag, only = flag or f2, only or o2
                    n2, pair_text = split_now_flag(pair_text)
                    now_flag = now_flag or n2
                    c2, p2, lyrics = parse_caption(pair_text)
                    count = max(count, c2)
                    phrase = phrase or p2
            if cfg is None:
                cfg = settings()
            if plan.weekly(cfg) and not now_flag:
                if pair is not None:
                    consumed.add(pair.uid)
                if not lyrics.strip() and phrase is None:
                    say(it.chat, "К треку нет текста. Пришли трек ещё раз, а текст припева — в подписи. "
                                 "Нужен клип без текста — напиши в подписи только таймкод, например 1:05.")
                else:
                    queue_track(it, lyrics, count, phrase, cfg)
                confirm(it.uid)
                continue
            if mode == "poll":
                return True
            if pair is not None:
                consumed.add(pair.uid)
            if not lyrics.strip() and phrase is None:
                say(it.chat, "К треку нет текста. Пришли трек ещё раз, а текст припева — в подписи. "
                             "Нужен клип без текста — напиши в подписи только таймкод, например 1:05.")
                confirm(it.uid)
                continue
            if stt is None:
                stt = make_stt(cfg)
            log("трек от %s, клипов: %d" % (it.user or it.user_id, count))
            do_job(it, lyrics, count, cfg, stt, phrase, autopost=(only or []) if flag else None)
            confirm(it.uid)
        elif it.kind == "text":
            if nearby(items, i, "audio", consumed) is not None:
                continue            # это текст к треку — его заберёт трек
            if now - it.date < DEFER_SEC:
                return False        # вдруг трек ещё в пути
            say(it.chat, "Это похоже на текст. Пришли трек (mp3), а текст припева — в подписи к нему. /help")
            confirm(it.uid)
        elif it.kind == "post":
            do_post(it)
            confirm(it.uid)
        elif it.kind == "cancel":
            do_cancel(it)
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
    started = time.time()
    try:
        has_jobs = process(mode)
    except Exception:
        traceback.print_exc()
        has_jobs = False
    if mode == "poll":
        for step in (check_instagram, alert_short_day, remind):
            try:
                step()
            except Exception:
                traceback.print_exc()
        try:
            cfg = settings()
            q = plan.load()
            if plan.weekly(cfg) and plan.due_builds(q, cfg, utcnow()):
                has_jobs = True
        except Exception:
            traceback.print_exc()
    else:
        try:
            build_due(started + 25 * 60)
        except Exception:
            traceback.print_exc()
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
