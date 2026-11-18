#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plan.py — расписание клипов.

  • Instagram: каждый клип встаёт на ближайший свободный слот (instagram_times)
    и выходит сам через Zernio.
  • TikTok: клипы раскладываются по вечерам (tiktok_per_day в день). В tiktok_time
    бот присылает в Telegram готовые видео с подписью — остаётся выложить.
    tiktok_mode = "inbox" — вместо этого ролик заранее приходит черновиком
    во «Входящие» TikTok, а в Telegram — только подписи.

Недельный режим (weekly): треки из Telegram встают в очередь на ближайший день
(не раньше week_start и завтрашнего). tracks_per_day — лимит треков в день,
0 — без лимита: все треки встают на ближайший день. Клипы для дня собираются
накануне в build_time, каждый трек даёт clips_per_track клипов. Если клипов за день
больше, чем instagram_times, они расходятся поровну между первым и последним
временем. TikTok держит во «Входящих» не больше tiktok_inbox_max черновиков —
остальные ролики приходят видео в Telegram.

Очередь хранится в queue.json рядом с ботом (его сохраняет workflow).
"""

import os
import json
import datetime as dt
from collections import Counter
from zoneinfo import ZoneInfo

import publish

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(HERE, "queue.json")
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ------------------------------------------------------------- очередь

def load():
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            q = json.load(f)
    except Exception:
        q = {}
    if not isinstance(q, dict):
        q = {}
    q.setdefault("items", [])
    return q


def save(q, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    keep = []
    for it in q["items"]:
        ig_left = it.get("instagram_at") and dt.datetime.fromisoformat(it["instagram_at"]) > now - dt.timedelta(days=1)
        tt_left = it.get("tiktok_day") and not it.get("tiktok_done")
        if ig_left or tt_left:
            keep.append(it)
    q["items"] = keep
    week_ago = (now - dt.timedelta(days=8)).date().isoformat()
    q["tracks"] = [t for t in q.get("tracks", []) if t.get("status") == "waiting" or t.get("day", "") >= week_ago]
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------- время

def _tz(name, default):
    try:
        return ZoneInfo(name or default)
    except Exception:
        return ZoneInfo(default)


def my_tz(cfg):
    return _tz(cfg.get("timezone"), "Asia/Krasnoyarsk")


def ig_tz(cfg):
    return _tz(cfg.get("instagram_timezone"), "Europe/Moscow")


def hm(s, default="21:00"):
    try:
        h, m = str(s or default).strip().split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return hm(default)


def tz_label(cfg):
    name = cfg.get("instagram_timezone") or "Europe/Moscow"
    return "МСК" if name == "Europe/Moscow" else name


def day_label(d):
    return "%s %s" % (DAYS[d.weekday()], d.strftime("%d.%m"))


def tiktok_mode(cfg):
    return "inbox" if str(cfg.get("tiktok_mode", "telegram")).lower() == "inbox" else "telegram"


# ------------------------------------------------------------- слоты

def next_instagram(q, cfg, now, start=None):
    """Ближайшее свободное время для Instagram (не раньше start) или None, если Instagram выключен."""
    times = sorted({hm(t) for t in (cfg.get("instagram_times") or [])})
    if not times:
        return None
    tz = ig_tz(cfg)
    lead = dt.timedelta(minutes=int(cfg.get("min_lead_minutes", 90)))
    taken = set()
    for it in q["items"]:
        if it.get("instagram_at"):
            taken.add(dt.datetime.fromisoformat(it["instagram_at"]))
    floor = max(now + lead, start) if start else now + lead
    base = floor.astimezone(tz).date()
    for d in range(0, 120):
        day = base + dt.timedelta(days=d)
        for h, m in times:
            cand = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if cand < floor or cand in taken:
                continue
            return cand
    return None


def ig_grid(cfg, day, n=0):
    """Времена Instagram на день. Клипов больше, чем instagram_times, — n точек поровну
    между первым и последним временем (9:00–21:00 и 15 клипов → каждые 51 мин)."""
    times = sorted({hm(t) for t in (cfg.get("instagram_times") or [])})
    if not times:
        return []
    mins = [h * 60 + m for h, m in times]
    if n > len(mins):
        a, b = mins[0], mins[-1]
        if b - a < 60:
            b = min(a + 12 * 60, 23 * 60 + 59)
        mins = [a + round(i * (b - a) / (n - 1)) for i in range(n)]
    tz = ig_tz(cfg)
    return [dt.datetime(day.year, day.month, day.day, x // 60, x % 60, tzinfo=tz) for x in mins]


def instagram_slot(q, cfg, now, day, slot, total=0):
    """Слот Instagram для недельного клипа: slot-й по счёту в этот день (сетка на total клипов),
    занят — следующий свободный в этот же день, иначе ближайший на следующих днях."""
    grid = ig_grid(cfg, day, total or 0)
    if not grid:
        return None
    lead = dt.timedelta(minutes=int(cfg.get("min_lead_minutes", 90)))
    taken = [dt.datetime.fromisoformat(it["instagram_at"]) for it in q["items"] if it.get("instagram_at")]
    step = min((b - a for a, b in zip(grid, grid[1:])), default=dt.timedelta(hours=1))
    gap = min(dt.timedelta(minutes=10), step / 2)
    if slot is not None and slot < len(grid):
        order = grid[slot:] + grid[:slot][::-1]
    else:
        order = grid
    for cand in order:
        if cand >= now + lead and all(abs(cand - t) >= gap for t in taken):
            return cand
    return next_instagram(q, cfg, now, start=grid[-1] + dt.timedelta(minutes=1))


def next_tiktok_day(q, cfg, now):
    """Вечер, на который встаёт клип для TikTok (date) или None, если TikTok выключен."""
    per = int(cfg.get("tiktok_per_day", 3))
    if per <= 0:
        return None
    tz = my_tz(cfg)
    h, m = hm(cfg.get("tiktok_time"))
    local = now.astimezone(tz)
    start = local.date()
    remind_at = dt.datetime(start.year, start.month, start.day, h, m, tzinfo=tz)
    if local > remind_at - dt.timedelta(minutes=30) or q.get("reminded") == start.isoformat():
        start += dt.timedelta(days=1)
    counts = Counter(it["tiktok_day"] for it in q["items"]
                     if it.get("tiktok_day") and not it.get("tiktok_done"))
    for d in range(0, 120):
        day = start + dt.timedelta(days=d)
        if counts[day.isoformat()] < per:
            return day
    return None


# ------------------------------------------------------------- подписи

def caption(hook, cfg, net):
    hook = " ".join((hook or "").split())
    if hook:
        hook = hook[0].upper() + hook[1:]
    emoji = (cfg.get("caption_emoji") or "").strip()
    first = (hook + (" " + emoji if emoji else "")).strip()
    if net == "instagram":
        tags = (cfg.get("hashtags_instagram") or cfg.get("hashtags") or "").strip()
        return first + ("\n\n" + tags if tags else "")
    tags = (cfg.get("hashtags_tiktok") or cfg.get("hashtags") or "").strip()
    return (first + (" " + tags if tags else "")).strip()


# ------------------------------------------------------------- постановка в план

def add_clip(q, cfg, now, *, cid, hook, path, file_id, msg_id, chat, only=None, day=None, slot=None,
             total=0, tracks=0):
    """Ставит клип в расписание. path — файл клипа (нужен для Instagram и черновиков TikTok).
    Недельный клип: day — его день, slot — номер в сетке Instagram на total клипов,
    tracks — сколько треков в этом дне (черновики TikTok делятся между ними).
    Возвращает (запись, список проблем)."""
    problems = []
    if find(q, cid):
        return find(q, cid), problems
    item = {"id": cid, "hook": hook, "file_id": file_id, "msg_id": msg_id,
            "created": now.isoformat()}
    q["chat"] = chat
    want_ig = (not only or "instagram" in only) and cfg.get("instagram_times")
    want_tt = (not only or "tiktok" in only) and int(cfg.get("tiktok_per_day", 3)) > 0
    mode = tiktok_mode(cfg)

    media = None
    if (want_ig or (want_tt and mode == "inbox")) and publish.enabled():
        try:
            media = publish.upload(path)
        except publish.PublishError as e:
            problems.append("видео не загрузилось в Zernio: %s" % e)

    if want_ig:
        at = instagram_slot(q, cfg, now, day, slot, total) if day else next_instagram(q, cfg, now)
        if not publish.enabled():
            problems.append("Instagram: не задан секрет ZERNIO_API_KEY")
        elif media and at:
            try:
                item["instagram_post"] = publish.schedule(
                    "instagram", media, caption(hook, cfg, "instagram"), at, cfg, key="ig-" + cid)
                item["instagram_at"] = at.isoformat()
            except publish.PublishError as e:
                problems.append("Instagram: %s" % e)

    if want_tt:
        day = day or next_tiktok_day(q, cfg, now)
        if day:
            item["tiktok_day"] = day.isoformat()
            cap = int(cfg.get("tiktok_inbox_max", 5))
            same_day = [it for it in q["items"] if it.get("tiktok_day") == item["tiktok_day"] and it.get("tiktok_post")]
            room = len(same_day) < cap
            if tracks:
                # недельный трек: черновики делим между треками дня поровну (5 на 3 трека → 2, 2, 1)
                group = cid.rsplit("-", 1)[0] + "-"
                mine = sum(1 for it in same_day if it.get("id", "").startswith(group))
                room = room and mine < -(-cap // tracks)
            if mode == "inbox" and room:
                tz = my_tz(cfg)
                h, m = hm(cfg.get("tiktok_time"))
                at = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz) - dt.timedelta(minutes=20)
                if media:
                    try:
                        item["tiktok_post"] = publish.schedule(
                            "tiktok", media, caption(hook, cfg, "tiktok"), at, cfg, draft=True, key="tt-" + cid)
                    except publish.PublishError as e:
                        problems.append("TikTok-черновик: %s — пришлю видео в Telegram" % e)
    if item.get("instagram_at") or item.get("tiktok_day"):
        q["items"].append(item)
    return item, problems


def find(q, cid):
    for it in q["items"]:
        if it.get("id") == cid:
            return it
    return None


def describe(item, cfg):
    """«Instagram — ср 18.11 в 19:00 МСК · TikTok — ср 18.11, напомню в 21:00»"""
    parts = []
    if item.get("instagram_at"):
        at = dt.datetime.fromisoformat(item["instagram_at"]).astimezone(ig_tz(cfg))
        parts.append("Instagram — %s в %s %s" % (day_label(at.date()), at.strftime("%H:%M"), tz_label(cfg)))
    if item.get("tiktok_day"):
        d = dt.date.fromisoformat(item["tiktok_day"])
        parts.append("TikTok — %s, напомню в %s" % (day_label(d), "%02d:%02d" % hm(cfg.get("tiktok_time"))))
    return " · ".join(parts) or "не в расписании"


def cancel(q, msg_id):
    """Убирает из плана клипы, привязанные к сообщению. -> (сколько убрал, заметки)"""
    return cancel_items(q, [it for it in q["items"] if msg_id in (it.get("msg_id"), it.get("plan_msg_id"))])


def cancel_items(q, hit):
    """Снимает посты в Zernio и убирает клипы из плана. -> (сколько убрал, заметки)"""
    notes = []
    for it in hit:
        if it.get("instagram_post"):
            st = publish.status(it["instagram_post"]) if publish.enabled() else None
            if st in ("published", "publishing", "partial"):
                notes.append("«%s» в Instagram уже вышел — удалить можно только в приложении." % it.get("hook", ""))
            elif publish.enabled() and not publish.delete(it["instagram_post"]):
                notes.append("«%s»: Instagram не снялся, проверь в Zernio." % it.get("hook", ""))
        if it.get("tiktok_post") and publish.enabled():
            publish.delete(it["tiktok_post"])
    ids = {id(it) for it in hit}
    q["items"] = [it for it in q["items"] if id(it) not in ids]
    return len(hit), notes


def overview(q, cfg, now):
    """Текст для /plan."""
    ig = sorted((it for it in q["items"] if it.get("instagram_at")
                 and dt.datetime.fromisoformat(it["instagram_at"]) > now),
                key=lambda it: it["instagram_at"])
    tt = {}
    for it in q["items"]:
        if it.get("tiktok_day") and not it.get("tiktok_done"):
            tt.setdefault(it["tiktok_day"], []).append(it)
    if not ig and not tt:
        return "🗓 Расписание пустое. Пришли трек — клипы встанут в план сами."
    lines = ["🗓 Расписание"]
    if ig:
        lines.append("\nInstagram (выйдет сам):")
        for it in ig:
            at = dt.datetime.fromisoformat(it["instagram_at"]).astimezone(ig_tz(cfg))
            lines.append("• %s %s — %s" % (day_label(at.date()), at.strftime("%H:%M"), it.get("hook", "")))
        lines.append("Время — %s." % tz_label(cfg))
    if tt:
        lines.append("\nTikTok (напомню в %02d:%02d):" % hm(cfg.get("tiktok_time")))
        for day in sorted(tt):
            hooks = ", ".join("«%s»" % it.get("hook", "") for it in tt[day])
            lines.append("• %s — %d: %s" % (day_label(dt.date.fromisoformat(day)), len(tt[day]), hooks))
    lines.append("\nУбрать клип — ответь на него словом: отмена")
    return "\n".join(lines)


# ------------------------------------------------------------- напоминание

def due(q, cfg, now):
    """Пора ли вечернее напоминание. -> (сегодня, клипы) или None."""
    tz = my_tz(cfg)
    local = now.astimezone(tz)
    today = local.date().isoformat()
    if local.time() < dt.time(*hm(cfg.get("tiktok_time"))):
        return None
    if q.get("reminded") == today:
        return None
    items = [it for it in q["items"]
             if it.get("tiktok_day") and it["tiktok_day"] <= today and not it.get("tiktok_done")]
    return today, items


def instagram_since(q, now):
    """Клипы Instagram, которые по плану вышли после прошлого напоминания (или за сутки)."""
    try:
        prev = dt.datetime.fromisoformat(q["reminded_at"])
    except Exception:
        prev = now - dt.timedelta(days=1)
    out = []
    for it in q["items"]:
        if it.get("instagram_at"):
            at = dt.datetime.fromisoformat(it["instagram_at"])
            if prev < at <= now:
                out.append(it)
    return out


# ------------------------------------------------------------- недельный режим: треки

def weekly(cfg):
    return bool(cfg.get("weekly", True))


def day_cap(cfg):
    """Лимит треков в день; 0 — без лимита."""
    try:
        return max(0, int(cfg.get("tracks_per_day") or 0))
    except Exception:
        return 0


def week_start(cfg, now):
    today = now.astimezone(my_tz(cfg)).date()
    try:
        return max(dt.date.fromisoformat(str(cfg.get("week_start"))), today + dt.timedelta(days=1))
    except Exception:
        return today + dt.timedelta(days=1)


def build_at(cfg, day):
    """Когда собирать клипы для дня: накануне в build_time."""
    prev = day - dt.timedelta(days=1)
    h, m = hm(cfg.get("build_time"), "12:00")
    return dt.datetime(prev.year, prev.month, prev.day, h, m, tzinfo=my_tz(cfg))


def _day_tracks(q, day_iso):
    return [t for t in q.setdefault("tracks", []) if t.get("day") == day_iso and t.get("status") != "failed"]


day_tracks = _day_tracks


def day_shape(q, day_iso):
    """(сколько треков в дне, сколько клипов у самого большого) — для сетки слотов."""
    ts = _day_tracks(q, day_iso)
    n = max([len(ts)] + [t.get("index", 0) + 1 for t in ts])
    return n, max([1] + [int(t.get("count") or 1) for t in ts])


def _renumber(q, day_iso):
    for i, t in enumerate(sorted(_day_tracks(q, day_iso), key=lambda t: t.get("index", 0))):
        t["index"] = i


def free_day(q, cfg, now):
    """Первый день (с week_start и не раньше завтра), где ещё есть место для трека.
    Без лимита — просто ближайший день."""
    start = week_start(cfg, now)
    cap = day_cap(cfg)
    for d in range(0, 400):
        day = start + dt.timedelta(days=d)
        if not cap or len(_day_tracks(q, day.isoformat())) < cap:
            return day
    return None


def add_track(q, cfg, now, *, tid, file, voice, lyrics, phrase, count, title, msg_id, chat):
    """Ставит трек в очередь на ближайший свободный день."""
    q.setdefault("tracks", [])
    q["chat"] = chat
    for t in q["tracks"]:
        if t["id"] == tid:
            return t
    day = free_day(q, cfg, now)
    t = {"id": tid, "file_id": file.get("file_id"), "file_name": file.get("file_name"),
         "file_size": file.get("file_size"), "voice": bool(voice), "lyrics": lyrics, "phrase": phrase,
         "count": count, "title": title, "msg_id": msg_id, "day": day.isoformat(),
         "index": max([-1] + [o.get("index", 0) for o in _day_tracks(q, day.isoformat())]) + 1,
         "status": "waiting", "attempts": 0,
         "added": now.isoformat()}
    q["tracks"].append(t)
    return t


def rehome(q, cfg, now):
    """Ожидающие треки — на ближайший подходящий день: если их день прошёл, а клипы так и не
    собрались, или раньше освободилось место (отменили трек, сняли лимит в день).
    -> список перенесённых треков"""
    today = now.astimezone(my_tz(cfg)).date().isoformat()
    moved = []
    waiting = sorted((t for t in q.setdefault("tracks", []) if t.get("status") == "waiting"),
                     key=lambda t: (t.get("day", ""), t.get("index", 0)))
    for t in waiting:
        old = t.get("day", "")
        t["day"] = "0000-00-00"
        day = free_day(q, cfg, now)
        new = day.isoformat() if day else old
        if old >= today and new >= old:
            t["day"] = old
            continue
        t["day"] = new
        t["index"] = max([-1] + [o.get("index", 0) for o in _day_tracks(q, new) if o is not t]) + 1
        _renumber(q, old)
        moved.append(t)
    return moved


def due_builds(q, cfg, now):
    """Треки, которым пора собирать клипы (по дням и порядку)."""
    rehome(q, cfg, now)
    out = [t for t in q.setdefault("tracks", [])
           if t.get("status") == "waiting" and now >= build_at(cfg, dt.date.fromisoformat(t["day"]))]
    return sorted(out, key=lambda t: (t["day"], t["index"]))


def cancel_track(q, msg_id):
    """Убирает из очереди ещё не собранный трек. -> его запись или None"""
    for t in list(q.setdefault("tracks", [])):
        if t.get("msg_id") == msg_id or t.get("note_id") == msg_id:
            if t.get("status") == "waiting":
                q["tracks"].remove(t)
                _renumber(q, t["day"])                      # подвинуть порядок в дне
                return t
    return None


def short_day(q, cfg, now):
    """День, для которого уже пора собирать, а треков меньше нормы (один раз на день). -> (day, есть) или None"""
    tz = my_tz(cfg)
    tomorrow = now.astimezone(tz).date() + dt.timedelta(days=1)
    if tomorrow < week_start(cfg, now - dt.timedelta(days=1)) or now < build_at(cfg, tomorrow):
        return None
    if q.get("short_alert") == tomorrow.isoformat():
        return None
    have = len(_day_tracks(q, tomorrow.isoformat()))
    if have >= max(1, day_cap(cfg)):
        return None
    return tomorrow, have


def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def week_overview(q, cfg, now):
    tz = my_tz(cfg)
    start = max(now.astimezone(tz).date(), week_start(cfg, now - dt.timedelta(days=1)))
    cap = day_cap(cfg)
    per = int(cfg.get("clips_per_track", 3))
    if cap:
        head = "🗓 Неделя (%d %s × %d %s в день)" % (
            cap, _plural(cap, "трек", "трека", "треков"), per, _plural(per, "клип", "клипа", "клипов"))
    else:
        head = "🗓 Неделя (треков в день — без лимита, по %d %s с трека)" % (
            per, _plural(per, "клип", "клипа", "клипов"))
    lines = [head]
    total_missing = 0
    for d in range(7):
        day = start + dt.timedelta(days=d)
        ts = sorted(_day_tracks(q, day.isoformat()), key=lambda t: t["index"])
        names = ", ".join("«%s»%s" % (t["title"], " ✔" if t.get("status") == "built" else "") for t in ts) or "—"
        if cap:
            missing = max(0, cap - len(ts))
            total_missing += missing
            mark = "✅" if not missing else "⚠️"
            tail = (" · нужно ещё %d" % missing) if missing else ""
            lines.append("%s %s: %s%s" % (mark, day_label(day), names, tail))
        else:
            clips = sum(int(t.get("count") or per) for t in ts)
            tail = (" · %d %s" % (clips, _plural(clips, "клип", "клипа", "клипов"))) if ts else ""
            lines.append("%s %s: %s%s" % ("✅" if ts else "▫️", day_label(day), names, tail))
    lines.append("\n✔ — клипы уже собраны. Клипы на день собираю накануне в %02d:%02d." % hm(cfg.get("build_time"), "12:00"))
    if total_missing:
        lines.append("Пришли ещё %d трек(ов) с текстом припева в подписи — встанут в пустые места." % total_missing)
    elif not cap:
        lines.append("Новый трек встаёт на ближайший день.")
    lines.append("Убрать трек из очереди — ответь на него словом: отмена")
    return "\n".join(lines)


def find_track(q, tid):
    for t in q.setdefault("tracks", []):
        if t["id"] == tid:
            return t
    return None
