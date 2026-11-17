#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — отложенные посты через Zernio (Instagram сам, TikTok по желанию черновиком).

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


def schedule(platform, media_url, caption, at, cfg, draft=False, key=None):
    """Ставит пост в очередь Zernio на время at (datetime с часовым поясом).
    draft=True для TikTok: ролик придёт черновиком во «Входящие» TikTok.
    Возвращает id поста в Zernio."""
    if not enabled():
        raise PublishError("не задан секрет ZERNIO_API_KEY")
    acc = accounts(cfg)
    if platform not in acc:
        raise PublishError("в Zernio не подключён %s" % NAMES[platform])
    body = {
        "content": caption,
        "mediaItems": [{"type": "video", "url": media_url}],
        "platforms": [{"platform": platform, "accountId": acc[platform]}],
        "scheduledFor": at.isoformat(),
    }
    if platform == "tiktok":
        body["tiktokSettings"] = {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True,
            "allow_duet": True,
            "allow_stitch": True,
            "content_preview_confirmed": True,
            "express_consent_given": True,
        }
        if draft:
            body["tiktokSettings"]["draft"] = True
    code, res = _api("POST", "/posts", body,
                     headers={"Idempotency-Key": key or str(uuid.uuid4())}, timeout=300)
    if code == 409:
        # тот же ролик с той же подписью уже стоит в очереди — второй не создаём
        existing = (res.get("details") or {}).get("existingPostId") or (res.get("post") or {}).get("_id")
        if existing:
            return existing
        raise PublishError("Zernio: %s" % _err(res))
    if code not in (200, 201, 202):
        raise PublishError("Zernio не принял пост (%s): %s" % (code, _err(res)))
    post = res.get("post") or {}
    pid = post.get("_id") or post.get("id") or res.get("postId")
    if not pid:
        raise PublishError("Zernio не вернул номер поста")
    return pid


def delete(post_id):
    """Снимает ещё не вышедший пост. True — снят (или его уже нет)."""
    code, res = _api("DELETE", "/posts/%s" % post_id)
    return code in (200, 202, 204, 404)


def status(post_id):
    """scheduled / publishing / published / failed / partial … или None, если не узнать."""
    try:
        code, res = _api("GET", "/posts/%s" % post_id)
    except PublishError:
        return None
    if code == 404:
        return "deleted"
    if code != 200:
        return None
    post = res.get("post") if isinstance(res.get("post"), dict) else res
    return (post or {}).get("status")
