"""Refaz as trilhas gravadas de um turno, uma vez.

A leitura do mapa não chama este script. Ele lê os pulsos, casa o caminho
na malha completa e grava a polilinha por cima da antiga.

O banco padrão no servidor é /var/sagtrack/sagtrack_live.db. Sem esse
arquivo, cai no banco local. O turno pedido é o gravado em ponto.

Uso:
  py -3.14 scripts/refazer_trilhas.py --data 2026-11-12 --turno diurno
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _banco(pedido: str) -> str:
    if pedido:
        return str(Path(pedido))
    persistente = Path("/var/sagtrack/sagtrack_live.db")
    if persistente.is_file():
        return str(persistente)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Refaz as trilhas de um turno fora da leitura do mapa.")
    parser.add_argument("--data", required=True, help="Dia operacional, AAAA-MM-DD")
    parser.add_argument("--turno", default="diurno", choices=("diurno", "noturno"))
    parser.add_argument("--db", default="", help="Caminho do sagtrack_live.db. Vazio usa o disco persistente.")
    args = parser.parse_args()
    banco = _banco(args.db)
    if Path("/var/sagtrack").is_dir() and not banco:
        print("banco ausente /var/sagtrack/sagtrack_live.db")
        return 1
    if banco and not Path(banco).is_file():
        print(f"banco ausente {banco}")
        return 1
    if banco:
        os.environ["SAGTRACK_LIVE_DB"] = banco
    raiz = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(raiz))

    from inlog_agent.live_db import caminho_banco, conectar
    from inlog_agent.live_mapa import atualizar_trilha

    con = conectar()
    print(f"banco {caminho_banco()}")
    try:
        turno = str(args.turno or "").strip().lower()
        placas = [
            str(row["placa"] or "")
            for row in con.execute(
                "SELECT DISTINCT placa FROM ponto WHERE data = ? AND turno = ? ORDER BY placa",
                (args.data, turno),
            )
        ]
        placas = [placa for placa in placas if placa]
        print(f"{args.data} {turno}: {len(placas)} placas")
        if not placas:
            vistos = con.execute(
                "SELECT data, turno, COUNT(DISTINCT placa) AS placas FROM ponto GROUP BY data, turno ORDER BY data, turno"
            ).fetchall()
            for row in vistos:
                print(f"existe {row['data']} {row['turno']}: {row['placas']} placas")
            return 1
        for indice, placa in enumerate(placas, start=1):
            atualizar_trilha(con, args.data, turno, placa, completo=True)
            print(f"{indice}/{len(placas)} {placa}")
    finally:
        con.close()
    print("trilhas regravadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
