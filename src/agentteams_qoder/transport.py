"""Raw Matrix HTTP transport; durable consumption stays in Core."""
import json
from urllib.parse import quote
from urllib.request import Request, urlopen


class MatrixClient:
    def __init__(self, config):
        self.config = config

    def request(self, method, path, body=None, *, timeout=15):
        request = Request(self.config["homeserver"].rstrip("/") + path, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + self.config["accessToken"],
                                   "Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def send(self, room, content, transaction):
        return self.request("PUT", "/_matrix/client/v3/rooms/" + quote(room, safe="") +
                            "/send/m.room.message/" + quote(transaction, safe=""), content)["event_id"]


def allowed(config, room, event, *, invitation=False):
    """Evaluate projected policy for ordinary input; control packets use Core."""
    member, team = config["member"], config.get("team", {})
    if event.get("sender") == member["matrixUserId"]:
        return False
    content = event.get("content") or {}
    if content.get("msgtype") != "m.text" or not isinstance(content.get("body"), str):
        return False
    personal = room == member.get("personalRoomId")
    transport = config["matrix"]["transport"]
    policy = transport.get("dm", {}) if personal else transport
    mode = policy.get("policy" if personal else "groupPolicy", "allowlist")
    accepted = policy.get("allowFrom" if personal else "groupAllowFrom", [])
    overrides = config.get("desired", {}).get("channelPolicy", {})
    prefix = "dm" if personal else "group"
    denied = overrides.get(prefix + "DenyExtra", [])
    sender = event.get("sender")
    if mode == "disabled" or sender in denied or "*" in denied:
        return False
    if mode != "open" and sender not in [*accepted, *overrides.get(prefix + "AllowExtra", [])] and "*" not in accepted:
        return False
    # Personal and execution rooms have explicit sender policies. General
    # team rooms require an actual mention to avoid agent reply loops.
    if not invitation and not personal and room == team.get("teamRoomId"):
        mentions = (content.get("m.mentions") or {}).get("user_ids", [])
        return member["matrixUserId"] in mentions
    return True
