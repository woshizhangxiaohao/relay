#!/usr/bin/env python3
"""只为明确指定的节点创建灰云 DNS 记录；不覆盖已有冲突。可在管理电脑运行。"""
import argparse
import ipaddress
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def fqdn(value):
    name = value.lower().rstrip(".")
    if len(name) > 253 or len(name.split(".")) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")
    ):
        raise ValueError("域名无效：" + value)
    return name


def token_from(path):
    if path:
        with Path(path).expanduser().open() as f:
            for line in f:
                m = re.match(r"^\s*(?:export\s+)?CLOUDFLARE_API_TOKEN\s*=\s*(.*)$", line)
                if m:
                    values = shlex.split(m[1], comments=True)
                    if len(values) == 1 and values[0]:
                        return values[0]
    else:
        token = os.getenv("CLOUDFLARE_API_TOKEN")
        if token:
            return token
    raise ValueError("缺少 CLOUDFLARE_API_TOKEN；请在管理端使用环境变量或 --token-file")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ("zone", "host", "ipv4"):
        ap.add_argument("--" + key, required=True)
    ap.add_argument("--ipv6")
    ap.add_argument("--token-file")
    ap.add_argument("--apply", action="store_true", help="默认只预览")
    args = ap.parse_args()
    host, zone = fqdn(args.host), fqdn(args.zone)
    if not host.endswith("." + zone):
        raise ValueError("只允许在指定 zone 内创建专用子域名")
    wanted = {"A": ipaddress.IPv4Address(args.ipv4)}
    if args.ipv6:
        wanted["AAAA"] = ipaddress.IPv6Address(args.ipv6)
    if any(not x.is_global for x in wanted.values()):
        raise ValueError("只接受公网入口地址")
    token = token_from(args.token_file)

    def api(path, payload=None):
        req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path,
                  data=None if payload is None else json.dumps(payload).encode(),
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "relay-node-dns/1"})
        try:
            with urllib.request.urlopen(req, timeout=16) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            safe_codes = []
            try:
                safe_codes = [i.get("code") for i in json.load(e).get("errors", [])]
            except (ValueError, AttributeError):
                pass
            raise ValueError(f"Cloudflare 返回 HTTP {e.code}，错误码 {safe_codes}") from None
        if not result.get("success", False):
            raise ValueError("Cloudflare API 操作未确认成功")
        return result["result"]

    zones = api("/zones?" + urllib.parse.urlencode({"name": zone, "status": "active"}))
    if len(zones) != 1 or zones[0]["name"] != zone:
        raise ValueError(f"当前凭据没有 {zone} 的可见管理权限")
    path = "/zones/" + zones[0]["id"] + "/dns_records"
    query = "?" + urllib.parse.urlencode({"name":host, "per_page":100})
    def existing():
        records = api(path + query)
        if len(records) >= 100:
            raise ValueError("同名记录过多，停止自动修改")
        found = {}
        for row in records:
            if row["name"].lower() != host or row["type"] not in ("A", "AAAA", "CNAME"):
                continue
            t = row["type"]
            if t not in wanted or row.get("proxied") or t in found or ipaddress.ip_address(row["content"]) != wanted[t]:
                raise ValueError(f"{host} 已有冲突的 {t} 记录，未覆盖")
            found[t] = row
        return found

    seen = existing()
    for kind, addr in wanted.items():
        print(f"{host} {kind} {addr} 灰云：" + ("已匹配" if kind in seen else "待创建"))
    if args.apply:
        for kind, addr in wanted.items():
            if kind not in seen:
                api(path, {"type":kind, "name":host, "content":str(addr), "ttl":300, "proxied":False})
        if set(existing()) != set(wanted):
            raise ValueError("写入后的记录与预期不一致")
        print("已向 Cloudflare 权威账户核对所有指定记录。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as e:
        sys.exit(str(e))
