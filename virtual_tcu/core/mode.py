from enum import Enum


class Mode(Enum):
    AUTO = "AUTO"
    RACE = "RACE"
    DRIFT = "DRIFT"
    OFFROAD = "OFFROAD"
    LEARN = "LEARN"
    MANUAL = "MANUAL"


MODE_ORDER = [
    Mode.AUTO,
    Mode.RACE,
    Mode.DRIFT,
    Mode.OFFROAD,
    Mode.LEARN,
    Mode.MANUAL,
]
