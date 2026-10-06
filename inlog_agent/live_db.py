"""Estado do turno que precisa sobreviver a restart.

Persistente, no disco do Render: /var/sagtrack/sagtrack_live.db
Temporário, pode ser recriado: OUTPUT_DIR (PDF, Word, GPKG, mapa.html e o JSON do relatório manual).
No Render, se OUTPUT_DIR não for mudado, esses arquivos continuam em
/opt/render/project/src/output e somem no deploy. Este módulo não os move.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT

DISCO = Path("/var/sagtrack")
NOME_BANCO = "sagtrack_live.db"
_DB = threading.Lock()


def no_render() -> bool:
    return (os.getenv("RENDER") or "").strip().lower() in {"1", "true", "yes", "sim"}


def caminho_banco(disco: Path | None = None) -> Path:
    """No Render o banco só pode existir dentro de /var/sagtrack."""
    alvo = disco if disco is not None else DISCO
    forçado = (os.getenv("SAGTRACK_LIVE_DB") or "").strip()
    if no_render():
        if not alvo.is_dir():
            raise RuntimeError(
                "Disco persistente /var/sagtrack não está montado. "
                "O turno ao vivo não será gravado em /opt/render/project/src/output."
            )
        destino = Path(forçado) if forçado else (alvo / NOME_BANCO)
        try:
            destino.resolve().relative_to(alvo.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "SAGTRACK_LIVE_DB precisa ficar em /var/sagtrack. "
                "Caminho recusado para não usar o disco temporário."
            ) from exc
        destino.parent.mkdir(parents=True, exist_ok=True)
        return destino
    if forçado:
        destino = Path(forçado)
        destino.parent.mkdir(parents=True, exist_ok=True)
        return destino
    if alvo.is_dir():
        return alvo / NOME_BANCO
    destino = ROOT / "output" / NOME_BANCO
    destino.parent.mkdir(parents=True, exist_ok=True)
    return destino


def provar_disco() -> str:
    """Escreve um carimbo no disco persistente. Não cria pasta se o mount não existe."""
    if not DISCO.is_dir():
        return "ausente"
    alvo = DISCO / "persistence_test.txt"
    try:
        alvo.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
        texto = alvo.read_text(encoding="utf-8")
    except OSError:
        return "sem_escrita"
    return "ok" if texto.strip() else "sem_leitura"


def _hora_diag(iso: str) -> str:
    texto = str(iso or "").strip()
    if "T" in texto:
        return texto.split("T", 1)[1][:8]
    if " " in texto and ":" in texto:
        return texto.split(" ", 1)[1][:8]
    return texto[:8]


def diagnostico() -> dict[str, Any]:
    """Estado operacional, sem credenciais."""
    from .live_sync import ocupada

    info: dict[str, Any] = {
        "persistent_disk": "ausente",
        "sqlite": "nao_aberto",
        "wal": "nao",
        "fulltrack": "nao_configurada",
        "live_sync": "inativo",
        "ultimo_sync": "",
        "sync_em_andamento": ocupada(),
        "banco": "",
        "erro": "",
    }
    try:
        from .config import load_settings

        settings = load_settings()
        if settings.fulltrack_username and settings.fulltrack_password:
            info["fulltrack"] = "configurada"
    except Exception as exc:
        info["erro"] = str(exc)
    try:
        from .live_sync import sincronizacao_ativa

        info["live_sync"] = "ativo" if sincronizacao_ativa() else "inativo"
        from .live_sync import ciclos_executados

        info["ciclos_gps"] = ciclos_executados()
        info["ciclos"] = info["ciclos_gps"]
    except Exception:
        info["live_sync"] = "inativo"
    if no_render() and not DISCO.is_dir():
        info["erro"] = (
            "Disco persistente /var/sagtrack não está montado. "
            "O painel ao vivo não vai gravar no disco temporário."
        )
        return info
    info["persistent_disk"] = "ok" if DISCO.is_dir() else "local"
    try:
        info["banco"] = str(caminho_banco())
        con = conectar()
        try:
            modo = str(con.execute("PRAGMA journal_mode").fetchone()[0] or "")
            info["sqlite"] = "ok"
            info["wal"] = "ok" if modo.lower() == "wal" else modo or "nao"
            from .live_sync import agora_brt, turno_operacional

            dia_op, turno_op = turno_operacional(agora_brt())
            info["data_operacional"] = dia_op.strftime("%d/%m/%Y")
            info["turno_ativo"] = turno_op.upper()
            row = con.execute(
                """
                SELECT data, turno, ultima_sincronizacao, falha, telemetria,
                       reconstrucao_em, reconstrucao_tentativas, reconstrucao_duracao_s,
                       reconstrucao_memoria_mb, reconstrucao_pontos,
                       situacao, consolidar_ate
                FROM turno
                WHERE data = ? AND turno = ?
                """,
                (dia_op.isoformat(), turno_op),
            ).fetchone()
            if row is None:
                info["data"] = dia_op.isoformat()
                info["turno"] = turno_op
                info["pontos"] = 0
                info["rotas"] = 0
                info["ultimo_sync_hora"] = ""
                info["ultimo_gps_hora"] = ""
                info["veiculos"] = 0
                info["veiculos_com_cursor"] = 0
                info["situacao_turno"] = ""
                info["consolidar_ate"] = ""
                from .live_turno import situacao_pelo_relogio

                info["situacao_turno"] = situacao_pelo_relogio(dia_op, turno_op) or "ABERTO"
            if row and row["ultima_sincronizacao"]:
                info["ultimo_sync"] = str(row["ultima_sincronizacao"])
            if row:
                dia = str(row["data"])
                turno = str(row["turno"])
                info["data"] = dia
                info["turno"] = turno
                info["falha"] = str(row["falha"] or "")
                info["reconstrucao_tentativas"] = int(row["reconstrucao_tentativas"] or 0)
                info["reconstrucao_duracao_s"] = float(row["reconstrucao_duracao_s"] or 0)
                info["reconstrucao_memoria_mb"] = float(row["reconstrucao_memoria_mb"] or 0)
                info["reconstrucao_pontos"] = int(row["reconstrucao_pontos"] or 0)
                info["pontos"] = int(
                    con.execute(
                        "SELECT COUNT(*) FROM ponto WHERE data = ? AND turno = ?",
                        (dia, turno),
                    ).fetchone()[0]
                )
                info["veiculos"] = int(
                    con.execute(
                        "SELECT COUNT(*) FROM placa WHERE data = ? AND turno = ?",
                        (dia, turno),
                    ).fetchone()[0]
                )
                info["veiculos_com_cursor"] = int(
                    con.execute(
                        """
                        SELECT COUNT(*) FROM placa
                        WHERE data = ? AND turno = ? AND last_gps_timestamp != ''
                        """,
                        (dia, turno),
                    ).fetchone()[0]
                )
                gps = con.execute(
                    """
                    SELECT MAX(last_gps_timestamp) FROM placa
                    WHERE data = ? AND turno = ?
                    """,
                    (dia, turno),
                ).fetchone()[0]
                info["ultimo_gps"] = str(gps or "")
                rotas = [
                    {
                        "na_grade": bool(item["na_grade"]),
                        "grupo": item["grupo"],
                        "percentual": item["percentual"],
                    }
                    for item in con.execute(
                        "SELECT na_grade, grupo, percentual FROM rota WHERE data = ? AND turno = ?",
                        (dia, turno),
                    )
                ]
                from .live_report import _resumo

                info["rotas"] = len(rotas)
                info["resumo"] = _resumo(rotas)
                info["situacao_turno"] = str(row["situacao"] or "")
                info["consolidar_ate"] = str(row["consolidar_ate"] or "")
                info["ultimo_sync_hora"] = _hora_diag(info.get("ultimo_sync") or "")
                info["ultimo_gps_hora"] = _hora_diag(info.get("ultimo_gps") or "")
            from .live_sync import metricas_ciclo, reconstruindo

            info["placas_com_cursor"] = int(info.get("veiculos_com_cursor") or 0)
            info["placas_sem_cursor"] = max(0, int(info.get("veiculos") or 0) - info["placas_com_cursor"])
            info["modo"] = "reconstrucao" if reconstruindo() else "incremental"
            info.update(metricas_ciclo())
            from .live_turno import candidatos

            fechando = [item for item in candidatos() if item[0] == "FINALIZANDO"]
            info["turno_finalizando"] = ""
            if fechando:
                _sit, dia_f, turno_f = fechando[0]
                extra = con.execute(
                    "SELECT situacao, consolidar_ate FROM turno WHERE data = ? AND turno = ?",
                    (dia_f.isoformat(), turno_f),
                ).fetchone()
                if extra is not None:
                    info["turno_finalizando"] = f"{dia_f.isoformat()} {turno_f}"
                    info["situacao_finalizando"] = str(extra["situacao"] or "")
                    info["consolidar_ate_finalizando"] = str(extra["consolidar_ate"] or "")
            caminho = Path(info["banco"])
            tamanho = 0
            for extra in (caminho, Path(str(caminho) + "-wal"), Path(str(caminho) + "-shm")):
                if extra.is_file():
                    tamanho += extra.stat().st_size
            info["tamanho_bytes"] = tamanho
            prova = DISCO / "persistence_test.txt"
            info["prova_escrita"] = "ok" if prova.is_file() and prova.read_text(encoding="utf-8").strip() else "ausente"
            if no_render() and not str(info["banco"]).startswith("/var/sagtrack"):
                info["sqlite"] = "erro"
                info["erro"] = "Banco fora de /var/sagtrack."
        finally:
            con.close()
    except Exception as exc:
        info["sqlite"] = "erro"
        info["erro"] = str(exc)
    return info


def plano_armazenamento() -> dict[str, str]:
    return {
        "persistente": str(caminho_banco()) if not no_render() or DISCO.is_dir() else "",
        "temporario": "OUTPUT_DIR: PDF, Word, GPKG, mapa.html e JSON do relatório manual",
        "efemero_render": "/opt/render/project/src/output",
    }


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    banco = caminho or caminho_banco()
    banco.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(banco), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    _schema(con)
    return con


def _schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS turno (
            data TEXT NOT NULL,
            turno TEXT NOT NULL,
            aberto INTEGER NOT NULL DEFAULT 1,
            periodo TEXT NOT NULL DEFAULT '',
            hora_inicio TEXT NOT NULL DEFAULT '',
            hora_fim TEXT NOT NULL DEFAULT '',
            data_fim TEXT NOT NULL DEFAULT '',
            ultima_sincronizacao TEXT NOT NULL DEFAULT '',
            ultima_fulltrack TEXT NOT NULL DEFAULT '',
            falha TEXT NOT NULL DEFAULT '',
            telemetria TEXT NOT NULL DEFAULT '',
            atualizado_em TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (data, turno)
        );
        CREATE TABLE IF NOT EXISTS rota (
            data TEXT NOT NULL,
            turno TEXT NOT NULL,
            codigo TEXT NOT NULL,
            rota TEXT NOT NULL DEFAULT '',
            frequencia TEXT NOT NULL DEFAULT '',
            veiculo TEXT NOT NULL DEFAULT '',
            placa TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            grupo TEXT NOT NULL DEFAULT '',
            percentual REAL NOT NULL DEFAULT 0,
            inicio TEXT NOT NULL DEFAULT '',
            ultimo_gps TEXT NOT NULL DEFAULT '',
            tempo_em_rota TEXT NOT NULL DEFAULT '',
            km_coleta TEXT NOT NULL DEFAULT '',
            km_oficial TEXT NOT NULL DEFAULT '',
            na_grade INTEGER NOT NULL DEFAULT 0,
            atualizado_em TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (data, turno, codigo)
        );
        CREATE TABLE IF NOT EXISTS placa (
            data TEXT NOT NULL,
            turno TEXT NOT NULL,
            placa TEXT NOT NULL,
            veiculo TEXT NOT NULL DEFAULT '',
            vehicle_id TEXT NOT NULL DEFAULT '',
            last_gps_timestamp TEXT NOT NULL DEFAULT '',
            ultimo_lat REAL,
            ultimo_lon REAL,
            telemetria TEXT NOT NULL DEFAULT '',
            atualizado_em TEXT NOT NULL DEFAULT '',
            consulta_ate TEXT NOT NULL DEFAULT '',
            consulta_falha_em TEXT NOT NULL DEFAULT '',
            consulta_falhas INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (data, turno, placa)
        );
        CREATE TABLE IF NOT EXISTS ponto (
            data TEXT NOT NULL,
            turno TEXT NOT NULL,
            placa TEXT NOT NULL,
            ts TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            PRIMARY KEY (data, turno, placa, ts)
        );
        """
    )
    colunas = {str(row[1]) for row in con.execute("PRAGMA table_info(turno)")}
    extras = {
        "reconstrucao_em": "TEXT NOT NULL DEFAULT ''",
        "reconstrucao_tentativas": "INTEGER NOT NULL DEFAULT 0",
        "reconstrucao_duracao_s": "REAL NOT NULL DEFAULT 0",
        "reconstrucao_memoria_mb": "REAL NOT NULL DEFAULT 0",
        "reconstrucao_pontos": "INTEGER NOT NULL DEFAULT 0",
        "situacao": "TEXT NOT NULL DEFAULT ''",
        "consolidar_ate": "TEXT NOT NULL DEFAULT ''",
        "aberto_em": "TEXT NOT NULL DEFAULT ''",
        "fechado_em": "TEXT NOT NULL DEFAULT ''",
        "fechado_origem": "TEXT NOT NULL DEFAULT ''",
        "primeiro_gps": "TEXT NOT NULL DEFAULT ''",
        "ultimo_gps_turno": "TEXT NOT NULL DEFAULT ''",
        "resumo_json": "TEXT NOT NULL DEFAULT ''",
    }
    for nome, tipo in extras.items():
        if nome not in colunas:
            con.execute(f"ALTER TABLE turno ADD COLUMN {nome} {tipo}")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS turno_evento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            turno TEXT NOT NULL,
            acao TEXT NOT NULL,
            quando TEXT NOT NULL,
            origem TEXT NOT NULL DEFAULT '',
            usuario TEXT,
            antes TEXT NOT NULL DEFAULT '',
            depois TEXT NOT NULL DEFAULT ''
        )
        """
    )
    colunas_rota = {str(row[1]) for row in con.execute("PRAGMA table_info(rota)")}
    extras_rota = {
        "entrada_lat": "REAL",
        "entrada_lon": "REAL",
        "entrada_hora": "TEXT",
        "saida_lat": "REAL",
        "saida_lon": "REAL",
        "saida_hora": "TEXT",
        "trechos_feitos": "TEXT",
        "trechos_faltando": "TEXT",
    }
    for nome, tipo in extras_rota.items():
        if nome not in colunas_rota:
            con.execute(f"ALTER TABLE rota ADD COLUMN {nome} {tipo}")
    colunas_placa = {str(row[1]) for row in con.execute("PRAGMA table_info(placa)")}
    extras_placa = {
        "consulta_ate": "TEXT NOT NULL DEFAULT ''",
        "consulta_falha_em": "TEXT NOT NULL DEFAULT ''",
        "consulta_falhas": "INTEGER NOT NULL DEFAULT 0",
        "posicao_em": "TEXT NOT NULL DEFAULT ''",
    }
    for nome, tipo in extras_placa.items():
        if nome not in colunas_placa:
            con.execute(f"ALTER TABLE placa ADD COLUMN {nome} {tipo}")
    con.commit()
    from .live_turno import classificar_vazios

    classificar_vazios(con)
    con.commit()


def agora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def turno_existe(con: sqlite3.Connection, data: str, turno: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM turno WHERE data = ? AND turno = ?",
        (data, turno),
    ).fetchone()
    return row is not None


def gravar_turno(con: sqlite3.Connection, campos: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT INTO turno (
            data, turno, aberto, periodo, hora_inicio, hora_fim, data_fim,
            ultima_sincronizacao, ultima_fulltrack, falha, telemetria, atualizado_em
        ) VALUES (
            :data, :turno, :aberto, :periodo, :hora_inicio, :hora_fim, :data_fim,
            :ultima_sincronizacao, :ultima_fulltrack, :falha, :telemetria, :atualizado_em
        )
        ON CONFLICT(data, turno) DO UPDATE SET
            aberto = CASE WHEN situacao = 'ENCERRADO' THEN aberto ELSE excluded.aberto END,
            periodo = excluded.periodo,
            hora_inicio = excluded.hora_inicio,
            hora_fim = excluded.hora_fim,
            data_fim = excluded.data_fim,
            ultima_sincronizacao = excluded.ultima_sincronizacao,
            ultima_fulltrack = excluded.ultima_fulltrack,
            falha = excluded.falha,
            telemetria = excluded.telemetria,
            atualizado_em = excluded.atualizado_em
        """,
        {
            "aberto": 1,
            "periodo": "",
            "hora_inicio": "",
            "hora_fim": "",
            "data_fim": "",
            "ultima_sincronizacao": "",
            "ultima_fulltrack": "",
            "falha": "",
            "telemetria": "",
            "atualizado_em": agora_iso(),
            **campos,
        },
    )


