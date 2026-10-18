"""Read-only gateway routing and model discovery. Never expose credentials."""
from __future__ import annotations

import ipaddress
from contextlib import closing
import json
import os
import sqlite3
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    pass


@dataclass
class Gateway:
    base_url: str
    api_key: str = field(repr=False)
    source: str = ""
    catalog: list[str] = field(default_factory=list)
    requested_model: str | None = None


@dataclass
class Route:
    mode: str
    gateway: Gateway | None = None
    reason: str = ""
    alternatives: list[dict] = field(default_factory=list)


def norm_base(value: str) -> str:
    p = urlsplit(value.strip())
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ConfigError("网关地址必须是不含账号、查询参数或片段的完整 URL。")
    local = p.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        pass
    if p.scheme != "https" and not (p.scheme == "http" and local):
        raise ConfigError("远程网关必须使用 HTTPS；仅本机回环地址允许 HTTP。")
    try:
        p.port
    except ValueError:
        raise ConfigError("网关端口无效。") from None
    path = p.path.rstrip("/") or "/v1"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def usable_key(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ConfigError("凭据格式无效。")
    return value


def model_entries(catalog) -> list:
    if isinstance(catalog, list):
        return catalog
    if isinstance(catalog, dict):
        return catalog.get("models", catalog.get("data", [])) or []
    return []


def model_ids(entries) -> list[str]:
    if not isinstance(entries, list):
        return []
    result = []
    for m in entries:
        v = m if isinstance(m, str) else (m.get("id") or m.get("model") or m.get("slug") or m.get("name")) if isinstance(m, dict) else None
        if isinstance(v, str) and v and not any(c in v for c in "\r\n") and v not in result:
            result.append(v)
    return result


def compatible(model: str) -> bool:
    # This adapter uses the bundled GPT Image engine, not vendor-specific APIs.
    return model.startswith("gpt-image-")


def image_models(entries) -> tuple[list[str], list[str]]:
    supported, other = [], []
    for mid in model_ids(entries):
        if compatible(mid):
            supported.append(mid)
        elif mid.lower().startswith(("dall-e-", "flux", "imagen", "glm-image", "seedream", "wanx", "qwen-image", "ideogram", "stable-diffusion")):
            other.append(mid)
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        mid = next(iter(model_ids([entry])), None)
        architecture = entry.get("architecture")
        output = entry.get("output_modalities") or (architecture.get("output_modalities") if isinstance(architecture, dict) else None)
        if mid and isinstance(output, list) and "image" in output and mid not in supported + other:
            other.append(mid)
    return supported, other


def pick_model(models: list[str], requested: str | None = None) -> str | None:
    if requested:
        return requested if requested in models else None
    # Prefer a stable explicit choice; never guess "latest" from alphabetic order.
    for model in ("gpt-image-2", "gpt-image-2.5", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"):
        if model in models:
            return model
    return sorted(models)[0] if models else None


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ConfigError("JSON 配置无法读取或格式无效。") from None
    if not isinstance(value, dict):
        raise ConfigError("JSON 配置必须为对象。")
    return value


def first_string(d: dict, names: tuple[str, ...]) -> str | None:
    return next((d[n].strip() for n in names if isinstance(d.get(n), str) and d[n].strip()), None)


def from_json(path: Path) -> Gateway:
    d = read_json(path)
    key = usable_key(first_string(d, ("api_key", "apiKey", "OPENAI_API_KEY")))
    base = first_string(d, ("base_url", "baseUrl", "OPENAI_BASE_URL"))
    if not key or not base:
        raise ConfigError("指定配置缺少 api_key 或 base_url；不会静默切换来源。")
    return Gateway(norm_base(base), key, "explicit-json", model_ids(model_entries(d.get("modelCatalog"))), first_string(d, ("image_model", "model")))


def load_codex(home: Path, profile: str | None = None) -> tuple[dict, dict]:
    try:
        c = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig")) if (home / "config.toml").exists() else {}
        a = read_json(home / "auth.json") if (home / "auth.json").exists() else {}
    except (OSError, ValueError):
        raise ConfigError("Codex 配置无法读取；请显式指定模式或生图配置。") from None
    profile = profile or c.get("profile")
    if profile:
        profiles = c.get("profiles", {})
        if not isinstance(profiles, dict) or profile not in profiles or not isinstance(profiles[profile], dict):
            raise ConfigError("指定的 Codex profile 不存在。")
        c = {**c, **profiles[profile]}
    return c, a


def cc_gateways(db: Path) -> list[tuple[Gateway, bool, str]]:
    if not db.exists():
        return []
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id,name,is_current,settings_config FROM providers WHERE app_type='codex'").fetchall()
            result = []
            for row in rows:
                try:
                    d = json.loads(row["settings_config"])
                    config = d.get("config", {})
                    config = tomllib.loads(config) if isinstance(config, str) else config
                    p = config.get("model_provider", "openai")
                    provider = config.get("model_providers", {}).get(p, {})
                    base = provider.get("base_url") or config.get("base_url")
                    if not base:
                        e = con.execute("SELECT url FROM provider_endpoints WHERE provider_id=? AND app_type='codex' LIMIT 1", (row["id"],)).fetchone()
                        base = e["url"] if e else None
                    key = usable_key((d.get("auth") or {}).get("OPENAI_API_KEY"))
                    if not key or not base:
                        continue
                    models = model_ids(model_entries(d.get("modelCatalog")))
                    result.append((Gateway(norm_base(base), key, "cc-switch:" + str(row["name"]), models, config.get("image_model")), bool(row["is_current"]), str(row["name"])))
                except (ValueError, TypeError, AttributeError, sqlite3.Error):
                    continue
            return result
    except sqlite3.Error:
        raise ConfigError("cc-switch 数据库暂时无法读取；没有修改或切换供应商。") from None


def resolve(*, home: Path, db: Path, mode="auto", config=None, provider=None, from_cc=False, from_codex=False, profile=None, saved_loader=None) -> Route:
    if mode == "native":
        return Route("native", reason="用户明确选择账号内置模式。")
    if config:
        return Route("relay", from_json(Path(config)))
    c, a = ({}, {}) if from_cc or provider else load_codex(home, profile)
    selected = c.get("model_provider", "openai")
    providers = c.get("model_providers", {})
    if not isinstance(selected, str) or not isinstance(providers, dict):
        raise ConfigError("Codex provider 配置类型无效。")
    pd = providers.get(selected, {})
    if not isinstance(pd, dict):
        raise ConfigError("当前 Codex provider 必须是配置表。")
    native = selected == "openai" and (a.get("auth_mode") == "chatgpt" or (a.get("tokens") and not a.get("auth_mode") and not a.get("OPENAI_API_KEY")))
    if mode == "auto" and not provider and not from_cc and native:
        return Route("native", reason="当前配置为 ChatGPT 登录；不读取 API 凭据或 cc-switch。")
    if saved_loader and not provider and not from_cc and not from_codex:
        saved = saved_loader()
        if saved:
            return Route("relay", saved, reason="用户已授权的独立生图配置。")
    cc = [] if from_codex else cc_gateways(db)
    alternatives = [{"provider": name, "declared_image_models": image_models(g.catalog)[0] + image_models(g.catalog)[1]} for g, cur, name in cc if not cur and any(image_models(g.catalog))]
    if provider:
        matches = [g for g, _, name in cc if name == provider]
        if len(matches) != 1:
            raise ConfigError("指定供应商不存在或名称不唯一；请确认 cc-switch 名称。")
        return Route("relay", matches[0], alternatives=alternatives)
    if not from_cc:
        base = pd.get("base_url") or c.get("base_url")
        if not base and selected == "openai" and not native and (a.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or a.get("auth_mode") == "apikey"):
            base = "https://api.openai.com/v1"
        key = usable_key(pd.get("experimental_bearer_token"))
        env_key = pd.get("env_key")
        if env_key and not key:
            key = usable_key(os.environ.get(env_key))
        if not key and not native and (pd.get("requires_openai_auth", True)):
            key = usable_key(a.get("OPENAI_API_KEY"))
        # Credentials may only be borrowed from cc-switch when the URL matches.
        matched = next((g for g, current, _ in cc if current and base and norm_base(base) == g.base_url), None)
        if not key and matched:
            key = matched.api_key
        if not key and selected == "openai" and not native:
            key = usable_key(os.environ.get("OPENAI_API_KEY"))
            if key and os.environ.get("OPENAI_BASE_URL"):
                base = os.environ["OPENAI_BASE_URL"]
        if base:
            gateway = Gateway(norm_base(base), key or "", "codex-active:" + selected, matched.catalog if matched else [], c.get("image_model"))
            return Route("relay", gateway, alternatives=alternatives)
    current = [g for g, active, _ in cc if active]
    if len(current) == 1:
        return Route("relay", current[0], alternatives=alternatives)
    if len(current) > 1:
        raise ConfigError("发现多个当前供应商，无法安全自动选择。")
    return Route("unknown", reason="无法确认账号模式或当前网关；请说明登录方式或提供生图服务配置。", alternatives=alternatives)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_gateway(g: Gateway | None) -> dict:
    if not g:
        return {}
    return {"source": g.source, "base_url": g.base_url, "has_api_key": bool(g.api_key), "declared_image_models": image_models(g.catalog)[0] + image_models(g.catalog)[1]}


def discover(route: Route, *, timeout=15, model=None, opener=None) -> dict:
    result = {"mode": route.mode, "status": "unknown", "reason": route.reason, **public_gateway(route.gateway), "alternatives": route.alternatives}
    if route.mode == "native":
        return {**result, "status": "native", "next_action": "use_builtin_image_gen"}
    g = route.gateway
    if not g:
        return {**result, "next_action": "clarify_mode_or_configure"}
    if not g.api_key:
        return {**result, "status": "need_credentials", "next_action": "hidden_credential_input"}
    request = urllib.request.Request(g.base_url + "/models", headers={"Authorization": "Bearer " + g.api_key, "Accept": "application/json"})
    try:
        opener = opener or urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("oversize")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a models response")
            if isinstance(data.get("data"), list):
                entries = data["data"]
                evidence = "live_openai_models_list"
            elif isinstance(data.get("models"), list):
                entries = data["models"]
                evidence = "live_gateway_models_list"
            else:
                raise ValueError("not a models response")
            if entries and not model_ids(entries):
                raise ValueError("invalid model entries")
    except urllib.error.HTTPError as exc:
        code = exc.code
        status = "authentication_failed" if code == 401 else "permission_denied" if code == 403 else "model_list_unavailable" if code in (404, 405, 501) else "rate_limited" if code == 429 else "gateway_error"
        declared, other = image_models(g.catalog)
        return {**result, "status": status, "http_status": code, "unverified_models": declared, "other_image_models": other, "next_action": "diagnose_or_confirm_explicit_model"}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {**result, "status": "network_error", "next_action": "diagnose_network_not_new_key"}
    except (ValueError, TypeError):
        return {**result, "status": "invalid_model_list", "next_action": "diagnose_gateway_response"}
    supported, other = image_models(entries)
    wanted = model or g.requested_model
    chosen = pick_model(supported, wanted)
    status = "ready" if chosen else "model_not_listed" if wanted else "unsupported_image_api" if other else "need_image_model"
    return {**result, "status": status, "model": chosen, "requested_model": wanted, "image_models": supported, "other_image_models": other, "evidence": evidence, "next_action": "generate_or_edit" if chosen else "select_model_or_configure_image_service"}
