#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Supabase REST 客户端（只用标准库，无第三方依赖）。

- 目标端点：SUPABASE_URL 指向 opentrust.net 网关（supabase.co 官方域名在本地解析超时）。
- 鉴权：service_role key（绕过 RLS，可写）。读写都走 PostgREST /rest/v1。
- 用法：
    from supabase_client import SupabaseREST
    db = SupabaseREST()
    db.select("shooting_sessions", select="*", filters={"tag":"eq.v"})
    db.upsert("shooting_sessions", [row], on_conflict="tag")
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env.supabase")


def _ssl_context():
    """macOS python.org 版默认找不到系统根证书，这里显式指定 CA bundle。"""
    cafile = None
    try:
        import certifi
        cafile = certifi.where()
    except Exception:
        cafile = None
    if not cafile or not os.path.exists(cafile):
        for p in ("/etc/ssl/cert.pem", os.environ.get("SSL_CERT_FILE")):
            if p and os.path.exists(p):
                cafile = p
                break
    try:
        return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    except Exception:
        return ssl.create_default_context()


def load_env(path=ENV_PATH):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class SupabaseREST:
    def __init__(self, env_path=ENV_PATH):
        self.env = load_env(env_path)
        self.url = self.env["SUPABASE_URL"].rstrip("/")
        self.key = self.env["SUPABASE_SERVICE_ROLE_KEY"]
        self.base = self.url + "/rest/v1"

    def _headers(self):
        return {
            "apikey": self.key,
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(self, method, path, params=None, body=None, extra_headers=None):
        url = self.base + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        if extra_headers:
            for k, v in extra_headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
                raw = r.read().decode("utf-8")
                status = r.status
                payload = json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw}
        return status, payload

    def select(self, table, select="*", filters=None, params=None):
        p = dict(params or {})
        p["select"] = select
        if filters:
            p.update(filters)
        return self.request("GET", table, params=p)

    def upsert(self, table, rows, on_conflict):
        return self.request(
            "POST", table, params={"on_conflict": on_conflict}, body=rows,
            extra_headers={"Prefer": "resolution=merge-duplicates"},
        )

    def delete(self, table, filters):
        return self.request("DELETE", table, params=filters)
