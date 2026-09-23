FACTIONS = {
    "aegis": "Aegis Dynamics — оборонные роботы, тяжёлая броня",
    "vex": "Vex Industries — быстрые разведывательные дроны",
    "core": "CoreForge — инженерные роботы, ставка на энергию и крафт",
}

# Зоны открытого мира. chance_fight / chance_resource — вероятности события при исследовании.
ZONES = {
    "scrapfields": {
        "name": "Ржавые поля",
        "min_level": 1,
        "npc_pool": ["rogue_drone", "scrap_crawler"],
        "resource": "scrap",
        "resource_amount": (3, 8),
    },
    "reactor_ruins": {
        "name": "Руины реактора",
        "min_level": 3,
        "npc_pool": ["sentry_bot", "rogue_drone"],
        "resource": "energy_core",
        "resource_amount": (1, 3),
    },
    "iron_canyon": {
        "name": "Железный каньон",
        "min_level": 6,
        "npc_pool": ["sentry_bot", "war_walker"],
        "resource": "scrap",
        "resource_amount": (6, 14),
    },
}

NPCS = {
    "scrap_crawler": {"name": "Ржавый краулер", "hp": 20, "attack": 3, "exp": 8, "scrap": (2, 5)},
    "rogue_drone": {"name": "Дрон-отступник", "hp": 30, "attack": 5, "exp": 14, "scrap": (3, 7)},
    "sentry_bot": {"name": "Робот-страж", "hp": 55, "attack": 9, "exp": 26, "scrap": (5, 12)},
    "war_walker": {"name": "Боевой шагоход", "hp": 90, "attack": 14, "exp": 45, "scrap": (10, 20)},
}

STARTING_STATS = {
    "level": 1,
    "exp": 0,
    "hp": 50,
    "max_hp": 50,
    "attack": 6,
    "defense": 2,
    "scrap": 0,
    "energy_core": 0,
}


def exp_to_next_level(level: int) -> int:
    return 40 + (level - 1) * 25
