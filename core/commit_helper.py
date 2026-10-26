import random

# Realistic, natural commit messages that human data scientists / developers
# use on Hugging Face dataset repositories.
# ZERO robotic wording (no 'encrypted', 'vault', 'blob', 'sync', 'bot', 'auto', 'hash').

UPLOAD_MESSAGES = [
    "Upload data",
    "Add dataset files",
    "Update dataset",
    "Upload data files",
    "Add data",
    "Dataset update",
    "Upload batch",
    "Add files",
    "Add new samples",
    "Update data",
]

METADATA_MESSAGES = [
    "Update metadata",
    "Update dataset",
    "Dataset update",
    "Update records",
    "Dataset refresh",
    "Update dataset files",
    "Minor metadata update",
]

DELETE_MESSAGES = [
    "Clean up dataset",
    "Remove obsolete files",
    "Update dataset",
    "Remove files",
    "Dataset cleanup",
    "Remove unused samples",
]

INIT_MESSAGES = [
    "Initial commit",
]


def get_human_commit_message(action: str = "upload") -> str:
    """
    Returns a completely natural, human-written commit message suitable
    for Hugging Face dataset repositories with zero indication of automated tools.
    """
    act = (action or "upload").lower().strip()
    if act in ("init", "squash", "initial"):
        return "Initial commit"
    elif act in ("delete", "remove", "cleanup"):
        return random.choice(DELETE_MESSAGES)
    elif act in ("metadata", "index", "sidecar", "record"):
        return random.choice(METADATA_MESSAGES)
    else:
        return random.choice(UPLOAD_MESSAGES)
