"""Optional Windows DPAPI storage; plaintext secrets are never persisted here."""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

from gateway import ConfigError, Gateway, norm_base, usable_key


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ConfigError("加密保存目前只支持 Windows；请使用单次隐藏输入或专用环境变量。")
    storage = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), storage)
    target = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ConfigError("Windows DPAPI 操作失败；原配置未被覆盖。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def save_gateway(path: Path, g: Gateway):
    payload = json.dumps({"api_key": g.api_key, "base_url": g.base_url, "model": g.requested_model}, ensure_ascii=False).encode("utf-8")
    encrypted = dpapi(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encrypted)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_gateway(path: Path) -> Gateway | None:
    if not path.exists():
        return None
    try:
        d = json.loads(dpapi(path.read_bytes(), decrypt=True))
        key = usable_key(d.get("api_key"))
        if not key:
            raise ValueError("no key")
        return Gateway(norm_base(d["base_url"]), key, "saved-dpapi", requested_model=d.get("model"))
    except (OSError, ValueError, KeyError):
        raise ConfigError("独立生图凭据无法解密或已损坏；请在原 Windows 账号下使用或重新配置。") from None


def hidden_input(default_base=None, default_model=None) -> Gateway:
    if not sys.stdin.isatty():
        raise ConfigError("请在本地交互终端运行 --credential-input；禁止使用可回显的管道输入密钥。")
    print("独立生图服务：密钥不会显示或写入命令历史。直接回车沿用括号内的值。")
    base = input("Base URL" + (f" [{default_base}]" if default_base else "") + ": ").strip() or default_base
    model = input("模型名" + (f" [{default_model}]" if default_model else " [自动查询]") + ": ").strip() or default_model
    if not base:
        raise ConfigError("缺少 Base URL；仅凭 API Key 不能判断服务地址。")
    base = norm_base(base)
    key = usable_key(getpass.getpass("生图 API Key（隐藏输入）: "))
    if not key:
        raise ConfigError("未输入 API Key。")
    return Gateway(base, key, "hidden-input", requested_model=model)
