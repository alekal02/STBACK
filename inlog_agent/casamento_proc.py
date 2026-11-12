"""Casa pulsos novos fora do processo da API.

A malha sobe uma vez neste processo. A sessão, o status, a virada e o
/tracks só leem o que este processo já gravou.
"""

from __future__ import annotations

import os
import time
from typing import Any


def turnos_abertos(con: Any) -> list[tuple[str, str]]:
    from .live_sync import turno_operacional

    vistos: list[tuple[str, str]] = []
    dia, turno = turno_operacional()
    vistos.append((dia.isoformat(), turno))
    try:
        for row in con.execute("SELECT data, turno FROM turno WHERE situacao = 'ABERTO'"):
            chave = (str(row["data"] or ""), str(row["turno"] or ""))
            if chave[0] and chave[1] and chave not in vistos:
                vistos.append(chave)
    except Exception:
        return vistos
    return vistos


def placas_pendentes(con: Any, data: str, turno: str) -> list[str]:
    try:
        rows = con.execute(
            """
            SELECT p.placa AS placa, MAX(p.ts) AS ultimo, IFNULL(t.ate, '') AS ate
            FROM ponto p
            LEFT JOIN trilha t
              ON t.data = p.data AND t.turno = p.turno AND t.placa = p.placa
            WHERE p.data = ? AND p.turno = ?
            GROUP BY p.placa
            HAVING ate = '' OR ultimo > ate
            """,
            (data, turno),
        ).fetchall()
    except Exception:
        return []
    return [str(row["placa"] or "") for row in rows if str(row["placa"] or "")]


def passo(malha: Any) -> int:
    """Casa só a placa com pulso depois do trecho gravado. Devolve quantas andou."""
    from .live_db import conectar
    from .live_mapa import atualizar_trilha

    con = conectar()
    feitas = 0
    try:
        for data, turno in turnos_abertos(con):
            for placa in placas_pendentes(con, data, turno):
                atualizar_trilha(con, data, turno, placa, completo=False, rede=malha)
                feitas += 1
    finally:
        con.close()
    return feitas


def main() -> None:
    os.environ["SAGTRACK_CASAMENTO_PROC"] = "1"
    from .trajetoria import rede_oficial

    print("Casamento: carregando a malha uma vez.", flush=True)
    malha = rede_oficial()
    print(f"Casamento: malha com {len(malha.linhas)} peças.", flush=True)
    while True:
        try:
            feitas = passo(malha)
            if feitas:
                print(f"Casamento: {feitas} placas com pulso novo.", flush=True)
        except Exception as exc:
            print(f"Casamento: {exc}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
