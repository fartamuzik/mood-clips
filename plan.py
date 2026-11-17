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

def next_instagram(q, cfg, now):
    """Ближайшее свободное время для Instagram или None, если Instagram выключен."""
    times = sorted({hm(t) for t in (cfg.get("instagram_times") or [])})
    if not times:
        return None
    tz = ig_tz(cfg)
    lead = dt.timedelta(minutes=int(cfg.get("min_lead_minutes", 90)))
    taken = set()
    for it in q["items"]:
        if it.get("instagram_at"):
            taken.add(dt.datetime.fromisoformat(it["instagram_at"]))
    base = now.astimezone(tz).date()
    for d in range(0, 120):
        day = base + dt.timedelta(days=d)
        for h, m in times:
            cand = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if cand < now + lead or cand in taken:
                continue
            return cand
    return None


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

def add_clip(q, cfg, now, *, cid, hook, path, file_id, msg_id, chat, only=None):
    """Ставит клип в расписание. path — файл клипа (нужен для Instagram и черновиков TikTok).
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
        at = next_instagram(q, cfg, now)
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
        day = next_tiktok_day(q, cfg, now)
        if day:
            item["tiktok_day"] = day.isoformat()
            if mode == "inbox":
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
