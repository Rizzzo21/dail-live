from dataclasses import dataclass, field

@dataclass
class World:
    tick: int = 0
    resources: dict = field(default_factory=lambda: {"energy": 1000, "food": 1000})
    events: list = field(default_factory=list)

    def step(self):
        self.tick += 1
        self.resources["energy"] = max(0, self.resources["energy"] - 1)
        event = {"tick": self.tick, "type": "world_tick"}
        self.events.append(event)
        return event
