#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clip.py — вертикальный клип для TikTok прямо на айфоне (a-Shell).

Видеоряд из твоего ролика + трек + строки припева по центру, которые
сменяются под пение. Готовый clip.mp4 и подпись caption.txt кладутся
туда же, откуда Команда (Shortcuts) их забирает.

Команды в a-Shell (из папки ~/Documents/mood):
  python3 clip.py --check              проверить, что всё работает на этом телефоне
  python3 clip.py --demo               собрать тестовый клип demo.mp4 без своих файлов
  python3 clip.py --set key=value      поменять настройку (например groq_api_key=gsk_...)
  python3 clip.py                      собрать клип из файлов, переданных Командой

Тайминг строк (по приоритету):
  1) времена в тексте:  [0:12] первая строка   /   0:15.5 вторая строка
  2) автораспознавание через Whisper (бесплатный ключ Groq в настройках)
  3) равномерно по длине клипа (приблизительно)
"""

import os
import sys
import re
import json
import math
import array
import random
import shutil
import shlex
import uuid
import difflib
import time
import tempfile
import traceback
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
WORK = os.path.join(HERE, "work")
TRACKS = os.path.join(HERE, "tracks")
BG_DIR = os.path.join(HERE, "bg")
FONT_URL = ("https://github.com/JulietaUla/Montserrat/raw/master/"
            "fonts/ttf/Montserrat-ExtraBold.ttf")

DEFAULTS = {
    "groq_api_key": "",
    "stt_url": "https://api.groq.com/openai/v1/audio/transcriptions",
    "stt_model": "whisper-large-v3",
    "language": "",          # "ru", "en"... пусто = определить само
    "max_len": 60,           # максимум секунд в клипе
    "min_len": 8,            # минимум секунд в клипе
    "auto_chorus": True,     # сам искать в треке место, где поётся присланный текст
    "chorus_pick": "first",  # какой припев брать: first / last / best
    "chorus_repeats": 1,     # припев спет несколько раз подряд — сколько повторов брать
    "pre_roll": 0.6,         # секунд музыки до первой строки
    "post_roll": 1.5,        # секунд музыки после последней строки
    "audio_start": 0,        # >0 — всегда начинать с этой секунды (без автопоиска)
    "intro_sec": 0,          # для равномерного режима: когда начинается пение
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "font": "font.ttf",
    "font_size": 84,
    "uppercase": False,
    "text_color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke": 6,
    "text_y_offset": 0,      # сдвиг текста от центра, px (минус = выше)
    "dim": 0.25,             # затемнение видео под текстом, 0..1
    "fade": True,            # плавное появление строк
    "video_bitrate": "8M",
    "hashtags": "",
    "delete_inputs": True,
}

AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg")
VIDEO_EXT = (".mov", ".mp4", ".m4v")


class Fail(Exception):
    pass


def log(msg):
    print(msg, flush=True)


_NOTES = []


def note(msg):
    """Подробности для разбора, если тайминг не получился (бот присылает их в чат)."""
    log("  " + msg)
    _NOTES.append(msg)


# ----------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            raise Fail("config.json повреждён (%s). Удали его — создастся заново." % e)
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def set_option(cfg, pair):
    if "=" not in pair:
        raise Fail("Формат: --set ключ=значение")
    k, v = pair.split("=", 1)
    k = k.strip()
    if k not in DEFAULTS:
        raise Fail("Нет такой настройки: %s. Есть: %s" % (k, ", ".join(DEFAULTS)))
    d = DEFAULTS[k]
    if isinstance(d, bool):
        v = v.strip().lower() in ("1", "true", "yes", "да", "on")
    elif isinstance(d, int) and not isinstance(d, bool):
        v = float(v) if "." in v else int(v)
    elif isinstance(d, float):
        v = float(v)
    cfg[k] = v
    save_config(cfg)
    shown = (v[:6] + "…") if k.endswith("key") and v else v
    log("Сохранено: %s = %s" % (k, shown))


# ------------------------------------------------------------ run commands
# В a-Shell subprocess может не работать — тогда идём через os.system
# с перенаправлением вывода в файлы.

_SUBPROCESS_OK = None


def run(args, force_system=False):
    """Возвращает (код, stdout, stderr)."""
    global _SUBPROCESS_OK
    if not force_system and _SUBPROCESS_OK is not False \
            and not os.environ.get("CLIP_FORCE_OSSYSTEM"):
        try:
            import subprocess
            p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _SUBPROCESS_OK = True
            return (p.returncode,
                    (p.stdout or b"").decode("utf-8", "replace"),
                    (p.stderr or b"").decode("utf-8", "replace"))
        except FileNotFoundError:
            return 127, "", "not found: %s" % args[0]
        except Exception:
            if _SUBPROCESS_OK:
                raise
            _SUBPROCESS_OK = False
    os.makedirs(WORK, exist_ok=True)
    o = os.path.join(WORK, "_stdout.txt")
    e = os.path.join(WORK, "_stderr.txt")
    for p in (o, e):
        if os.path.exists(p):
            os.remove(p)
    cmd = " ".join(shlex.quote(a) for a in args)
    code = os.system("%s > %s 2> %s" % (cmd, shlex.quote(o), shlex.quote(e)))
    out = err = ""
    if os.path.exists(o):
        with open(o, encoding="utf-8", errors="replace") as f:
            out = f.read()
    if os.path.exists(e):
        with open(e, encoding="utf-8", errors="replace") as f:
            err = f.read()
    return code, out, err


def run_expect(args, token):
    """Запускает команду; если в выводе нет token — пробует другим способом."""
    code, out, err = run(args)
    if token not in out + err:
        code, out, err = run(args, force_system=True)
    return code, out, err


def have_cmd(name):
    if shutil.which(name):
        return True
    code, out, err = run([name, "-version"])
    return code != 127 and "not found" not in (err.lower())


# ------------------------------------------------------------------ ffmpeg

def media_duration(path):
    code, out, err = run_expect(["ffmpeg", "-hide_banner", "-nostdin", "-i", path], "Duration")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out + err)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def pick_encoder(cfg):
    code, out, err = run_expect(["ffmpeg", "-hide_banner", "-encoders"], "aac")
    text = out + err
    if "h264_videotoolbox" in text:
        return "h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-b:v", str(cfg["video_bitrate"])]
    if "libx264" in text:
        br = str(cfg["video_bitrate"])
        m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)$", br)
        buf = ("%g%s" % (float(m.group(1)) * 2, m.group(2))) if m else br
        return "libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                           "-maxrate", br, "-bufsize", buf]
    return "mpeg4", ["-c:v", "mpeg4", "-q:v", "2"]


# ------------------------------------------------------------------ lyrics

TIME_RE = re.compile(r"^\s*\[?\s*(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*\]?\s+(.*\S)\s*$")
TAG_RE = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")   # [Chorus], (Instrumental)


def parse_lyrics(text, uppercase=False):
    """-> список (время|None, строка)"""
    lines = []
    for raw in text.replace("\r", "\n").split("\n"):
        s = raw.strip()
        if not s or TAG_RE.match(s):
            continue
        t = None
        m = TIME_RE.match(s)
        if m and (m.group(1) is not None or s.startswith("[")):
            mins = int(m.group(1) or 0)
            t = mins * 60 + float(m.group(2).replace(",", "."))
            s = m.group(3).strip()
        s = re.sub(r"\s*\[[^\]]*\]\s*", " ", s).strip()  # теги внутри строки
        if not s:
            continue
        lines.append((t, s.upper() if uppercase else s))
    return lines


def norm_word(w):
    w = w.lower().replace("ё", "е")
    return re.sub(r"[\W_]+", "", w, flags=re.UNICODE)


def pick_hook(lines):
    """Подпись: самая повторяющаяся строка, иначе первая."""
    def key(l):
        return " ".join(norm_word(w) for w in l.split())
    counts = {}
    for l in lines:
        counts[key(l)] = counts.get(key(l), 0) + 1
    best, best_n = lines[0], 0
    for l in lines:
        if counts[key(l)] > best_n:
            best, best_n = l, counts[key(l)]
    return best


# ------------------------------------------------------------- whisper/STT

def _multipart(fields, file_field, file_path):
    boundary = "----clip" + uuid.uuid4().hex
    parts = []
    for k, v in fields:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode("utf-8"))
    name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = f.read()
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n"
                  % (boundary, file_field, name)).encode("utf-8"))
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return boundary, b"".join(parts)


def transcribe(audio_path, cfg, prompt):
    key = cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    base = [("model", cfg["stt_model"]), ("response_format", "verbose_json"),
            ("temperature", "0"), ("timestamp_granularities[]", "word")]
    if cfg.get("language"):
        base.append(("language", cfg["language"]))
    attempts = []
    if prompt:
        attempts.append(base + [("timestamp_granularities[]", "segment"), ("prompt", prompt)])
        attempts.append(base + [("prompt", prompt)])
    attempts.append(base)
    last = None
    for fields in attempts:
        boundary, body = _multipart(fields, "file", audio_path)
        req = urllib.request.Request(cfg["stt_url"], data=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "clip.py",
        })
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP %s: %s" % (e.code, msg)
            if e.code in (401, 403):
                raise Fail("Ключ Groq не подошёл (%s). Проверь groq_api_key." % e.code)
            if e.code != 400:
                raise Fail("Распознавание не удалось: " + last)
        except Exception as e:
            raise Fail("Нет связи с сервисом распознавания: %s" % e)
    else:
        raise Fail("Распознавание не удалось: %s" % last)

    words = []
    for w in data.get("words") or []:
        try:
            words.append((str(w["word"]), float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError):
            pass
    if not words:  # только сегменты — делим время сегмента между словами
        for seg in data.get("segments") or []:
            toks = str(seg.get("text", "")).split()
            if not toks:
                continue
            s, e = float(seg["start"]), float(seg["end"])
            step = (e - s) / len(toks)
            for i, t in enumerate(toks):
                words.append((t, s + i * step, s + (i + 1) * step))
    return words


# ---------------------------------------------------------------- alignment

def _dp_align(A, B, simcache):
    """Полу-глобальное выравнивание слов текста A со словами распознавания B."""
    n, m = len(A), len(B)
    GAP_A, GAP_B = -0.4, -0.15
    prev = [0.0] * (m + 1)
    back = [bytearray(m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        back[0][j] = 2
    for i in range(1, n + 1):
        cur = [0.0] * (m + 1)
        cur[0] = prev[0] + GAP_A
        back[i][0] = 1
        a = A[i - 1]
        bi = back[i]
        for j in range(1, m + 1):
            b = B[j - 1]
            key = (a, b)
            s = simcache.get(key)
            if s is None:
                if a == b:
                    s = 1.0
                elif min(len(a), len(b)) <= 2:     # «в»/«во», «и»/«ты» — только точно
                    s = 0.0
                else:
                    s = difflib.SequenceMatcher(None, a, b).ratio()
                simcache[key] = s
            d = prev[j - 1] + 2 * s - 1
            u = prev[j] + GAP_A
            l = cur[j - 1] + GAP_B
            if d >= u and d >= l:
                cur[j] = d
            elif u >= l:
                cur[j] = u
                bi[j] = 1
            else:
                cur[j] = l
                bi[j] = 2
        prev = cur
    jbest = max(range(m + 1), key=lambda j: prev[j])
    pairs = []
    i, j = n, jbest
    while i > 0:
        step = back[i][j] if j > 0 else 1
        if step == 0:
            pairs.append((i - 1, j - 1, simcache[(A[i - 1], B[j - 1])]))
            i -= 1
            j -= 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return prev[jbest], pairs


def align_lines(lines, words, kmax=3, stats=None):
    """lines: [str], words: [(word,start,end)] -> (lines_out, [(start,end)|None])
    Текст может повторяться в песне до kmax раз подряд (lines_out = lines * k).
    stats (dict) получает "words" — сколько слов текста совпало."""
    tw = [(norm_word(w), s, e) for w, s, e in words]
    tw = [x for x in tw if x[0]]
    B = [x[0] for x in tw]
    simcache = {}
    best = None
    for k in range(1, kmax + 1):
        seq_lines = lines * k
        A, owner, pos = [], [], []
        for li, l in enumerate(seq_lines):
            p = 0
            for t in l.split():
                nw = norm_word(t)
                if nw:
                    A.append(nw)
                    owner.append(li)
                    pos.append(p)
                    p += 1
        if not A or not B:
            if stats is not None:
                stats["words"] = 0
            return lines, [None] * len(lines)
        score, pairs = _dp_align(A, B, simcache)
        # лишний повтор текста принимаем, только если в нём реально нашлись слова
        last_copy = sum(1 for ai, bj, s in pairs if s >= 0.6 and owner[ai] >= len(lines) * (k - 1))
        copy_words = len(A) // k
        if best is None or (score > best[0] + 1.0 and last_copy >= 0.3 * copy_words):
            best = (score, pairs, seq_lines, owner, pos)
    score, pairs, seq_lines, owner, pos = best
    nwords = [len([t for t in l.split() if norm_word(t)]) for l in seq_lines]
    hits = [[] for _ in seq_lines]          # (номер слова в строке, начало, конец)
    for ai, bj, s in pairs:
        if s >= 0.6:
            hits[owner[ai]].append((pos[ai], tw[bj][1], tw[bj][2]))
    # слова одной строки поются подряд: оставляем самую плотную группу
    for li, h in enumerate(hits):
        if len(h) < 2:
            continue
        groups, cur = [], [h[0]]
        for x in h[1:]:
            if x[1] - cur[-1][2] > 1.5:
                groups.append(cur)
                cur = [x]
            else:
                cur.append(x)
        groups.append(cur)
        hits[li] = max(groups, key=len)
    if stats is not None:
        stats["words"] = sum(len(h) for h in hits)
    # средняя длина слова — для строк, где расслышано одно слово
    rates = sorted((h[-1][2] - h[0][1]) / (h[-1][0] - h[0][0] + 1)
                   for h in hits if len(h) >= 2 and h[-1][0] > h[0][0])
    d_def = rates[len(rates) // 2] if rates else 0.35
    spans = [None] * len(seq_lines)
    prev_end = -1e9
    last_start = -1.0
    for li, h in enumerate(hits):
        if not h:
            continue
        pf, st, _ = h[0]
        pl, _, en = h[-1]
        # если первое/последнее слово строки не расслышано — достраиваем по темпу строки
        d = (en - st) / (pl - pf + 1) if pl > pf else d_def
        d = min(0.8, max(0.15, d))
        st = max(st - pf * d, prev_end - 0.1)
        en = en + (nwords[li] - 1 - pl) * d
        if st < last_start - 0.05:         # строка не может начаться раньше предыдущей
            continue
        spans[li] = [st, en]
        prev_end, last_start = en, st
    return seq_lines, spans


def fill_gaps(lines, spans, t0, t1):
    """Строки без времени распределяем между соседями пропорционально длине."""
    n = len(lines)
    spans = [list(s) if s else None for s in spans]
    known = [k for k in range(n) if spans[k]]
    steps = sorted((spans[b][0] - spans[a][0]) / (b - a) for a, b in zip(known, known[1:]))
    per_line = steps[len(steps) // 2] if steps else None
    i = 0
    while i < n:
        if spans[i] is not None:
            i += 1
            continue
        j = i
        while j < n and spans[j] is None:
            j += 1
        left = spans[i - 1][1] if i > 0 else t0
        right = spans[j][0] if j < n else t1
        if per_line and i == 0 and j < n:          # нерасслышанные первые строки
            left = max(t0, right - per_line * (j - i))
        if per_line and j == n and i > 0:          # нерасслышанные последние строки
            right = min(t1, spans[i - 1][0] + per_line * (j - i + 1))
        if right <= left:
            right = left + 1.5 * (j - i)
        weights = [len(lines[k]) + 8 for k in range(i, j)]
        tot = float(sum(weights))
        cur = left
        for k, w in zip(range(i, j), weights):
            dur = (right - left) * w / tot
            spans[k] = [cur, cur + dur * 0.92]
            cur += dur
        i = j
    return spans


def even_spans(lines, t0, t1):
    return fill_gaps(lines, [None] * len(lines), t0, t1)


def explicit_spans(parsed, clip_end):
    """parsed: [(t|None, line)] с хотя бы одним временем.
    Строки без времени получают начало между соседними временами;
    каждая строка держится до начала следующей."""
    lines = [l for _, l in parsed]
    starts = [t for t, _ in parsed]
    n = len(lines)
    known = [k for k in range(n) if starts[k] is not None]

    def spread(i, j, left, right):
        """строки i..j-1 делят отрезок [left, right]; строка i начинается в left"""
        weights = [len(lines[k]) + 8 for k in range(i, j)]
        tot = float(sum(weights))
        cur = left
        for k, w in zip(range(i, j), weights):
            starts[k] = cur
            cur += (right - left) * w / tot

    if known[0] > 0:
        spread(0, known[0], 0.0, starts[known[0]])
    for a, b in zip(known, known[1:]):
        if b - a > 1:
            spread(a, b, starts[a], starts[b])
    last = known[-1]
    if last < n - 1:
        spread(last, n, starts[last], max(clip_end, starts[last] + 2.0 * (n - last)))
    for k in range(1, n):  # строго по порядку
        if starts[k] < starts[k - 1] + 0.3:
            starts[k] = starts[k - 1] + 0.3
    spans = [[starts[k], starts[k + 1] if k + 1 < n else clip_end] for k in range(n)]
    # последняя строка звучит примерно столько же, сколько обычная строка
    if n > 1:
        durs = sorted(starts[k + 1] - starts[k] for k in range(n - 1))
        spans[-1][1] = min(clip_end, starts[-1] + max(2.0, durs[len(durs) // 2]))
    else:
        spans[0][1] = min(clip_end, starts[0] + 3.0)
    return lines, spans


# ------------------------------------------------------------ chorus search

WINDOW_RE = re.compile(
    r"^\s*(?:с|от|старт|start|from|@)?\s*(\d{1,2}):(\d{2}(?:[.,]\d+)?)"
    r"\s*(?:(?:-|–|—|до|to)\s*(\d{1,2}):(\d{2}(?:[.,]\d+)?))?\s*$", re.I)


def split_window_hint(text):
    """Первая непустая строка «1:05» или «1:05-1:25» — отрывок трека задан вручную."""
    rows = (text or "").replace("\r", "\n").split("\n")
    for i, row in enumerate(rows):
        if not row.strip():
            continue
        m = WINDOW_RE.match(row)
        if not m:
            break
        s = int(m.group(1)) * 60 + float(m.group(2).replace(",", "."))
        e = None
        if m.group(3):
            e = int(m.group(3)) * 60 + float(m.group(4).replace(",", "."))
            if e <= s + 1:
                e = None
        return (s, e), "\n".join(rows[:i] + rows[i + 1:])
    return None, text or ""


def guess_language(text):
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    if cyr > lat:
        return "uk" if re.search(r"[іїєґІЇЄҐ]", text) else "ru"
    return ""


def occurrences(n, spans):
    """Где в песне звучит текст: по блокам из n строк -> [{start,end,cov,first,last}]"""
    out = []
    for r in range(len(spans) // n):
        blk = spans[r * n:(r + 1) * n]
        idx = [k for k, s in enumerate(blk) if s]
        if not idx:
            continue
        got = [blk[k] for k in idx]
        out.append({"r": r, "cov": len(idx) / float(n),
                    "start": min(s[0] for s in got), "end": max(s[1] for s in got),
                    "first": idx[0], "last": idx[-1]})
    return out


def pick_chorus(n, spans, max_len, prefer="first", repeats=1):
    """-> [начало, конец, доля найденных строк] для выбранного припева или None.
    repeats>1 — если припев спет несколько раз подряд, взять столько повторов."""
    occ = occurrences(n, spans)
    if not occ:
        return None
    good = [o for o in occ if o["cov"] >= 0.5] or [max(occ, key=lambda o: o["cov"])]
    if prefer == "last":
        o = good[-1]
    elif prefer == "best":
        o = max(good, key=lambda x: x["cov"])
    else:
        o = good[0]
    # нераспознанные крайние строки: дотягиваем окно на их примерную длину
    per_line = (o["end"] - o["start"]) / max(1, o["last"] - o["first"] + 1)
    s = o["start"] - per_line * o["first"]
    e = o["end"] + per_line * (n - 1 - o["last"])
    for nxt in occ[occ.index(o) + 1:occ.index(o) + max(1, int(repeats))]:
        if nxt["cov"] >= 0.5 and nxt["start"] - e <= 2.5 and nxt["end"] - s <= max_len:
            e = nxt["end"] + per_line * (n - 1 - nxt["last"])
        else:
            break
    return [max(0.0, s), e, o["cov"]]


def to_asr_audio(src, dst, start=None, dur=None):
    """16 кГц моно FLAC — компактно и для Whisper, и для Groq."""
    args = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        args += ["-ss", "%.3f" % start]
    if dur is not None:
        args += ["-t", "%.3f" % dur]
    args += ["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", dst]
    run(args)
    if not os.path.exists(dst) or os.path.getsize(dst) < 500:
        raise Fail("ffmpeg не смог подготовить звук для распознавания")
    return dst


def cut_audio(src, dst, start, dur):
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", src, "-vn",
         "-c:a", "pcm_s16le", dst])
    if not os.path.exists(dst):
        raise Fail("ffmpeg не смог вырезать отрывок")
    return dst


def vocal_envelope(path, start, dur, tmpdir, hop=0.01, sr=8000):
    """Громкость (дБ) каждые 10 мс."""
    raw = os.path.join(tmpdir, "env.raw")
    if os.path.exists(raw):
        os.remove(raw)
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "%.3f" % max(0.0, start), "-t", "%.3f" % dur, "-i", path,
         "-ac", "1", "-ar", str(sr), "-f", "s16le", raw])
    if not os.path.exists(raw):
        return []
    a = array.array("h")
    with open(raw, "rb") as f:
        a.frombytes(f.read())
    if sys.byteorder == "big":
        a.byteswap()
    step = int(sr * hop)
    env = []
    for i in range(0, len(a) - step + 1, step):
        fr = a[i:i + step]
        env.append(10 * math.log10(sum(x * x for x in fr) / float(step) + 1.0))
    return env


def phrase_onsets(env, hop=0.01, min_quiet=0.12):
    """Моменты, когда голос вступает после паузы (по дорожке чистого вокала)."""
    if len(env) < 50:
        return []
    srt = sorted(env)
    floor, peak = srt[int(len(srt) * 0.15)], srt[int(len(srt) * 0.97)]
    if peak - floor < 12:
        return []
    thr = floor + 0.45 * (peak - floor)
    low = floor + 0.25 * (peak - floor)
    n = len(env)
    sm = [(env[max(0, i - 1)] + env[i] + env[min(n - 1, i + 1)]) / 3.0 for i in range(n)]
    out, quiet = [], 0
    for i, v in enumerate(sm):
        if v < thr:
            quiet += 1
            continue
        if quiet * hop >= min_quiet:
            j = i
            while j > 0 and sm[j - 1] > low and i - j < 8:
                j -= 1
            out.append(j * hop)
        quiet = 0
    return out


def snap_to_onsets(spans, onsets, offset, back=1.0, fwd=0.45):
    """Сдвигает начало строки к ближайшему вступлению голоса. -> (spans, сколько сдвинуто)
    Вступление не может быть раньше, чем закончилась предыдущая строка."""
    res, prev, moved = [], -1e9, 0
    prev_end = -1e9
    for sp in spans:
        if not sp:
            res.append(sp)
            continue
        s, e = sp
        best = None
        for o in onsets:
            t = o + offset
            d = t - s
            if (-back <= d <= fwd and t > prev + 0.3 and t >= prev_end - 0.25
                    and (best is None or abs(d) < abs(best - s))):
                best = t
        prev_end = e
        if best is not None:
            s = best - 0.03
            e = max(e, s + 0.4)
            moved += 1
        res.append([s, e])
        prev = s
    return res, moved


def _count(spans):
    return sum(1 for s in spans if s)


def _recognize(audio, a_dur, src, c, stt, separate, prompt, hint, tmp):
    """Распознавание: ищет припев, уточняет тайминг на отрывке.
    -> (lines, spans, [начало, конец] пения, описание)"""
    n = len(src)
    max_len = float(c["max_len"])
    vocals_full = None
    seq = sp = words = None
    win = None
    st1 = {}
    if hint:
        s0 = max(0.0, hint[0] - 0.5)
        e0 = min(a_dur, (hint[1] or hint[0] + max_len) + 0.5)
    else:
        log("  ищу припев в треке…")
        try:
            words = stt(to_asr_audio(audio, os.path.join(tmp, "full.flac")), c, prompt)
        except Fail as e:
            note("распознавание трека: %s" % e)
            words = []
        note("услышал (весь трек): %s" % (" ".join(w for w, _, _ in words)[:160] or "—"))
        if words:
            seq, sp = align_lines(src, words, kmax=4, stats=st1)
            win = pick_chorus(n, sp, max_len, c.get("chorus_pick", "first"),
                              c.get("chorus_repeats", 1))
        if (win is None or win[2] < 0.5) and separate:
            log("  плохо слышно — выделяю голос из всего трека…")
            v = separate(audio, os.path.join(tmp, "vocals_full.wav"))
            if v:
                vocals_full = v
                words_v = stt(to_asr_audio(v, os.path.join(tmp, "vfull.flac")),
                              dict(c, _vad=True), prompt)
                if words_v:
                    st_v = {}
                    seq_v, sp_v = align_lines(src, words_v, kmax=4, stats=st_v)
                    w2 = pick_chorus(n, sp_v, max_len, c.get("chorus_pick", "first"),
                                     c.get("chorus_repeats", 1))
                    if w2 and (win is None or w2[2] >= win[2]):
                        win, seq, sp, words = w2, seq_v, sp_v, words_v
        if win is None:
            raise Fail("не нашёл присланный текст в треке")
        log("  припев: %.1f–%.1f c (найдено %d%% строк)" % (win[0], win[1], win[2] * 100))
        s0 = max(0.0, win[0] - 2.0)
        e0 = min(a_dur, win[1] + 2.0)

    # второй проход — только отрывок с припевом, по чистому голосу
    seg_dur = e0 - s0
    voc = None
    if vocals_full:
        voc = cut_audio(vocals_full, os.path.join(tmp, "vseg.wav"), s0, seg_dur)
    elif separate:
        log("  выделяю голос в припеве…")
        seg = cut_audio(audio, os.path.join(tmp, "seg.wav"), s0, seg_dur)
        voc = separate(seg, os.path.join(tmp, "vseg_sep.wav"))
    note("голос: %s" % ("выделен" if voc else "не выделен"))
    kmax2 = 3 if hint else max(1, int(c.get("chorus_repeats", 1)))
    total_words = sum(len(l.split()) for l in src)

    def listen(path, start, vad, label):
        try:
            w = stt(to_asr_audio(path, os.path.join(tmp, "seg16_%s.flac" % label), start,
                                 None if start is None else seg_dur), dict(c, _vad=vad), prompt)
        except Fail as e:
            note("распознавание (%s): %s" % (label, e))
            return []
        w = [(x, s + s0, e + s0) for x, s, e in (w or []) if s <= seg_dur + 0.5]
        note("услышал (%s): %s" % (label, " ".join(x for x, _, _ in w)[:160] or "—"))
        return w

    cand = []
    tries = [(voc, None, True, "голос")] if voc else []
    tries.append((audio, s0, False, "микс"))
    for path, start, vad, label in tries:
        w2 = listen(path, start, vad, label)
        if w2:
            cand.append(_window_lines(n, *align_lines(src, w2, kmax=kmax2), s0=s0, e0=e0))
        if cand and cand[-1][2] >= 0.6 * n and len(w2) >= 0.4 * total_words:
            break                            # голос расслышан хорошо — микс не нужен
    if sp:
        cand.append(_window_lines(n, seq, sp, s0=s0, e0=e0))
    cand = [x for x in cand if x[2] > 0]
    best = max(cand, key=lambda x: x[2]) if cand else None   # при равенстве — первый (по голосу)

    env = vocal_envelope(voc, 0, seg_dur, tmp) if voc else []
    by_voice = None
    if env:
        phr = vocal_phrases(env)
        if hint:
            # припев начинается с указанного времени; хвост предыдущей строки отбрасываем
            phr = [p for p in phr if p[0] >= hint[0] - s0 - 0.3]
            if not hint[1]:
                phr = chorus_phrases(phr, n)
        by_voice = phrases_to_lines(phr, n)
    if by_voice and (best is None or best[2] < max(2, 0.4 * n)):
        # слова почти не расслышаны: ставим строки по фразам — между строками певец берёт дыхание
        lines = list(src)
        spans = [[a + s0, b + s0] for a, b in by_voice]
        desc = "строки по паузам в голосе (слова распознаны %d из %d)" % (
            best[2] if best else 0, n)
    elif best is not None:
        lines, spans, hit = best
        spans = fill_gaps(lines, spans, s0, e0)
        desc = "распознано %d из %d строк" % (hit, len(lines))
        if voc:
            spans, moved = snap_to_onsets(spans, phrase_onsets(env), s0)
            desc += ", голос выделен"
            if moved:
                desc += ", подровнено %d" % moved
    else:
        raise Fail("не нашёл текст в отрывке %s–%s" % (fmt_time(s0), fmt_time(e0)))
    sing = [max(s0 - 0.5, min(s[0] for s in spans)), min(e0 + 0.5, max(s[1] for s in spans))]
    if win:
        sing = [min(sing[0], win[0]), max(sing[1], win[1])]
    return lines, spans, sing, desc


def vocal_phrases(env, hop=0.01, min_len=0.3, min_gap=0.18):
    """Участки, где звучит голос: [[начало, конец], ...] (по дорожке выделенного голоса)."""
    if len(env) < 50:
        return []
    srt = sorted(env)
    floor, peak = srt[int(len(srt) * 0.15)], srt[int(len(srt) * 0.97)]
    if peak - floor < 10:
        return []
    thr = floor + 0.35 * (peak - floor)
    n = len(env)
    sm = [sum(env[max(0, i - 2):i + 3]) / len(env[max(0, i - 2):i + 3]) for i in range(n)]
    regions, start = [], None
    for i, v in enumerate(sm):
        if v >= thr and start is None:
            start = i
        elif v < thr and start is not None:
            regions.append([start * hop, i * hop])
            start = None
    if start is not None:
        regions.append([start * hop, n * hop])
    merged = []
    for r in regions:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= min_len]


def chorus_phrases(phr, n):
    """Сколько фраз от начала относится к припеву: ищем паузу-границу секции
    (после припева обычно проигрыш), не дальше 2n фраз."""
    if len(phr) <= n:
        return phr
    gaps = [phr[i + 1][0] - phr[i][1] for i in range(len(phr) - 1)]
    typical = sorted(gaps[:max(1, n - 1)])[max(0, (n - 1) // 2)] if n > 1 else 0.5
    best_m, best_score = n, None
    for m in range(n, min(len(phr), 2 * n) + 1):
        after = gaps[m - 1] if m - 1 < len(gaps) else 5.0
        score = after - max(typical, 0.3) - 0.6 * (m - n)
        if best_score is None or score > best_score:
            best_m, best_score = m, score
    return phr[:best_m]


def phrases_to_lines(phrases, n):
    """Подгоняет число фраз под число строк: склеивает самые близкие, делит самые длинные."""
    ph = [list(p) for p in phrases]
    if not ph or len(ph) > 3 * n or len(ph) * 3 < n:
        return None
    while len(ph) > n:
        k = min(range(len(ph) - 1), key=lambda i: ph[i + 1][0] - ph[i][1])
        ph[k][1] = ph[k + 1][1]
        del ph[k + 1]
    while len(ph) < n:
        k = max(range(len(ph)), key=lambda i: ph[i][1] - ph[i][0])
        s, e = ph[k]
        m = (s + e) / 2
        ph[k:k + 1] = [[s, m], [m, e]]
    return ph


def _window_lines(n, lines, spans, s0, e0):
    """Оставляет только повторы текста, которые звучат внутри отрывка.
    -> (lines, spans, сколько строк найдено)"""
    keep_l, keep_s = [], []
    for r in range(len(lines) // n):
        blk = spans[r * n:(r + 1) * n]
        inside = [s if (s and s0 - 0.5 <= s[0] <= e0 + 0.5) else None for s in blk]
        # повтор целиком (или в основном) внутри отрывка, а не задет краем
        if _count(inside) and _count(inside) >= 0.5 * _count(blk):
            keep_l += lines[r * n:(r + 1) * n]
            keep_s += inside
    return keep_l, keep_s, _count(keep_s)


def analyze(audio, lyrics_text, cfg, stt=None, separate=None):
    """Тайминг строк и отрывок трека для клипа.
    stt(путь, cfg, prompt) -> [(слово, начало, конец)]; separate(путь, куда) -> путь к голосу|None
    -> {"lines","spans","start","end","mode","source_lines"} (время — от начала трека)"""
    del _NOTES[:]
    a_dur = media_duration(audio)
    if not a_dur:
        raise Fail("Не могу прочитать аудио: %s" % os.path.basename(audio))
    hint, text = split_window_hint(lyrics_text)
    parsed = parse_lyrics(text, cfg["uppercase"])
    if not parsed:
        raise Fail("Нет текста песни — пришли текст припева.")
    src = [l for _, l in parsed]
    max_len = float(cfg["max_len"])
    if not hint and (not cfg.get("auto_chorus", True) or float(cfg.get("audio_start") or 0) > 0):
        a0 = float(cfg.get("audio_start") or 0)
        hint = (a0, min(a_dur, a0 + max_len))
    c = dict(cfg)
    c["language"] = cfg.get("language") or guess_language(" ".join(src))
    prompt = " ".join(src)[:300]
    if stt is None and (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY")):
        stt = transcribe

    lines = spans = sing = None
    mode = ""
    manual = hint is not None
    if any(t is not None for t, _ in parsed):
        lines, spans = explicit_spans(parsed, a_dur)
        sing = [spans[0][0], spans[-1][1]]
        mode = "время из текста"
    elif stt is not None:
        tmp = tempfile.mkdtemp(prefix="clip_an_")
        try:
            lines, spans, sing, mode = _recognize(audio, a_dur, src, c, stt, separate,
                                                  prompt, hint, tmp)
        except Fail as e:
            note("итог: %s" % e)
        except Exception as e:
            traceback.print_exc()
            note("ошибка: %s: %s" % (type(e).__name__, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if spans is None:
        lines = src
        base = hint[0] if hint else 0.0
        end = (hint[1] if hint and hint[1] else min(a_dur, base + max_len))
        spans = even_spans(lines, max(base, float(cfg.get("intro_sec") or 0)), end - 1.0)
        hint = (base, end)
        mode = ("равномерно — текст в треке не найден" if stt is not None
                else "равномерно (без распознавания)")

    pre, post = float(cfg.get("pre_roll", 0.6)), float(cfg.get("post_roll", 1.5))
    if hint:
        start = hint[0]
        end = hint[1] if hint[1] else min(a_dur, sing[1] + post, start + max_len)
        if manual:
            mode += ", отрывок задан вручную"
    else:
        start = max(0.0, sing[0] - pre)
        end = min(a_dur, sing[1] + post)
    if end - start > max_len:
        end = start + max_len
    min_len = float(cfg.get("min_len", 8))
    if end - start < min_len:
        end = min(a_dur, start + min_len)
    if end - start < 3:
        raise Fail("Отрывок слишком короткий (%.1f c)." % (end - start))
    rel = [[s[0] - start, s[1] - start] if s else None for s in spans]
    if not display_plan(lines, rel, end - start):     # страховка: строки мимо отрывка
        log("  ! строки не попали в отрывок — расставляю равномерно")
        lines = src
        spans = even_spans(lines, start + min(pre, 1.0), end - min(post, 1.0))
        mode += " (строки равномерно)"
    return {"lines": lines, "spans": spans, "start": round(start, 2), "end": round(end, 2),
            "mode": mode, "source_lines": src, "notes": list(_NOTES)}


def fmt_time(t):
    return "%d:%02d" % (int(t) // 60, int(t) % 60)


# ----------------------------------------------------------- display plan

def display_plan(lines, spans, clip_len, lead=0.12, hide_gap=2.0):
    """-> [(t_show, t_hide, line_idx)] без перекрытий."""
    items = sorted([(sp[0], sp[1], i) for i, sp in enumerate(spans) if sp],
                   key=lambda x: x[0])
    plan = []
    for k, (st, en, i) in enumerate(items):
        if en is not None and en < 0.6:   # строка почти целиком до начала клипа
            continue
        show = max(0.0, st - lead)
        if k + 1 < len(items):
            nxt = items[k + 1][0] - lead
            hide = nxt
            if en is not None and nxt - en > hide_gap:
                hide = en + 0.5
        else:
            hide = clip_len if en is None else min(clip_len, en + 1.5)
        if plan and show < plan[-1][1]:
            show = plan[-1][1]
        hide = min(hide, clip_len)
        if show >= clip_len - 0.3 or hide - show < 0.25:
            continue
        plan.append((show, hide, i))
    return plan


# --------------------------------------------------------------- renderers

def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class PilRenderer:
    supports_fade = True
    name = "Pillow"

    def __init__(self, cfg, font_path):
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        self.Image, self.ImageDraw, self.ImageFont, self.ImageFilter = \
            Image, ImageDraw, ImageFont, ImageFilter
        self.cfg = cfg
        self.font_path = font_path
        self.W, self.H = int(cfg["width"]), int(cfg["height"])
        self.ImageFont.truetype(font_path, 40)  # упадёт, если нет FreeType
        self._layers = {}
        self._fonts = {}

    def _font(self, size):
        if size not in self._fonts:
            self._fonts[size] = self.ImageFont.truetype(self.font_path, size)
        return self._fonts[size]

    @staticmethod
    def _w(font, s):
        try:
            return font.getlength(s)
        except AttributeError:
            return font.getsize(s)[0]

    def _wrap(self, text, font, maxw):
        out, cur = [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if not cur or self._w(font, t) <= maxw:
                cur = t
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        return out

    def _balanced(self, text, font, maxw):
        """Перенос с выравниванием длины строк: «Мы танцуем / под неоном»."""
        lines = self._wrap(text, font, maxw)
        n = len(lines)
        if n < 2:
            return lines
        lo, hi = self._w(font, text) / n * 0.9, maxw
        for _ in range(14):
            mid = (lo + hi) / 2
            if len(self._wrap(text, font, mid)) <= n:
                hi = mid
            else:
                lo = mid
        return self._wrap(text, font, hi)

    def _layout(self, text):
        maxw = self.W * 0.84
        size = int(self.cfg["font_size"])
        while True:
            font = self._font(size)
            lines = self._balanced(text, font, maxw)
            widest = max(self._w(font, l) for l in lines)
            if (len(lines) <= 3 and widest <= maxw) or size <= 40:
                return font, lines, size
            size = int(size * 0.9)

    def _layer(self, text):
        if text in self._layers:
            return self._layers[text]
        Image, ImageDraw = self.Image, self.ImageDraw
        font, lines, size = self._layout(text)
        asc, desc = font.getmetrics()
        lh = int((asc + desc) * 1.12)
        total = lh * len(lines)
        y0 = (self.H - total) // 2 + int(self.cfg["text_y_offset"])
        sw = max(1, int(int(self.cfg["stroke"]) * size / 84.0))
        fill = hex_rgb(self.cfg["text_color"]) + (255,)
        sfill = hex_rgb(self.cfg["stroke_color"]) + (255,)
        shadow = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        layer = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        ds, dl = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
        for i, l in enumerate(lines):
            x = (self.W - self._w(font, l)) / 2.0
            y = y0 + i * lh
            ds.text((x, y + size * 0.06), l, font=font, fill=(0, 0, 0, 160),
                    stroke_width=sw + 2, stroke_fill=(0, 0, 0, 160))
            dl.text((x, y), l, font=font, fill=fill, stroke_width=sw, stroke_fill=sfill)
        shadow = shadow.filter(self.ImageFilter.GaussianBlur(max(4, size // 8)))
        layer = Image.alpha_composite(shadow, layer)
        self._layers[text] = layer
        return layer

    def render(self, text, alpha, out_path):
        base = self.Image.new("RGBA", (self.W, self.H),
                              (0, 0, 0, int(255 * float(self.cfg["dim"]))))
        if text:
            layer = self._layer(text)
            if alpha < 0.999:
                layer = layer.copy()
                a = layer.getchannel("A").point(lambda v: int(v * alpha))
                layer.putalpha(a)
            base = self.Image.alpha_composite(base, layer)
        base.save(out_path, compress_level=1)


class MagickRenderer:
    """Запасной вариант, если в Python нет Pillow: ImageMagick convert."""
    supports_fade = False
    name = "ImageMagick"

    def __init__(self, cfg, font_path):
        self.cfg, self.font_path = cfg, font_path
        self.W, self.H = int(cfg["width"]), int(cfg["height"])
        self.bin = "magick" if have_cmd("magick") else "convert"
        test = os.path.join(WORK, "_im_test.png")
        code, out, err = run([self.bin, "-size", "200x80", "xc:none", "-font", font_path,
                              "-pointsize", "40", "-fill", "white", "-annotate", "+10+50",
                              "Тест", test])
        if not os.path.exists(test):
            raise RuntimeError("ImageMagick не рисует текст: %s" % (err or out)[:200])

    def _wrap(self, text, size):
        maxchars = max(6, int(self.W * 0.84 / (size * 0.6)))
        out, cur = [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if not cur or len(t) <= maxchars:
                cur = t
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        return out

    def render(self, text, alpha, out_path):
        dim = float(self.cfg["dim"])
        args = [self.bin, "-size", "%dx%d" % (self.W, self.H), "xc:rgba(0,0,0,%.3f)" % dim]
        if text:
            size = int(self.cfg["font_size"])
            lines = self._wrap(text, size)
            while len(lines) > 3 and size > 40:
                size = int(size * 0.9)
                lines = self._wrap(text, size)
            # без @файла (часто запрещён политикой ImageMagick); \n — перенос строки
            txt = "\\n".join(l.replace("\\", "").replace("%", "%%") for l in lines)
            sw = max(1, int(int(self.cfg["stroke"]) * size / 84.0))
            dy = int(self.cfg["text_y_offset"])
            off = "+0%+d" % dy
            args += ["-font", self.font_path, "-pointsize", str(size), "-gravity", "center",
                     "-interline-spacing", str(int(size * 0.15)),
                     "-stroke", self.cfg["stroke_color"], "-strokewidth", str(sw * 2),
                     "-fill", self.cfg["stroke_color"], "-annotate", off, txt,
                     "-stroke", "none", "-fill", self.cfg["text_color"],
                     "-annotate", off, txt]
        args.append("PNG32:" + out_path)
        code, out, err = run(args)
        if not os.path.exists(out_path):
            raise Fail("ImageMagick не создал кадр: %s" % (err or out)[:300])


def ensure_font(cfg):
    path = cfg["font"]
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        return path
    for f in sorted(os.listdir(HERE)):
        if f.lower().endswith((".ttf", ".otf")):
            return os.path.join(HERE, f)
    log("  скачиваю шрифт Montserrat…")
    try:
        req = urllib.request.Request(FONT_URL, headers={"User-Agent": "clip.py"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if len(data) < 10000:
            raise Fail("скачался не шрифт")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        raise Fail("Нет шрифта. Положи любой .ttf в папку mood (ошибка: %s)" % e)


def make_renderer(cfg):
    font = ensure_font(cfg)
    errors = []
    if not os.environ.get("CLIP_FORCE_MAGICK"):
        try:
            return PilRenderer(cfg, font)
        except Exception as e:
            errors.append("Pillow: %s" % e)
    try:
        return MagickRenderer(cfg, font)
    except Exception as e:
        errors.append("ImageMagick: %s" % e)
    raise Fail("Не могу нарисовать текст. " + " | ".join(errors))


# ------------------------------------------------------------ overlay track

def build_overlay(renderer, lines, plan, clip_len, fade, frames_dir):
    os.makedirs(frames_dir, exist_ok=True)
    cache = {}

    def frame(text, alpha):
        key = (text, round(alpha, 2))
        if key not in cache:
            p = os.path.join(frames_dir, "f%04d.png" % len(cache))
            renderer.render(text, alpha, p)
            cache[key] = p
        return cache[key]

    entries = []
    cur = 0.0
    FD = 0.05
    fade = fade and renderer.supports_fade
    for show, hide, i in plan:
        if show > cur + 0.001:
            entries.append((frame(None, 1), show - cur))
        dur = hide - show
        text = lines[i]
        if fade and dur > 0.7:
            for a in (0.3, 0.6, 0.85):
                entries.append((frame(text, a), FD))
            entries.append((frame(text, 1), dur - 5 * FD))
            for a in (0.6, 0.25):
                entries.append((frame(text, a), FD))
        else:
            entries.append((frame(text, 1), dur))
        cur = hide
    if cur < clip_len:
        entries.append((frame(None, 1), clip_len - cur + 0.5))
    lst = os.path.join(frames_dir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p, d in entries:
            f.write("file '%s'\nduration %.3f\n" % (p, max(d, 0.001)))
        f.write("file '%s'\n" % entries[-1][0])
    return lst, len(cache)


# --------------------------------------------------------------------- core

def newest(folder, exts, exclude=()):
    if not folder or not os.path.isdir(folder):
        return None
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith(exts) and f not in exclude and not f.startswith(".")]
    files = [f for f in files if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


def random_bg():
    if not os.path.isdir(BG_DIR):
        return None
    files = [os.path.join(BG_DIR, f) for f in os.listdir(BG_DIR)
             if f.lower().endswith(VIDEO_EXT)]
    return random.choice(files) if files else None


def safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE)[:80] or "track"


def make_clip(cfg, audio, video, lyrics_text, out_path, track_key=None, stt=None,
              timing=None, separate=None):
    """timing — готовый результат analyze() (бот считает его один раз на трек).
    Иначе считается здесь: stt — распознавание (по умолчанию Groq, если есть ключ)."""
    t_start = time.time()
    W, H, FPS = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    a_ext = os.path.splitext(audio)[1].lower()
    v_ext = os.path.splitext(video)[1].lower()
    a_local = os.path.join(WORK, "audio" + a_ext)
    v_local = os.path.join(WORK, "bg" + v_ext)
    shutil.copyfile(audio, a_local)
    shutil.copyfile(video, v_local)

    log("1/5 Читаю файлы…")
    v_dur = media_duration(v_local) or 0

    # --- текст, тайминг и отрывок трека
    log("2/5 Тайминг строк…")
    if timing is None:
        timing = timing_with_cache(cfg, a_local, lyrics_text, track_key, stt, separate)
    lines, spans, mode = timing["lines"], timing["spans"], timing["mode"]
    src_lines = timing["source_lines"]
    a_start = float(timing["start"])
    clip_len = round(float(timing["end"]) - a_start, 2)
    log("  режим: %s; отрывок %s–%s" % (mode, fmt_time(a_start), fmt_time(a_start + clip_len)))

    rel = [[s[0] - a_start, s[1] - a_start] if s else None for s in spans]
    plan = display_plan(lines, rel, clip_len)
    if not plan:
        raise Fail("Ни одна строка не попала в клип — проверь audio_start/max_len.")

    # --- кадры с текстом
    log("3/5 Рисую текст…")
    renderer = make_renderer(cfg)
    lst, nframes = build_overlay(renderer, lines, plan, clip_len, cfg["fade"],
                                 os.path.join(WORK, "frames"))
    log("  %d кадров (%s)" % (nframes, renderer.name))

    # --- монтаж
    enc_name, enc_args = pick_encoder(cfg)
    log("4/5 Монтирую (%s, %.1f c)…" % (enc_name, clip_len))
    if v_dur and v_dur > clip_len + 1:
        v_off = round(random.uniform(0, v_dur - clip_len - 0.5), 2)
    else:
        v_off = 0
    fo = max(0.5, min(1.5, clip_len / 10))
    fc = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
          "setsar=1,fps=%d[bg];"
          "[bg][1:v]overlay=0:0,format=yuv420p[v];"   # format=auto втрое медленнее
          "[2:a]afade=t=in:st=0:d=0.15,afade=t=out:st=%.2f:d=%.2f[a]"
          % (W, H, W, H, FPS, clip_len - fo, fo))
    tmp_out = os.path.join(WORK, "out.mp4")
    args = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-ss", str(v_off), "-i", v_local,
            "-f", "concat", "-safe", "0", "-i", lst,
            "-ss", str(a_start), "-t", str(clip_len), "-i", a_local,
            "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
            "-t", str(clip_len), "-r", str(FPS)] + enc_args + \
           ["-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart", tmp_out]
    code, out, err = run(args)
    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 10000:
        raise Fail("ffmpeg не собрал видео:\n%s" % (err or out)[-800:])

    log("5/5 Готово")
    if os.path.exists(out_path):
        os.remove(out_path)
    shutil.move(tmp_out, out_path)
    return {"lines": lines, "hook": pick_hook(src_lines), "mode": mode,
            "start": a_start, "end": a_start + clip_len,
            "seconds": round(time.time() - t_start, 1), "encoder": enc_name}


def timing_with_cache(cfg, audio, lyrics_text, track_key, stt=None, separate=None):
    """Для айфона: повторный клип того же трека не распознаёт заново.
    Если текст не прислан — берёт прошлый текст этого трека."""
    cache_path = os.path.join(TRACKS, safe_name(track_key or "x") + ".json")
    cached = None
    if track_key and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = None
    if cached and "start" not in cached:
        cached = None                      # старый формат кэша
    text = lyrics_text or ""
    if len(parse_lyrics(split_window_hint(text)[1])) < 2 and cached:
        text = cached.get("lyrics_text", "")
        log("  текст взят из прошлого раза для этого трека")
    if cached and cached.get("lyrics_text") == text and cached.get("cfg_key") == _cfg_key(cfg):
        t = dict(cached)
        t["mode"] = t["mode"] + " (сохранено)"
        return t
    t = analyze(audio, text, cfg, stt=stt, separate=separate)
    if track_key and t["mode"].startswith("распознавание"):
        os.makedirs(TRACKS, exist_ok=True)
        d = dict(t, lyrics_text=text, cfg_key=_cfg_key(cfg))
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    return t


def _cfg_key(cfg):
    return [cfg.get(k) for k in ("max_len", "audio_start", "pre_roll", "post_roll",
                                 "uppercase", "auto_chorus", "chorus_pick", "chorus_repeats",
                                 "min_len")]


# --------------------------------------------------------------- commands

def find_inbox(arg):
    if arg:
        return arg
    sc = os.environ.get("SHORTCUTS")
    if sc and os.path.isdir(sc):
        return sc
    return os.getcwd()


def read_lyrics(inbox, explicit_path):
    path = explicit_path
    if not path:
        cand = [os.path.join(inbox, f) for f in os.listdir(inbox)
                if f.lower().endswith(".txt") and f not in ("caption.txt",)
                and not f.startswith("_")]
        cand = [c for c in cand if os.path.isfile(c)]
        path = max(cand, key=os.path.getmtime) if cand else None
    if path:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), path
    code, out, err = run(["pbpaste"])  # буфер обмена a-Shell
    if code == 0 and out.strip():
        return out, None
    return "", None


def cmd_make(cfg, a):
    inbox = find_inbox(a.get("inbox"))
    out_path = a.get("out") or os.path.join(inbox, "clip.mp4")
    cap_path = os.path.join(os.path.dirname(out_path), "caption.txt")
    for p in (out_path, cap_path):
        if os.path.exists(p):
            os.remove(p)
    audio = a.get("audio") or newest(inbox, AUDIO_EXT)
    if not audio:
        raise Fail("Не нашёл трек (mp3/m4a/wav) в %s" % inbox)
    video = a.get("video") or newest(inbox, VIDEO_EXT, exclude=("clip.mp4", "demo.mp4")) \
        or random_bg()
    if not video:
        raise Fail("Не нашёл видео для фона (ни от Команды, ни в папке mood/bg)")
    lyrics, lyrics_path = read_lyrics(inbox, a.get("lyrics"))
    key = os.path.splitext(os.path.basename(audio))[0]
    log("Трек: %s\nФон:  %s" % (os.path.basename(audio), os.path.basename(video)))
    res = make_clip(cfg, audio, video, lyrics, out_path, track_key=key)
    caption = res["hook"]
    if cfg.get("hashtags"):
        caption = caption + " " + cfg["hashtags"].strip()
    with open(cap_path, "w", encoding="utf-8") as f:
        f.write(caption)
    if cfg.get("delete_inputs") and not a.get("keep"):
        for p in (audio, video, lyrics_path):
            if p and os.path.dirname(os.path.abspath(p)) == os.path.abspath(inbox):
                try:
                    os.remove(p)
                except OSError:
                    pass
    log("Клип: %s (%s c, %s)\nПодпись: %s" % (out_path, res["seconds"], res["mode"], caption))


def cmd_demo(cfg):
    os.makedirs(WORK, exist_ok=True)
    demo_dir = os.path.join(HERE, "demo_src")
    os.makedirs(demo_dir, exist_ok=True)
    a = os.path.join(demo_dir, "demo_audio.m4a")
    v = os.path.join(demo_dir, "demo_bg.mp4")
    enc_name, enc_args = pick_encoder(cfg)
    log("Делаю тестовые звук и видео…")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=330:duration=12", "-c:a", "aac", a])
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=1280x720:rate=30:duration=12"] + enc_args +
        ["-pix_fmt", "yuv420p", v])
    if not (os.path.exists(a) and os.path.exists(v)):
        raise Fail("ffmpeg не смог создать тестовые файлы (нет lavfi?). "
                   "Проверь на своих файлах.")
    lyrics = "[Chorus]\nЭто тестовый клип\nСтроки меняются сами\nПод музыку, по центру\nВсё работает"
    out = os.path.join(HERE, "demo.mp4")
    res = make_clip(cfg, a, v, lyrics, out)
    log("Тестовый клип: %s (%s c, кодек %s). Открой его в Файлах → a-Shell → mood."
        % (out, res["seconds"], res["encoder"]))


def cmd_check(cfg, a):
    ok = True
    log("Python %s" % sys.version.split()[0])
    log("Папка скрипта: %s" % HERE)
    log("Папка Команд:  %s" % find_inbox(a.get("inbox")))
    code, out, err = run_expect(["ffmpeg", "-hide_banner", "-version"], "ffmpeg")
    first = (out + err).strip().splitlines()[:1]
    if code == 127 or not first or "version" not in first[0]:
        log("✗ ffmpeg не найден")
        ok = False
    else:
        log("✓ %s" % first[0][:60])
        enc, _ = pick_encoder(cfg)
        log(("✓" if enc != "mpeg4" else "! ") + " видеокодек: %s" % enc)
        if enc == "mpeg4":
            log("  (нет H.264 — TikTok может не принять файл, напиши мне)")
        code, out, err = run_expect(["ffmpeg", "-hide_banner", "-filters"], "overlay")
        f = out + err
        missing = [x for x in ("overlay", "scale", "crop", "afade", "fps") if (" %s " % x) not in f]
        log("✓ фильтры на месте" if not missing else "✗ нет фильтров: %s" % ", ".join(missing))
        ok = ok and not missing
    log("Субпроцессы: %s" % ("subprocess" if _SUBPROCESS_OK else "os.system"))
    try:
        r = make_renderer(cfg)
        os.makedirs(WORK, exist_ok=True)
        p = os.path.join(WORK, "_check.png")
        r.render("Проверка ЁЖ abc", 1, p)
        log("✓ текст рисуется (%s), шрифт: %s" % (r.name, os.path.basename(r.font_path)))
    except Exception as e:
        log("✗ текст: %s" % e)
        ok = False
    key = cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY")
    log("✓ ключ Groq есть — строки по распознаванию" if key else
        "! ключа Groq нет — строки будут ставиться равномерно (приблизительно)")
    log("\nВСЁ ГОТОВО" if ok else "\nЕсть проблемы — пришли этот вывод в чат")


def parse_args(argv):
    a = {}
    i = 0
    while i < len(argv):
        x = argv[i]
        if x in ("--check", "--demo", "--keep"):
            a[x[2:]] = True
        elif x in ("--set", "--inbox", "--audio", "--video", "--lyrics", "--out"):
            if i + 1 >= len(argv):
                raise Fail("После %s нужно значение" % x)
            if x == "--set":
                a.setdefault("set", []).append(argv[i + 1])
            else:
                a[x[2:]] = argv[i + 1]
            i += 1
        elif x in ("-h", "--help"):
            a["help"] = True
        else:
            raise Fail("Не понял аргумент: %s (см. python3 clip.py --help)" % x)
        i += 1
    return a


def main(argv):
    try:
        a = parse_args(argv)
        if a.get("help"):
            log(__doc__)
            return 0
        cfg = load_config()
        if a.get("set"):
            for pair in a["set"]:
                set_option(cfg, pair)
            return 0
        if a.get("check"):
            cmd_check(cfg, a)
        elif a.get("demo"):
            cmd_demo(cfg)
        else:
            cmd_make(cfg, a)
        return 0
    except Fail as e:
        log("\n✗ %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
