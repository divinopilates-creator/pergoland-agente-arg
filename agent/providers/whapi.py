import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")

# Tipos de archivo que se interpretan como respuesta al pedido de material
TIPOS_ARCHIVO = {"image", "video", "document", "audio", "sticker", "ptt"}


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.cloud."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.url_envio = "https://gate.whapi.cloud/messages/text"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        body = await request.json()
        mensajes = []

        if "messages" not in body:
            return mensajes

        for msg in body.get("messages", []):
            tipo = msg.get("type", "")
            es_propio = msg.get("from_me", False)

            # Mensajes de texto — flujo normal (incluye los de Martín, es_propio=True)
            if tipo == "text":
                texto = msg.get("text", {}).get("body", "")
                if not texto:
                    continue
                mensajes.append(MensajeEntrante(
                    telefono=msg.get("chat_id", ""),
                    texto=texto,
                    mensaje_id=msg.get("id", ""),
                    es_propio=es_propio,
                    nombre=msg.get("from_name", ""),
                ))

            # Archivos (foto, video, audio, documento, plano)
            # Si lo manda el cliente (es_propio=False): se trata como respuesta de material, Gian sigue el flujo.
            # Si lo manda Martín (es_propio=True): se registra como intervención humana, no se descarta.
            elif tipo in TIPOS_ARCHIVO:
                if es_propio:
                    logger.info(f"Archivo enviado por Martín tipo={tipo} a {msg.get('chat_id', '')} — registrado como intervención humana")
                else:
                    logger.info(f"Archivo recibido tipo={tipo} de {msg.get('chat_id', '')} — tratado como respuesta de material")
                mensajes.append(MensajeEntrante(
                    telefono=msg.get("chat_id", ""),
                    texto="" if es_propio else "[ARCHIVO_RECIBIDO]",
                    mensaje_id=msg.get("id", ""),
                    es_propio=es_propio,
                    nombre=msg.get("from_name", ""),
                ))

        return mensajes

    async def validar_webhook(self, request: Request):
        return None

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado")
            return False

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            r = await client.post(
                self.url_envio,
                json={"to": telefono, "body": mensaje},
                headers=headers,
            )
            if r.status_code != 200:
                logger.error(f"Error Whapi: {r.status_code} - {r.text}")
            return r.status_code == 200
