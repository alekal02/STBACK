"""Refaz as trilhas gravadas de um turno, uma vez.

A leitura do mapa não chama este script. Ele lê os pulsos, casa o caminho
na malha completa e grava a polilinha por cima da antiga.

Uso:
  py -3.14 scripts/refazer_trilhas.py --data 2026-11-11 --turno diurno
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Refaz as trilhas de um turno fora da leitura do mapa.")
    parser.add_argument("--data", required=True, help="Dia operacional, AAAA-MM-DD")
    parser.add_argument("--turno", default="diurno", choices=("diurno", "noturno"))
    parser.add_argument("--db", default="", help="Caminho do sagtrack_live.db. Vazio usa o banco local.")
    args = parser.parse_args()
    if args.db:
        os.environ["SAGTRACK_LIVE_DB"] = str(Path(args.db))
    raiz = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(raiz))

    from inlog_agent.live_db import conectar
    from inlog_agent.live_mapa import atualizar_trilha

    con = conectar()
    try:
        placas = [
            str(row["placa"] or "")
            for row in con.execute(
                "SELECT DISTINCT placa FROM ponto WHERE data = ? AND turno = ? ORDER BY placa",
                (args.data, args.turno),
            )
        ]
        placas = [placa for placa in placas if placa]
        print(f"{args.data} {args.turno}: {len(placas)} placas")
        for indice, placa in enumerate(placas, start=1):
            atualizar_trilha(con, args.data, args.turno, placa, completo=True)
            print(f"{indice}/{len(placas)} {placa}")
    finally:
        con.close()
    print("trilhas regravadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
