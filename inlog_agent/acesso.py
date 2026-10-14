"""Acesso ao painel Rota ao Vivo.

Banco próprio (sagtrack_acesso.db), no mesmo disco persistente do Render.
Não abre e não altera o SQLite de turnos, pontos, trajetórias ou cobertura.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import ROOT

DISCO = Path("/var/sagtrack")
NOME_BANCO = "sagtrack_acesso.db"
BRT = ZoneInfo("America/Sao_Paulo")

ABSOLUTE = timedelta(hours=12)
IDLE = timedelta(hours=2)
RECENTE = timedelta(minutes=3)
TOQUE = timedelta(seconds=60)
JANELA_TENTATIVA = timedelta(minutes=15)
TENTATIVAS_LOGIN = 8
TENTATIVAS_IP = 30
SENHA_MIN = 10
SENHA_MAX = 128
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

_LOCK = threading.Lock()
_DUMMY: str | None = None

ROTULOS = {
    "login_ok": "Login",
    "login_falha": "Tentativa recusada",
    "login_bloqueado": "Tentativa de usuário bloqueado",
    "logout": "Logout",
    "sessao_expirada": "Sessão expirada",
    "acesso_negado": "Acesso negado",
}


class ErroAcesso(Exception):
    def __init__(self, status: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


def no_render() -> bool:
    return (os.getenv("RENDER") or "").strip().lower() in {"1", "true", "yes", "sim"}


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(momento: datetime | None = None) -> str:
    return (momento or _agora()).isoformat(timespec="seconds")


def _parse(valor: str) -> datetime:
    texto = (valor or "").strip()
    if not texto:
        return datetime.fromtimestamp(0, timezone.utc)
    momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento


def fmt_brt(valor: str) -> str:
    texto = (valor or "").strip()
    if not texto:
        return ""
    return _parse(texto).astimezone(BRT).strftime("%d/%m/%Y %H:%M")


def caminho_banco() -> Path:
    """Arquivo novo. Recusa caminho fora de /var/sagtrack quando está no Render."""
    forçado = (os.getenv("SAGTRACK_AUTH_DB") or "").strip()
    if no_render():
        if not DISCO.is_dir():
            raise RuntimeError(
                "Disco persistente /var/sagtrack não está montado. "
                "O acesso não será gravado no disco temporário."
            )
        destino = Path(forçado) if forçado else (DISCO / NOME_BANCO)
        try:
            destino.resolve().relative_to(DISCO.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "SAGTRACK_AUTH_DB precisa ficar em /var/sagtrack."
            ) from exc
        destino.parent.mkdir(parents=True, exist_ok=True)
        return destino
    if forçado:
        destino = Path(forçado)
        destino.parent.mkdir(parents=True, exist_ok=True)
        return destino
    destino = ROOT / "output" / NOME_BANCO
    destino.parent.mkdir(parents=True, exist_ok=True)
    return destino


def _conectar() -> sqlite3.Connection:
    banco = caminho_banco()
    con = sqlite3.connect(str(banco), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    _migrar(con)
    return con


@contextmanager
def banco() -> Iterator[sqlite3.Connection]:
    with _LOCK:
        con = _conectar()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def _colunas(con: sqlite3.Connection, tabela: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({tabela})")}


def _adicionar(con: sqlite3.Connection, tabela: str, colunas: dict[str, str]) -> None:
    atuais = _colunas(con, tabela)
    if not atuais:
        return
    for nome, tipo in colunas.items():
        if nome not in atuais:
            con.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")


def _migrar(con: sqlite3.Connection) -> None:
    """Migração aditiva: cria o que falta e acrescenta colunas. Não apaga dados."""
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS acesso_meta (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usuario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            login TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            ultimo_login_em TEXT NOT NULL DEFAULT '',
            ultimo_acesso_em TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            csrf TEXT NOT NULL,
            criada_em TEXT NOT NULL,
            expira_em TEXT NOT NULL,
            ultima_atividade_em TEXT NOT NULL,
            revogada_em TEXT NOT NULL DEFAULT '',
            expiracao_registrada INTEGER NOT NULL DEFAULT 0,
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (usuario_id) REFERENCES usuario(id)
        );
        CREATE TABLE IF NOT EXISTS acesso_evento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quando TEXT NOT NULL,
            tipo TEXT NOT NULL,
            login TEXT NOT NULL DEFAULT '',
            usuario_id INTEGER,
            ip TEXT NOT NULL DEFAULT '',
            detalhe TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sessao_token ON sessao(token_hash);
        CREATE INDEX IF NOT EXISTS idx_sessao_usuario ON sessao(usuario_id);
        CREATE INDEX IF NOT EXISTS idx_evento_quando ON acesso_evento(quando);
        CREATE INDEX IF NOT EXISTS idx_evento_login ON acesso_evento(login, quando);
        """
    )
    _adicionar(
        con,
        "usuario",
        {
            "nome": "TEXT NOT NULL DEFAULT ''",
            "login": "TEXT NOT NULL DEFAULT ''",
            "senha_hash": "TEXT NOT NULL DEFAULT ''",
            "perfil": "TEXT NOT NULL DEFAULT 'operador'",
            "ativo": "INTEGER NOT NULL DEFAULT 1",
            "criado_em": "TEXT NOT NULL DEFAULT ''",
            "atualizado_em": "TEXT NOT NULL DEFAULT ''",
            "ultimo_login_em": "TEXT NOT NULL DEFAULT ''",
            "ultimo_acesso_em": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _adicionar(
        con,
        "sessao",
        {
            "usuario_id": "INTEGER NOT NULL DEFAULT 0",
            "token_hash": "TEXT NOT NULL DEFAULT ''",
            "csrf": "TEXT NOT NULL DEFAULT ''",
            "criada_em": "TEXT NOT NULL DEFAULT ''",
            "expira_em": "TEXT NOT NULL DEFAULT ''",
            "ultima_atividade_em": "TEXT NOT NULL DEFAULT ''",
            "revogada_em": "TEXT NOT NULL DEFAULT ''",
            "expiracao_registrada": "INTEGER NOT NULL DEFAULT 0",
            "ip": "TEXT NOT NULL DEFAULT ''",
            "user_agent": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _adicionar(
        con,
        "acesso_evento",
        {
            "quando": "TEXT NOT NULL DEFAULT ''",
            "tipo": "TEXT NOT NULL DEFAULT ''",
            "login": "TEXT NOT NULL DEFAULT ''",
            "usuario_id": "INTEGER",
            "ip": "TEXT NOT NULL DEFAULT ''",
            "detalhe": "TEXT NOT NULL DEFAULT ''",
        },
    )
    con.execute(
        """
        INSERT INTO acesso_meta (chave, valor) VALUES ('schema', '1')
        ON CONFLICT(chave) DO UPDATE SET valor = '1'
        """
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    chave = hashlib.scrypt(
        senha.encode("utf-8"),
        salt=sal,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${sal.hex()}${chave.hex()}"


def _dummy() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_senha("dummy-rota-ao-vivo")
    return _DUMMY


def verificar_senha(senha: str, armazenada: str) -> bool:
    try:
        algoritmo, n, r, p, sal_hex, chave_hex = (armazenada or "").split("$", 5)
        if algoritmo != "scrypt":
            return False
        calculada = hashlib.scrypt(
            senha.encode("utf-8"),
            salt=bytes.fromhex(sal_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(chave_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calculada, bytes.fromhex(chave_hex))


def normalizar_login(valor: str) -> str:
    return " ".join((valor or "").split()).strip().lower()


def validar_senha(senha: str, login: str, nome: str = "") -> None:
    if not isinstance(senha, str) or not senha or senha.strip() != senha:
        raise ErroAcesso(400, "Informe uma senha sem espaços no começo ou no fim.")
    if len(senha) < SENHA_MIN:
        raise ErroAcesso(400, "A senha precisa ter pelo menos 10 caracteres.")
    if len(senha) > SENHA_MAX:
        raise ErroAcesso(400, "A senha é longa demais.")
    proibidos = {normalizar_login(login), normalizar_login(nome)}
    proibidos.discard("")
    if senha.casefold() in proibidos:
        raise ErroAcesso(400, "A senha não pode ser igual ao usuário.")


def _limpar_texto(valor: str, limite: int) -> str:
    texto = " ".join(str(valor or "").split())
    return texto[:limite]


def _evento(
    con: sqlite3.Connection,
    tipo: str,
    *,
    login: str = "",
    usuario_id: int | None = None,
    ip: str = "",
    detalhe: str = "",
) -> None:
    seguro = _limpar_texto(detalhe, 180)
    baixas = seguro.casefold()
    if "senha" in baixas or "token" in baixas or "csrf" in baixas:
        seguro = ""
    con.execute(
        """
        INSERT INTO acesso_evento (quando, tipo, login, usuario_id, ip, detalhe)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (_iso(), tipo, normalizar_login(login)[:120], usuario_id, _limpar_texto(ip, 64), seguro),
    )


def _foto(row: sqlite3.Row) -> dict[str, Any]:
    return {str(chave): row[chave] for chave in row.keys()}


def publico(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    perfil = str(row["perfil"] or "")
    return {
        "id": int(row["id"]),
        "nome": str(row["nome"] or ""),
        "login": str(row["login"] or ""),
        "perfil": perfil,
        "perfil_rotulo": "Administrador" if perfil == "admin" else "Operador",
        "ativo": bool(row["ativo"]),
    }


def auth_exigida() -> bool:
    """Exige login quando há administrador ativo, salvo desligamento explícito.

    AUTH_ENFORCE=0 mantém a API operacional aberta (não usar depois da validação).
    AUTH_ENFORCE=1 exige login mesmo antes do primeiro administrador.
    Vazio: só exige depois que o primeiro administrador existir.
    """
    flag = (os.getenv("AUTH_ENFORCE") or "").strip().lower()
    if flag in {"0", "false", "nao", "não", "off"}:
        return False
    if flag in {"1", "true", "yes", "sim", "on"}:
        return True
    with banco() as con:
        row = con.execute(
            "SELECT 1 FROM usuario WHERE perfil = 'admin' AND ativo = 1 LIMIT 1"
        ).fetchone()
    return row is not None


def existe_admin() -> bool:
    with banco() as con:
        row = con.execute(
            "SELECT 1 FROM usuario WHERE perfil = 'admin' AND ativo = 1 LIMIT 1"
        ).fetchone()
    return row is not None


def _falhas(con: sqlite3.Connection, *, login: str = "", ip: str = "") -> int:
    desde = _iso(_agora() - JANELA_TENTATIVA)
    if login:
        row = con.execute(
            """
            SELECT COUNT(*) FROM acesso_evento
            WHERE tipo IN ('login_falha', 'login_bloqueado') AND login = ? AND quando >= ?
            """,
            (login, desde),
        ).fetchone()
        return int(row[0] if row else 0)
    row = con.execute(
        """
        SELECT COUNT(*) FROM acesso_evento
        WHERE tipo IN ('login_falha', 'login_bloqueado') AND ip = ? AND quando >= ?
        """,
        (ip, desde),
    ).fetchone()
    return int(row[0] if row else 0)


def _chaves_conta(row: sqlite3.Row | dict[str, Any]) -> set[str]:
    chaves = set()
    for campo in ("login", "nome"):
        chave = normalizar_login(str(row[campo] or ""))
        if chave:
            chaves.add(chave)
    return chaves


def _localizar(con: sqlite3.Connection, chave: str) -> list[sqlite3.Row]:
    return [
        row
        for row in con.execute("SELECT * FROM usuario")
        if chave in _chaves_conta(row)
    ]


def _recusar_conflito(
    con: sqlite3.Connection,
    login: str,
    nome: str,
    *,
    exceto: int | None = None,
) -> None:
    novos = {normalizar_login(login), normalizar_login(nome)}
    novos.discard("")
    for row in con.execute("SELECT id, login, nome FROM usuario"):
        if exceto is not None and int(row["id"]) == exceto:
            continue
        if novos & _chaves_conta(row):
            raise ErroAcesso(409, "Já existe um usuário com esse acesso.")


def _limite_chaves(con: sqlite3.Connection, chaves: set[str], ip: str) -> None:
    marcas = tuple(sorted(chave for chave in chaves if chave))
    if marcas:
        desde = _iso(_agora() - JANELA_TENTATIVA)
        pontos = ",".join("?" * len(marcas))
        row = con.execute(
            f"""
            SELECT COUNT(*) FROM acesso_evento
            WHERE tipo IN ('login_falha', 'login_bloqueado')
              AND quando >= ?
              AND login IN ({pontos})
            """,
            (desde, *marcas),
        ).fetchone()
        if int(row[0] if row else 0) >= TENTATIVAS_LOGIN:
            raise ErroAcesso(429, "Muitas tentativas. Aguarde 15 minutos e tente de novo.")
    if ip and _falhas(con, ip=ip) >= TENTATIVAS_IP:
        raise ErroAcesso(429, "Muitas tentativas. Aguarde 15 minutos e tente de novo.")


def _sessao_valida(row: sqlite3.Row, agora: datetime) -> bool:
    if str(row["revogada_em"] or ""):
        return False
    if _parse(str(row["expira_em"] or "")) <= agora:
        return False
    atividade = _parse(str(row["ultima_atividade_em"] or row["criada_em"] or ""))
    if agora - atividade > IDLE:
        return False
    return True


def _presenca(validas: list[sqlite3.Row], agora: datetime) -> str:
    if not validas:
        return "sem_sessao"
    recente = False
    for row in validas:
        atividade = _parse(str(row["ultima_atividade_em"] or ""))
        if agora - atividade <= RECENTE:
            recente = True
            break
    if recente:
        return "ativo_agora"
    return "sessao_aberta"


def entrar(login_bruto: str, senha: str, *, ip: str = "", user_agent: str = "") -> dict[str, Any]:
    login = normalizar_login(login_bruto)
    if not login or not isinstance(senha, str) or not senha:
        raise ErroAcesso(400, "Informe usuário e senha.")
    if len(senha) > SENHA_MAX:
        raise ErroAcesso(400, "Usuário ou senha incorretos.")
    with banco() as con:
        achados = _localizar(con, login)
        row = _foto(achados[0]) if len(achados) == 1 else None
        chaves = {login}
        if row is not None:
            chaves |= _chaves_conta(row)
        _limite_chaves(con, chaves, ip)
    armazenada = str(row["senha_hash"]) if row else _dummy()
    confere = verificar_senha(senha, armazenada)
    if row is None or not confere:
        with banco() as con:
            _limite_chaves(con, chaves, ip)
            _evento(con, "login_falha", login=login, ip=ip, detalhe="credencial recusada")
        raise ErroAcesso(401, "Usuário ou senha incorretos.")
    if not int(row["ativo"]):
        with banco() as con:
            _evento(
                con,
                "login_bloqueado",
                login=login,
                usuario_id=int(row["id"]),
                ip=ip,
                detalhe="usuário bloqueado",
            )
        raise ErroAcesso(403, "Usuário bloqueado. Fale com o administrador.")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    agora = _agora()
    with banco() as con:
        _limite_chaves(con, chaves, ip)
        con.execute(
            """
            INSERT INTO sessao (
                usuario_id, token_hash, csrf, criada_em, expira_em,
                ultima_atividade_em, revogada_em, expiracao_registrada, ip, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, '', 0, ?, ?)
            """,
            (
                int(row["id"]),
                _hash_token(token),
                csrf,
                _iso(agora),
                _iso(agora + ABSOLUTE),
                _iso(agora),
                _limpar_texto(ip, 64),
                _limpar_texto(user_agent, 180),
            ),
        )
        con.execute(
            """
            UPDATE usuario
            SET ultimo_login_em = ?, ultimo_acesso_em = ?, atualizado_em = ?
            WHERE id = ?
            """,
            (_iso(agora), _iso(agora), _iso(agora), int(row["id"])),
        )
        _evento(con, "login_ok", login=login, usuario_id=int(row["id"]), ip=ip, detalhe="entrada")
        atual = con.execute("SELECT * FROM usuario WHERE id = ?", (int(row["id"]),)).fetchone()
        foto = _foto(atual)
    return {"token": token, "csrf": csrf, "usuario": publico(foto)}


def resolver(token: str, *, ip: str = "") -> tuple[dict[str, Any] | None, str]:
    bruto = (token or "").strip()
    if not bruto:
        return None, "ausente"
    with banco() as con:
        row = con.execute(
            """
            SELECT s.id AS sessao_id, s.csrf, s.criada_em, s.expira_em,
                   s.ultima_atividade_em, s.revogada_em, s.expiracao_registrada,
                   u.id, u.nome, u.login, u.perfil, u.ativo
            FROM sessao s
            JOIN usuario u ON u.id = s.usuario_id
            WHERE s.token_hash = ?
            """,
            (_hash_token(bruto),),
        ).fetchone()
        if row is None:
            return None, "ausente"
        agora = _agora()
        if str(row["revogada_em"] or ""):
            return None, "revogada"
        expirou = _parse(str(row["expira_em"] or "")) <= agora
        ociosa = agora - _parse(str(row["ultima_atividade_em"] or row["criada_em"] or "")) > IDLE
        if expirou or ociosa:
            if not int(row["expiracao_registrada"] or 0):
                con.execute(
                    "UPDATE sessao SET revogada_em = ?, expiracao_registrada = 1 WHERE id = ?",
                    (_iso(agora), int(row["sessao_id"])),
                )
                _evento(
                    con,
                    "sessao_expirada",
                    login=str(row["login"] or ""),
                    usuario_id=int(row["id"]),
                    ip=ip,
                    detalhe="sessão encerrada por tempo",
                )
            return None, "expirada"
        if not int(row["ativo"]):
            con.execute(
                "UPDATE sessao SET revogada_em = ? WHERE id = ? AND revogada_em = ''",
                (_iso(agora), int(row["sessao_id"])),
            )
            return None, "bloqueado"
        return {
            "sessao_id": int(row["sessao_id"]),
            "id": int(row["id"]),
            "nome": str(row["nome"] or ""),
            "login": str(row["login"] or ""),
            "perfil": str(row["perfil"] or ""),
            "csrf": str(row["csrf"] or ""),
            "ativo": True,
        }, ""


def tocar(sessao_id: int, usuario_id: int) -> None:
    agora = _agora()
    with banco() as con:
        row = con.execute(
            "SELECT ultima_atividade_em FROM sessao WHERE id = ? AND revogada_em = ''",
            (sessao_id,),
        ).fetchone()
        if row is None:
            return
        if agora - _parse(str(row["ultima_atividade_em"] or "")) < TOQUE:
            return
        marca = _iso(agora)
        con.execute(
            "UPDATE sessao SET ultima_atividade_em = ? WHERE id = ?",
            (marca, sessao_id),
        )
        con.execute(
            "UPDATE usuario SET ultimo_acesso_em = ? WHERE id = ?",
            (marca, usuario_id),
        )


def csrf_confere(esperado: str, recebido: str) -> bool:
    if not esperado or not recebido:
        return False
    a = esperado.encode("utf-8")
    b = recebido.encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def encerrar(token: str, *, ip: str = "") -> None:
    bruto = (token or "").strip()
    if not bruto:
        return
    with banco() as con:
        row = con.execute(
            """
            SELECT s.id AS sessao_id, u.id, u.login
            FROM sessao s JOIN usuario u ON u.id = s.usuario_id
            WHERE s.token_hash = ? AND s.revogada_em = ''
            """,
            (_hash_token(bruto),),
        ).fetchone()
        if row is None:
            return
        con.execute(
            "UPDATE sessao SET revogada_em = ? WHERE id = ?",
            (_iso(), int(row["sessao_id"])),
        )
        _evento(
            con,
            "logout",
            login=str(row["login"] or ""),
            usuario_id=int(row["id"]),
            ip=ip,
            detalhe="saída",
        )


def negar(usuario: dict[str, Any], *, ip: str = "") -> None:
    with banco() as con:
        _evento(
            con,
            "acesso_negado",
            login=str(usuario.get("login") or ""),
            usuario_id=int(usuario["id"]),
            ip=ip,
            detalhe="perfil sem permissão de administração",
        )


def mensagem_sessao(codigo: str) -> str:
    if codigo == "expirada":
        return "Sessão expirada. Entre novamente."
    if codigo == "revogada":
        return "Sessão encerrada. Entre novamente."
    if codigo == "bloqueado":
        return "Usuário bloqueado. Fale com o administrador."
    return "Faça login para continuar."


def _revogar_usuario(con: sqlite3.Connection, usuario_id: int) -> None:
    con.execute(
        "UPDATE sessao SET revogada_em = ? WHERE usuario_id = ? AND revogada_em = ''",
        (_iso(), usuario_id),
    )


def _admins_ativos(con: sqlite3.Connection, exceto: int | None = None) -> int:
    if exceto is None:
        row = con.execute(
            "SELECT COUNT(*) FROM usuario WHERE perfil = 'admin' AND ativo = 1"
        ).fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) FROM usuario WHERE perfil = 'admin' AND ativo = 1 AND id != ?",
            (exceto,),
        ).fetchone()
    return int(row[0] if row else 0)


def criar_primeiro_admin(login_bruto: str, senha: str, nome: str = "") -> dict[str, Any]:
    login = normalizar_login(login_bruto)
    if not login or len(login) > 120 or any(c.isspace() for c in login):
        raise ErroAcesso(400, "Informe o usuário ou e-mail do administrador.")
    nome_salvo = _limpar_texto(nome, 80) or login
    validar_senha(senha, login, nome_salvo)
    senha_hash = hash_senha(senha)
    with banco() as con:
        if _admins_ativos(con):
            raise ErroAcesso(
                409,
                "Já existe um administrador. Novos usuários entram pelo painel.",
            )
        _recusar_conflito(con, login, nome_salvo)
        agora = _iso()
        con.execute(
            """
            INSERT INTO usuario (
                nome, login, senha_hash, perfil, ativo, criado_em, atualizado_em,
                ultimo_login_em, ultimo_acesso_em
            ) VALUES (?, ?, ?, 'admin', 1, ?, ?, '', '')
            """,
            (nome_salvo, login, senha_hash, agora, agora),
        )
        row = con.execute("SELECT * FROM usuario WHERE login = ?", (login,)).fetchone()
        foto = _foto(row)
    return publico(foto)


def _perfil(valor: str) -> str:
    perfil = (valor or "").strip().lower()
    if perfil not in {"admin", "operador"}:
        raise ErroAcesso(400, "O perfil precisa ser administrador ou operador.")
    return perfil


def criar_usuario(dados: dict[str, Any]) -> dict[str, Any]:
    login = normalizar_login(str(dados.get("login") or ""))
    nome = _limpar_texto(str(dados.get("nome") or ""), 80)
    if not nome:
        raise ErroAcesso(400, "Informe o nome.")
    if not login or len(login) > 120 or any(c.isspace() for c in login):
        raise ErroAcesso(400, "Informe o usuário ou e-mail.")
    perfil = _perfil(str(dados.get("perfil") or "operador"))
    senha = str(dados.get("senha") or "")
    validar_senha(senha, login, nome)
    senha_hash = hash_senha(senha)
    with banco() as con:
        _recusar_conflito(con, login, nome)
        agora = _iso()
        con.execute(
            """
            INSERT INTO usuario (
                nome, login, senha_hash, perfil, ativo, criado_em, atualizado_em,
                ultimo_login_em, ultimo_acesso_em
            ) VALUES (?, ?, ?, ?, 1, ?, ?, '', '')
            """,
            (nome, login, senha_hash, perfil, agora, agora),
        )
        row = con.execute("SELECT * FROM usuario WHERE login = ?", (login,)).fetchone()
        foto = _foto(row)
    return _enriquecer(foto)


def _enriquecer(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    agora = _agora()
    with banco() as con:
        sessoes = [
            _foto(item)
            for item in con.execute(
                "SELECT * FROM sessao WHERE usuario_id = ?",
                (int(row["id"]),),
            ).fetchall()
        ]
    validas = [s for s in sessoes if _sessao_valida(s, agora)]
    presenca = _presenca(validas, agora)
    item = publico(row)
    item.update(
        {
            "ultimo_login": fmt_brt(str(row["ultimo_login_em"] or "")),
            "ultimo_acesso": fmt_brt(str(row["ultimo_acesso_em"] or "")),
            "sessoes_ativas": len(validas),
            "atividade_recente": presenca == "ativo_agora",
            "presenca": presenca,
        }
    )
    return item


def listar_usuarios() -> list[dict[str, Any]]:
    with banco() as con:
        fotos = [_foto(row) for row in con.execute("SELECT * FROM usuario ORDER BY nome COLLATE NOCASE, id")]
    return [_enriquecer(row) for row in fotos]


def listar_auditoria(limite: int = 80) -> list[dict[str, Any]]:
    teto = min(max(int(limite or 80), 1), 200)
    with banco() as con:
        rows = [
            _foto(row)
            for row in con.execute(
                """
                SELECT quando, tipo, login, ip, detalhe
                FROM acesso_evento
                ORDER BY id DESC
                LIMIT ?
                """,
                (teto,),
            )
        ]
    return [
        {
            "quando": fmt_brt(str(row["quando"] or "")),
            "tipo": str(row["tipo"] or ""),
            "rotulo": ROTULOS.get(str(row["tipo"] or ""), str(row["tipo"] or "")),
            "login": str(row["login"] or ""),
            "ip": str(row["ip"] or ""),
            "detalhe": str(row["detalhe"] or ""),
        }
        for row in rows
    ]


def editar_usuario(dados: dict[str, Any], *, autor_id: int) -> dict[str, Any]:
    try:
        usuario_id = int(dados.get("id"))
    except (TypeError, ValueError) as exc:
        raise ErroAcesso(400, "Usuário inválido.") from exc
    nome = _limpar_texto(str(dados.get("nome") or ""), 80)
    login = normalizar_login(str(dados.get("login") or ""))
    perfil = _perfil(str(dados.get("perfil") or ""))
    senha = str(dados.get("senha") or "")
    if not nome:
        raise ErroAcesso(400, "Informe o nome.")
    if not login or len(login) > 120:
        raise ErroAcesso(400, "Informe o usuário ou e-mail.")
    if senha:
        validar_senha(senha, login, nome)
    senha_hash = hash_senha(senha) if senha else ""
    with banco() as con:
        atual = con.execute("SELECT * FROM usuario WHERE id = ?", (usuario_id,)).fetchone()
        if atual is None:
            raise ErroAcesso(404, "Usuário não encontrado.")
        perfil_anterior = str(atual["perfil"])
        _recusar_conflito(con, login, nome, exceto=usuario_id)
        era_admin = str(atual["perfil"]) == "admin" and int(atual["ativo"]) == 1
        continua_admin = perfil == "admin" and int(atual["ativo"]) == 1
        if era_admin and not continua_admin and _admins_ativos(con, exceto=usuario_id) == 0:
            raise ErroAcesso(400, "É preciso manter pelo menos um administrador ativo.")
        agora = _iso()
        if senha_hash:
            con.execute(
                """
                UPDATE usuario
                SET nome = ?, login = ?, perfil = ?, senha_hash = ?, atualizado_em = ?
                WHERE id = ?
                """,
                (nome, login, perfil, senha_hash, agora, usuario_id),
            )
            _revogar_usuario(con, usuario_id)
        else:
            con.execute(
                """
                UPDATE usuario
                SET nome = ?, login = ?, perfil = ?, atualizado_em = ?
                WHERE id = ?
                """,
                (nome, login, perfil, agora, usuario_id),
            )
            if perfil_anterior != perfil:
                _revogar_usuario(con, usuario_id)
        row = _foto(con.execute("SELECT * FROM usuario WHERE id = ?", (usuario_id,)).fetchone())
    item = _enriquecer(row)
    item["encerrou_propria_sessao"] = autor_id == usuario_id and bool(senha_hash or perfil_anterior != perfil)
    return item


def definir_bloqueio(usuario_id: int, bloquear: bool, *, autor_id: int) -> dict[str, Any]:
    with banco() as con:
        atual = con.execute("SELECT * FROM usuario WHERE id = ?", (usuario_id,)).fetchone()
        if atual is None:
            raise ErroAcesso(404, "Usuário não encontrado.")
        if bloquear and int(atual["id"]) == autor_id:
            raise ErroAcesso(400, "Você não pode bloquear o próprio usuário.")
        if (
            bloquear
            and str(atual["perfil"]) == "admin"
            and int(atual["ativo"]) == 1
            and _admins_ativos(con, exceto=usuario_id) == 0
        ):
            raise ErroAcesso(400, "É preciso manter pelo menos um administrador ativo.")
        con.execute(
            "UPDATE usuario SET ativo = ?, atualizado_em = ? WHERE id = ?",
            (0 if bloquear else 1, _iso(), usuario_id),
        )
        if bloquear:
            _revogar_usuario(con, usuario_id)
        row = _foto(con.execute("SELECT * FROM usuario WHERE id = ?", (usuario_id,)).fetchone())
    return _enriquecer(row)


def pedido_seguro(headers: Any) -> bool:
    proto = str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    if no_render():
        return True
    return (os.getenv("AUTH_COOKIE_SECURE") or "").strip().lower() in {"1", "true", "yes", "sim"}


def _host_cabecalho(valor: str) -> str:
    return str(valor or "").split(",")[0].strip().lower().split(":")[0]


def cookie_primeira_parte(headers: Any) -> bool:
    """O navegador falou com a Vercel, que repassa ao Render. O cookie fica no domínio do site."""
    encaminhado = _host_cabecalho(str(headers.get("X-Forwarded-Host") or ""))
    return encaminhado.endswith(".vercel.app")


def cabecalho_sessao(token: str, *, seguro: bool, max_age: int, primeira_parte: bool = False) -> str:
    partes = [f"rav_sessao={token}", "HttpOnly", "Path=/", f"Max-Age={max_age}"]
    if not seguro:
        partes.append("SameSite=Lax")
    elif primeira_parte:
        # SameSite=Lax no próprio domínio. Não depende de cookie de terceiro.
        partes.extend(["Secure", "SameSite=Lax"])
    else:
        # Chamada direta ao Render, em outro domínio. CHIPS para quem ainda aceita.
        partes.extend(["Secure", "SameSite=None", "Partitioned"])
    return "; ".join(partes)


def cookie_sessao(cabecalho: str) -> str:
    for parte in (cabecalho or "").split(";"):
        if "=" not in parte:
            continue
        chave, valor = parte.split("=", 1)
        if chave.strip() == "rav_sessao":
            return valor.strip()
    return ""


def ip_cliente(headers: Any, direto: str) -> str:
    bruto = str(headers.get("X-Forwarded-For") or "").strip()
    if no_render() and bruto:
        primeiro = bruto.split(",")[0].strip()
        if primeiro:
            return primeiro[:64]
    return (direto or "")[:64]
