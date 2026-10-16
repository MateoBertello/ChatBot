"""Arranca el simulador: ``python -m app.simulador [--puerto 8080] [--no-abrir]``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path


def cargar_env(path: Path) -> None:
    """Carga un archivo .env simple (CLAVE=valor) sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valor = valor.split(" #", 1)[0].strip().strip('"').strip("'")
        if valor:
            os.environ.setdefault(clave.strip(), valor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulador local del bot de WhatsApp (sin Chatwoot ni Meta).")
    parser.add_argument("--puerto", type=int, default=8080)
    parser.add_argument("--no-abrir", action="store_true", help="no abrir el navegador automáticamente")
    args = parser.parse_args()

    cargar_env(Path(".env"))
    # Valores que en el simulador no se usan: Chatwoot está simulado en memoria.
    os.environ.setdefault("CHATWOOT_URL", "http://simulador.local")
    os.environ.setdefault("CHATWOOT_BOT_TOKEN", "simulador")
    os.environ.setdefault("WEBHOOK_SECRET", "simulador")
    os.environ["USAGE_FILE"] = ""

    from ..config import get_settings
    from .web import create_sim_app

    try:
        settings = get_settings()
    except RuntimeError as exc:
        sys.exit(
            f"{exc}.\n\nDefiní una API key antes de arrancar, por ejemplo:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...          (Anthropic directo)\n"
            "  export OPENROUTER_API_KEY=sk-or-...          (OpenRouter)\n"
            "o agregala a un archivo .env en esta carpeta."
        )
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = create_sim_app(settings)
    except RuntimeError as exc:  # p. ej. transcripción mal configurada
        sys.exit(str(exc))

    url = f"http://127.0.0.1:{args.puerto}"
    from ..llm import provider_name

    print(
        f"\n  Simulador del bot listo en {url}\n"
        f"  Modelo: {settings.claude_model} ({provider_name(settings)})\n"
        f"  Clientes: {settings.clients_dir.resolve()}\n  Ctrl+C para salir.\n"
    )
    if not args.no_abrir:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.puerto, log_level="warning")


if __name__ == "__main__":
    main()
