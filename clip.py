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
import random
import shutil
import shlex
import uuid
import difflib
import time
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
    "audio_start": 0,        # с какой секунды трека начинать
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
                s = 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()
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


def align_lines(lines, words):
    """lines: [str], words: [(word,start,end)] -> (lines_out, [(start,end)|None])"""
    tw = [(norm_word(w), s, e) for w, s, e in words]
    tw = [x for x in tw if x[0]]
    B = [x[0] for x in tw]
    simcache = {}
    best = None
    for k in (1, 2, 3):
        seq_lines = lines * k
        A, owner = [], []
        for li, l in enumerate(seq_lines):
            for t in l.split():
                nw = norm_word(t)
                if nw:
                    A.append(nw)
                    owner.append(li)
        if not A or not B:
            return lines, [None] * len(lines)
        score, pairs = _dp_align(A, B, simcache)
        if best is None or score > best[0] + 1.0:
            best = (score, pairs, seq_lines, owner)
    score, pairs, seq_lines, owner = best
    spans = [None] * len(seq_lines)
    for ai, bj, s in pairs:
        if s < 0.6:
            continue
        li = owner[ai]
        st, en = tw[bj][1], tw[bj][2]
        if spans[li] is None:
            spans[li] = [st, en]
        else:
            spans[li][0] = min(spans[li][0], st)
            spans[li][1] = max(spans[li][1], en)
    # монотонность: строка не может начаться раньше предыдущей
    last = -1.0
    for i, sp in enumerate(spans):
        if sp is None:
            continue
        if sp[0] < last - 0.05:
            spans[i] = None
        else:
            last = sp[0]
    return seq_lines, spans


def fill_gaps(lines, spans, t0, t1):
    """Строки без времени распределяем между соседями пропорционально длине."""
    n = len(lines)
    spans = [list(s) if s else None for s in spans]
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
    return lines, spans


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


def make_clip(cfg, audio, video, lyrics_text, out_path, track_key=None, stt=None):
    """stt: функция (audio_path, cfg, prompt) -> [(слово, начало, конец)];
    по умолчанию — Groq, если задан ключ."""
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
    a_dur = media_duration(a_local)
    if not a_dur:
        raise Fail("Не могу прочитать аудио: %s" % os.path.basename(audio))
    v_dur = media_duration(v_local) or 0
    a_start = float(cfg["audio_start"])
    clip_len = round(min(a_dur - a_start, float(cfg["max_len"])), 2)
    if clip_len < 3:
        raise Fail("Трек слишком короткий (или audio_start больше длины).")

    # --- текст и тайминг
    log("2/5 Тайминг строк…")
    cache_path = os.path.join(TRACKS, safe_name(track_key or "x") + ".json")
    cached = None
    if track_key and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = None
    parsed = parse_lyrics(lyrics_text or "", cfg["uppercase"])
    if len(parsed) < 2 and cached:
        parsed = [(None, l) for l in cached["source_lines"]]
        log("  текст взят из прошлого раза для этого трека")
    if not parsed:
        raise Fail("Нет текста песни. Скопируй текст припева перед запуском.")
    src_lines = [l for _, l in parsed]

    mode = None
    if any(t is not None for t, _ in parsed):
        lines, spans = explicit_spans(parsed, a_dur)
        mode = "время из текста"
    elif cached and cached.get("source_lines") == src_lines and cached.get("spans"):
        lines, spans = cached["lines"], cached["spans"]
        mode = cached.get("mode", "кэш") + " (сохранено)"
    else:
        lines, spans = None, None
        if stt is None and (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY")):
            stt = transcribe
        if stt is not None:
            log("  распознаю пение через Whisper…")
            try:
                prompt = " ".join(src_lines)[:300]
                words = stt(a_local, cfg, prompt)
                if words:
                    lines, spans = align_lines(src_lines, words)
                    hit = sum(1 for s in spans if s)
                    if hit == 0:
                        lines = spans = None
                        log("  ! не нашёл строки в распознанном — ставлю равномерно")
                    else:
                        last_word = max(e for _, _, e in words)
                        spans = fill_gaps(lines, spans, words[0][1], last_word)
                        mode = "распознавание (%d из %d строк)" % (hit, len(lines))
            except Fail as e:
                log("  ! %s — ставлю строки равномерно" % e)
        if spans is None:
            lines = src_lines
            t0 = max(float(cfg["intro_sec"]), a_start)
            spans = even_spans(lines, t0, a_start + clip_len - 1.0)
            mode = "равномерно"
        if track_key and not mode.startswith("равномерно"):
            os.makedirs(TRACKS, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"source_lines": src_lines, "lines": lines, "spans": spans,
                           "mode": mode}, f, ensure_ascii=False)
    log("  режим: %s" % mode)

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
            "seconds": round(time.time() - t_start, 1), "encoder": enc_name}


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
