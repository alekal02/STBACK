from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Response

GPS_KEY_HINTS = (
    "lat",
    "latitude",
    "lng",
    "lon",
    "long",
    "longitude",
    "coordenad",
    "posicao",
    "posição",
    "gps",
    "polyline",
    "trajeto",
    "rota",
    "route",
    "track",
    "geo",
    "wkt",
    "kml",
)

INTERESTING_PATH_HINTS = (
    "sinot",
    "veiculo",
    "veículo",
    "posic",
    "rota",
    "route",
    "histor",
    "gps",
    "mapa",
    "map",
    "coleta",
    "frota",
    "linha",
    "itiner",
    "monitor",
    "telemet",
)


def _looks_like_json(content_type: str, url: str) -> bool:
    ctype = (content_type or "").lower()
    if "json" in ctype or "javascript" in ctype:
        return True
    return bool(re.search(r"\.(json|geojson)(\?|$)", url, re.I))


def _has_gps_hint(payload: Any) -> bool:
    blob = json.dumps(payload, ensure_ascii=False).lower()
    return any(hint in blob for hint in GPS_KEY_HINTS)


@dataclass
class CapturedCall:
    method: str
    url: str
    status: int
    content_type: str
    captured_at: str
    json_body: Any = None
    text_preview: str = ""
    has_gps_hint: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "captured_at": self.captured_at,
            "has_gps_hint": self.has_gps_hint,
            "json": self.json_body,
            "text_preview": self.text_preview,
        }


@dataclass
class NetworkSpy:
    calls: list[CapturedCall] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        try:
            request = response.request
            resource = request.resource_type
            if resource not in {"xhr", "fetch", "document"}:
                return

            url = response.url
            host = urlparse(url).netloc.lower()
            if "inlog.com.br" not in host and "googleapis" not in host:
                return

            content_type = response.headers.get("content-type", "")
            status = response.status
            if status >= 400:
                return

            json_body: Any = None
            text_preview = ""
            if _looks_like_json(content_type, url):
                try:
                    json_body = response.json()
                except Exception:
                    try:
                        text_preview = response.text()[:4000]
                    except Exception:
                        return
            elif resource == "xhr" or resource == "fetch":
                try:
                    text_preview = response.text()[:4000]
                    json_body = json.loads(text_preview)
                except Exception:
                    pass

            if json_body is None and not text_preview:
                return

            path = urlparse(url).path.lower()
            interesting = any(hint in path for hint in INTERESTING_PATH_HINTS)
            gps_hint = _has_gps_hint(json_body) if json_body is not None else False
            if not interesting and not gps_hint and resource == "document":
                return

            self.calls.append(
                CapturedCall(
                    method=request.method,
                    url=url,
                    status=status,
                    content_type=content_type,
                    captured_at=datetime.now().isoformat(timespec="seconds"),
                    json_body=json_body,
                    text_preview=text_preview,
                    has_gps_hint=gps_hint,
                )
            )
        except Exception:
            return

    def gps_calls(self) -> list[CapturedCall]:
        return [call for call in self.calls if call.has_gps_hint]

    def dump(self) -> list[dict[str, Any]]:
        return [_sanitizar(call.to_dict()) for call in self.calls]


def _sanitizar(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            chave: "***"
            if "senha" in str(chave).lower() or "password" in str(chave).lower()
            else _sanitizar(item)
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sanitizar(item) for item in valor[:300]]
    return valor
