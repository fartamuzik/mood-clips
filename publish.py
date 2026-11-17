#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — выкладывает готовый клип в TikTok и Instagram через Zernio.

Нужен один секрет: ZERNIO_API_KEY (кабинет Zernio → API keys).
Аккаунты берутся из settings.json ("zernio_accounts"), а если там пусто —
бот сам спрашивает у Zernio, какие аккаунты подключены.

Только стандартная библиотека, ничего ставить не нужно.
"""

import os
import json
import uuid
import urllib.request
import urllib.error

API = os.environ.get("ZERNIO_API", "https://zernio.com/api/v1").rstrip("/")
KEY = os.environ.get("ZERNIO_API_KEY", "").strip()
NETWORKS = ("tiktok", "instagram")
NAMES = {"tiktok": "TikTok", "instagram": "Instagram"}


class PublishError(Exception):
    pass


def enabled():
    return bool(KEY)


def _api(method, path, body=None, headers=None, timeout=120):
    h = {"Authorization": "Bearer " + KEY, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw[:300]}
    except Exception as e:
        raise PublishError("Zernio недоступен: %s" % e)


def _err(res):
    if isinstance(res, dict):
        for k in ("error", "message", "details"):
            v = res.get(k)
            if v:
                return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)[:300]
    return json.dumps(res, ensure_ascii=False)[:300]


def accounts(cfg):
    """{'tiktok': id, 'instagram': id} — из настроек или из самого Zernio."""
    fixed = {k: str(v).strip() for k, v in (cfg.get("zernio_accounts") or {}).items()
             if k in NETWORKS and str(v).strip()}
    if fixed:
        return fixed
    code, res = _api("GET", "/accounts")
    if code != 200:
        raise PublishError("не получил список аккаунтов (%s): %s" % (code, _err(res)))
    found, twice = {}, set()
    for a in res.get("accounts") or []:
        p = (a.get("platform") or "").lower()
        aid = a.get("_id") or a.get("id") or a.get("accountId")
        if p not in NETWORKS or not aid:
            continue
        if p in found:
            twice.add(p)
        found[p] = str(aid)
    if twice:
        raise PublishError("в Zernio несколько аккаунтов %s — впиши нужные в settings.json, "
                           "поле zernio_accounts" % ", ".join(NAMES[p] for p in sorted(twice)))
    return found


def upload(path):
    """Загружает файл в хранилище Zernio, возвращает его адрес."""
    name = os.path.basename(path) or "clip.mp4"
    code, res = _api("POST", "/media/presign", {"filename": name, "contentType": "video/mp4"})
    if code != 200 or not res.get("uploadUrl") or not res.get("publicUrl"):
        raise PublishError("не получил адрес для загрузки (%s): %s" % (code, _err(res)))
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(res["uploadUrl"], data=data, method="PUT",
                                 headers={"Content-Type": "video/mp4"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            if r.status not in (200, 201, 204):
                raise PublishError("загрузка видео: ответ %s" % r.status)
    except urllib.error.HTTPError as e:
        raise PublishError("загрузка видео: ответ %s" % e.code)
    except PublishError:
        raise
    except Exception as e:
        raise PublishError("загрузка видео: %s" % e)
    return res["publicUrl"]


def with_tags(caption, tags):
    tags = (tags or "").strip()
    caption = (caption or "").strip()
    if not tags:
        return caption
    return (caption + "\n\n" + tags) if caption else tags


def publish(path, caption, cfg, only=None, key=None):
    """Публикует видео сразу. only — список сетей или None (все подключённые).
    key — метка публикации: повтор с той же меткой не создаст второй пост.
    Возвращает список названий сетей, куда ушёл ролик."""
    if not enabled():
        raise PublishError("не задан секрет ZERNIO_API_KEY")
    acc = accounts(cfg)
    want = [n for n in NETWORKS if n in acc and (not only or n in only)]
    if not want:
        missing = only or NETWORKS
        raise PublishError("в Zernio не подключён %s" % " / ".join(NAMES[n] for n in missing))
    url = upload(path)
    tags = {"tiktok": cfg.get("hashtags", ""),
            "instagram": cfg.get("hashtags_instagram", cfg.get("hashtags", ""))}
    platforms = [{"platform": n, "accountId": acc[n],
                  "customContent": with_tags(caption, tags[n])} for n in want]
    body = {
        "content": (caption or "").strip(),
        "mediaItems": [{"type": "video", "url": url}],
        "platforms": platforms,
        "publishNow": True,
    }
    if "tiktok" in want:
        body["tiktokSettings"] = {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True,
            "allow_duet": True,
            "allow_stitch": True,
            "content_preview_confirmed": True,
            "express_consent_given": True,
        }
    code, res = _api("POST", "/posts", body,
                     headers={"Idempotency-Key": key or str(uuid.uuid4())}, timeout=300)
    # 409 — этот же ролик уже ушёл в этот аккаунт за последние сутки: дубль не создаём
    if code == 409:
        raise PublishError("такой пост уже выходил за последние сутки — второй раз не выкладываю")
    if code not in (200, 201):
        raise PublishError("Zernio не принял пост (%s): %s" % (code, _err(res)))
    return [NAMES[n] for n in want]
