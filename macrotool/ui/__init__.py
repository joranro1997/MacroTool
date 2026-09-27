"""Interfaz gráfica de MacroTool (PySide6, tema oscuro).

Módulos:
- ``theme``: paleta, hoja de estilos, iconos y el icono de la aplicación.
- ``widgets``: widgets reutilizables (interruptor, captura de combinaciones, listas…).
- ``step_dialog``: diálogo para crear/editar pasos (``edit_step``).
- ``main_window``: ventana principal (``MainWindow``), ajustes y bandeja del sistema.
"""
from __future__ import annotations

from typing import Any

__all__ = ["MainWindow", "apply_theme", "edit_step"]


def __getattr__(name: str) -> Any:
    # Importación perezosa: importar ``macrotool.ui`` no carga Qt ni el motor.
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name == "apply_theme":
        from .theme import apply_theme

        return apply_theme
    if name == "edit_step":
        from .step_dialog import edit_step

        return edit_step
    raise AttributeError(name)
