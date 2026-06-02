import json
from pathlib import Path


STATE_DIR = Path("./state")
STATE_DIR.mkdir(exist_ok=True)


def state_file(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def load_state_from_disk(session_id: str):
    path = state_file(session_id)
    if not path.exists():
        return None
    return json.loads(path.read_text("utf-8"))


def save_state_to_disk(session_id: str, team_state):
    state_file(session_id).write_text(json.dumps(team_state), encoding="utf-8")


def clear_state_from_disk(session_id: str) -> None:
    path = state_file(session_id)
    if path.exists():
        path.unlink()
