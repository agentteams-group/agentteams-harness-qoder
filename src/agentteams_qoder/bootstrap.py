"""Restore Controller assets and this Harness's native transcript mirror."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def listing(prefix):
    result = subprocess.run(["mc", "ls", "--json", prefix.rstrip("/") + "/"],
                            check=True, capture_output=True, text=True, timeout=30)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if any(row.get("status") != "success" for row in rows):
        raise ValueError("Member storage listing failed")
    return {row["key"] for row in rows}


def restore():
    member_home = Path(os.environ["TEAMHARNESS_RUNTIME_CONFIG"]).parent.parent
    prefix = os.environ["AGENTTEAMS_STORAGE_PREFIX"].rstrip("/") + "/agents/" + os.environ["AGENTTEAMS_WORKER_NAME"]
    roots, runtime = listing(prefix), listing(prefix + "/runtime")
    with tempfile.TemporaryDirectory(dir=member_home) as directory:
        def pull(relative):
            target = Path(directory) / Path(relative).name
            subprocess.run(["mc", "cp", prefix + "/" + relative, str(target)], check=True, capture_output=True, timeout=30)
            target.replace(member_home / relative)
        pull("runtime/runtime.yaml")
        if "qoder-sessions.sqlite" in runtime:
            pull("runtime/qoder-sessions.sqlite")
        for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md"):
            if name in roots:
                pull(name)
    if "skills/" in roots:
        subprocess.run(["mc", "mirror", prefix + "/skills/", str(member_home / "skills"), "--overwrite"],
                       check=True, capture_output=True, timeout=60)


if __name__ == "__main__":
    restore()
