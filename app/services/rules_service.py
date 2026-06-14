from pathlib import Path


RULES_PATH = Path(__file__).resolve().parent.parent.parent / "RULES.md"


def get_rules() -> str:
    """Read only the project's fixed rules document."""
    try:
        return RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "Pravidlá momentálne nie sú dostupné."