def garantir_rotas_previstas(
    con: sqlite3.Connection,
    data: str,
    turno: str,
    rotas: list[dict[str, Any]],
) -> list[str]:
    """Inclui a programação que ainda não existe. Não altera rota já gravada."""
    quando = agora_iso()
    inseridas: list[str] = []
    for rota in rotas:
        codigo = str(rota.get("codigo") or "").upper()
        if not codigo:
            continue
        cursor = con.execute(
            """
            INSERT INTO rota (
                data, turno, codigo, rota, frequencia, veiculo, placa, status, grupo,
                percentual, inicio, ultimo_gps, tempo_em_rota, km_coleta, km_oficial,
                na_grade, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, '', '', 'Não iniciada', 'nao', 0, '', '', '', '', '', 1, ?)
            ON CONFLICT(data, turno, codigo) DO NOTHING
            """,
            (
                data,
                turno,
                codigo,
                str(rota.get("rotulo") or rota.get("rota") or codigo),
                str(rota.get("frequencia") or ""),
                quando,
            ),
        )
        if cursor.rowcount:
            inseridas.append(codigo)
    return inseridas


def gravar_rotas(con: sqlite3.Connection, data: str, turno: str, rotas: list[dict[str, Any]]) -> None:
    quando = agora_iso()
    for rota in rotas:
        codigo = str(rota.get("codigo") or "").upper()
        if not codigo:
            continue
        tem_geo = 1 if rota.get("_geo") else 0
        con.execute(
            """
            INSERT INTO rota (
                data, turno, codigo, rota, frequencia, veiculo, placa, status, grupo,
                percentual, inicio, ultimo_gps, tempo_em_rota, km_coleta, km_oficial,
                na_grade, atualizado_em,
                entrada_lat, entrada_lon, entrada_hora, saida_lat, saida_lon, saida_hora,
                trechos_feitos, trechos_faltando
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(data, turno, codigo) DO UPDATE SET
                rota = excluded.rota,
                frequencia = excluded.frequencia,
                veiculo = excluded.veiculo,
                placa = excluded.placa,
                status = excluded.status,
                grupo = excluded.grupo,
                percentual = excluded.percentual,
                inicio = excluded.inicio,
                ultimo_gps = excluded.ultimo_gps,
                tempo_em_rota = excluded.tempo_em_rota,
                km_coleta = excluded.km_coleta,
                km_oficial = excluded.km_oficial,
                na_grade = excluded.na_grade,
                atualizado_em = excluded.atualizado_em,
                entrada_lat = CASE WHEN ? = 1 THEN excluded.entrada_lat ELSE rota.entrada_lat END,
                entrada_lon = CASE WHEN ? = 1 THEN excluded.entrada_lon ELSE rota.entrada_lon END,
                entrada_hora = CASE WHEN ? = 1 THEN excluded.entrada_hora ELSE rota.entrada_hora END,
                saida_lat = CASE WHEN ? = 1 THEN excluded.saida_lat ELSE rota.saida_lat END,
                saida_lon = CASE WHEN ? = 1 THEN excluded.saida_lon ELSE rota.saida_lon END,
                saida_hora = CASE WHEN ? = 1 THEN excluded.saida_hora ELSE rota.saida_hora END,
                trechos_feitos = CASE WHEN ? = 1 THEN excluded.trechos_feitos ELSE rota.trechos_feitos END,
                trechos_faltando = CASE WHEN ? = 1 THEN excluded.trechos_faltando ELSE rota.trechos_faltando END
            """,
            (
                data,
                turno,
                codigo,
                rota.get("rota") or "",
                rota.get("frequencia") or "",
                rota.get("veiculo") or "",
                rota.get("placa") or "",
                rota.get("status") or "",
                rota.get("grupo") or "",
                float(rota.get("percentual") or 0),
                rota.get("inicio") or "",
                rota.get("ultimo_gps") or "",
                rota.get("tempo_em_rota") or "",
                rota.get("km_coleta") or "",
                rota.get("km_oficial") or "",
                1 if rota.get("na_grade") else 0,
                quando,
                rota.get("entrada_lat"),
                rota.get("entrada_lon"),
                rota.get("entrada_hora") or "",
                rota.get("saida_lat"),
                rota.get("saida_lon"),
                rota.get("saida_hora") or "",
                rota.get("trechos_feitos") or "[]",
                rota.get("trechos_faltando") or "[]",
                tem_geo,
                tem_geo,
                tem_geo,
                tem_geo,
                tem_geo,
                tem_geo,
                tem_geo,
                tem_geo,
            ),
        )


