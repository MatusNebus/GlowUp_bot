CAPABILITIES = [
    "vytvoriť dynamickú aktivitu a určiť jej povinné výsledkové parametre",
    "vypísať aktívne aktivity a ich parametre",
    "navrhnúť úpravu alebo deaktiváciu aktivity na schválenie adminovi",
    "nastaviť záväzky a naplánovať tréningy",
    "zapísať splnený, skrátený alebo vynechaný tréning",
    "posunúť tréning žolíkom",
    "čítať osobné aj skupinové tréningové štatistiky",
    "prečítať aktuálne pravidlá projektu",
    "poradiť s tréningom, výživou a motiváciou",
]


def get_help() -> str:
    return "Čo Jonáš vie:\n" + "\n".join(f"- {item}" for item in CAPABILITIES)
