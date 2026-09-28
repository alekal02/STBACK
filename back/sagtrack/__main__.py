from __future__ import annotations

import argparse
from datetime import date

from .config import load_settings
from .motor import executar
from .server import servir


def main() -> None:
    parser = argparse.ArgumentParser(prog="sagtrack", description="Cobertura das rotas Segtrack em Aparecida.")
    sub = parser.add_subparsers(dest="comando", required=True)

    web = sub.add_parser("servir", help="Abre o painel da sala técnica")
    web.add_argument("--porta", type=int)
    web.add_argument("--host", default="0.0.0.0")

    coletar = sub.add_parser("coletar", help="Baixa os pulsos e calcula a cobertura do dia")
    coletar.add_argument("--data", help="YYYY-MM-DD (padrão: hoje)")
    coletar.add_argument("--hora-inicio", default=None)
    coletar.add_argument("--hora-fim", default=None)
    args = parser.parse_args()

    if args.comando == "servir":
        servir(porta=args.porta, host=args.host)
        return

    settings = load_settings()
    if args.data:
        from .config import Settings

        settings = Settings(**{**settings.__dict__, "data_rota": date.fromisoformat(args.data)})
    if args.hora_inicio or args.hora_fim:
        from .config import aplicar_periodo

        settings = aplicar_periodo(settings, hora_inicio=args.hora_inicio, hora_fim=args.hora_fim)
    executar(settings)


if __name__ == "__main__":
    main()
