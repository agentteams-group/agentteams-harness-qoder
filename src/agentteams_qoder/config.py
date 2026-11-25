"""Consume Controller-owned configuration; never persist resolved secrets."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def load(path):
    from worker_runtime_config import project

    path = Path(path)
    record = yaml.safe_load(path.read_text())
    if not isinstance(record, dict):
        raise ValueError("Member runtime configuration must be an object")
    projected = project(record, "qoder", os.environ["AGENTTEAMS_WORKER_NAME"])
    role = {"team_leader": "leader", "standalone": "worker"}.get(
        record["member"].get("role"), record["member"].get("role"))
    if role not in {"leader", "worker"}:
        raise ValueError("This Qoder candidate supports Leader and Worker roles only")
    if record.get("desired", {}).get("agentPackage", {}).get("ref"):
        raise ValueError("Qoder AgentSpec packages are not implemented")
    matrix = projected["channels"]["matrix"]
    if matrix.get("encryption") is not False:
        raise ValueError("Qoder raw Matrix transport requires encryption=false")
    provider = projected["models"]["providers"]["agentteams-gateway"]
    selected = projected["agents"]["defaults"]["model"]["primary"].split("/", 1)[1]
    return record, role, matrix, {"model": selected, "api_key": provider["apiKey"], "url": provider["baseUrl"]}


def instructions(record, role, plugin, member_home):
    plugin, member_home = Path(plugin).resolve(), Path(member_home)
    sections = []
    if record.get("team"):
        sections.extend([(plugin / "prompts/team/TEAMS.md").read_text(),
                         (plugin / f"prompts/agent/{role}.md").read_text()])
    else:
        sections.append("You are an independent AgentTeams Worker. Answer in the current room; do not invent a Team.")
    sections.append("Your registered identity and team:\n" + yaml.safe_dump(
        {"member": record["member"], "team": record.get("team", {})}, allow_unicode=True))
    for filename in ("IDENTITY.md", "SOUL.md", "AGENTS.md"):
        asset = member_home / filename
        if asset.is_file():
            sections.append(asset.read_text())
    manifest = yaml.safe_load((plugin / "plugin.yaml").read_text())
    builtins = set()
    for section in ("agent", "team"):
        for skill in manifest.get("skills", {}).get(section, []):
            if role in skill.get("roles", []) and skill["id"] != "mcporter":
                builtins.add(skill["id"])
                source = (plugin / skill["path"] / "SKILL.md").resolve()
                if not source.is_relative_to(plugin):
                    raise ValueError("Shared skill escapes plugin directory")
                sections.append(source.read_text())
    for name, value in record.get("desired", {}).get("inlineConfig", {}).items():
        if name in {"identity", "soul", "agents"} and value:
            sections.append(name + ":\n" + value)
    for name in record.get("desired", {}).get("skills", []):
        if name in builtins:
            continue
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid assigned skill name")
        source = (member_home / "skills" / name / "SKILL.md").resolve()
        if not source.is_relative_to((member_home / "skills").resolve()):
            raise ValueError("Assigned skill escapes member directory")
        sections.append(source.read_text())
    sections.append("Use your registered native Task tool for Task operations. Read its verified receipts. "
                    "The task system delivers accepted outputs; do not duplicate task notifications. "
                    "Nested agents and background commands are unavailable in this candidate.")
    sections.append("Your assigned Skills are already included above. Do not reread their files. In an execution phase, use Read for prepared input files and Write for your own reports; use Bash only when the work requires executing a command.")
    return "\n\n".join(sections)


def authentication():
    from qoder_agent_sdk import access_token_from_env, service_account_from_env, qodercli_auth

    # Explicit priority; gateway credentials do not authenticate a Qoder account.
    if os.getenv("QODER_SERVICE_ACCOUNT_KEY"):
        return service_account_from_env("QODER_SERVICE_ACCOUNT_KEY")
    if os.getenv("QODER_PERSONAL_ACCESS_TOKEN"):
        return access_token_from_env("QODER_PERSONAL_ACCESS_TOKEN")
    if os.getenv("QODER_AUTH_MODE") == "local":
        return qodercli_auth()
    raise ValueError("QODER_SERVICE_ACCOUNT_KEY or QODER_PERSONAL_ACCESS_TOKEN is required")


def gateway_options(projected):
    route = os.getenv("QODER_MODEL_ROUTE", "gateway")
    if route == "account":
        return {"model": os.getenv("QODER_ACCOUNT_MODEL") or "auto", "settings": {}, "env": {}}
    if route != "gateway":
        raise ValueError("QODER_MODEL_ROUTE must be gateway or account")
    if not all(projected.get(field) for field in ("model", "url", "api_key")):
        raise ValueError("Projected gateway model, URL and consumer key are required")
    return {"model": "agentteams-gateway/" + projected["model"],
            "settings": {"providers": {"agentteams-gateway": {
                "type": "openai-compatible", "protocol": "openai",
                "baseUrl": projected["url"], "apiKey": projected["api_key"], "model": projected["model"]}}},
            # The pinned CLI gates SDK custom endpoints behind this feature switch.
            # Normal Qoder authentication and account permission checks still apply.
            "env": {"QODER_SDK_CUSTOM_BASE_URL_BYOK": "1"}}


def external_mcp(record, gateway_key):
    servers = {}
    for server in record.get("desired", {}).get("mcpServers", []):
        name = server.get("name")
        if not name or name == "agentteams" or name in servers:
            raise ValueError("Invalid or duplicate MCP server name")
        transport = server.get("transport") or "http"
        if transport not in {"http", "sse"} or not server.get("url"):
            raise ValueError("Qoder requires a declared HTTP/SSE MCP endpoint")
        headers = {}
        gateway = record.get("desired", {}).get("model", {}).get("gatewayUrl", "")
        if gateway and urlsplit(server["url"]).netloc == urlsplit(gateway).netloc:
            headers["Authorization"] = "Bearer " + gateway_key
        servers[name] = {"type": transport, "url": server["url"], "headers": headers}
    return servers
