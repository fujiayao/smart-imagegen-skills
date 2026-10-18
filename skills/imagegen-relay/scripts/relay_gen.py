#!/usr/bin/env python3
"""Two-mode image skill adapter. Native mode returns a tool handoff, never OAuth tokens."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from credentials import hidden_input, load_gateway, save_gateway
from gateway import ConfigError, Gateway, Route, compatible, discover, from_json, image_models, pick_model, public_gateway, resolve, usable_key, norm_base

SKILL_DIR = Path(__file__).resolve().parents[1]
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
DEFAULT_DB = Path.home() / ".cc-switch" / "cc-switch.db"
SAVED = SKILL_DIR / ".secrets" / "image-service.dpapi"
ENGINE = CODEX_HOME / "skills" / ".system" / "imagegen" / "scripts" / "image_gen.py"
OUTPUT = Path(os.environ.get("IMAGEGEN_OUTPUT_DIR") or Path.cwd() / "outputs" / "images").expanduser().resolve()


def emit(data, secret=""):
    def redact(value):
        if isinstance(value, str):
            return value.replace(secret, "[REDACTED]") if secret else value
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {redact(k): redact(v) for k, v in value.items()}
        return value
    text = json.dumps(redact(data), ensure_ascii=False, indent=2)
    print(text)


def offline(route, model=None):
    if route.mode == "native":
        return {"mode": "native", "status": "native", "next_action": "use_builtin_image_gen", "reason": route.reason}
    g = route.gateway
    chosen = pick_model(image_models(g.catalog)[0], model or g.requested_model) if g else None
    return {"mode": route.mode, "status": "configured_not_verified" if g and g.api_key else "need_credentials" if g else "unknown", **public_gateway(g), "model": chosen, "requested_model": model or (g.requested_model if g else None), "alternatives": route.alternatives, "next_action": "discover_models", "reason": route.reason}


def reject_secret_flags(rest):
    forbidden = ("--api-key", "--api_key", "--token", "--authorization", "-k", "--base-url", "--base_url")
    if any(a.split("=", 1)[0].lower() in forbidden for a in rest):
        raise ConfigError("禁止把密钥或网关覆盖参数放入引擎命令行；请用隐藏输入或独立配置。")


def prepare_command(rest, model, engine):
    if not rest or rest[0] not in ("generate", "edit", "generate-batch"):
        raise ConfigError("请指定 generate、edit 或 generate-batch。")
    args = list(rest)
    if not any(a == "--model" or a.startswith("--model=") for a in args):
        args[1:1] = ["--model", model]
    if "--out" not in args and not any(a.startswith("--out=") for a in args) and "--out-dir" not in args and not any(a.startswith("--out-dir=") for a in args):
        if args[0] == "generate-batch":
            args += ["--out-dir", str(OUTPUT)]
        else:
            from datetime import datetime
            args += ["--out", str(OUTPUT / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".png"))]
    return [sys.executable, "-B", str(engine)] + args


def preflight(cmd, env, model, secret):
    """Run only the selected engine dry-run, validating final outputs before a paid call."""
    check = subprocess.run(cmd + ["--dry-run"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check.returncode:
        details = (check.stderr + "\n" + check.stdout).replace(secret, "[REDACTED]") if secret else check.stderr + "\n" + check.stdout
        print(details)
        raise ConfigError("所选引擎离线校验失败；没有发出生图请求。")
    decoder = json.JSONDecoder()
    text = check.stdout.strip()
    count = 0
    while text:
        try:
            record, end = decoder.raw_decode(text)
        except ValueError:
            raise ConfigError("无法解析所选引擎的离线输出；没有发出生图请求。") from None
        text = text[end:].lstrip()
        if not isinstance(record, dict) or record.get("model") != model:
            raise ConfigError("离线任务模型与已探测模型不一致；批量任务不能绕过模型选择。")
        outputs = record.get("outputs", []) + (record.get("outputs_downscaled") or [])
        if not outputs:
            raise ConfigError("离线校验未返回输出路径。")
        for filename in outputs:
            path = Path(filename).resolve()
            if path.exists() and "--force" not in cmd:
                raise ConfigError("输出文件已存在；请换一个文件名，不要未经用户同意覆盖。")
        count += 1
    if not count:
        raise ConfigError("官方引擎没有返回离线任务。")


def engine_model(rest, explicit):
    values = []
    for i, arg in enumerate(rest):
        if arg == "--model":
            if i + 1 == len(rest):
                raise ConfigError("--model 缺少模型名。")
            values.append(rest[i + 1])
        elif arg.startswith("--model="):
            values.append(arg.split("=", 1)[1])
    if len(values) > 1 or (explicit and values and explicit != values[0]):
        raise ConfigError("重复或冲突的模型参数。")
    return explicit or (values[0] if values else None)



def select_backend(backend, engine_override, rest):
    """Select before any paid request; never retry a failed call via another engine."""
    if backend == "awakefantasy" and engine_override:
        raise ConfigError("--backend awakefantasy 不能与自定义 --engine 混用。")
    if engine_override:
        return "custom", Path(engine_override)
    if backend == "auto":
        # The small Awake adapter supports only these flags and one output. Unknown
        # capabilities go to the full CLI, where its parser/preflight validates them.
        values = {"--model", "--prompt", "--prompt-file", "--image", "--size", "--quality", "--output-format", "--out", "--n"}
        switches = {"--force", "--dry-run"}
        simple = bool(rest) and rest[0] in ("generate", "edit")
        i = 1
        while simple and i < len(rest):
            name, sep, value = rest[i].partition("=")
            if name in switches and not sep:
                i += 1
                continue
            if name not in values:
                simple = False
                break
            if not sep:
                i += 1
                if i >= len(rest):
                    simple = False
                    break
                value = rest[i]
            if name == "--n" and value != "1":
                simple = False
            i += 1
        backend = "awakefantasy" if simple else "official"
    return backend, SKILL_DIR / "scripts" / "awake_engine.py" if backend == "awakefantasy" else ENGINE


def main(argv=None):
    p = argparse.ArgumentParser(description="智能生图skills：账号内置生图 / 当前网关模型发现 + 自动选择 API 引擎")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="离线诊断；不证明模型可调用")
    action.add_argument("--discover", action="store_true", help="只 GET 当前网关 /models；不生图")
    action.add_argument("--plan", action="store_true", help="离线执行预览，不调用接口")
    p.add_argument("--mode", choices=("auto", "native", "relay"), default="auto")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--config", help="显式 OpenAI-compatible JSON；失败不回退")
    source.add_argument("--provider", help="经用户确认使用的 cc-switch 供应商名称；不改变全局当前供应商")
    source.add_argument("--from-ccswitch", action="store_true")
    source.add_argument("--from-codex", action="store_true")
    p.add_argument("--profile", help="当前会话显式使用的 Codex profile")
    p.add_argument("--image-model", help="显式图片模型，不读取聊天模型来代替")
    p.add_argument("--allow-unverified", action="store_true", help="仅在 /models 不支持时，经用户确认试一次明确模型")
    p.add_argument("--credential-input", action="store_true", help="本地终端隐藏输入独立生图 Key")
    p.add_argument("--save-credentials", action="store_true", help="配合隐藏输入，使用 Windows DPAPI 加密保存")
    p.add_argument("--backend", choices=("auto", "official", "awakefantasy"), default="auto", help="API 执行后端；不会改变网关/凭据来源")
    p.add_argument("--engine", help="显式引擎脚本；不能与 awakefantasy 后端混用")
    p.add_argument("--codex-home", default=str(CODEX_HOME))
    p.add_argument("--cc-db", default=str(DEFAULT_DB))
    p.add_argument("--timeout", type=float, default=15)
    args, rest = p.parse_known_args(argv)
    backend, engine = select_backend(args.backend, args.engine, rest)
    reject_secret_flags(rest)
    wanted = engine_model(rest, args.image_model)
    if wanted and not compatible(wanted):
        raise ConfigError("当前执行适配器只支持 gpt-image-*；其它生图模型需要对应供应商适配器，不是换 Key 就能解决。")
    if args.mode == "native" and (args.config or args.provider or args.from_ccswitch or args.from_codex or args.credential_input):
        raise ConfigError("native 模式不能同时指定 API 凭据来源。")
    if args.save_credentials and not args.credential_input:
        raise ConfigError("--save-credentials 必须配合 --credential-input，并由用户授权保存。")
    if args.save_credentials and (args.check or args.plan or "--dry-run" in rest):
        raise ConfigError("离线预览不保存凭据；请用 --discover 验证后再加密保存。")
    if args.timeout <= 0 or args.timeout > 120:
        raise ConfigError("模型查询超时应为 0–120 秒之间的正数。")
    explicit_config = args.config or (os.environ.get("IMAGEGEN_RELAY_CONFIG") if not (args.provider or args.from_ccswitch or args.from_codex or args.mode == "native") else None)
    def saved():
        g = load_gateway(SAVED)
        if g:
            return g
        legacy = SKILL_DIR / ".secrets" / "config.json"
        if legacy.exists():
            return from_json(legacy)
        key, base = os.environ.get("IMAGEGEN_API_KEY"), os.environ.get("IMAGEGEN_BASE_URL")
        if key or base:
            if not key or not base:
                raise ConfigError("专用生图环境变量 IMAGEGEN_API_KEY / IMAGEGEN_BASE_URL 必须成对提供。")
            return Gateway(norm_base(base), usable_key(key), "imagegen-env", requested_model=os.environ.get("IMAGEGEN_MODEL"))
        return None
    route = resolve(home=Path(args.codex_home), db=Path(args.cc_db), mode=args.mode, config=explicit_config, provider=args.provider, from_cc=args.from_ccswitch, from_codex=args.from_codex, profile=args.profile, saved_loader=None if args.credential_input else saved)
    if args.credential_input:
        current = route.gateway
        g = hidden_input(current.base_url if current else None, wanted)
        route = Route("relay", g)
    g = route.gateway
    secret = g.api_key if g else ""
    if route.mode == "native":
        emit(offline(route))
        return 0
    if args.check:
        emit(offline(route, wanted), secret)
        return 0 if g and g.api_key else 2
    if args.plan or "--dry-run" in rest:
        report = offline(route, wanted)
        model = wanted or report.get("model")
        if not model:
            emit({**report, "status": "need_model_for_offline_plan"}, secret)
            return 2
        report["backend"] = backend
        report["engine_command"] = prepare_command(rest, model, engine)
        report["note"] = "仅离线预览；不会联网，也不证明模型可用。"
        emit(report, secret)
        return 0
    report = discover(route, timeout=args.timeout, model=wanted)
    selected = report.get("model")
    if report["status"] == "model_list_unavailable" and args.allow_unverified and (wanted or (g and g.requested_model)):
        selected = wanted or g.requested_model
        if not compatible(selected):
            raise ConfigError("确认试调用的模型不兼容当前引擎。")
        report.update(status="unverified_explicit_model", model=selected, evidence="user_confirmed_trial")
    if args.save_credentials:
        if report["status"] not in ("ready", "unverified_explicit_model"):
            emit(report, secret)
            return 2
        g.requested_model = selected
        save_gateway(SAVED, g)
        report["credential_storage"] = "windows_dpapi_current_user"
    if args.discover:
        emit(report, secret)
        return 0 if report["status"] in ("ready", "unverified_explicit_model") else 2
    if not selected:
        emit(report, secret)
        return 2
    if not engine.is_file():
        raise ConfigError("所选图片执行引擎不存在；请检查安装路径，不自动替换供应商。")
    cmd = prepare_command(rest, selected, engine)
    env = dict(os.environ)
    env.update(OPENAI_API_KEY=g.api_key, OPENAI_BASE_URL=g.base_url, PYTHONDONTWRITEBYTECODE="1")
    preflight(cmd, env, selected, secret)
    # Streaming redaction prevents upstream errors echoing the key into logs.
    emit({**report, "backend": backend, "note": "模型列表包含此模型；实际生图端点/额度是否可用仍以本次调用为准。"}, secret)
    process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in process.stdout:
        print(line.replace(secret, "[REDACTED]") if secret else line, end="")
    return process.wait()


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        raise SystemExit(main())
    except ConfigError as exc:
        emit({"status": "configuration_error", "message": str(exc)})
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("已取消；未继续生图。")
        raise SystemExit(130)
