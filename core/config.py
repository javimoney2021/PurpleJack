COIN = "<:PurpleCoin:1501855737842892941>"

ruleta_config = {
    "max_apuesta": 100,
    "cooldown": 120,
    "activa": True
}

rr_config = {
    "max_apuesta": 100,
    "cooldown": 120,
    "ganar_prob": 0.7,
    "perder_prob": 0.3,
    "activa": True
}

rob_config = {
    "activa": True,
    "cooldown": 3600  # segundos, default 1 hora
}

game_config = {
    "work": {
        "min": 100,
        "max": 150,
        "cooldown": 14400  # 4 horas en segundos
    },
    "crime": {
        "min": 10,
        "max": 50,
        "cooldown": 60,  # 1min Base
        "ganar_prob": 1.0,
        "perder_prob": 0.0
    }
}
