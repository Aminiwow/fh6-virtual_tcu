from enum import Enum


class Mode(Enum):
    COMFORT = "COMFORT"
    RACE = "RACE"
    DRIFT = "DRIFT"
    OFFROAD = "OFFROAD"
    LEARN = "LEARN"
    MANUAL = "MANUAL"


MODE_ORDER = [
    Mode.COMFORT,
    Mode.RACE,
    Mode.DRIFT,
    Mode.OFFROAD,
    Mode.LEARN,
    Mode.MANUAL,
]