def inserir_pontos(
    con: sqlite3.Connection,
    data: str,
    turno: str,
    placa: str,
    pontos: list[dict[str, Any]],
) -> int:
    novos = 0
    for ponto in pontos:
        ts = str(ponto.get("timestamp") or ponto.get("ts") or "").strip()
        if not ts:
            continue
        try:
            lat = float(ponto["latitude"] if "latitude" in ponto else ponto["lat"])
            lon = float(ponto["longitude"] if "longitude" in ponto else ponto["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        con.execute(
            """
            INSERT OR IGNORE INTO ponto (data, turno, placa, ts, lat, lon)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (data, turno, placa, ts, lat, lon),
        )
        novos += int(con.execute("SELECT changes()").fetchone()[0])
    return novos


def marcar_placa(
    con: sqlite3.Connection,
    data: str,
    turno: str,
    placa: str,
    *,
    veiculo: str = "",
    vehicle_id: str = "",
    last_gps_timestamp: str = "",
    lat: float | None = None,
    lon: float | None = None,
    telemetria: str = "",
    posicao_em: str = "",
) -> None:
    atual = con.execute(
        "SELECT veiculo, vehicle_id, last_gps_timestamp, ultimo_lat, ultimo_lon, telemetria FROM placa WHERE data = ? AND turno = ? AND placa = ?",
        (data, turno, placa),
    ).fetchone()
    con.execute(
        """
        INSERT INTO placa (
            data, turno, placa, veiculo, vehicle_id, last_gps_timestamp,
            ultimo_lat, ultimo_lon, telemetria, atualizado_em
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(data, turno, placa) DO UPDATE SET
            veiculo = excluded.veiculo,
            vehicle_id = excluded.vehicle_id,
            last_gps_timestamp = excluded.last_gps_timestamp,
            ultimo_lat = excluded.ultimo_lat,
            ultimo_lon = excluded.ultimo_lon,
            telemetria = excluded.telemetria,
            atualizado_em = excluded.atualizado_em
        """,
        (
            data,
            turno,
            placa,
            veiculo or (atual["veiculo"] if atual else ""),
            vehicle_id or (atual["vehicle_id"] if atual else ""),
            last_gps_timestamp or (atual["last_gps_timestamp"] if atual else ""),
            lat if lat is not None else (atual["ultimo_lat"] if atual else None),
            lon if lon is not None else (atual["ultimo_lon"] if atual else None),
            telemetria or (atual["telemetria"] if atual else ""),
            agora_iso(),
        ),
    )
    if posicao_em:
        con.execute(
            "UPDATE placa SET posicao_em = ? WHERE data = ? AND turno = ? AND placa = ?",
            (posicao_em, data, turno, placa),
        )


def registrar_consulta_sem_dados(
    con: sqlite3.Connection,
    data: str,
    turno: str,
    placa: str,
    consulta_ate: str,
) -> None:
    """Marca a janela ja consultada. Nao grava cursor GPS."""
    con.execute(
        """
        UPDATE placa SET
            consulta_ate = ?,
            consulta_falha_em = '',
            consulta_falhas = 0,
            telemetria = CASE
                WHEN last_gps_timestamp = '' THEN 'sem_dados'
                ELSE telemetria
            END,
            atualizado_em = ?
        WHERE data = ? AND turno = ? AND placa = ?
        """,
        (consulta_ate, agora_iso(), data, turno, placa),
    )


def registrar_falha_consulta(
    con: sqlite3.Connection,
    data: str,
    turno: str,
    placa: str,
    falha_em: str,
) -> None:
    """Falha real. Nao avanca a marca e nao vira sem dados."""
    con.execute(
        """
        UPDATE placa SET
            consulta_falha_em = ?,
            consulta_falhas = consulta_falhas + 1,
            atualizado_em = ?
        WHERE data = ? AND turno = ? AND placa = ?
        """,
        (falha_em, agora_iso(), data, turno, placa),
    )


def limpar_falha_consulta(con: sqlite3.Connection, data: str, turno: str, placa: str) -> None:
    con.execute(
        """
        UPDATE placa SET consulta_falha_em = '', consulta_falhas = 0
        WHERE data = ? AND turno = ? AND placa = ?
        """,
        (data, turno, placa),
    )


def ler_placas(con: sqlite3.Connection, data: str, turno: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM placa WHERE data = ? AND turno = ? ORDER BY placa",
        (data, turno),
    ).fetchall()


def ler_pontos_placa(con: sqlite3.Connection, data: str, turno: str, placa: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT ts, lat, lon FROM ponto WHERE data = ? AND turno = ? AND placa = ? ORDER BY ts",
        (data, turno, placa),
    ).fetchall()
    return [
        {"timestamp": row["ts"], "latitude": row["lat"], "longitude": row["lon"], "tz": "BRT"}
        for row in rows
    ]


def ler_view(
    con: sqlite3.Connection,
    data: str,
    turno: str,
) -> dict[str, Any] | None:
    cab = con.execute(
        "SELECT * FROM turno WHERE data = ? AND turno = ?",
        (data, turno),
    ).fetchone()
    if cab is None:
        return None
    rotas = [
        {
            "codigo": row["codigo"],
            "rota": row["rota"],
            "frequencia": row["frequencia"],
            "veiculo": row["veiculo"],
            "placa": row["placa"],
            "status": row["status"],
            "grupo": row["grupo"],
            "percentual": row["percentual"],
            "inicio": row["inicio"],
            "ultimo_gps": row["ultimo_gps"],
            "tempo_em_rota": row["tempo_em_rota"],
            "km_coleta": row["km_coleta"],
            "km_oficial": row["km_oficial"],
            "na_grade": bool(row["na_grade"]),
            "entrada_lat": row["entrada_lat"],
            "entrada_lon": row["entrada_lon"],
            "entrada_hora": row["entrada_hora"] or "",
            "saida_lat": row["saida_lat"],
            "saida_lon": row["saida_lon"],
            "saida_hora": row["saida_hora"] or "",
        }
        for row in con.execute(
            "SELECT * FROM rota WHERE data = ? AND turno = ? ORDER BY codigo",
            (data, turno),
        )
    ]
    posicoes = [
        {
            "placa": row["placa"],
            "veiculo": row["veiculo"],
            "lat": row["ultimo_lat"],
            "lon": row["ultimo_lon"],
            "ultimo_gps": row["last_gps_timestamp"],
            "posicao_em": row["posicao_em"] if "posicao_em" in row.keys() else "",
            "telemetria": row["telemetria"],
        }
        for row in ler_placas(con, data, turno)
        if row["ultimo_lat"] is not None and row["ultimo_lon"] is not None
    ]
    return {"turno": cab, "rotas": rotas, "posicoes": posicoes}


_COLUNAS_TURNO = {
    "aberto",
    "periodo",
    "hora_inicio",
    "hora_fim",
    "data_fim",
    "ultima_sincronizacao",
    "ultima_fulltrack",
    "falha",
    "telemetria",
    "atualizado_em",
    "reconstrucao_em",
    "reconstrucao_tentativas",
    "reconstrucao_duracao_s",
    "reconstrucao_memoria_mb",
    "reconstrucao_pontos",
}


def atualizar_turno(con: sqlite3.Connection, data: str, turno: str, **campos: Any) -> None:
    usar = {chave: valor for chave, valor in campos.items() if chave in _COLUNAS_TURNO}
    if not usar:
        return
    nomes = list(usar)
    sql = "UPDATE turno SET " + ", ".join(f"{nome} = ?" for nome in nomes) + " WHERE data = ? AND turno = ?"
    con.execute(sql, [usar[nome] for nome in nomes] + [data, turno])


def fechar_turno(con: sqlite3.Connection, data: str, turno: str) -> None:
    from datetime import date as data_tipo

    from .live_turno import encerrar_turno

    encerrar_turno(con, data_tipo.fromisoformat(data), turno, "automatico")
