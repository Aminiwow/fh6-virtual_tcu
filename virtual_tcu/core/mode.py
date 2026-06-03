from enum import Enum


class Mode(Enum):
    RACE = "RACE"
    DRIFT = "DRIFT"
    OFFROAD = "OFFROAD"
    LEARN = "LEARN"
    MANUAL = "MANUAL"


MODE_ORDER = [
    Mode.RACE,
    Mode.DRIFT,
    Mode.OFFROAD,
    Mode.LEARN,
    Mode.MANUAL,
]
