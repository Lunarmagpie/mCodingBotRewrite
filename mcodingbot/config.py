import inspect
import json
import dataclasses
import pathlib
import typing as t

_ALWAYS_SAVE = ["discord_token"]
_CONFIG_PATH = "config.json"

@dataclasses.dataclass
class Config:
    discord_token: str = "DISCORD_TOKEN"

    def save(self) -> None:
        pth = pathlib.Path(_CONFIG_PATH)

        dct = dataclasses.asdict(self)
        to_save: t.Dict[str, t.Any] = {}
        defaults = type(self)

        for k, v in dct.items():
            if k in _ALWAYS_SAVE or getattr(defaults, k) != v:
                to_save[k] = v

        with pth.open("w+") as f:
            f.write(json.dumps(to_save, indent=4))

    @classmethod
    def load(cls) -> "Config":
        pth = pathlib.Path(_CONFIG_PATH)

        if not pth.exists():
            c = cls()
            return

        keys = set(inspect.signature(cls).parameters)

        with pth.open("r") as f:
            c = cls(
                **{
                    k: v
                    for k, v in t.cast(
                        "t.Dict[t.Any, t.Any]", json.loads(f.read())
                    ).items()
                    if k in keys
                }
            )

        c.save()

        return c


CONFIG = Config.load()
