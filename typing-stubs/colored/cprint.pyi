from .colored import Colored as Colored

def cprint(text: str, fore_256: int | str = '', back_256: int | str = '', fore_rgb: tuple = (255, 255, 255), back_rgb: tuple = (0, 0, 0), formatting: int | str = '', line_color: int | str = '', reset: bool = True, **kwargs) -> None: ...